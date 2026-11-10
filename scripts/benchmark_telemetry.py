"""Local normalized-dispatch overhead; no network or user data.

Run from the repository with its Python environment. Emits timings only.
"""
import asyncio
import contextlib
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rmzotero import telemetry
from mcp import types
from mcp.server.fastmcp import FastMCP

async def main():
    mcp = FastMCP("benchmark")
    @mcp.tool()
    async def noop() -> str:
        return ""
    async def handler(request):
        return types.ServerResult(types.CallToolResult(content=[]))
    mcp._mcp_server.request_handlers[types.CallToolRequest] = handler
    telemetry.install(mcp, "rmzotero", "1")
    measured = mcp._mcp_server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(name="noop"))
    samples = {"0": [], "1": []}
    with open(os.devnull,"w") as sink, contextlib.redirect_stderr(sink):
        for _ in range(7):
            for mode in samples:
                os.environ["MCP_TELEMETRY"] = mode
                start = time.perf_counter()
                for _ in range(5000):
                    await measured(req)
                samples[mode].append((time.perf_counter()-start)*1e6/5000)
    baseline=statistics.median(samples["0"])
    active=statistics.median(samples["1"])
    print(f"disabled_us={baseline:.3f} enabled_us={active:.3f} overhead_us={active-baseline:.3f}")

asyncio.run(main())
