"""What Claude can do with the library (ADR 0015). Plain Python; the MCP layer only wraps these.

Reads use the service's library mirror and never block on the sync loop.
Writes go through the Zotero web API while holding the engine lock (so they
never interleave with a sync cycle), are recorded in the mirror straight
away, and wake the sync loop so the tablet follows. Nothing here deletes items.
"""

from __future__ import annotations

import functools
import html
import json
import re
import time
from contextlib import contextmanager

from .. import telemetry
from ..annotations import convert as cv
from ..pdf import edit
from ..pdf.text import TextLayer
from ..tablet.scene import Page
from ..zotero.client import new_key
from ..zotero.schema import NOT_REGULAR, Schema, parse_creators
from . import render
from .resolve import PdfAttempt, Resolver, isbn_digits, normalise_doi, normalise_title

COLOR_NAMES = {"#ffd400": "yellow", "#ff6666": "red", "#5fb236": "green", "#2ea8e5": "blue", "#a28ae5": "purple",
               "#e56eee": "magenta", "#f19837": "orange", "#aaaaaa": "gray"}
NOT_EDITABLE = {"key", "version", "parentItem", "collections", "deleted", "relations", "dateAdded", "dateModified",
                "linkMode", "contentType", "filename", "md5", "mtime", "charset"}
MAX_TEXT_PAGES = 20
MAX_IMAGE_PAGES = 4
WEB_TYPES = {"webpage", "blogPost", "newspaperArticle", "magazineArticle", "forumPost", "report"}


class Busy(Exception):
    pass


class NotFound(Exception):
    pass


def _retry_reads(fn):
    """The sync loop may update the mirror mid-read; just try again."""

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        for attempt in range(5):
            try:
                return fn(*a, **kw)
            except RuntimeError as e:
                if "changed size" not in str(e) or attempt == 4:
                    raise
                time.sleep(0.05)

    return wrapper


def parse_pages(spec: str | None, count: int, limit: int) -> list[int]:
    """'3', '1-4', '2,5-6' (1-based) -> 0-based indexes within the document, at most ``limit``."""
    if not spec:
        return list(range(min(count, limit)))
    out: list[int] = []
    for part in str(spec).replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo, hi = int(a or 1), int(b or count)
        else:
            lo = hi = int(part)
        for p in range(max(1, lo), min(count, hi) + 1):
            if p - 1 not in out:
                out.append(p - 1)
    return out[:limit]


def strip_html(s: str) -> str:
    s = re.sub(r"<(br|/p|/div|/li|/h\d)[^>]*>", "\n", s or "", flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def creators_text(item: dict) -> str:
    names = []
    for c in item.get("creators") or []:
        n = c.get("name") or " ".join(x for x in (c.get("firstName"), c.get("lastName")) if x)
        if n:
            role = c.get("creatorType", "author")
            names.append(n if role == "author" else f"{n} ({role})")
    return "; ".join(names)


class LibraryOps:
    def __init__(self, engine, resolver: Resolver | None = None):
        self.engine = engine
        self.resolver = resolver
        self._schema: Schema | None = None

    def schema(self) -> Schema | None:
        """Zotero's item schema, fetched once; None if it can't be had (writes then go unchecked)."""
        if self._schema is None:
            fetch = getattr(self.engine.zotero, "schema", None)
            if fetch is None:
                return None
            try:
                self._schema = Schema(fetch())
            except Exception:  # network or parse trouble: try again next time
                import logging

                logging.getLogger(__name__).warning("couldn't load Zotero's item schema")
                return None
        return self._schema

    @property
    def lib(self):
        return self.engine.lib

    @contextmanager
    def _writing(self):
        with telemetry.phase("lock_wait"):
            if not self.engine.lock.acquire(timeout=120):
                raise Busy("The service is in the middle of a long sync; try again in a minute.")
        try:
            yield
        finally:
            self.engine.lock.release()
        self.engine.wakeup.set()

    # -- lookups --------------------------------------------------------------------
    @telemetry.timed("mirror_read")
    def _regular(self, key: str) -> dict:
        it = self.lib.items.get(key)
        if it is None or it.get("deleted"):
            raise NotFound(f"no item {key}")
        if it.get("itemType") in ("attachment", "note", "annotation") and it.get("parentItem"):
            parent = self.lib.items.get(it["parentItem"])
            if parent is not None:
                return parent
        return it

    def _collection_path(self, ckey: str) -> str:
        return " / ".join(self.lib.collection_path(ckey))

    def _tablet(self, att_key: str) -> dict:
        row = self.engine.state.doc(att_key)
        if row is None:
            return {"on_tablet": False}
        root = [p for p in self.engine.settings.sync_root.split("/") if p]
        folder = row["folder_key"]
        path = self.lib.collection_path(folder) if folder in self.lib.collections else ["Unfiled Items"]
        return {"on_tablet": row["state"] == "active", "tablet_state": row["state"], "tablet_location": "/".join(root + path)}

    @telemetry.timed("mirror_read")
    def _pdf_attachments(self, item_key: str) -> list[dict]:
        atts = [a for a in self.lib.attachments(item_key) if a.get("contentType") == "application/pdf" and not a.get("deleted")]
        synced = {r["att_key"] for r in self.engine.state.docs()}
        return sorted(atts, key=lambda a: (a["key"] not in synced, a.get("dateAdded", "")))

    @telemetry.timed("mirror_read")
    def _attachment_for(self, key: str) -> dict:
        it = self.lib.items.get(key)
        if it is not None and it.get("itemType") == "attachment":
            return it
        atts = self._pdf_attachments(self._regular(key)["key"])
        if not atts:
            raise NotFound("this item has no PDF")
        return atts[0]

    def _file(self, att: dict) -> bytes:
        row = self.engine.state.doc(att["key"])
        if row and row["zotero_md5"] == att.get("md5") and row["zotero_file_hash"]:
            data = self.engine.cache.get_local(row["zotero_file_hash"])
            if data is not None:
                return data
        _name, data = self.engine.files.get(att["key"])
        return data

    @telemetry.timed("mirror_read")
    def _summary(self, it: dict) -> dict:
        return {
            "key": it["key"], "title": it.get("title", ""), "creators": creators_text(it), "date": it.get("date", ""),
            "itemType": it.get("itemType"), "collections": [self._collection_path(c) for c in it.get("collections") or [] if c in self.lib.collections],
        }

    # -- reading ----------------------------------------------------------------------
    @_retry_reads
    @telemetry.timed("mirror_read")
    def search_library(self, query: str, collection: str | None = None, limit: int = 20, offset: int = 0) -> dict:
        tokens = [t for t in re.findall(r"\w+", (query or "").lower()) if t]
        scope = None
        if collection:
            if collection not in self.lib.collections:
                raise NotFound(f"no collection {collection}")
            scope = {collection, *self.lib.collection_descendants(collection)}
        notes: dict[str, list[str]] = {}
        for a in self.lib.items.values():
            if a.get("itemType") == "annotation" and not a.get("deleted"):
                att = self.lib.items.get(a.get("parentItem") or "")
                owner = att.get("parentItem") if att else None
                if owner:
                    notes.setdefault(owner, []).append(" ".join(x for x in (a.get("annotationText"), a.get("annotationComment")) if x))
        results = []
        for it in self.lib.regular_items():
            if it.get("deleted") or (scope is not None and not scope & set(it.get("collections") or [])):
                continue
            main = " ".join([it.get("title", ""), it.get("shortTitle", ""), creators_text(it), it.get("date", "")]).lower()
            other = " ".join([it.get("publicationTitle", ""), it.get("DOI", ""), it.get("ISBN", ""), it.get("abstractNote", ""),
                              " ".join(t.get("tag", "") for t in it.get("tags") or [])]).lower()
            ann = " ".join(notes.get(it["key"], [])).lower()
            if tokens and not all(t in main or t in other or t in ann for t in tokens):
                continue
            score = sum(3 * (t in main) + (t in other) + (t in ann) for t in tokens)
            hit = None
            if tokens and any(t in ann and t not in main for t in tokens):
                hit = next((n for n in notes.get(it["key"], []) if any(t in n.lower() for t in tokens)), None)
            results.append((score, it, hit))
        results.sort(key=lambda r: (-r[0], r[1].get("title", "")))
        out = []
        start = max(0, offset)
        for _s, it, hit in results[start : start + max(1, min(limit, 100))]:
            d = self._summary(it)
            d["has_pdf"] = any(a.get("contentType") == "application/pdf" and not a.get("deleted") for a in self.lib.attachments(it["key"]))
            if hit:
                d["matching_annotation"] = hit[:300]
            out.append(d)
        res = {"count": len(results), "items": out}
        if start + len(out) < len(results):
            res["next_offset"] = start + len(out)
        return res

    @_retry_reads
    @telemetry.timed("mirror_read")
    def get_item(self, key: str) -> dict:
        it = self._regular(key)
        skip = {"key", "version", "creators", "tags", "collections", "relations", "deleted", "_kind"}
        fields = {k: v for k, v in it.items() if k not in skip and v not in ("", None, [], {}) and not isinstance(v, (dict, list))}
        atts, notes, n_ann = [], [], 0
        for child_key in self.lib.children.get(it["key"], []):
            c = self.lib.items[child_key]
            if c.get("deleted"):
                continue
            if c.get("itemType") == "attachment":
                anns = self.lib.annotations(child_key)
                n_ann += len(anns)
                atts.append({"key": child_key, "title": c.get("title"), "contentType": c.get("contentType"),
                             "annotations": len(anns), **self._tablet(child_key)})
            elif c.get("itemType") == "note":
                notes.append({"key": child_key, "text": strip_html(c.get("note", ""))[:3000]})
        return {**self._summary(it), "fields": fields, "tags": [t.get("tag") for t in it.get("tags") or []],
                "attachments": atts, "notes": notes, "annotation_count": n_ann}

    @_retry_reads
    @telemetry.timed("mirror_read")
    def list_collections(self) -> dict:
        counts: dict[str, int] = {}
        for it in self.lib.regular_items():
            if not it.get("deleted"):
                for c in it.get("collections") or []:
                    counts[c] = counts.get(c, 0) + 1
        cols = [{"key": k, "path": self._collection_path(k), "items": counts.get(k, 0), "parent": c.get("parentCollection") or None}
                for k, c in self.lib.collections.items() if not self.lib.collection_trashed(k)]
        cols.sort(key=lambda c: c["path"].lower())
        return {"collections": cols}

    @_retry_reads
    @telemetry.timed("mirror_read")
    def get_annotations(self, key: str, limit_items: int = 50) -> dict:
        if key in self.lib.collections:
            items = [it for it in self.lib.regular_items() if key in (it.get("collections") or []) and not it.get("deleted")][:limit_items]
        else:
            items = [self._regular(key)]
        out = []
        for it in items:
            entries = []
            for att in self.lib.attachments(it["key"]):
                origin = {r["zkey"]: r["origin"] for r in self.engine.state.links_for(att["key"])}
                anns = sorted(self.lib.annotations(att["key"]).values(), key=lambda a: a.get("annotationSortIndex", ""))
                for a in anns:
                    e = {"key": a["key"], "page": a.get("annotationPageLabel"), "type": a.get("annotationType"),
                         "color": COLOR_NAMES.get((a.get("annotationColor") or "").lower(), a.get("annotationColor")),
                         "from": origin.get(a["key"], "zotero")}
                    if a.get("annotationText"):
                        e["text"] = a["annotationText"]
                    if a.get("annotationComment"):
                        e["comment"] = a["annotationComment"]
                    if a.get("annotationType") in ("ink", "image"):
                        e["note"] = "drawing or handwriting: use view_pages to see it"
                    if a.get("tags"):
                        e["tags"] = [t.get("tag") for t in a["tags"]]
                    entries.append(e)
            if entries:
                out.append({**self._summary(it), "annotations": entries})
        return {"items": out}

    def diagnostics(self) -> dict:
        from ..diagnostics import collect

        return collect(self.engine)

    @_retry_reads
    def read_pages(self, key: str, pages: str | None = None) -> dict:
        att = self._attachment_for(key)
        data = self._file(att)
        tl = TextLayer(data)
        labels = edit.page_labels(data)
        idx = parse_pages(pages, len(tl), MAX_TEXT_PAGES)
        text = []
        for i in idx:
            t = tl.page_text(i)
            text.append({"page": labels[i], "text": t[:15000]})
        return {"attachment": att["key"], "page_count": len(tl), "pages": text}

    def view_pages(self, key: str, pages: str | None = None) -> list[tuple[str, bytes]]:
        att = self._attachment_for(key)
        data = self._file(att)
        boxes = edit.page_boxes(data)
        labels = edit.page_labels(data)
        row = self.engine.state.doc(att["key"])
        mapping = json.loads(row["pages"]) if row and row["pages"] else []
        hashes = json.loads(row["page_hashes"]) if row and row["page_hashes"] else {}
        anns = list(self.lib.annotations(att["key"]).values())
        out = []
        for i in parse_pages(pages, len(boxes), MAX_IMAGE_PAGES):
            tablet_page = None
            if i < len(mapping) and mapping[i] in hashes:
                blob = self.engine.cache.get_local(hashes[mapping[i]])
                tablet_page = Page.parse(blob) if blob else None
            on_page = [a for a in anns if cv.position_of(a).get("pageIndex") == i]
            with telemetry.phase("render"):
                out.append((labels[i], render.render_page(data, i, boxes[i], tablet_page, on_page)))
        return out

    # -- changing the library ------------------------------------------------------------
    def create_collection(self, name: str, parent: str | None = None) -> dict:
        name = name.strip()
        if not name:
            raise ValueError("a collection needs a name")
        if parent and parent not in self.lib.collections:
            raise NotFound(f"no collection {parent}")
        for k, c in self.lib.collections.items():
            if c.get("name") == name and (c.get("parentCollection") or None) == (parent or None) and not c.get("deleted"):
                return {"key": k, "path": self._collection_path(k), "created": False}
        with self._writing():
            key = new_key()
            obj = {"key": key, "version": 0, "name": name, "parentCollection": parent or False}
            res = self.engine.zotero.write_collections([obj])
            if key not in res.versions:
                raise RuntimeError(f"Zotero refused the collection: {res.failed.get(key) or 'conflict'}")
            self.lib.apply_local_collections([{**obj, "version": res.versions[key]}])
        return {"key": key, "path": self._collection_path(key), "created": True}

    def file_items(self, item_keys: list[str], add_to: list[str] | None = None, remove_from: list[str] | None = None) -> dict:
        add_to, remove_from = list(add_to or []), list(remove_from or [])
        for c in add_to + remove_from:
            if c not in self.lib.collections:
                raise NotFound(f"no collection {c}")
        changed, unchanged = [], []
        with self._writing():
            objs = []
            for k in item_keys:
                it = self._regular(k)
                cols = [c for c in it.get("collections") or [] if c not in remove_from]
                cols += [c for c in add_to if c not in cols]
                if cols == list(it.get("collections") or []):
                    unchanged.append(it["key"])
                    continue
                objs.append({"key": it["key"], "version": it["version"], "collections": cols})
            res = self.engine.zotero.write_items(objs) if objs else None
            for o in objs:
                if res and o["key"] in res.versions:
                    self.lib.apply_local_write([{**self.lib.items[o["key"]], "collections": o["collections"], "version": res.versions[o["key"]]}])
                    changed.append(o["key"])
        failed = [o["key"] for o in objs if o["key"] not in changed]
        return {"changed": changed, "unchanged": unchanged, "failed": failed,
                "note": "The tablet follows at the next sync." if changed else ""}

    def add_note(self, item_key: str, text: str) -> dict:
        it = self._regular(item_key)
        paras = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
        body = "".join(f"<p>{html.escape(p).replace(chr(10), '<br/>')}</p>" for p in paras) or "<p></p>"
        with self._writing():
            key = new_key()
            obj = {"key": key, "version": 0, "itemType": "note", "parentItem": it["key"], "note": f"<div data-schema-version=\"9\">{body}</div>", "tags": []}
            res = self.engine.zotero.write_items([obj])
            if key not in res.versions:
                raise RuntimeError(f"Zotero refused the note: {res.failed.get(key) or 'conflict'}")
            self.lib.apply_local_write([{**obj, "version": res.versions[key]}])
        return {"key": key, "item": it["key"]}

    def edit_item(self, item_key: str, fields: dict | None = None, add_tags: list[str] | None = None, remove_tags: list[str] | None = None) -> dict:
        it = self._regular(item_key)
        fields = dict(fields or {})
        bad = sorted(set(fields) & NOT_EDITABLE)
        if bad:
            raise ValueError(f"these can't be edited here: {', '.join(bad)} (use file_items for collections)")
        schema = self.schema()
        new_type = fields.pop("itemType", None) or it.get("itemType")
        changing = new_type != it.get("itemType")
        moved, dropped = {}, {}
        if changing:
            if schema is None:
                raise RuntimeError("couldn't load Zotero's item schema, which changing an item's type needs; try again shortly")
            if not schema.has_type(new_type) or new_type in NOT_REGULAR:
                raise ValueError(f"no item type {new_type!r}; types: {', '.join(schema.item_types())}")
            moved, dropped, creators = schema.convert(it, new_type)
            for f in fields:  # a field set explicitly isn't dropped or overwritten by the move
                dropped.pop(f, None)
                moved.pop(f, None)
            if creators is not None and "creators" not in fields:
                fields["creators"] = creators
        if "creators" in fields:
            fields["creators"] = parse_creators(fields["creators"], new_type, schema)
        if schema is not None and schema.has_type(new_type):
            unknown = [f for f in schema.unknown_fields(new_type, fields) if f not in ("creators", "tags")]
            if unknown:
                raise ValueError(f"{', '.join(unknown)} isn't a field of {new_type}. {schema.describe(new_type)}")
        tags = [t for t in it.get("tags") or [] if t.get("tag") not in set(remove_tags or [])]
        tags += [{"tag": t} for t in add_tags or [] if t not in {x.get("tag") for x in tags}]
        obj = {"key": it["key"], "version": it["version"], **moved, **fields}
        if changing:
            obj["itemType"] = new_type
        if add_tags or remove_tags:
            obj["tags"] = tags
        with self._writing():
            res = self.engine.zotero.write_items([obj])
            if it["key"] not in res.versions:
                raise RuntimeError(f"Zotero refused the change: {res.failed.get(it['key']) or 'the item changed meanwhile; read it again'}")
            stored = res.data.get(it["key"])
            if stored is None:
                stored = {k: v for k, v in {**it, **obj}.items() if k not in dropped}
                if changing and schema is not None:
                    stored = schema.apply_type_change({**it, **obj, "itemType": it.get("itemType")}, new_type)
            self.lib.apply_local_write([{**stored, "version": res.versions[it["key"]]}])
        out = {"key": it["key"], "updated": sorted(set(obj) - {"key", "version"})}
        if changing:
            out["itemType"] = f"{it.get('itemType')} -> {new_type}"
            if moved:
                out["moved"] = moved
            if dropped:
                out["dropped"] = dropped
                out["note"] = f"{new_type} has no place for the dropped fields; set them under its own fields if they matter."
        return out

    def create_item(self, item_type: str, fields: dict, collections: list[str] | None = None) -> dict:
        """A new item from metadata given directly (when no translator can read the source)."""
        schema = self.schema()
        fields = dict(fields or {})
        bad = sorted(set(fields) & (NOT_EDITABLE | {"itemType"}))
        if bad:
            raise ValueError(f"these can't be set here: {', '.join(bad)} (collections go in the collections argument)")
        if schema is not None and (not schema.has_type(item_type) or item_type in NOT_REGULAR):
            raise ValueError(f"no item type {item_type!r}; types: {', '.join(schema.item_types())}")
        if not str(fields.get("title") or "").strip():
            raise ValueError("an item needs a title")
        fields["creators"] = parse_creators(fields.get("creators"), item_type, schema)
        tags = fields.pop("tags", None) or []
        if schema is not None:
            unknown = [f for f in schema.unknown_fields(item_type, fields) if f != "creators"]
            if unknown:
                raise ValueError(f"{', '.join(unknown)} isn't a field of {item_type}. {schema.describe(item_type)}")
        collections = list(collections or [])
        for c in collections:
            if c not in self.lib.collections:
                raise NotFound(f"no collection {c}")
        item = {"itemType": item_type, **fields, "tags": [t if isinstance(t, dict) else {"tag": str(t)} for t in tags]}
        existing = self._find_existing(item)
        with self._writing():
            if existing is not None:
                parent, status = self._file_existing(existing, collections), "already in library"
            else:
                key = new_key()
                obj = {**item, "key": key, "version": 0, "collections": collections}
                res = self.engine.zotero.write_items([obj])
                if key not in res.versions:
                    raise RuntimeError(f"Zotero refused it: {res.failed.get(key) or 'conflict'}")
                parent = {**(res.data.get(key) or obj), "version": res.versions[key]}
                self.lib.apply_local_write([parent])
                status = "added"
        out = {"key": parent["key"], "title": parent.get("title"), "status": status,
               "collections": [self._collection_path(c) for c in parent.get("collections") or [] if c in self.lib.collections]}
        if not self._pdf_attachments(parent["key"]):
            out["next"] = "No PDF yet: attach_pdf can add one from a link" + (
                ", or typeset the page itself (typeset_webpage=true)" if item_type in WEB_TYPES and parent.get("url") else "") + "."
        return out

    # -- adding items ------------------------------------------------------------------------
    def _find_existing(self, item: dict) -> dict | None:
        doi = normalise_doi(item.get("DOI") or item.get("extra"))
        isbns = isbn_digits(item.get("ISBN"))
        title = normalise_title(item.get("title"))
        for it in self.lib.regular_items():
            if it.get("deleted"):
                continue
            if doi and doi == normalise_doi(it.get("DOI") or it.get("extra")):
                return it
            if isbns and isbns & isbn_digits(it.get("ISBN")):
                return it
            if title and len(title) > 12 and title == normalise_title(it.get("title")) and (
                    not item.get("itemType") or item.get("itemType") == it.get("itemType")):
                return it
        return None

    def _file_existing(self, existing: dict, collections: list[str]) -> dict:
        cols = list(existing.get("collections") or [])
        new_cols = cols + [c for c in collections if c not in cols]
        if new_cols != cols:
            res = self.engine.zotero.write_items([{"key": existing["key"], "version": existing["version"], "collections": new_cols}])
            if existing["key"] in res.versions:
                self.lib.apply_local_write([{**existing, "collections": new_cols, "version": res.versions[existing["key"]]}])
        return self.lib.items[existing["key"]]

    def _attach_pdf(self, parent: dict, data: bytes, title: str = "Full Text PDF", tag: str | None = None, url: str = "") -> str:
        key = new_key()
        base = re.sub(r"[^\w\- ,.()]+", "", parent.get("title") or "document")[:80].strip() or "document"
        obj = {"key": key, "version": 0, "itemType": "attachment", "parentItem": parent["key"], "linkMode": "imported_file",
               "title": title, "contentType": "application/pdf", "charset": "", "filename": f"{base}.pdf",
               "md5": None, "mtime": None, "note": "", "tags": [{"tag": tag}] if tag else [], "relations": {}}
        if url:
            obj["url"] = url
        res = self.engine.zotero.write_items([obj])
        if key not in res.versions:
            raise RuntimeError(f"Zotero refused the attachment: {res.failed.get(key) or 'conflict'}")
        md5, mtime = self.engine.files.put(key, obj["filename"], data)
        res2 = self.engine.zotero.write_items([{"key": key, "version": res.versions[key], "md5": md5, "mtime": mtime}])
        self.lib.apply_local_write([{**obj, "version": res2.versions.get(key, res.versions[key]), "md5": md5, "mtime": mtime}])
        return key

    def _same_pdf_attached(self, parent_key: str, data: bytes) -> str | None:
        import hashlib

        md5 = hashlib.md5(data).hexdigest()
        return next((a["key"] for a in self._pdf_attachments(parent_key) if a.get("md5") == md5), None)

    def _find_and_attach(self, parent: dict, first: list[PdfAttempt]) -> tuple[dict, object]:
        """Look for a free PDF of an item and attach the first one found. Returns (a report saying what
        was tried and why each failed, the first web page seen, for typesetting)."""
        doi = normalise_doi(parent.get("DOI") or parent.get("extra"))
        oa_pdfs, oa_pages, notes = self.resolver.open_access_candidates(doi)
        pages = list(oa_pages)
        if parent.get("url") and parent["url"] not in {a.url for a in first + oa_pdfs + pages}:
            pages.append(PdfAttempt(parent["url"], "the item's URL"))
        data, hit, tried, page = self.resolver.find_pdf(first + oa_pdfs, pages)
        report: dict = {}
        if data:
            same = self._same_pdf_attached(parent["key"], data)
            if same:
                hit.result = f"already attached ({same})"
                report["pdf"] = f"that PDF is already attached ({same})"
            else:
                with self._writing():
                    self._attach_pdf(parent, data, url=hit.url)
                report["pdf"] = f"PDF attached from {hit.url}"
        else:
            report["pdf"] = "no free PDF found"
        if not data or len(tried) > 1:
            report["pdf_attempts"] = [a.as_dict() for a in tried]
        if notes and not data:
            report["pdf_notes"] = notes
        return report, page

    def add_items(self, queries: list[str], collections: list[str] | None = None, attach_pdfs: bool = True) -> dict:
        if self.resolver is None:
            raise RuntimeError("adding items needs the translation-server (see [translation_server] in the config)")
        collections = list(collections or [])
        for c in collections:
            if c not in self.lib.collections:
                raise NotFound(f"no collection {c}")
        results = []
        for q in queries:
            r = self.resolver.resolve(q)  # network work outside the lock
            entry: dict = {"query": q}
            if r.error:
                entry["error"] = r.error
                results.append(entry)
                continue
            if r.choices:
                entry["ambiguous"] = r.choices
                entry["hint"] = "Several matches: ask which one, then call add_items with its DOI/ISBN/URL."
                results.append(entry)
                continue
            if not r.items and r.pdf_urls:
                entry["error"] = ("that link is a bare PDF with no citation data: add the item from its DOI or article page (or create_item), "
                                  "then attach_pdf with this link")
                results.append(entry)
                continue
            for item in r.items:
                existing = self._find_existing(item)
                with self._writing():
                    if existing is not None:
                        parent = self._file_existing(existing, collections)
                        status = "already in library"
                    else:
                        key = new_key()
                        obj = {**item, "key": key, "version": 0, "collections": collections}
                        res = self.engine.zotero.write_items([obj])
                        if key not in res.versions:
                            results.append({**entry, "title": item.get("title"), "error": f"Zotero refused it: {res.failed.get(key) or 'conflict'}"})
                            continue
                        parent = {**obj, "version": res.versions[key]}
                        self.lib.apply_local_write([parent])
                        status = "added"
                out = {**entry, "key": parent["key"], "title": parent.get("title"), "itemType": parent.get("itemType"), "status": status}
                if self._pdf_attachments(parent["key"]):
                    out["pdf"] = "already had a PDF"
                elif not attach_pdfs:
                    out["pdf"] = "no PDF (not requested)"
                else:
                    report, _page = self._find_and_attach(parent, [PdfAttempt(u, "translator") for u in r.pdf_urls])
                    out.update(report)
                    if not self._pdf_attachments(parent["key"]) and parent.get("itemType") in WEB_TYPES:
                        out["hint"] = "A web article: attach_pdf with typeset_webpage=true makes a tablet PDF of the page's text."
                out["collections"] = [self._collection_path(c) for c in parent.get("collections") or [] if c in self.lib.collections]
                results.append(out)
        return {"results": results, "note": "Items in synced collections reach the tablet at the next sync."}

    def attach_pdf(self, item_key: str, url: str | None = None, typeset_webpage: bool = False) -> dict:
        """Attach a PDF to an existing item: from a link to the PDF or to a page that links to it, or
        (with no link) by looking for a free copy. ``typeset_webpage`` makes one from an article page's text."""
        if self.resolver is None:
            raise RuntimeError("attaching PDFs needs the translation-server settings (see [translation_server] in the config)")
        parent = self._regular(item_key)
        if url:
            data, hit, tried, page = self.resolver.find_pdf([PdfAttempt(url, "given")])
            report: dict = {}
            if data:
                same = self._same_pdf_attached(parent["key"], data)
                if same:
                    report["pdf"] = f"that PDF is already attached ({same})"
                else:
                    with self._writing():
                        self._attach_pdf(parent, data, url=hit.url)
                    report["pdf"] = f"PDF attached from {hit.url}"
            else:
                report["pdf"] = "no PDF at that link"
            if not data or len(tried) > 1:
                report["pdf_attempts"] = [a.as_dict() for a in tried]
        elif typeset_webpage:
            report, page = {}, None  # typeset the item's own page
        else:
            if self._pdf_attachments(parent["key"]):
                return {"key": parent["key"], "pdf": "already has a PDF; pass a url to attach another"}
            report, page = self._find_and_attach(parent, [])
        result = {"key": parent["key"], "title": parent.get("title"), **report}
        attached = report.get("pdf", "").startswith(("PDF attached", "that PDF is already"))
        if not attached and typeset_webpage:
            result.update(self._typeset_page(parent, page, url or parent.get("url")))
        elif not attached and page is not None and parent.get("itemType") in WEB_TYPES:
            result["hint"] = "This is a web page with no PDF: call again with typeset_webpage=true to make a tablet PDF of its text."
        if self._pdf_attachments(parent["key"]):
            result["note"] = "If the item is in a synced collection, the PDF reaches the tablet at the next sync."
        return result

    def _typeset_page(self, parent: dict, page, url: str | None) -> dict:
        from ..derive.rendition import RenditionError, render_webpage

        if page is None:
            if not url:
                return {"webpage_pdf": "the item has no URL to typeset; pass one"}
            _data, page, why = self.resolver.fetch(url)
            if page is None:
                return {"webpage_pdf": f"couldn't get the page: {why}"}
        settings = self.engine.settings
        authors = creators_text(parent)
        site = parent.get("websiteTitle") or parent.get("blogTitle") or parent.get("publicationTitle") or parent.get("institution") or ""
        try:
            pdf = render_webpage(page.html, page.url, settings.epub, title=parent.get("title") or "", byline=authors, site=site,
                                 date=parent.get("date") or "", pandoc=self.engine.tools.pandoc, font_paths=self.engine.tools.font_paths)
        except RenditionError as e:
            return {"webpage_pdf": f"couldn't typeset the page: {e}"}
        with self._writing():
            key = self._attach_pdf(parent, pdf, title="Web page (tablet PDF)", tag="rmzotero:webpage", url=page.url)
        return {"webpage_pdf": f"typeset the page's text into a PDF ({key}); open it in Zotero to check it"}
