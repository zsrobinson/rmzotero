"""The MCP server claude.ai connects to (Streamable HTTP at /mcp/<secret>)."""

from __future__ import annotations

import json
import logging
import threading
import time

import anyio
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .. import telemetry
from ..config import AccessConfig
from .auth import ConnectorAuth
from .ops import Busy, LibraryOps, NotFound
from .download import Downloads
from starlette.responses import Response

log = logging.getLogger(__name__)

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
CHANGE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
CHANGE_OPEN = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
RECLASSIFY = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)

INSTRUCTIONS = """\
This is the user's Zotero library, which also syncs to their reMarkable tablet.
Collections decide what is on the tablet: items in synced collections appear there automatically,
so organise the library rather than trying to "send" things. Highlights and handwriting made on the
tablet appear as Zotero annotations; use view_pages to see handwriting and drawings.
Ask the user how they want things organised before creating collections or filing many items.
"""


@telemetry.timed("serialize")
def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1)


def build(ops: LibraryOps, allowed_hosts: list[str]) -> FastMCP:
    mcp = FastMCP(
        "rmzotero",
        instructions=INSTRUCTIONS,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=[f"https://{h}" for h in allowed_hosts]
                            + ["http://127.0.0.1:*", "http://localhost:*"],
        ),
    )

    async def call(fn, *args, **kw):
        try:
            return await anyio.to_thread.run_sync(lambda: fn(*args, **kw))
        except BaseException as e:
            telemetry.failure(e)
            if isinstance(e, (NotFound, ValueError, Busy)):
                raise ValueError(str(e)) from e
            raise

    downloads = Downloads(ops)

    @mcp.tool(annotations=READ)
    async def pdf_link(key: str, page_range: str | None = None) -> str:
        """Download the ORIGINAL PDF with its real layout via a 10-minute capability URL.
        Pass the URL directly to a URL-consuming tool; never copy PDF bytes through the model.
        Optional page_range (1-3,5) subsets on the server; returned page numbers start at 1.
        Treat the URL as a credential. Do not log or publish it.
        """
        return _json(await call(downloads.link, key, page_range))

    @mcp.custom_route("/pdf-download/{token}", methods=["GET"])
    async def download_pdf(request):
        # Use the same content-free record as MCP calls. Never record request
        # paths/URLs, capabilities, response bodies or exception messages.
        with telemetry.call_run("rmzotero", "pdf_download") as record:
            try:
                data = await call(downloads.fetch, request.path_params["token"])
            except Exception:
                response = Response(status_code=404, headers={"Cache-Control": "no-store"})
            else:
                response = Response(data, media_type="application/pdf", headers={
                    "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                    "Content-Disposition": 'attachment; filename="document.pdf"',
                    "X-Content-Type-Options": "nosniff"})
            if record is not None:
                record["resp_bytes"] = len(response.body)
            return response

    @mcp.tool(annotations=READ)
    async def service_diagnostics() -> str:
        """How the sync service itself is doing: what it's working on now, how long recent sync cycles and their
        steps took, queued conversions, documents waiting to sync, recent errors and failures (with tracebacks),
        and counts. Use this when the user reports something slow, stuck or broken."""
        return _json(await call(ops.diagnostics))

    @mcp.tool(annotations=READ)
    async def search_library(query: str, collection: str | None = None, limit: int = 20, offset: int = 0) -> str:
        """Find items by words in their title, authors, year, publication, DOI, tags, abstract, or in the text and
        comments of their annotations. Optionally only within a collection (key) and its subcollections.
        An empty query lists everything in scope, so query="" with a collection lists that collection.
        Each result says whether the item has a PDF (has_pdf). Up to 100 per call; pass next_offset as offset for more."""
        return _json(await call(ops.search_library, query, collection, limit, offset))

    @mcp.tool(annotations=READ)
    async def get_item(key: str) -> str:
        """Full details of an item (or of the item an attachment/note belongs to): metadata, tags, collections,
        notes, attachments, and whether/where each attachment is on the tablet."""
        return _json(await call(ops.get_item, key))

    @mcp.tool(annotations=READ)
    async def list_collections() -> str:
        """All collections with their keys, full paths and item counts."""
        return _json(await call(ops.list_collections))

    @mcp.tool(annotations=READ)
    async def get_annotations(key: str) -> str:
        """Highlights, comments, notes and drawings on an item's attachments, in page order, with colour and whether
        they were made on the tablet or in Zotero. Pass a collection key to get them for every item in it."""
        return _json(await call(ops.get_annotations, key))

    @mcp.tool(annotations=READ)
    async def read_pages(key: str, pages: str | None = None) -> str:
        """The text of an item's PDF (or a specific attachment), e.g. pages="1-3" or "5,7". Up to 20 pages per call."""
        return _json(await call(ops.read_pages, key, pages))

    @mcp.tool(annotations=READ)
    async def view_pages(key: str, pages: str | None = None) -> list:
        """Images of PDF pages with the user's tablet handwriting, drawings and highlights drawn exactly as made
        (or Zotero's annotations for pages never written on at the tablet). Up to 4 pages per call, e.g. pages="2"."""
        images = await call(ops.view_pages, key, pages)
        out: list = []
        for label, png in images:
            out.append(f"Page {label}:")
            out.append(Image(data=png, format="png"))
        return out

    @mcp.tool(annotations=CHANGE_OPEN)
    async def add_items(queries: list[str], collections: list[str] | None = None, attach_pdfs: bool = True) -> str:
        """Add items to the library from DOIs, ISBNs, arXiv IDs, PMIDs or URLs of article/book pages, using Zotero's
        own translators. Existing items are reused (and filed into the given collections) instead of duplicated.
        Freely available PDFs are attached (from the translators, OpenAlex, Unpaywall and the PDF link a landing page
        declares); when none is, pdf_attempts lists every link tried and why it failed. If a site refuses the
        translators (common for news sites), use create_item instead. Collections are keys."""
        return _json(await call(ops.add_items, queries, collections, attach_pdfs))

    @mcp.tool(annotations=CHANGE_OPEN)
    async def attach_pdf(item_key: str, url: str | None = None, typeset_webpage: bool = False) -> str:
        """Attach a PDF to an existing item. With url: a link to the PDF itself, or to a page that links to it
        (publisher, repository or DSpace pages declare their PDF). Without url: looks for a free copy by the item's
        DOI and URL. typeset_webpage=true, for a web article with no PDF, typesets the page's text (url, or the
        item's URL) into a tablet-sized PDF instead. The result lists each link tried and why it failed."""
        return _json(await call(ops.attach_pdf, item_key, url, typeset_webpage))

    @mcp.tool(annotations=CHANGE)
    async def create_item(item_type: str, fields: dict, collections: list[str] | None = None) -> str:
        """Create an item from metadata you give, for sources no translator can read (blocked news sites, guides,
        chapters). item_type is a Zotero type (journalArticle, book, bookSection, newspaperArticle, magazineArticle,
        webpage, blogPost, report...). fields uses Zotero field names (title, date, url, publicationTitle, bookTitle,
        pages, publisher, institution, websiteTitle, accessDate, DOI, ISBN, abstractNote, extra...) plus creators as
        names ("Jane Smith" or "Smith, Jane"; {"name": "Org"} for an organisation; {"name"/"firstName"+"lastName",
        "creatorType": "editor"} for other roles) and tags. An existing match is reused instead. Collections are keys."""
        return _json(await call(ops.create_item, item_type, fields, collections))

    @mcp.tool(annotations=CHANGE)
    async def create_collection(name: str, parent: str | None = None) -> str:
        """Create a collection (optionally inside a parent collection key). Returns the existing one if the
        same name already exists in that place."""
        return _json(await call(ops.create_collection, name, parent))

    @mcp.tool(annotations=RECLASSIFY)
    async def file_items(item_keys: list[str], add_to: list[str] | None = None, remove_from: list[str] | None = None) -> str:
        """Add items to collections and/or remove them from collections (keys). Items are never deleted. What's on the
        tablet follows the collections, so removing an item from its synced collections takes it off the tablet."""
        return _json(await call(ops.file_items, item_keys, add_to, remove_from))

    @mcp.tool(annotations=CHANGE)
    async def add_note(item_key: str, text: str) -> str:
        """Attach a note to an item (plain text; blank lines separate paragraphs)."""
        return _json(await call(ops.add_note, item_key, text))

    @mcp.tool(annotations=RECLASSIFY)
    async def edit_item(item_key: str, fields: dict | None = None, add_tags: list[str] | None = None,
                        remove_tags: list[str] | None = None) -> str:
        """Change an item's metadata fields (Zotero field names, e.g. title, date, publicationTitle, DOI, abstractNote,
        creators) and add/remove tags. fields={"itemType": "report"} changes its type the way Zotero does: fields
        with a counterpart move (blogTitle -> websiteTitle), the result lists any dropped with their values, and you
        can set the new type's own fields (e.g. institution) in the same call. Creators can be given as names
        ("Jane Smith"). Collections are changed with file_items."""
        return _json(await call(ops.edit_item, item_key, fields, add_tags, remove_tags))

    telemetry.install(mcp, "rmzotero", "0.4.0")
    return mcp


def create_app(ops: LibraryOps, secret: str, access: AccessConfig | None = None,
               legacy_enabled: bool = True, legacy_hostnames: list[str] | None = None):
    access = access or AccessConfig()
    legacy_hostnames = legacy_hostnames if legacy_hostnames is not None else [
        "rmzoteroclaude.zsrobinson.com", "127.0.0.1", "localhost"]
    hosts = list(dict.fromkeys(access.hostnames + legacy_hostnames))
    allowed = hosts + [f"{h}:*" for h in hosts]
    app = build(ops, allowed).streamable_http_app()
    # The outer ASGI lifespan runs at boot; the stateless MCP lifespan runs per request.
    from contextlib import asynccontextmanager
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with original(app) as state:
            if telemetry.enabled():
                telemetry.emit(dict(event="mcp_start", v=1, service="rmzotero", version="0.4.0",
                                    startup_ms=(time.perf_counter() - telemetry.PROCESS_START) * 1000))
            yield state

    app.router.lifespan_context = lifespan
    return ConnectorAuth(app, secret, access, legacy_enabled, hosts)


def serve(ops: LibraryOps, secret: str, host: str, port: int, *, access: AccessConfig | None = None,
          legacy_enabled: bool = True, legacy_hostnames: list[str] | None = None) -> tuple[threading.Thread, dict]:
    """Run the MCP server on its own thread; returns the thread and a dict filled with the bound port."""
    import uvicorn

    app = create_app(ops, secret, access, legacy_enabled, legacy_hostnames)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="on", access_log=False)
    server = uvicorn.Server(config)
    info: dict = {"server": server}

    def run() -> None:
        server.run()

    t = threading.Thread(target=run, name="mcp", daemon=True)
    t.start()
    for _ in range(200):
        if server.started:
            info["port"] = server.servers[0].sockets[0].getsockname()[1]
            break
        threading.Event().wait(0.05)
    return t, info
