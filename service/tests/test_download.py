import asyncio
import io
import json
from unittest.mock import Mock

import httpx
import pikepdf
import pytest

from rmzotero.claude.download import Downloads
from rmzotero.claude.server import create_app
from rmzotero.pdf.simple import text_pdf


@pytest.fixture
def downloads(monkeypatch):
    monkeypatch.setenv("RMZOTERO_PDF_SECRET", "x" * 32)
    monkeypatch.setenv("RMZOTERO_PDF_BASE_URL", "https://pdf.example")
    data = text_pdf([[('first', 12)], [('second', 12)]], size=(400, 500))
    ops = Mock()
    ops.original_pdf.return_value = ("ATTACH01", "original.pdf", data)
    return Downloads(ops)


def token(link):
    return link["url"].rsplit("/", 1)[-1]


def test_original_bytes_and_metadata(downloads):
    link = downloads.link("PARENT01")
    assert downloads.fetch(token(link)) == downloads.ops.original_pdf.return_value[2]
    assert link["page_count"] == 2
    assert link["filename"] == "original.pdf"
    assert link["byte_size"] == len(downloads.fetch(token(link)))
    downloads.ops.original_pdf.assert_called_with("ATTACH01")


def test_subset_preserves_page_size_and_content(downloads):
    link = downloads.link("PARENT01", "2")
    assert link["page_count"] == 1
    with pikepdf.Pdf.open(io.BytesIO(downloads.fetch(token(link)))) as pdf:
        assert len(pdf.pages) == 1
        assert list(pdf.pages[0].MediaBox) == [0, 0, 400, 500]
        assert b'second' in pdf.pages[0].Contents.read_bytes()


@pytest.mark.parametrize("pages", ["0", "3", "2-1", "1-3", "", "1;2", "-1"])
def test_invalid_ranges(downloads, pages):
    with pytest.raises(ValueError):
        downloads.link("PARENT01", pages)


def test_tampering_expiry_size_and_missing_secret(downloads, monkeypatch):
    link = downloads.link("PARENT01")
    signed = token(link)
    for invalid in [signed + "x", "bad", "x" + signed, signed.replace(".", "..")]:
        with pytest.raises(ValueError):
            downloads.fetch(invalid)
    monkeypatch.setattr("rmzotero.claude.download.time.time", lambda: link["expires_at"])
    with pytest.raises(ValueError):
        downloads.fetch(signed)
    downloads.limit = 1
    with pytest.raises(ValueError):
        downloads.link("PARENT01")
    downloads.secret = b""
    with pytest.raises(ValueError):
        downloads.link("PARENT01")


def test_http_download_and_uniform_404(downloads, monkeypatch):
    monkeypatch.setenv("RMZOTERO_PDF_SECRET", "x" * 32)
    app = create_app(downloads.ops, "mcp-secret", legacy_hostnames=["pdf.example"])
    link = downloads.link("PARENT01")
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://pdf.example") as client:
            response = await client.get(link["url"])
            assert response.status_code == 200
            assert response.content == downloads.ops.original_pdf.return_value[2]
            assert response.headers["cache-control"] == "no-store"
            for path in ["bad", token(link) + "x"]:
                assert (await client.get("/pdf-download/" + path)).status_code == 404
            downloads.ops.original_pdf.side_effect = RuntimeError("private details")
            response = await client.get(link["url"])
            assert response.status_code == 404
            assert b"private" not in response.content
    asyncio.run(run())


def test_pdf_tool_and_route_share_content_free_telemetry(downloads, capsys, caplog, monkeypatch):
    from mcp import types
    from rmzotero.claude.server import build
    from rmzotero.config import AccessConfig

    monkeypatch.setenv('MCP_TELEMETRY', '1')
    monkeypatch.delenv('FLEET_OTLP_LOGS_URL', raising=False)

    async def run():
        mcp = build(downloads.ops, ['pdf.example'])
        request = types.CallToolRequest(method='tools/call', params=types.CallToolRequestParams(
            name='pdf_link', arguments={'key': 'PRIVATE_ITEM_KEY'}))
        result = await mcp._mcp_server.request_handlers[types.CallToolRequest](request)
        link = json.loads(result.root.content[0].text)
        access = AccessConfig('https://example.cloudflareaccess.com', 'test-audience', ['pdf.example'])
        app = create_app(downloads.ops, 'mcp-secret', access, legacy_enabled=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='https://pdf.example') as client:
            # Capability downloads work without an interactive Access assertion.
            response = await client.get(link['url'])
            assert response.status_code == 200
            assert response.content == downloads.ops.original_pdf.return_value[2]
            assert (await client.post('/mcp')).status_code == 401
            assert (await client.get('/pdf-download/PRIVATE_INVALID_TOKEN')).status_code == 404
            assert (await client.get(link['url'], headers={'Host': 'unknown.example'})).status_code == 404
        return link

    link = asyncio.run(run())
    output = capsys.readouterr().err
    assert link['url'] not in output + caplog.text
    assert token(link) not in output + caplog.text
    assert 'PRIVATE_INVALID_TOKEN' not in output + caplog.text
    assert 'PRIVATE_ITEM_KEY' not in output + caplog.text
    records = [json.loads(line.split(' ', 1)[1]) for line in output.splitlines() if line.startswith('mcp_call ')]
    assert [r['tool'] for r in records] == ['pdf_link', 'pdf_download', 'pdf_download']
    assert [r['outcome'] for r in records] == ['ok', 'ok', 'error']
    assert records[0]['resp_bytes'] > 0 and 'serialize' in records[0]['phase_ms']
    assert records[1]['resp_bytes'] == len(downloads.ops.original_pdf.return_value[2])
    assert records[2]['error_class'] == 'ValueError'
    assert all(set(r) == set(records[0]) for r in records)
