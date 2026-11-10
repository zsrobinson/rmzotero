"""Turning identifiers, URLs and citations into Zotero items, and finding free PDFs.

Metadata comes from Zotero's own translation-server (the engine behind "Add
Item by Identifier" and the browser connector), so items look exactly as if
Zotero had created them. Open-access PDFs come from links the translators
report, from direct PDF URLs, from OpenAlex and Unpaywall for DOIs, and from
the PDF link a publisher or repository page declares (``citation_pdf_url``).
Every URL tried is reported with what happened, so "no PDF" always says why.

Requests identify themselves honestly as rmzotero; a site that refuses them is
reported as refusing, not worked around.
"""

from __future__ import annotations

from .. import telemetry

import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

log = logging.getLogger(__name__)

MAX_PDF_BYTES = 150 * 1024 * 1024
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_PDF_ATTEMPTS = 8
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.I)
# fields translation-server returns that the Zotero web API won't accept on items
NON_API_FIELDS = {"key", "version", "attachments", "notes", "seeAlso", "id", "libraryCatalog_", "complete"}


@dataclass
class Resolution:
    query: str
    items: list[dict] = field(default_factory=list)  # Zotero item data (no key)
    pdf_urls: list[str] = field(default_factory=list)
    choices: dict[str, str] = field(default_factory=dict)  # when the query was ambiguous: id -> title
    error: str | None = None


@dataclass
class PdfAttempt:
    url: str
    source: str  # where the URL came from: "translator", "OpenAlex", "Unpaywall", "item URL", "given", "linked from a page"
    result: str = ""  # "attached", or why not

    def as_dict(self) -> dict:
        return {"url": self.url, "from": self.source, "result": self.result}


@dataclass
class Page:
    url: str  # after redirects
    html: str
    pdf_links: list[str]  # PDF links the page declares for itself


class _Meta(HTMLParser):
    """The PDF links a page declares: Highwire/Google Scholar ``citation_pdf_url`` metas (publishers,
    DSpace, EPrints, OJS...) and ``<link rel="alternate" type="application/pdf">``."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pdf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta" and a.get("name", "").lower() in ("citation_pdf_url", "eprints.document_url") and a.get("content"):
            self.pdf.append(a["content"].strip())
        elif tag == "link" and "alternate" in a.get("rel", "").lower() and a.get("type", "").lower() == "application/pdf" and a.get("href"):
            self.pdf.append(a["href"].strip())


# Repositories whose pages are built by JavaScript, so they declare no PDF link, but whose files sit at a fixed address.
OSF_RE = re.compile(r"^https?://(?:www\.)?osf\.io/(?:preprints/[a-z]+/)?([a-z0-9]{5}(?:_v\d+)?)(?:[/?#]|$)", re.I)


def known_pdf_links(url: str) -> list[str]:
    m = OSF_RE.match(url)
    if m and m.group(1).lower() not in ("search", "about", "login"):
        return [f"https://osf.io/{m.group(1)}/download"]
    return []


def _why(status: int) -> str:
    if status in (401, 403):
        return f"HTTP {status}: the site refused (it needs a login, or blocks automated downloads)"
    if status == 404:
        return "HTTP 404: not found"
    if status == 429:
        return "HTTP 429: the site is rate-limiting requests; try later"
    return f"HTTP {status}"


def normalise_doi(s: str | None) -> str:
    if not s:
        return ""
    m = DOI_RE.search(s)
    return m.group(1).rstrip(".,;)").lower() if m else ""


def isbn_digits(s: str | None) -> set[str]:
    """All ISBNs in a field, as ISBN-13 digit strings."""
    out = set()
    for raw in re.findall(r"[0-9Xx][0-9Xx\- ]{8,16}[0-9Xx]", s or ""):
        d = re.sub(r"[^0-9Xx]", "", raw).upper()
        if len(d) == 10:
            core = "978" + d[:9]
            check = (10 - sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(core)) % 10) % 10
            out.add(core + str(check))
        elif len(d) == 13:
            out.add(d)
    return out


def normalise_title(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def looks_like_identifier(q: str) -> bool:
    q = q.strip()
    return bool(DOI_RE.search(q) or re.fullmatch(r"(?i)(isbn[: ]*)?[0-9Xx\- ]{10,17}", q) or re.fullmatch(r"(?i)(arxiv:)?\d{4}\.\d{4,5}(v\d+)?", q)
                or re.fullmatch(r"(?i)pmid:?\s*\d+", q))


class Resolver:
    def __init__(self, translation_url: str, contact_email: str = "", *, client: httpx.Client | None = None):
        self.base = translation_url.rstrip("/")
        self.email = contact_email
        self.http = client or httpx.Client(timeout=60.0, follow_redirects=True, headers={"User-Agent": "rmzotero (+https://github.com/zsrobinson/rmzotero)"})

    # -- metadata ------------------------------------------------------------------
    @telemetry.timed("upstream")
    def resolve(self, query: str) -> Resolution:
        q = query.strip()
        res = Resolution(q)
        try:
            if re.match(r"https?://", q, re.I):
                r = self.http.post(f"{self.base}/web", content=q.encode(), headers={"Content-Type": "text/plain"})
                if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/pdf"):
                    res.pdf_urls.append(q)
                    return res
            else:
                r = self.http.post(f"{self.base}/search", content=q.encode(), headers={"Content-Type": "text/plain"})
        except httpx.HTTPError as e:
            res.error = f"translation-server unreachable: {e}"
            return res
        if r.status_code == 300:
            body = r.json()
            items = body.get("items", body) if isinstance(body, dict) else body
            if isinstance(items, dict):
                res.choices = {str(k): (v if isinstance(v, str) else (v or {}).get("title", str(k))) for k, v in items.items()}
            elif isinstance(items, list):
                res.choices = {str(i.get("DOI") or i.get("key") or n): i.get("title", "") for n, i in enumerate(items)}
            return res
        if r.status_code == 501:
            res.error = "no translator could read this (try a DOI, ISBN or a link to the article page)"
            return res
        if r.status_code >= 400:
            res.error = f"translation-server answered HTTP {r.status_code}: {r.text[:200]}"
            if re.match(r"https?://", q, re.I):
                res.error = self._explain_page_failure(q, res.error)
            return res
        for raw in r.json() if isinstance(r.json(), list) else []:
            if raw.get("itemType") in ("attachment", "note"):
                continue
            for a in raw.get("attachments") or []:
                if (a.get("mimeType") or "").lower() == "application/pdf" and a.get("url"):
                    res.pdf_urls.append(a["url"])
            res.items.append(clean_item(raw))
        if re.match(r"https?://", q, re.I) and q.lower().split("?")[0].endswith(".pdf"):
            res.pdf_urls.insert(0, q)
        return res

    def _explain_page_failure(self, url: str, error: str) -> str:
        """translation-server reports a site's refusal as a bare 500; ask the site once ourselves to say why."""
        try:
            r = self.http.get(url, headers={"Accept": "text/html"})
            status = r.status_code
        except httpx.HTTPError as e:
            return f"Zotero's translators couldn't read this page, and it didn't answer us either ({e}). Add it with create_item."
        if status in (401, 403, 429):
            return (f"The site refused to give this page to Zotero's translators ({_why(status)}); news sites often do. "
                    "Add it with create_item from the citation instead, and attach_pdf if you have a link to a PDF of it.")
        return f"Zotero's translators couldn't read this page ({error}; the page itself answers HTTP {status}). Add it with create_item."

    # -- PDFs ------------------------------------------------------------------------
    @telemetry.timed("upstream")
    def open_access_candidates(self, doi: str) -> tuple[list[PdfAttempt], list[PdfAttempt], list[str]]:
        """Free copies of a DOI: (PDF links, landing pages that may declare one, notes on what was checked)."""
        pdfs: list[PdfAttempt] = []
        pages: list[PdfAttempt] = []
        notes: list[str] = []
        if not doi:
            return pdfs, pages, ["no DOI, so OpenAlex and Unpaywall weren't asked"]

        def add(lst, url, source):
            if url and url not in {a.url for a in pdfs + pages}:
                lst.append(PdfAttempt(url, source))

        try:
            r = self.http.get(f"https://api.openalex.org/works/doi:{doi}", params={"mailto": self.email} if self.email else None)
            if r.status_code == 404:
                notes.append("OpenAlex has no record of this DOI")
            elif r.status_code != 200:
                notes.append(f"OpenAlex answered {_why(r.status_code)}")
            else:
                w = r.json()
                locs = [w.get("best_oa_location")] + list(w.get("locations") or [])
                oa = [loc for loc in locs if loc and loc.get("is_oa")]
                if not oa:
                    notes.append("OpenAlex: not open access")
                for loc in oa:
                    add(pdfs, loc.get("pdf_url"), "OpenAlex")
                for loc in oa:
                    add(pages, loc.get("landing_page_url"), "OpenAlex (landing page)")
        except (httpx.HTTPError, ValueError) as e:
            notes.append(f"OpenAlex unreachable: {e}")
        if not self.email:
            notes.append("Unpaywall not asked: [claude] contact_email isn't set in rmzotero.toml")
        else:
            try:
                r = self.http.get(f"https://api.unpaywall.org/v2/{doi}", params={"email": self.email})
                if r.status_code == 404:
                    notes.append("Unpaywall has no record of this DOI")
                elif r.status_code != 200:
                    notes.append(f"Unpaywall answered {_why(r.status_code)}")
                else:
                    data = r.json()
                    locs = [data.get("best_oa_location")] + list(data.get("oa_locations") or [])
                    if not data.get("is_oa") and not any(locs):
                        notes.append("Unpaywall: not open access")
                    for loc in locs:
                        if loc:
                            add(pdfs, loc.get("url_for_pdf"), "Unpaywall")
                    for loc in locs:
                        if loc:
                            add(pages, loc.get("url_for_landing_page") or loc.get("url"), "Unpaywall (landing page)")
            except (httpx.HTTPError, ValueError) as e:
                notes.append(f"Unpaywall unreachable: {e}")
        return pdfs, pages, notes

    def open_access_pdf_urls(self, doi: str) -> list[str]:
        return [a.url for a in self.open_access_candidates(doi)[0]]

    @telemetry.timed("upstream")
    def fetch(self, url: str) -> tuple[bytes | None, Page | None, str]:
        """GET a URL that should be a PDF or a page about one: (PDF bytes, the HTML page, why neither)."""
        try:
            with self.http.stream("GET", url, headers={"Accept": "application/pdf,text/html;q=0.9,*/*;q=0.5"}) as r:
                if r.status_code != 200:
                    return None, None, _why(r.status_code)
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                limit = MAX_PAGE_BYTES if "html" in ctype else MAX_PDF_BYTES
                data = bytearray()
                for chunk in r.iter_bytes():
                    data.extend(chunk)
                    if len(data) > limit:
                        return None, None, f"larger than {limit // (1024 * 1024)} MB"
                final = str(r.url)
                charset = r.charset_encoding or "utf-8"
        except httpx.HTTPError as e:
            return None, None, f"network error: {type(e).__name__}: {e}"
        if data[:5] == b"%PDF-":
            return bytes(data), None, ""
        if "html" in ctype or data.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")):
            html = bytes(data).decode(charset, "replace")
            meta = _Meta()
            try:
                meta.feed(html)
            except Exception:  # malformed markup: whatever was found so far
                pass
            links = []
            for link in meta.pdf + known_pdf_links(final) + known_pdf_links(url):
                full = urljoin(final, link)
                if full not in links and full != final:
                    links.append(full)
            return None, Page(final, html, links), "a web page, not a PDF" + ("" if links else " (and it doesn't link to one)")
        return None, None, f"not a PDF (the site sent {ctype or 'something else'})"

    def find_pdf(self, candidates: list[PdfAttempt], pages: list[PdfAttempt] | None = None) -> tuple[bytes | None, PdfAttempt | None, list[PdfAttempt], Page | None]:
        """Try PDF links, then landing pages (following the PDF link each declares), stopping at the
        first real PDF. Returns (PDF, the attempt that found it, every attempt, the first page seen)."""
        tried: list[PdfAttempt] = []
        queue = list(candidates) + list(pages or [])
        seen: set[str] = set()
        first_page: Page | None = None
        while queue and len(tried) < MAX_PDF_ATTEMPTS:
            a = queue.pop(0)
            if a.url in seen:
                continue
            seen.add(a.url)
            tried.append(a)
            data, page, why = self.fetch(a.url)
            if data:
                a.result = "attached"
                return data, a, tried, first_page
            a.result = why
            if page is not None:
                first_page = first_page or page
                follow = [PdfAttempt(u, f"linked from {a.url}") for u in page.pdf_links[:2] if u not in seen]
                queue[:0] = follow
        return None, None, tried, first_page

    def fetch_pdf(self, url: str) -> bytes | None:
        data, _page, _why = self.fetch(url)
        return data


def clean_item(raw: dict) -> dict:
    item = {k: v for k, v in raw.items() if k not in NON_API_FIELDS and v not in (None, "")}
    item.setdefault("tags", [])
    item["tags"] = [t if isinstance(t, dict) else {"tag": str(t)} for t in item["tags"]]
    item.setdefault("creators", [])
    return item
