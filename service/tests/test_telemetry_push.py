"""The optional OTLP push to the fleet collector: off by default, allowlisted, never in the way."""
import http.server
import json
import queue
import threading
import time

import pytest
from rmzotero import telemetry

from test_telemetry import invoke

ALLOWED = {"event", "v", "ts", "service", "tool", "outcome", "error_class", "total_ms",
           "phase_ms", "req_bytes", "resp_bytes", "version", "startup_ms"}


def decode(value):
    kind, inner = next(iter(value.items()))
    if kind == "kvlistValue":
        return {kv["key"]: decode(kv["value"]) for kv in inner.get("values", [])}
    return int(inner) if kind == "intValue" else inner


@pytest.fixture
def collector(monkeypatch):
    """A local OTLP/HTTP endpoint; yields a queue of decoded log bodies."""
    received = queue.Queue()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.put((self.path, self.headers["Content-Type"], payload))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv(telemetry.PUSH_ENV, f"http://127.0.0.1:{server.server_port}/v1/logs")
    yield received
    server.shutdown()
    server.server_close()


def bodies(received, count):
    out = []
    while len(out) < count:
        path, content_type, payload = received.get(timeout=5)
        assert path == "/v1/logs" and content_type == "application/json"
        for resource in payload["resourceLogs"]:
            for scope in resource["scopeLogs"]:
                for record in scope["logRecords"]:
                    assert int(record["timeUnixNano"]) > 0
                    out.append(decode(record["body"]))
    return out


def test_push_disabled_by_default(monkeypatch, capsys):
    monkeypatch.delenv(telemetry.PUSH_ENV, raising=False)
    sent = []
    monkeypatch.setattr(telemetry, "_post", lambda url, data: sent.append(data))
    monkeypatch.setattr(telemetry, "_start_pusher", lambda: sent.append("started"))
    invoke()
    telemetry.emit(dict(event="mcp_start", v=1, service="rmzotero", version="1", startup_ms=1.0))
    assert capsys.readouterr().err  # stderr still gets the records
    assert sent == [] and telemetry._queue.empty()


@pytest.mark.parametrize("url", ["", "  ", "file:///etc/passwd", "ftp://example.invalid/x"])
def test_push_needs_an_http_url(monkeypatch, url):
    monkeypatch.setenv(telemetry.PUSH_ENV, url)
    monkeypatch.setattr(telemetry, "_start_pusher", lambda: pytest.fail("pusher started"))
    telemetry.emit(dict(event="mcp_call", v=1, tool="sample", outcome="ok"))
    assert telemetry._queue.empty()


def test_tool_call_pushes_allowlisted_record_without_arguments(collector, capsys):
    invoke(error=ValueError("SECRET"))
    [body] = bodies(collector, 1)
    assert "SECRET" not in json.dumps(body) + capsys.readouterr().err
    assert set(body) <= ALLOWED
    assert body["event"] == "mcp_call" and body["service"] == "test" and body["tool"] == "sample"
    assert body["outcome"] == "error" and body["error_class"] == "ValueError"
    assert body["req_bytes"] > 0 and body["resp_bytes"] > 0 and body["total_ms"] >= 0
    assert set(body["phase_ms"]) == {"render", "serialize"}


def test_push_uses_the_emit_allowlist(collector, capsys):
    telemetry.emit(dict(event="mcp_call", v=1, service="rmzotero", tool="get_item", outcome="ok",
                        error_class=None, arguments="PRIVATE_SENTINEL", title="PRIVATE_SENTINEL",
                        phase_ms={"upstream": 1.5, "PRIVATE_SENTINEL": 2.0}))
    telemetry.emit(dict(event="mcp_sync", v=1, outcome="ok", items_count=3))  # stderr only
    telemetry.emit(dict(event="mcp_start", v=1, service="rmzotero", version="0.4.0",
                        startup_ms=5.0, query="PRIVATE_SENTINEL"))
    call, start = bodies(collector, 2)
    assert "PRIVATE_SENTINEL" not in json.dumps([call, start])
    assert call == dict(event="mcp_call", v=1, service="rmzotero", tool="get_item", outcome="ok",
                        phase_ms={"upstream": 1.5})  # error_class=None is omitted
    assert start == dict(event="mcp_start", v=1, service="rmzotero", version="0.4.0", startup_ms=5.0)
    time.sleep(0.2)
    assert collector.empty()  # mcp_sync was not pushed


def test_push_failures_are_swallowed(monkeypatch):
    calls = []

    def broken(url, data):
        calls.append(url)
        raise OSError("collector away")

    monkeypatch.setenv(telemetry.PUSH_ENV, "http://127.0.0.1:9/v1/logs")
    monkeypatch.setattr(telemetry, "_post", broken)
    for _ in range(3):
        result = invoke()
        assert not result.root.isError
    deadline = time.monotonic() + 5
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls  # tried, failed, dropped
    assert telemetry._pusher.is_alive()  # and the pusher keeps going


def test_unreachable_collector_does_not_slow_calls(monkeypatch):
    # Nothing listens on port 9 here; the real _post fails in the background thread.
    monkeypatch.setenv(telemetry.PUSH_ENV, "http://127.0.0.1:9/v1/logs")
    start = time.perf_counter()
    for _ in range(20):
        telemetry.emit(dict(event="mcp_call", v=1, tool="x", outcome="ok"))
    assert time.perf_counter() - start < 0.5


def test_full_queue_drops_without_blocking(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(telemetry, "_post", lambda url, data: release.wait(10))
    monkeypatch.setenv(telemetry.PUSH_ENV, "http://127.0.0.1:9/v1/logs")
    try:
        start = time.perf_counter()
        for _ in range(telemetry.PUSH_QUEUE * 3):
            telemetry.emit(dict(event="mcp_call", v=1, tool="x", outcome="ok"))
        assert time.perf_counter() - start < 1.0
        assert telemetry._queue.qsize() <= telemetry.PUSH_QUEUE
    finally:
        release.set()
        deadline = time.monotonic() + 5
        while not telemetry._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.01)
