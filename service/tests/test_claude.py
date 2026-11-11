"""The Claude connector: library operations, add_items resolution, page images, and the MCP protocol end to end."""

import asyncio
import io
import json

import httpx
import pytest
from PIL import Image as PILImage
from rmscene import scene_items as si

from rmzotero.claude.ops import LibraryOps, NotFound, parse_pages
from rmzotero.claude.render import rm_pixel
from rmzotero.claude.resolve import Resolver, isbn_digits, normalise_doi
from rmzotero.pdf import edit
from rmzotero.pdf.simple import text_pdf
from rmzotero.tablet import content as tc

from test_engine import PAPER, Clock, Tablet, add_paper, highlight_of_first_word, world  # noqa: F401  (fixture)

OA_PDF = text_pdf([[("An open access paper", 14)]], size=(400, 500))
TRANSLATED = {
    "itemType": "journalArticle", "title": "Deep Residual Learning", "date": "2016", "DOI": "10.1109/CVPR.2016.90",
    "creators": [{"creatorType": "author", "firstName": "Kaiming", "lastName": "He"}], "tags": ["vision"],
    "attachments": [{"title": "Snapshot", "mimeType": "text/html", "url": "https://example.org/page"}], "key": "XYZ", "version": 0,
}


REPO_PDF = text_pdf([[("A repository copy", 14)]], size=(400, 500))
ARTICLE_TEXT = " ".join(["Scooters were not the first vehicles to fight for the street; bicycles did it a century earlier."] * 12)
ARTICLE_PAGE = f"""<html><head><title>The fight over bicycles | Example Magazine</title></head><body>
<nav>Home | Sections | Subscribe</nav><article><h2>A century-old battle</h2><p>{ARTICLE_TEXT}</p><p>{ARTICLE_TEXT}</p>
<p>See https://example.org//odd/path and the street / sidewalk divide.</p><img src="lazy.jpg"/></article><footer>About us</footer></body></html>"""


def resolver(email="me@example.org"):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "translation-server" and req.url.path == "/web":
            u = req.content.decode()
            if "news.example.org" in u:
                return httpx.Response(500, text="An error occurred retrieving the document")
            raise AssertionError(u)
        if req.url.host == "api.openalex.org":
            if req.url.path.endswith("10.5555/repo"):
                return httpx.Response(200, json={"best_oa_location": {"is_oa": True, "pdf_url": None, "landing_page_url": "https://repo.example.org/item/7"},
                                                 "locations": [{"is_oa": False, "pdf_url": "https://publisher.example.org/paywalled.pdf"}]})
            if req.url.path.endswith("10.5555/closed"):
                return httpx.Response(200, json={"best_oa_location": None, "locations": [{"is_oa": False}]})
            return httpx.Response(404)
        if req.url.host == "repo.example.org":
            if req.url.path == "/item/7":
                return httpx.Response(200, text='<html><head><meta name="citation_pdf_url" content="/bitstreams/7/download"></head><body>Item</body></html>',
                                      headers={"content-type": "text/html; charset=utf-8"})
            if req.url.path == "/bitstreams/7/download":
                return httpx.Response(200, content=REPO_PDF, headers={"content-type": "application/octet-stream"})
        if req.url.host == "osf.io":
            if req.url.path == "/dpb36/download":
                return httpx.Response(200, content=REPO_PDF, headers={"content-type": "application/pdf"})
            return httpx.Response(200, text="<html><body><div id='app'></div></body></html>", headers={"content-type": "text/html"})
        if req.url.host == "blocked.example.org":
            return httpx.Response(403, text="Forbidden")
        if req.url.host == "news.example.org":
            if req.url.path == "/paywalled":
                return httpx.Response(403)
            return httpx.Response(200, text=ARTICLE_PAGE, headers={"content-type": "text/html; charset=utf-8"})
        if req.url.path == "/search":
            q = req.content.decode()
            if "10.1109" in q:
                return httpx.Response(200, json=[TRANSLATED])
            if "attention" in q.lower():
                return httpx.Response(200, json=[{"itemType": "journalArticle", "title": "Attention", "DOI": "10.5555/attention", "creators": []}])
            if q == "ambiguous words":
                return httpx.Response(300, json={"a1": "First match", "a2": "Second match"})
            return httpx.Response(501)
        if req.url.host == "api.unpaywall.org":
            assert req.url.params["email"] == email
            if "10.1109" not in req.url.path:
                return httpx.Response(404)
            return httpx.Response(200, json={"best_oa_location": {"url_for_pdf": "https://oa.example.org/paper.pdf"}})
        if req.url.host == "oa.example.org":
            return httpx.Response(200, content=OA_PDF, headers={"content-type": "application/pdf"})
        raise AssertionError(req.url)

    return Resolver("http://translation-server:1969", email, client=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.fixture
def lib_world(world):  # noqa: F811
    z, files, cloud, clock, eng, tablet = world
    ml = z.add_collection("ML")
    item, att = add_paper(z, files, [ml])
    z.add_item(highlight_of_first_word(att))
    eng.sync()
    return z, files, eng, tablet, clock, LibraryOps(eng, resolver()), ml, item, att


def test_helpers():
    assert parse_pages("2,4-5", 10, 4) == [1, 3, 4]
    assert parse_pages(None, 3, 20) == [0, 1, 2]
    assert normalise_doi("https://doi.org/10.1000/ABC.1.") == "10.1000/abc.1"
    assert isbn_digits("0-306-40615-2") == isbn_digits("978-0-306-40615-7")


def test_reading(lib_world):
    z, files, eng, tablet, clock, ops, ml, item, att = lib_world
    found = ops.search_library("vaswani attention")
    assert found["items"][0]["key"] == item and found["items"][0]["collections"] == ["ML"]
    assert ops.search_library("nothing-like-this")["count"] == 0
    detail = ops.get_item(att)  # an attachment key resolves to its item
    assert detail["key"] == item and detail["attachments"][0]["on_tablet"] and detail["attachments"][0]["tablet_location"] == "Zotero/ML"
    assert ops.list_collections()["collections"][0] == {"key": ml, "path": "ML", "items": 1, "parent": None}
    anns = ops.get_annotations(ml)["items"][0]["annotations"]
    assert anns[0]["text"] == "Attention" and anns[0]["color"] == "green" and anns[0]["comment"] == "key idea"
    pages = ops.read_pages(item, "2")
    assert pages["page_count"] == 3 and pages["pages"][0]["text"].startswith("Second page")
    with pytest.raises(NotFound):
        ops.get_item("NOPE1234")


def test_original_pdf_resolves_parent_and_attachment(lib_world):
    z, files, eng, tablet, clock, ops, ml, item, att = lib_world
    assert ops.original_pdf(item)[0] == att
    assert ops.original_pdf(att)[2] == PAPER
    assert ops.original_pdf(item)[2] == PAPER
    with pytest.raises(NotFound):
        ops.original_pdf("MISSING1")


def test_view_pages_draws_tablet_handwriting(lib_world):
    z, files, eng, tablet, clock, ops, ml, item, att = lib_world
    doc = tablet.open("Vaswani - 2017 - Attention")  # the tablet makes its page list on first open
    pid = tc.pages(doc.content)[1].id
    tablet.draw(doc, pid, [(-200, 900), (200, 900)])
    eng.sync()
    clock.advance(500)
    eng.sync()
    [(label, png)] = ops.view_pages(item, "2")
    img = PILImage.open(io.BytesIO(png)).convert("RGB")
    box = edit.page_boxes(PAPER)[1]
    assert label == "2" and img.getpixel(rm_pixel(box, 0, 900))[0] < 60  # the black stroke is there
    # a page never written on shows Zotero's highlight instead
    [(_l, png1)] = ops.view_pages(item, "1")
    r, g, b = PILImage.open(io.BytesIO(png1)).convert("RGB").getpixel((10, 10))
    assert (r, g, b) == (255, 255, 255)


def test_changing_the_library(lib_world):
    z, files, eng, tablet, clock, ops, ml, item, att = lib_world
    col = ops.create_collection("Reading group", parent=ml)
    assert col["created"] and col["path"] == "ML / Reading group"
    assert ops.create_collection("Reading group", parent=ml)["key"] == col["key"]  # no duplicate
    res = ops.file_items([item], add_to=[col["key"]])
    assert res["changed"] == [item] and set(z.objects[item]["collections"]) == {ml, col["key"]}
    assert ops.file_items([item], add_to=[col["key"]])["unchanged"] == [item]
    note = ops.add_note(item, "First thought.\n\nSecond paragraph <b>not html</b>.")
    assert "&lt;b&gt;" in z.objects[note["key"]]["note"] and z.objects[note["key"]]["parentItem"] == item
    ops.edit_item(item, {"title": "Attention Is All You Need"}, add_tags=["transformers"])
    assert z.objects[item]["title"] == "Attention Is All You Need" and {"tag": "transformers"} in z.objects[item]["tags"]
    with pytest.raises(ValueError):
        ops.edit_item(item, {"collections": []})
    assert eng.wakeup.is_set()
    # the tablet follows: filing into the subcollection leaves a pointer there
    clock.advance(500)
    assert not eng.sync().errors


def test_add_items(lib_world):
    z, files, eng, tablet, clock, ops, ml, item, att = lib_world
    z.update(item, DOI="https://doi.org/10.5555/ATTENTION")  # already in the library, under another spelling of its DOI
    eng.lib.refresh(z)
    res = ops.add_items(["doi:10.1109/CVPR.2016.90", "attention", "ambiguous words", "gibberish"], collections=[ml])["results"]
    added, reused, ambiguous, failed = res
    assert added["status"] == "added" and added["pdf"].startswith("PDF attached from https://oa.example.org")
    new = z.objects[added["key"]]
    assert new["title"] == "Deep Residual Learning" and new["collections"] == [ml] and new["tags"] == [{"tag": "vision"}]
    assert "attachments" not in new and new.get("key") == added["key"]
    pdfs = [o for o in z.items() if o.get("parentItem") == added["key"] and o.get("contentType") == "application/pdf"]
    assert len(pdfs) == 1 and files.get(pdfs[0]["key"])[1] == OA_PDF and pdfs[0]["md5"]
    assert reused["status"] == "already in library" and reused["key"] == item and reused["pdf"] == "already had a PDF"
    assert ambiguous["ambiguous"] == {"a1": "First match", "a2": "Second match"}
    assert "no translator" in failed["error"]
    # adding the same DOI again reuses it
    again = ops.add_items(["10.1109/cvpr.2016.90"])["results"][0]
    assert again["key"] == added["key"] and again["status"] == "already in library"
    # the new paper syncs to the tablet like anything else
    clock.advance(500)
    rep = eng.sync()
    assert not rep.errors and rep.uploaded == 1


def test_listing_a_collection_and_which_items_have_pdfs(lib_world):
    z, files, eng, tablet, clock, ops, ml, item, att = lib_world
    book = ops.create_item("book", {"title": "Redefining Geek", "creators": ["Cassidy Puckett"]}, collections=[ml])["key"]
    listed = ops.search_library("", collection=ml)
    assert listed["count"] == 2 and {i["key"]: i["has_pdf"] for i in listed["items"]} == {item: True, book: False}
    assert "next_offset" not in listed
    page = ops.search_library("", collection=ml, limit=1)
    assert len(page["items"]) == 1 and page["next_offset"] == 1
    assert len(ops.search_library("", collection=ml, limit=1, offset=1)["items"]) == 1


def test_create_item_for_sources_no_translator_reads(lib_world):
    z, files, eng, tablet, clock, ops, ml, item, att = lib_world
    res = ops.create_item("newspaperArticle", {
        "title": "Amazon and Baltimore", "publicationTitle": "The New York Times", "date": "2019-11-30",
        "url": "https://news.example.org/amazon", "creators": ["Smith, Jane", "John Q. Public", {"name": "Staff"}],
    }, collections=[ml])
    assert res["status"] == "added" and "typeset_webpage" in res["next"]
    new = z.objects[res["key"]]
    assert new["creators"] == [
        {"creatorType": "author", "firstName": "Jane", "lastName": "Smith"},
        {"creatorType": "author", "firstName": "John Q.", "lastName": "Public"},
        {"creatorType": "author", "name": "Staff"}] and new["collections"] == [ml]
    # the same citation again is reused
    assert ops.create_item("newspaperArticle", {"title": "Amazon and Baltimore"})["key"] == res["key"]
    with pytest.raises(ValueError, match="blogTitle isn't a field of newspaperArticle"):
        ops.create_item("newspaperArticle", {"title": "x", "blogTitle": "y"})
    with pytest.raises(ValueError, match="no item type"):
        ops.create_item("newsArticle", {"title": "x"})
    with pytest.raises(ValueError, match="no creator type"):
        ops.create_item("book", {"title": "A whole book", "creators": [{"lastName": "X", "creatorType": "bookAuthor"}]})


def test_edit_item_changes_the_item_type(lib_world):
    z, files, eng, tablet, clock, ops, ml, item, att = lib_world
    key = z.add_item({"itemType": "blogPost", "title": "Bystander intervention on social media", "blogTitle": "Brookings",
                      "websiteType": "Research", "url": "https://www.brookings.edu/articles/x/", "creators": [{"creatorType": "commenter", "name": "Reader"}]})
    eng.lib.refresh(z)
    res = ops.edit_item(key, {"itemType": "report", "institution": "Brookings Institution", "creators": ["Hanna, Alex"]})
    assert res["itemType"] == "blogPost -> report" and res["moved"] == {"reportType": "Research"} and res["dropped"] == {"blogTitle": "Brookings"}
    stored = z.objects[key]
    assert stored["itemType"] == "report" and stored["reportType"] == "Research" and stored["institution"] == "Brookings Institution"
    assert "blogTitle" not in stored and "websiteType" not in stored
    assert stored["creators"] == [{"creatorType": "author", "firstName": "Alex", "lastName": "Hanna"}]
    assert eng.lib.items[key]["itemType"] == "report" and "blogTitle" not in eng.lib.items[key]  # the mirror follows
    # a creator role the new type doesn't have becomes its primary role
    key2 = z.add_item({"itemType": "blogPost", "title": "Another post", "creators": [{"creatorType": "commenter", "name": "Reader"}]})
    eng.lib.refresh(z)
    ops.edit_item(key2, {"itemType": "webpage"})
    assert z.objects[key2]["creators"] == [{"creatorType": "author", "name": "Reader"}]
    with pytest.raises(ValueError, match="isn't a field of report"):
        ops.edit_item(key, {"blogTitle": "Brookings"})


def test_attach_pdf_from_links_and_pages(lib_world):
    z, files, eng, tablet, clock, ops, ml, item, att = lib_world
    def pdfs(key):
        return [o for o in z.items() if o.get("parentItem") == key and o.get("contentType") == "application/pdf"]

    # a repository landing page that declares its PDF (DSpace, EPrints, publishers...)
    k = ops.create_item("journalArticle", {"title": "Connecting spaces", "DOI": "10.5555/repo"})["key"]
    res = ops.attach_pdf(k)
    assert res["pdf"] == "PDF attached from https://repo.example.org/bitstreams/7/download"
    assert files.get(pdfs(k)[0]["key"])[1] == REPO_PDF
    assert [a["from"] for a in res["pdf_attempts"]] == ["OpenAlex (landing page)", "linked from https://repo.example.org/item/7"]
    assert ops.attach_pdf(k)["pdf"].startswith("already has a PDF")
    assert ops.attach_pdf(k, "https://repo.example.org/item/7")["pdf"].startswith("that PDF is already attached")

    # OSF pages are scripts with no PDF link, but the file has a fixed address
    k3 = ops.create_item("journalArticle", {"title": "Disciplining Play"})["key"]
    res = ops.attach_pdf(k3, "https://osf.io/preprints/socarxiv/dpb36")
    assert res["pdf"] == "PDF attached from https://osf.io/dpb36/download"

    # nothing free: every reason is given
    k2 = ops.create_item("journalArticle", {"title": "A closed paper", "DOI": "10.5555/closed", "url": "https://blocked.example.org/a"})["key"]
    res = ops.attach_pdf(k2)
    assert res["pdf"] == "no free PDF found" and not pdfs(k2)
    assert res["pdf_attempts"] == [{"url": "https://blocked.example.org/a", "from": "the item's URL",
                                    "result": "HTTP 403: the site refused (it needs a login, or blocks automated downloads)"}]
    assert "OpenAlex: not open access" in res["pdf_notes"]
    res = ops.attach_pdf(k2, "https://news.example.org/fight")
    assert res["pdf"] == "no PDF at that link" and "a web page, not a PDF" in res["pdf_attempts"][0]["result"]


def test_web_articles_typeset_into_a_tablet_pdf(lib_world):
    z, files, eng, tablet, clock, ops, ml, item, att = lib_world
    import shutil

    if not shutil.which("pandoc"):
        pytest.skip("pandoc not installed")
    k = ops.create_item("magazineArticle", {"title": "The fight over bicycles", "publicationTitle": "Example Magazine",
                                            "url": "https://news.example.org/fight", "creators": ["Ann Thompson"]}, collections=[ml])["key"]
    res = ops.attach_pdf(k)  # no DOI and no PDF anywhere
    assert res["pdf"] == "no free PDF found" and "typeset_webpage" in res["hint"]
    res = ops.attach_pdf(k, typeset_webpage=True)
    assert res["webpage_pdf"].startswith("typeset the page's text")
    [pdf] = [o for o in z.items() if o.get("parentItem") == k and o.get("contentType") == "application/pdf"]
    assert pdf["title"] == "Web page (tablet PDF)" and pdf["tags"] == [{"tag": "rmzotero:webpage"}]
    from rmzotero.pdf.text import TextLayer

    tl = TextLayer(files.get(pdf["key"])[1])
    text = "".join(c.text for i in range(len(tl)) for c in tl.chars(i))
    assert text.startswith("The fight over bicycles") and "Ann Thompson · Example Magazine" in text
    assert "bicycles did it a century earlier" in text and "example.org//odd/path" in text and "Subscribe" not in text
    assert ops.attach_pdf(k, "https://news.example.org/paywalled", typeset_webpage=True)["webpage_pdf"].startswith("couldn't get the page: HTTP 403")
    # the tablet gets it like any PDF
    clock.advance(500)
    assert not eng.sync().errors


def test_blocked_sites_and_bare_pdfs_explain_themselves(lib_world):
    z, files, eng, tablet, clock, ops, ml, item, att = lib_world
    [blocked] = ops.add_items(["https://news.example.org/paywalled"])["results"]
    assert "refused" in blocked["error"] and "create_item" in blocked["error"]
    ops.resolver.email = ""
    [closed] = ops.add_items(["doi:10.1109/CVPR.2016.90"])["results"]  # only Unpaywall knows a free copy of this one
    assert closed["status"] == "added" and closed["pdf"] == "no free PDF found"
    assert "Unpaywall not asked: [claude] contact_email isn't set in rmzotero.toml" in closed["pdf_notes"]


def test_mcp_protocol_end_to_end(lib_world):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    from rmzotero.claude.server import serve

    z, files, eng, tablet, clock, ops, ml, item, att = lib_world
    _t, info = serve(ops, "s3cret-path", "127.0.0.1", 0)
    base = f"http://127.0.0.1:{info['port']}"
    try:
        assert httpx.post(f"{base}/mcp/wrong", json={}).status_code == 404

        async def session():
            async with streamablehttp_client(f"{base}/mcp/s3cret-path") as (read, write, _):
                async with ClientSession(read, write) as s:
                    init = await s.initialize()
                    assert "reMarkable" in (init.instructions or "")
                    tools = {t.name: t for t in (await s.list_tools()).tools}
                    assert set(tools) == {"search_library", "get_item", "list_collections", "get_annotations", "read_pages", "view_pages", "pdf_link",
                                          "add_items", "attach_pdf", "create_item", "create_collection", "file_items", "add_note",
                                          "edit_item", "service_diagnostics"}
                    assert tools["search_library"].annotations.readOnlyHint is True
                    assert tools["add_items"].annotations.readOnlyHint is False
                    assert tools["file_items"].annotations.destructiveHint is True
                    assert tools["service_diagnostics"].annotations.readOnlyHint is True
                    r = await s.call_tool("service_diagnostics", {})
                    diag = json.loads(r.content[0].text)
                    assert "activity" in diag and "failures" in diag and "recent_cycles" in diag
                    r = await s.call_tool("search_library", {"query": "attention"})
                    assert json.loads(r.content[0].text)["items"][0]["key"] == item
                    r = await s.call_tool("view_pages", {"key": item, "pages": "1"})
                    kinds = [c.type for c in r.content]
                    assert kinds == ["text", "image"] and r.content[1].mimeType == "image/png"
                    r = await s.call_tool("get_item", {"key": "NOPE1234"})
                    assert r.isError and "no item" in r.content[0].text
                    r = await s.call_tool("create_collection", {"name": "From Claude"})
                    assert json.loads(r.content[0].text)["created"]

        asyncio.run(session())
    finally:
        info["server"].should_exit = True
    assert any(c.get("name") == "From Claude" for c in z.objects.values() if c["_kind"] == "collection")
