"""Content-free MCP metrics. Keep this schema identical across services."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import json
import os
import sys
import time
from typing import Any, Callable, Iterator, TypeVar
from mcp.server.fastmcp import FastMCP

F = TypeVar("F", bound=Callable[..., Any])

from mcp import types

PROCESS_START = time.perf_counter()
try:
    # Linux process birth, including imports before this module was loaded.
    PROCESS_START = time.perf_counter() - (time.clock_gettime(time.CLOCK_BOOTTIME) -
        int(open("/proc/self/stat").read().rsplit(")", 1)[1].split()[19]) / os.sysconf("SC_CLK_TCK"))
except (OSError, AttributeError, ValueError):
    pass

PHASES = frozenset({"render", "cups", "status", "db_open", "query", "serialize",
                    "lock_wait", "mirror_read", "upstream", "fetch", "store", "publish"})
ERRORS = frozenset({"ValueError", "TypeError", "OSError", "TimeoutError", "PermissionError",
                    "Busy", "NotFound", "ToolError", "CancelledError"})
_current: ContextVar[dict[str, Any] | None] = ContextVar("mcp_metric", default=None)
_active: ContextVar[frozenset[str]] = ContextVar("mcp_phases", default=frozenset())


def enabled() -> bool:
    return os.environ.get("MCP_TELEMETRY", "1").lower() not in {"0", "false", "off"}


def emit(record: dict[str, Any]) -> None:
    try:
        sys.stderr.write(record["event"] + " " + json.dumps(record, separators=(",", ":")) + "\n")
    except (OSError, ValueError):
        pass  # Metrics must never change tool behavior.


def classify(exc: BaseException) -> tuple[str, str]:
    name = type(exc).__name__
    code = name if name in ERRORS else "exception"
    if isinstance(exc, TimeoutError) or name == "Busy":
        return "timeout", code
    if isinstance(exc, PermissionError):
        return "refused", code
    return "error", code


@contextmanager
def phase(name: str) -> Iterator[None]:
    if name not in PHASES:
        raise ValueError("Unknown telemetry phase")
    record = _current.get()
    if record is None or name in _active.get():
        yield
        return
    token = _active.set(_active.get() | {name})
    start = time.perf_counter()
    try:
        yield
    finally:
        _active.reset(token)
        phases = record["phase_ms"]
        phases[name] = phases.get(name, 0.0) + (time.perf_counter() - start) * 1000


def timed(name: str) -> Callable[[F], F]:
    def decorate(fn: F) -> F:
        @wraps(fn)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            with phase(name):
                return fn(*args, **kwargs)
        return wrapped  # type: ignore[return-value]
    return decorate


def failure(exc: BaseException) -> None:
    record = _current.get()
    if record is not None:
        record["outcome"], record["error_class"] = classify(exc)


def install(mcp: FastMCP, service: str, version: str) -> None:
    """Record normalized MCP results, including SDK validation/error results.

    Sizes are UTF-8 JSON MCP params/result bytes, excluding the JSON-RPC envelope.
    Serialize the normalized result before stopping the monotonic clock.
    """
    handler = mcp._mcp_server.request_handlers[types.CallToolRequest]

    async def measured(request: types.CallToolRequest) -> types.ServerResult:
        if not enabled():
            return await handler(request)
        start = time.perf_counter()
        names = {t.name for t in mcp._tool_manager.list_tools()}
        record = dict(event="mcp_call", v=1, ts=datetime.now(timezone.utc).isoformat(),
                      service=service, tool=request.params.name if request.params.name in names else "unknown",
                      outcome="ok", error_class=None, total_ms=0.0, phase_ms={},
                      req_bytes=0, resp_bytes=0)
        token = _current.set(record)
        try:
            record["req_bytes"] = len(request.params.model_dump_json(by_alias=True, exclude_none=True).encode())
            result = await handler(request)
            with phase("serialize"):
                record["resp_bytes"] = len(result.model_dump_json(by_alias=True, exclude_none=True).encode())
            if getattr(result.root, "isError", False) and record["outcome"] == "ok":
                record["outcome"] = "tool_error"
            return result
        except BaseException as exc:
            failure(exc)
            raise
        finally:
            record["total_ms"] = (time.perf_counter() - start) * 1000
            _current.reset(token)
            emit(record)

    mcp._mcp_server.request_handlers[types.CallToolRequest] = measured
    original = mcp._mcp_server.lifespan

    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def lifespan(server: Any) -> Any:
        async with original(server) as state:
            if enabled():
                emit(dict(event="mcp_start", v=1, service=service, version=version,
                          startup_ms=(time.perf_counter() - PROCESS_START) * 1000))
            yield state
    mcp._mcp_server.lifespan = lifespan


def count(name: str, amount: int = 1) -> None:
    record = _current.get()
    if record is not None and name in {"pages", "retries", "items_count"}:
        record[name] += amount


@contextmanager
def sync_run() -> Iterator[None]:
    if not enabled():
        yield
        return
    start = time.perf_counter()
    record = dict(event="mcp_sync", v=1, ts=datetime.now(timezone.utc).isoformat(),
                  outcome="ok", error_class=None, total_ms=0.0, phase_ms={},
                  pages=0, retries=0, items_count=0)
    token = _current.set(record)
    try:
        yield
    except BaseException as exc:
        failure(exc)
        raise
    finally:
        record["total_ms"] = (time.perf_counter() - start) * 1000
        _current.reset(token)
        emit(record)
