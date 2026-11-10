"""Command line: run the service, register with rmfakecloud, and the two hardware checks."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import tomllib
import uuid
from pathlib import Path

from rmscene import scene_items as si

from .config import Config
from .state import State

log = logging.getLogger("rmzotero")


def _clients(cfg: Config):
    from .tablet.cloud import RmCloudClient
    from .zotero.client import ZoteroClient
    from .zotero.webdav import WebDAVFiles

    zotero = ZoteroClient(cfg.zotero_user_id, cfg.zotero_api_key)
    files = WebDAVFiles(cfg.webdav_url, cfg.webdav_username, cfg.webdav_password)
    cloud = RmCloudClient(cfg.rmfakecloud_url, cfg.data_dir / "device-token")
    return zotero, files, cloud


def _engine(cfg: Config):
    from .engine import Engine, Tools

    zotero, files, cloud = _clients(cfg)
    state = State(cfg.data_dir / "state.db")
    fonts = os.environ.get("RMZOTERO_FONT_PATHS", "")
    tools = Tools(font_paths=[p for p in fonts.split(":") if p], ocr_language=os.environ.get("RMZOTERO_OCR_LANGUAGE", "eng"))
    return Engine(cfg, state, zotero, files, cloud, tools=tools), cloud


def cmd_run(cfg: Config, args) -> int:
    from .api import Api
    from .listeners import rmcloud_notifications, run_forever, zotero_stream

    engine, cloud = _engine(cfg)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: (stop.set(), engine.wakeup.set()))
    signal.signal(signal.SIGINT, lambda *_: (stop.set(), engine.wakeup.set()))
    host, _, port = cfg.api_listen.rpartition(":")
    Api(engine, cfg.api_token).serve(host or "0.0.0.0", int(port))
    log.info("plugin API listening on %s", cfg.api_listen)
    if cfg.mcp_legacy_enabled and cfg.mcp_secret and len(cfg.mcp_secret) < 24:
        log.error("Claude connector disabled: [claude] secret must be at least 24 characters (it is the only credential)")
    elif (cfg.mcp_legacy_enabled and cfg.mcp_secret) or cfg.access.enabled:
        from .claude.ops import LibraryOps
        from .claude.resolve import Resolver
        from .claude.server import serve as serve_mcp

        mhost, _, mport = cfg.mcp_listen.rpartition(":")
        ops = LibraryOps(engine, Resolver(cfg.translation_server_url, cfg.contact_email))
        _t, info = serve_mcp(ops, cfg.mcp_secret, mhost or "0.0.0.0", int(mport),
                             access=cfg.access, legacy_enabled=cfg.mcp_legacy_enabled,
                             legacy_hostnames=cfg.mcp_legacy_hostnames)
        log.info("Claude connector listening on %s", cfg.mcp_listen)
    else:
        log.info("Claude connector disabled (set [claude] secret to enable)")
    zotero_stream(cfg.zotero_api_key, cfg.zotero_user_id, engine.wakeup, stop)
    rmcloud_notifications(cloud.notifications_url(), cloud.user_token, engine.wakeup, stop)
    run_forever(engine, cfg.poll_seconds, stop)
    return 0


def cmd_sync_once(cfg: Config, args) -> int:
    engine, _ = _engine(cfg)
    rep = engine.sync()
    print(json.dumps(rep.as_dict(), indent=2))
    return 1 if rep.errors else 0


def cmd_register(cfg: Config, args) -> int:
    _, _, cloud = _clients(cfg)
    cloud.register(args.code)
    root, gen = cloud.get_root()
    print(f"Registered with rmfakecloud (tree generation {gen}).")
    return 0


# -- check A: Zotero accepts annotations written through the web API -------------------------------

CHECK_TAG = "rmzotero-check"


def cmd_check_zotero(cfg: Config, args) -> int:
    from .zotero.client import new_key
    from .zotero.library import Library

    zotero, _files, _cloud = _clients(cfg)
    lib = Library(State(cfg.data_dir / "check.db"))
    lib.refresh(zotero)
    if args.cleanup:
        mine = [(k, it["version"]) for k, it in lib.items.items() if any(t.get("tag") == CHECK_TAG for t in it.get("tags") or [])]
        res = zotero.delete_items(mine)
        print(f"Deleted {len(mine) - len(res.conflicts)} check annotations.")
        return 0
    att = lib.items.get(args.attachment) if args.attachment else next(
        (it for it in lib.items.values() if it.get("itemType") == "attachment" and it.get("contentType") == "application/pdf" and not it.get("deleted")), None)
    if att is None:
        print("No PDF attachment found; pass --attachment KEY.")
        return 2
    hk, ik = new_key(), new_key()
    common = {"itemType": "annotation", "parentItem": att["key"], "annotationComment": "Written by rmzotero check-zotero",
              "annotationPageLabel": "1", "tags": [{"tag": CHECK_TAG}]}
    objs = [
        {**common, "key": hk, "version": 0, "annotationType": "highlight", "annotationText": "(check)", "annotationColor": "#5fb236",
         "annotationSortIndex": "00000|000000|00110", "annotationPosition": json.dumps({"pageIndex": 0, "rects": [[72, 520, 300, 534]]})},
        {**common, "key": ik, "version": 0, "annotationType": "ink", "annotationColor": "#ff6666", "annotationSortIndex": "00000|000000|00100",
         "annotationPosition": json.dumps({"pageIndex": 0, "width": 2, "paths": [[100, 600, 150, 650, 200, 600, 250, 650]]})},
    ]
    res = zotero.write_items(objs)
    print(f"Attachment: {att.get('title')} ({att['key']})")
    ok = True
    for o in objs:
        if o["key"] in res.versions:
            print(f"  ✓ {o['annotationType']} annotation {o['key']} accepted (version {res.versions[o['key']]})")
        else:
            ok = False
            print(f"  ✗ {o['annotationType']} annotation refused: {res.failed.get(o['key']) or 'conflict'}")
    lib.refresh(zotero)
    for o in objs:
        back = lib.items.get(o["key"])
        if back is not None:
            print(f"  ✓ read back {o['annotationType']}: page {json.loads(back['annotationPosition'])['pageIndex'] + 1}")
    if not ok:
        print("\nZotero refused the annotations; please share the lines above.")
        return 1
    print("\nNow open this PDF in Zotero (after it syncs). You should see a green highlight near the top of page 1 and a red zig-zag.")
    print("Check that you can recolour or delete them. Remove them afterwards with: rmzotero check-zotero --cleanup")
    return 0 if ok else 1


# -- check B: the tablet accepts pages with a Zotero layer written by the service ---------------------

CHECK_DOC_NAME = "rmzotero check"


def cmd_check_tablet(cfg: Config, args) -> int:
    """Three steps, mirroring how the service really works with the tablet:

    1. ``check-tablet``: upload a test PDF without a page list (the tablet makes its own on first open).
    2. ``check-tablet --annotate``: after you've opened it and drawn on page 1, add a "Zotero" layer to
       the tablet's own page 1 (a box and a highlight), keeping your writing.
    3. ``check-tablet --verify``: after you've seen them and drawn a bit more, confirm nothing was lost.
    """
    from .pdf.edit import page_boxes
    from .pdf.simple import text_pdf
    from .pdf.text import TextLayer, line_rects
    from .tablet import content as tc
    from .tablet.scene import Page
    from .tablet.tree import TRASH, BlobCache, CloudTree, new_metadata

    _z, _f, cloud = _clients(cfg)
    tree = CloudTree(cloud, BlobCache(cfg.data_dir / "blobs"))
    tree.mirror()
    existing = [d for d in tree.docs.values() if d.name == CHECK_DOC_NAME and not tree.is_trashed(d.id)]
    state_file = cfg.data_dir / "check-tablet.json"

    def first_page(doc):
        listed = tc.pages(doc.content)
        if not listed:
            return None, None
        pid = listed[0].id
        e = doc.page_entries().get(pid)
        return pid, e

    def summarise(page: Page) -> tuple[int, int, int]:
        marks = page.marks()
        z = [m for m in marks if m.on_zotero_layer]
        return sum(m.is_stroke for m in z), sum(m.is_highlight for m in z), sum(1 for m in marks if not m.on_zotero_layer)

    if not (args.annotate or args.verify):
        for d in existing:  # start clean
            tree.stage(d.id, metadata={**d.metadata, "parent": TRASH})
        pdf = text_pdf([[("rmzotero tablet check", 20), ("Highlighted text written by the service sits on this line.", 12),
                         ("Step 1: draw a short line anywhere on this page, let the tablet sync, then run: rmzotero check-tablet --annotate", 11)],
                        [("Page two", 14)]])
        doc_id = str(uuid.uuid4())
        tree.stage(doc_id, metadata=new_metadata(CHECK_DOC_NAME, ""), content=tc.upload_content(2), files={".pdf": pdf})
        tree.commit()
        state_file.unlink(missing_ok=True)
        print(f"Uploaded '{CHECK_DOC_NAME}' to the top level of your tablet{' (the old one is in the tablet trash)' if existing else ''}.")
        print("On the tablet: open it, draw a short line on page 1, close it and let it sync. Then run: rmzotero check-tablet --annotate")
        return 0

    if not existing:
        print("No check document on the tablet; run check-tablet first.")
        return 2
    doc = existing[0]
    pid, entry = first_page(doc)
    if pid is None:
        print("The tablet hasn't opened the document yet (it has no page list). Open it on the tablet, let it sync, and try again.")
        return 2

    if args.annotate:
        if entry is not None:
            data = tree.read(entry)
            if not Page.roundtrips(data):
                print("✗ The tablet's page uses a format the service can't reproduce exactly, so it won't edit it. Please share this output.")
                return 1
            page = Page.parse(data)
        else:
            page = Page.empty()
        zs, zh, mine = summarise(page)
        if zs or zh:
            print("This page already has a Zotero layer; run check-tablet --verify.")
            return 0
        pdf = tree.read(doc.file(".pdf"))
        box = page_boxes(pdf)[0]
        chars = TextLayer(pdf).chars(0)
        start = "".join(c.text for c in chars).index("Highlighted")
        hl_rect = box.user_rect_to_rm(line_rects(chars[start:start + 11])[0])
        zl = page.zotero_layer_id()
        page.add(zl, [
            si.Line(si.PenColor.BLACK, si.Pen.FINELINER_2, [si.Point(x, y, 0, 0, 12, 100) for x, y in [(-300, 600), (300, 600), (300, 900), (-300, 900), (-300, 600)]], 1.0, 0.0),
            si.GlyphRange(None, 11, "Highlighted", si.PenColor.HIGHLIGHT, [si.Rectangle(*hl_rect)], color_rgba=(166, 230, 120, 255)),
        ])
        tree.stage(doc.id, files={f"/{pid}.rm": page.to_bytes()})
        if tree.commit():
            print("The tablet changed the document at the same moment; run --annotate again.")
            return 1
        state_file.write_text(json.dumps({"doc": doc.id, "page": pid, "mine": mine}))
        print(f"Added a 'Zotero' layer to page 1 (your page already had {mine} item{'s' if mine != 1 else ''}).")
        print("On the tablet: open the document again. You should see a box in the upper middle and a highlight on the word 'Highlighted',")
        print("on a layer called 'Zotero', with your line still there. Draw one more line, let it sync, then run: rmzotero check-tablet --verify")
        return 0

    # --verify
    before = json.loads(state_file.read_text()) if state_file.exists() else {}
    if entry is None:
        print("✗ Page 1 has no writing at all on rmfakecloud. Did the tablet sync?")
        return 1
    page = Page.parse(tree.read(entry))
    zs, zh, mine = summarise(page)
    print(f"Page 1 layers: {[label for _, label in page.layers()]}")
    print(f"Zotero layer: {zs} box + {zh} highlight (expected 1 and 1)")
    print(f"Your writing: {mine} items (was {before.get('mine', '?')} before the Zotero layer was added)")
    if before.get("page") and before["page"] != pid:
        print("✗ The tablet replaced the page the Zotero layer was written to.")
        return 1
    ok = zs == 1 and zh == 1 and mine > int(before.get("mine", 0))
    if ok:
        print("✓ The tablet kept the service's Zotero layer and your writing on the same page.")
    elif zs == 1 and zh == 1:
        print("… The Zotero layer survived, but your newer writing hasn't arrived yet. Let the tablet sync and run --verify again.")
    else:
        print("✗ The Zotero layer was lost; please share this output.")
    return 0 if ok else 1


def cmd_debug_page(cfg: Config, args) -> int:
    """Print the block structure of a tablet page, to compare layers the tablet made with the service's."""
    from rmscene.scene_stream import (AuthorIdsBlock, SceneGlyphItemBlock, SceneGroupItemBlock, SceneLineItemBlock,
                                      SceneTreeBlock, TreeNodeBlock)

    from .tablet import content as tc
    from .tablet.scene import Page
    from .tablet.tree import BlobCache, CloudTree

    _z, _f, cloud = _clients(cfg)
    tree = CloudTree(cloud, BlobCache(cfg.data_dir / "blobs"))
    tree.mirror()
    docs = [d for d in tree.docs.values() if d.name == args.document and not tree.is_trashed(d.id)]
    if not docs:
        print(f"No document named {args.document!r} on the tablet.")
        return 2
    doc = docs[0]
    listed = tc.pages(doc.content)
    print(f"content: formatVersion={doc.content.get('formatVersion')} cPages={'yes' if (doc.content.get('cPages') or {}).get('pages') else 'no'} pages={len(listed)}")
    if not 1 <= args.page <= len(listed):
        print("No such page.")
        return 2
    e = doc.page_entries().get(listed[args.page - 1].id)
    if e is None:
        print("That page has no writing.")
        return 0
    page = Page.parse(tree.read(e))
    for b in page.blocks:
        name = type(b).__name__
        item = getattr(b, "item", None)
        if isinstance(b, AuthorIdsBlock):
            info = {k: str(v)[:8] for k, v in b.author_uuids.items()}
        elif isinstance(b, SceneTreeBlock):
            info = f"tree={b.tree_id} parent={b.parent_id}"
        elif isinstance(b, TreeNodeBlock):
            g = b.group
            info = f"node={g.node_id} label={g.label.value!r}@{g.label.timestamp} visible={g.visible.value}@{g.visible.timestamp} anchor={g.anchor_id}"
        elif isinstance(b, (SceneGroupItemBlock, SceneLineItemBlock, SceneGlyphItemBlock)) and item is not None:
            v = item.value
            kind = "deleted" if v is None else type(v).__name__
            info = f"parent={b.parent_id} id={item.item_id} left={item.left_id} right={item.right_id} {kind}"
            if isinstance(v, si.Line):
                info += f" tool={v.tool.name} color={v.color.name} points={len(v.points)} thick={v.thickness_scale}"
        else:
            info = ""
        print(f"  {name:22} {info}")
    return 0


def cmd_restore_removed(cfg: Config, args) -> int:
    """Ask the running service to send back every document marked 'removed on tablet'."""
    import httpx

    _host, _, port = cfg.api_listen.rpartition(":")
    r = httpx.post(f"http://127.0.0.1:{port}/api/restore-removed", headers={"Authorization": f"Bearer {cfg.api_token}"}, timeout=30)
    r.raise_for_status()
    print(f"Restoring {r.json()['restored']} documents at the next sync.")
    return 0


def cmd_cleanup(cfg: Config, args) -> int:
    """List (or, with --apply, trash) tablet editions and searchable copies you didn't ask for."""
    import httpx

    _host, _, port = cfg.api_listen.rpartition(":")
    r = httpx.post(f"http://127.0.0.1:{port}/api/cleanup-copies", json={"apply": args.apply},
                   headers={"Authorization": f"Bearer {cfg.api_token}"}, timeout=1800)
    r.raise_for_status()
    copies = r.json()["copies"]
    if not copies:
        print("Nothing to clean up.")
        return 0
    for c in copies:
        what = "tablet edition" if c["kind"] == "edition" else "searchable copy"
        note = f", {c['annotations']} annotation(s) go to the trash with it" if c["annotations"] else ""
        err = f"  FAILED: {c['error']}" if c.get("error") else ""
        print(f"- {c['item']}: {what} \"{c['title']}\" ({c['reason']}{note}){err}")
    if args.apply:
        print(f"\nMoved {sum(1 for c in copies if not c.get('error'))} to Zotero's trash. The tablet catches up at the next sync.")
    else:
        print(f"\n{len(copies)} attachment(s) would go to Zotero's trash (recoverable there). Run again with --apply to do it.")
    return 0


def cmd_diagnostics(cfg: Config, args) -> int:
    """Ask the running service for its diagnostics; fall back to what's stored if it isn't running."""
    import httpx

    from .diagnostics import Failures, format_text

    _host, _, port = cfg.api_listen.rpartition(":")
    data = None
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/api/diagnostics", headers={"Authorization": f"Bearer {cfg.api_token}"}, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"(the service isn't answering on port {port}: {e}; showing what it last stored)", file=sys.stderr)
    if data is None:
        state = State(cfg.data_dir / "state.db")
        cycles = [json.loads(r["report"]) for r in state.all("SELECT report FROM cycles ORDER BY id DESC LIMIT 20")]
        data = {"stored_only": True, "last_report": state.get("last_report"), "recent_cycles": cycles,
                "failures": Failures(state).all(), "recent_events": state.events(limit=30)}
        print(json.dumps(data, indent=1, default=str))
        return 1
    print(json.dumps(data, indent=1, default=str) if args.json else format_text(data))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="rmzotero", description="Sync a Zotero library with a reMarkable tablet via rmfakecloud.")
    p.add_argument("--config", default=os.environ.get("RMZOTERO_CONFIG", "/config/rmzotero.toml"))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the service (sync loop + plugin API)")
    sub.add_parser("sync-once", help="run a single sync cycle and print the report")
    r = sub.add_parser("register", help="register with rmfakecloud using a one-time code from its web UI")
    r.add_argument("--code", required=True)
    cz = sub.add_parser("check-zotero", help="check that Zotero accepts annotations written by the service")
    cz.add_argument("--attachment", help="PDF attachment key to annotate (default: the first PDF found)")
    cz.add_argument("--cleanup", action="store_true", help="delete the check annotations")
    ct = sub.add_parser("check-tablet", help="check that the tablet accepts pages written by the service")
    ct.add_argument("--annotate", action="store_true", help="step 2: add a Zotero layer to the page you drew on")
    ct.add_argument("--verify", action="store_true", help="step 3: confirm the Zotero layer and your writing survived")
    sub.add_parser("restore-removed", help="send back every document marked 'removed on tablet'")
    cl = sub.add_parser("cleanup", help="list tablet editions and searchable copies you didn't ask for")
    cl.add_argument("--apply", action="store_true", help="move them to Zotero's trash")
    dg = sub.add_parser("diagnostics", help="what the running service is doing, timings and failures")
    dg.add_argument("--json", action="store_true", help="the full report as JSON")
    dp = sub.add_parser("debug-page", help="print the structure of a tablet page (for troubleshooting)")
    dp.add_argument("document", help="the document's name on the tablet")
    dp.add_argument("--page", type=int, default=1)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not args.verbose:
        for noisy in ("httpx", "httpcore", "pikepdf"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        # rmscene warns about newer tablet blocks it reads only partly; they're preserved and harmless here
        logging.getLogger("rmscene").setLevel(logging.ERROR)
    try:
        cfg = Config.load(args.config)
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.data_dir / ".write-test"
        probe.write_text("ok")
        probe.unlink()
    except FileNotFoundError as e:
        print(f"rmzotero: {e.filename} not found. Put rmzotero.toml in the folder mounted at /config.", file=sys.stderr)
        return 2
    except tomllib.TOMLDecodeError as e:
        print(f"rmzotero: {args.config} isn't valid TOML: {e}.\n"
              "Common causes: the same [section] written twice (merge them into one), or a missing quote.", file=sys.stderr)
        return 2
    except PermissionError as e:
        print(f"rmzotero: permission denied for {e.filename} (running as uid {os.getuid()}, gid {os.getgid()}).\n"
              "Make the config readable and the data folder writable by that user, or set PUID/PGID "
              "in the container's environment to the owner of those files.", file=sys.stderr)
        return 2
    return {"run": cmd_run, "sync-once": cmd_sync_once, "register": cmd_register,
            "check-zotero": cmd_check_zotero, "check-tablet": cmd_check_tablet, "debug-page": cmd_debug_page, "diagnostics": cmd_diagnostics, "restore-removed": cmd_restore_removed,
            "cleanup": cmd_cleanup}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
