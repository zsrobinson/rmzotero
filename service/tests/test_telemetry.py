import asyncio
import json

import anyio
import pytest
from mcp import types
from mcp.server.fastmcp import FastMCP
from rmzotero import telemetry


def invoke(outcome="ok", error=None):
    async def run():
        mcp = FastMCP("test")
        @mcp.tool()
        async def sample(value: str) -> str:
            return value
        async def handler(request):
            def work():
                with telemetry.phase("render"):
                    if error is not None:
                        telemetry.failure(error)
            await anyio.to_thread.run_sync(work)
            return types.ServerResult(types.CallToolResult(
                content=[types.TextContent(type="text", text="SECRET")], isError=outcome != "ok"))
        mcp._mcp_server.request_handlers[types.CallToolRequest] = handler
        telemetry.install(mcp, "test", "1")
        request = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(
            name="sample", arguments={"value": "SECRET"}))
        return await mcp._mcp_server.request_handlers[types.CallToolRequest](request)
    return asyncio.run(run())


@pytest.mark.parametrize("error,outcome,code", [
    (None,"ok",None), (ValueError("SECRET"),"error","ValueError"),
    (TimeoutError("SECRET"),"timeout","TimeoutError"),
    (PermissionError("SECRET"),"refused","PermissionError"),
    (RuntimeError("SECRET"),"error","exception"),
])
def test_private_record(capsys, monkeypatch, error, outcome, code):
    monkeypatch.setenv("MCP_TELEMETRY", "1")
    invoke(error=error)
    line = capsys.readouterr().err
    assert "SECRET" not in line
    prefix, body = line.strip().split(" ", 1)
    record = json.loads(body)
    assert prefix == "mcp_call"
    assert set(record) == {"event","v","ts","service","tool","outcome","error_class",
                           "total_ms","phase_ms","req_bytes","resp_bytes"}
    assert record["outcome"] == outcome and record["error_class"] == code
    assert record["tool"] == "sample"
    assert record["req_bytes"] > 0 and record["resp_bytes"] > 0
    assert record["total_ms"] >= record["phase_ms"]["serialize"] >= 0
    assert record["phase_ms"]["render"] >= 0


def test_returned_error(capsys):
    invoke("tool_error")
    assert json.loads(capsys.readouterr().err.split(" ",1)[1])["outcome"] == "tool_error"


def test_disabled(monkeypatch,capsys):
    monkeypatch.setenv("MCP_TELEMETRY","off")
    invoke()
    assert not capsys.readouterr().err


def test_phase_allowlist():
    with pytest.raises(ValueError):
        with telemetry.phase("SECRET"):
            pass


def test_boot(capsys):
    from starlette.testclient import TestClient
    from rmzotero.claude.server import create_app

    app = create_app(None, "test-token")
    assert "mcp_start " not in capsys.readouterr().err
    with TestClient(app, base_url="http://localhost") as client:
        lines = capsys.readouterr().err.splitlines()
        starts = [line for line in lines if line.startswith("mcp_start ")]
        assert len(starts) == 1  # Before the first authenticated request.
        record = json.loads(starts[0].split(" ", 1)[1])
        assert set(record) == {"event", "v", "service", "version", "startup_ms"}
        assert record == dict(event="mcp_start", v=1, service="rmzotero", version="0.4.0",
                              startup_ms=record["startup_ms"])
        assert record["startup_ms"] > 0
        for request_id in range(3):
            response = client.post("/mcp/test-token", headers={
                "Accept": "application/json, text/event-stream"}, json={
                "jsonrpc": "2.0", "id": request_id, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "test", "version": "1"}}})
            assert response.status_code == 200
        assert "mcp_start " not in capsys.readouterr().err
    assert "mcp_start " not in capsys.readouterr().err


def test_boot_disabled(monkeypatch, capsys):
    from starlette.testclient import TestClient
    from rmzotero.claude.server import create_app

    monkeypatch.setenv("MCP_TELEMETRY", "0")
    with TestClient(create_app(None, "test-token")):
        pass
    assert "mcp_start " not in capsys.readouterr().err


def test_emitter_allowlists(capsys):
    telemetry.emit(dict(event="mcp_call", v=1, arguments="PRIVATE_SENTINEL",
                        outcome="PRIVATE_SENTINEL", error_class="PRIVATE_SENTINEL",
                        phase_ms={"query":1.0,"PRIVATE_SENTINEL":2.0}))
    line=capsys.readouterr().err
    assert "PRIVATE_SENTINEL" not in line
    record=json.loads(line.split(" ",1)[1])
    assert record["phase_ms"]=={"query":1.0}
    assert record["error_class"]=="exception" and record["outcome"]=="error"


def test_unknown_tool_never_reaches_sdk(capsys,caplog):
    async def run():
        mcp=FastMCP("test")
        async def forbidden(request):
            pytest.fail("unknown name reached SDK handler")
        mcp._mcp_server.request_handlers[types.CallToolRequest]=forbidden
        telemetry.install(mcp,"test","1")
        request=types.CallToolRequest(method="tools/call",params=types.CallToolRequestParams(
            name="PRIVATE_SENTINEL",arguments={"value":"PRIVATE_SENTINEL"}))
        result=await mcp._mcp_server.request_handlers[types.CallToolRequest](request)
        assert result.root.isError
    asyncio.run(run())
    line=capsys.readouterr().err
    assert "PRIVATE_SENTINEL" not in line+caplog.text
    record=json.loads(line.split(" ",1)[1])
    assert record["tool"]=="unknown" and record["outcome"]=="tool_error"


def test_network_timeout_classification():
    import httpx
    assert telemetry.classify(httpx.ReadTimeout("PRIVATE_SENTINEL"))==("timeout","exception")


def test_output_conversion_exception_is_not_returned_tool_error(capsys):
    async def run():
        mcp=FastMCP("test")
        @mcp.tool()
        async def broken() -> int:
            return "PRIVATE_SENTINEL"
        telemetry.install(mcp,"test","1")
        request=types.CallToolRequest(method="tools/call",params=types.CallToolRequestParams(
            name="broken",arguments={}))
        result=await mcp._mcp_server.request_handlers[types.CallToolRequest](request)
        assert result.root.isError
    asyncio.run(run())
    line=capsys.readouterr().err
    assert "PRIVATE_SENTINEL" not in line
    record=json.loads(line.split(" ",1)[1])
    assert record["outcome"]=="error" and record["error_class"]=="exception"
