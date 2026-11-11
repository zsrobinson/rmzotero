# rmzotero

Keep a Zotero library and a reMarkable 2 in step, through your self-hosted rmfakecloud:

- your library's PDFs appear on the tablet, in folders that mirror your Zotero collections, and so do EPUBs once you've made their tablet edition;
- what you write on the tablet shows up in Zotero as native annotations: highlights with their text, pen strokes as ink;
- what you annotate in Zotero shows up on the tablet, on its own "Zotero" layer;
- page edits made on either side (note pages inserted on the tablet; pages deleted or rotated in Zotero) carry across.

Primary use cases: **class readings and papers** (often scanned; a searchable copy is made automatically) and **books for personal reading** (EPUBs are typeset on request into tablet-sized PDFs with the tablet's own fonts and text settings).

Design notes: [GLOSSARY.md](GLOSSARY.md), decisions in [docs/adr/](docs/adr/), and the [working assumptions](docs/assumptions.md) that still need checking on real hardware.

**Status:** built and tested end to end against in-memory Zotero/WebDAV, a simulated tablet, and a real rmfakecloud (v0.0.32) in Docker. Not yet run against a real tablet, a real Zotero account, or inside Zotero itself; the two checks in step 3 below cover the riskiest of those.

## How it fits together

```
 Zotero desktop ── plugin ──HTTPS──┐
      │                            │
 zotero.org (data sync)            ▼
      ▲            ┌──────── rmzotero service (Docker, on your NAS) ────────┐
      │            │ Zotero web API ◄── sync engine ──► rmfakecloud (as a   │
 WebDAV share ◄────┤  WebDAV files        │              second device)     │
 (your files)      │                      ▼                                 │
                   │     state + exact copy of every tablet page            │
                   └────────────────────────────────────────────────────────┘
                                                   ▲
                                         reMarkable 2 + rmfakecloud-proxy
```

- **service/**: a Python service: the sync engine, a small HTTP API for the plugin, and command-line checks.
- **plugin/**: a Zotero 7/8 plugin that adds a reMarkable column, an item pane section and a settings pane.
- **deploy/**: an example config and a docker compose snippet.

## Setup

You need rmfakecloud already working with your tablet (yours is), Zotero syncing files to WebDAV (yours does), and a Zotero API key with write access to your personal library (zotero.org → Settings → Security → Create new private key). Your user ID is shown on the same page.

### 1. Deploy the service next to rmfakecloud

The image is built by GitHub Actions and published as `ghcr.io/zsrobinson/rmzotero` (amd64 and arm64). On the NAS, in the folder with your rmfakecloud `docker-compose.yml`:

```sh
mkdir -p rmzotero/config rmzotero/data
curl -L -o rmzotero/config/rmzotero.toml https://raw.githubusercontent.com/zsrobinson/rmzotero/main/deploy/rmzotero.example.toml
# edit rmzotero/config/rmzotero.toml: Zotero user id + key, WebDAV URL/login, a long random api.token
```

Add the service from [`deploy/docker-compose.snippet.yml`](deploy/docker-compose.snippet.yml) to your compose file, then:

```sh
docker compose pull rmzotero
```

If the repository is private, the image is too: run `docker login ghcr.io` on the NAS first, with your GitHub username and a token that has `read:packages`.

### 2. Register the service with rmfakecloud

In rmfakecloud's web UI, generate a one-time code (the same kind you used for the tablet), then:

```sh
docker compose run --rm rmzotero register --code <code>
```

### 3. Run the two checks (recommended before the first sync)

These answer the two open questions the design depends on (assumptions A1 and A2):

```sh
# Zotero accepts annotations written by the service (writes a highlight and a zig-zag on one PDF)
docker compose run --rm rmzotero check-zotero              # then look at that PDF in Zotero
docker compose run --rm rmzotero check-zotero --cleanup

# The tablet accepts pages with a "Zotero" layer written by the service
docker compose run --rm rmzotero check-tablet              # then open "rmzotero check" on the tablet and draw on page 1
docker compose run --rm rmzotero check-tablet --verify
```

If either fails, don't start the sync; the output tells you what was lost. `docs/assumptions.md` lists the fallback for each.

### 4. Start it

```sh
docker compose up -d rmzotero
docker compose logs -f rmzotero
```

To start small, set `excluded_collections` in the config (collection keys), or start with the plugin and untick collections before saving.

Expose port 8787 through your existing tunnel or reverse proxy **with HTTPS**, the same way you expose WebDAV.

### 5. Install the plugin

Download `rmzotero.xpi` from the [latest release](https://github.com/zsrobinson/rmzotero/releases/latest). In Zotero: Tools → Plugins → ⚙ → Install Plugin From File… → `rmzotero.xpi`. Zotero updates it automatically after that. Then Settings → reMarkable: enter the service address and token, press **Connect**, choose collections, **Save**.

### If the container keeps restarting

Check its log. A *permission denied* message means the container can't read `rmzotero.toml` or write the data folder. The container runs as whoever owns the `rmzotero/data` folder on the NAS. Make sure that's the same account that owns the config, or set `PUID`/`PGID` in the container's environment (Synology: `id <your-user>` over SSH shows the numbers, usually 1026/100).

### Updating

With `pull_policy: always` (in the snippet), restarting or rebuilding the project in Container Manager pulls the newest image. Over SSH:

```sh
docker compose pull rmzotero && docker compose up -d rmzotero
```

To see which commit you're running: `docker image inspect ghcr.io/zsrobinson/rmzotero:latest --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'`

## Using it

- **Item list**: right-click the column header → enable "reMarkable". ✓ on tablet · ↻ syncing · ⏳ making a tablet edition · ⊘ removed on tablet · ☁ archived · ⚠ problem.
- **Item pane → reMarkable**: where the document lives on the tablet, annotation counts, *Send to tablet again* / *Remove from tablet*, the folder used when the item is in several collections, and for an EPUB, *Make tablet edition* with its text settings.
- **Right-click items**: Sync now, Make reMarkable edition (EPUBs, with each book's text settings), Send to reMarkable again, Remove from reMarkable.
- **EPUBs**: only go to the tablet once you've made a tablet edition: a PDF typeset like the tablet's own ebook view, added to the item next to the EPUB. A progress window follows it (usually 10–30 seconds); open the PDF in Zotero to check it. Making a new edition replaces the old one unless you've written on it.
- **On the tablet**: write and highlight as usual. Moves and renames are put back on the next sync, because Zotero owns the organisation. Deleting a document marks it "removed on tablet"; it stays in Zotero and can be sent back.
- **Timing**: a document syncs once it has been left alone for 2 minutes (`quiet_seconds`), so the service never fights you mid-page.

## Claude connector

Claude can search and organise the library, read your highlights and see your handwriting (ADR 0015). It works from claude.ai on any device.

1. In `rmzotero.toml`, set `[claude] secret` to a long random string (`openssl rand -hex 32`) and, optionally, `contact_email` (Unpaywall needs one; without it free PDFs are still looked up through OpenAlex).
2. Add the `translation-server` service and the `8788` port from the compose snippet, and restart the project.
3. Expose port 8788 through your reverse proxy with HTTPS, like the plugin API.
4. In claude.ai: **Settings → Connectors → Add custom connector**, with the URL `https://<that address>/mcp/<secret>`.

Tools: `search_library` (an empty query lists a collection), `get_item`, `list_collections`, `get_annotations`, `read_pages`, `view_pages` (page images with your tablet writing), and, after you confirm each one in Claude, `add_items` (from DOIs, ISBNs, arXiv IDs or article URLs, with free PDFs attached), `create_item` (from a citation, for sources no translator can read), `attach_pdf` (from a link, a free copy, or by typesetting a web article's text), `create_collection`, `file_items`, `add_note`, `edit_item` (including the item type). Nothing deletes items. Claude never puts things on the tablet directly: filing an item into a synced collection does that.

When no PDF is found, the result lists each link tried and why it failed. Sites that refuse automated access (most news sites) are reported as such: Claude enters those readings with `create_item`, and you add the PDF. ADR 0017 has the details.

The URL is the password: keep it private, and change the secret to revoke access.

## When something seems slow or stuck

The service keeps a record of what it's doing: per-step timings for recent sync cycles, what's queued, and
anything that failed (with its error and when it will be retried; failures back off from 5 minutes up to a day,
or retry immediately when the file or settings change). Three ways to see it:

- **Plugin:** Zotero Settings → reMarkable shows what the service is doing now and any problems waiting to retry.
- **Claude:** ask Claude (with the connector) to look; it has a `service_diagnostics` tool.
- **SSH:** `sudo docker compose exec rmzotero rmzotero diagnostics` (add `--json` for the full report).

`rmzotero cleanup` lists tablet editions and searchable copies you didn't ask for: editions made automatically by versions before 0.3.0, and searchable copies of PDFs that already had text. `rmzotero cleanup --apply` moves them to Zotero's trash; the original PDFs go back to the tablet.

## What isn't synced (by design, for now)

- **Comments** on highlights and Zotero **notes** stay in Zotero (ADR 0008).
- **EPUB publisher styling** is replaced by our typesetting. Fixed-layout EPUBs are sent as-is without annotation sync, and DRM-protected EPUBs are skipped (ADR 0010).
- **A new version of a paper** is a new attachment and a new tablet document. Markup doesn't move between files (ADR 0011).
- **Reordering pages** isn't supported on either side.
- **Tablet software updates**: rmfakecloud is tested up to 3.27.1, and updates remove rmfakecloud-proxy. Check before updating.

## Development

GitHub Actions run the tests on every push (`ci.yml`), publish the Docker image whenever `service/` changes on `main` (`docker.yml`), and publish a plugin release whenever `plugin/package.json`'s version changes on `main` (`release.yml`). `translation-server.yml` builds Zotero's translation server for amd64 and arm64 every week (Zotero's own image is arm64-only) and publishes `ghcr.io/zsrobinson/translation-server`. To ship a plugin update, bump that version and merge.

Locally:

```sh
cd service && python -m venv .venv && .venv/bin/pip install -e '.[test]' && .venv/bin/pytest
cd plugin && npm ci && npx tsc --noEmit && npm test && npm run build   # → build/rmzotero.xpi
```

The service tests run everything end to end against in-memory Zotero, WebDAV and rmfakecloud, plus a simulated tablet. `tests/test_rmfakecloud_live.py` runs the same flow against a real rmfakecloud:

```sh
docker run -d -p 3001:3000 -e JWT_SECRET_KEY=dev -e STORAGE_URL=http://localhost:3001 --name rmfc ddvk/rmfakecloud
docker exec rmfc /rmfakecloud-docker setuser -u tester -p testpass123 -a -s && docker restart rmfc
RMFAKECLOUD_TEST_URL=http://localhost:3001 RMFAKECLOUD_TEST_LOGIN=tester:testpass123 .venv/bin/pytest tests/test_rmfakecloud_live.py
```

### Cloudflare Access connector

The origin can validate Cloudflare Access assertions for a separate hostname while
keeping the legacy connector available during migration. Access and its managed
OAuth application must already be configured upstream. Example (no credentials):

```toml
[access]
team_domain = "https://your-team.cloudflareaccess.com"
audience = "your-application-audience"
hostnames = ["zoteromcp.example.com"]
# allowed_emails = ["you@example.com"] # optional additional restriction
```

Connect Claude to `https://zoteromcp.example.com/mcp` and complete the Access sign-in.
Only configured Access hosts serve this path, and every request requires a valid
`Cf-Access-Jwt-Assertion`. The origin checks RS256 signatures against the team's
cached public JWKS, issuer, audience, expiry, and not-before (30 seconds of clock
leeway). Keys are cached for five minutes; unknown keys trigger refresh after the
JWT client's short refresh cooldown. Validation or JWKS failures return a generic
401; an email allowlist denial returns 403. Cookies and bearer headers do not
replace the assertion. A partial Access configuration fails startup.

Environment overrides: `RMZOTERO_ACCESS_TEAM_DOMAIN`, `RMZOTERO_ACCESS_AUDIENCE`,
`RMZOTERO_ACCESS_HOSTNAMES`, and `RMZOTERO_ACCESS_ALLOWED_EMAILS`. List overrides
are comma-separated; an empty list override clears the list.

Under the existing `[claude]` section, `legacy_enabled = true` keeps
`/mcp/<secret>` available on legacy hosts. Set it to `false` only after switching
connectors. `legacy_hostnames` defaults to
`["rmzoteroclaude.zsrobinson.com", "127.0.0.1", "localhost"]`, preserving the
existing deployment and local checks while rejecting other hosts. Deployments
using other legacy DNS names must add them before upgrading. These settings have
`RMZOTERO_MCP_LEGACY_ENABLED` (`true`/`false`) and
`RMZOTERO_MCP_LEGACY_HOSTNAMES` overrides. Host checks use the actual Host header,
including optional ports, and ignore forwarded host headers; HTTPS origins for
allowed hosts and local HTTP origins are accepted. Access hosts never accept the
secret path, even if also listed as legacy hosts. With Access unconfigured,
`/mcp` remains unavailable (404). Access-only operation needs no legacy secret.

To migrate: verify both endpoints, add and authorize the new connector, test a
read-only tool, then disable the old connector. Keep the legacy flag enabled
until the switch is complete. Do not share the legacy URL or include it in logs.

## Telemetry

Enabled by default; set `MCP_TELEMETRY=0` (or `false`/`off`) to disable.
Stderr receives one `mcp_call ` prefix followed by a JSON object per dispatched
call, and one `mcp_start ` record when the outer ASGI application starts, before any MCP request:

```json
{"event":"mcp_call","v":1,"ts":"<UTC ISO>","service":"rmzotero","tool":"<registered name>","outcome":"ok|tool_error|error|timeout|refused","error_class":null,"total_ms":0.0,"phase_ms":{"serialize":0.0},"req_bytes":0,"resp_bytes":0}
{"event":"mcp_start","v":1,"service":"rmzotero","version":"<package version>","startup_ms":0.0}
```

Durations use a monotonic clock. Startup includes process imports on Linux
(import-to-ready fallback elsewhere). Byte counts measure UTF-8 JSON MCP params
and normalized results, excluding the JSON-RPC envelope. Total time includes
result normalization and a serialization pass; transport delivery is excluded.
Arguments, results and exception messages are never emitted. Unknown tool names
become `unknown`; unknown exception types become `exception`. Returned MCP error
results are `tool_error`; raised exceptions are `error`, with timeouts and
permission failures classified separately. Error classes are fixed:
`ValueError`, `TypeError`, `OSError`, `TimeoutError`, `PermissionError`, `Busy`,
`NotFound`, `ToolError`, `CancelledError`, or `exception` (null on success).
Only measured phases appear; nested occurrences of the same phase count once.

Call phases: `lock_wait`, `mirror_read`, `upstream`, `render`, `serialize`.

### Pushing to the fleet collector

The NAS runs no log collector, so the service can also push the same records to
one: set `FLEET_OTLP_LOGS_URL` in the container's environment to an OTLP/HTTP
logs endpoint, e.g. `http://100.100.74.90:4318/v1/logs` (home's tailnet address;
see the compose snippet). Unset (the default), nothing is sent anywhere. When set,
each `mcp_call` and `mcp_start` record, exactly as allowlisted above, becomes the
map body of one OTLP JSON log record; `mcp_sync` stays on stderr. Delivery is best
effort from a background thread: a bounded queue (256 records), batches of up to
64, a 2-second timeout, no retries, no proxies or redirects. Records that don't
fit the queue or fail to send are dropped, so the collector being away never
slows or changes a tool call. Stderr output is unchanged.

## Original PDF handoff

`pdf_link(key, page_range=None)` resolves an item or PDF attachment and returns
`url`, `filename`, `page_count`, `byte_size`, and `expires_at` (Unix seconds).
The URL expires after ten minutes. Give it directly to a URL-consuming service;
PDF bytes never need to pass through model arguments. Without a range the original
attachment bytes are unchanged; `1-3,5` produces a PDF subset with original page
layout, numbered from 1. No tablet overlays or typeset renditions are added.

Set these process environment variables on the MCP service:

- `RMZOTERO_PDF_SECRET`: independent random signing secret, at least 32 bytes;
  keep outside source control. Rotation immediately invalidates existing links.
- `RMZOTERO_PDF_BASE_URL`: externally reachable HTTP(S) base URL, without query,
  fragment or credentials. Prefer a private network route.
- `RMZOTERO_PDF_MAX_BYTES`: positive byte limit (default 52428800), checked on
  original and subset output. Existing attachment retrieval loads the source in memory.

The same MCP listener serves `GET /pdf-download/{token}`. Any invalid, expired,
missing or unavailable capability returns 404. Responses are not cacheable. Only
exempt this path from upstream interactive authentication if needed for service
fetches; retain authentication on MCP and all other paths. Tokens authorize access
until expiry and bind the attachment and range; they are reusable, not single-use.
Disable/redact download URL logging at every proxy and monitoring layer. The
service disables HTTP access logging; do not publish returned URLs.

`pdf_link` uses the same content-free call telemetry as other MCP tools. Download
requests emit that same record schema under the fixed operation `pdf_download`,
with byte counts and outcome only; no path, URL, token or document body is logged.
The origin retains Host validation on downloads and checks their HMAC capability
instead of requiring an interactive Access assertion. MCP authentication is unchanged.
