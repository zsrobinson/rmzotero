import json
from types import SimpleNamespace

import pytest

from rmzotero import telemetry
from rmzotero.claude.ops import Busy, LibraryOps


def test_lock_timeout_has_safe_phase(capsys):
    class Lock:
        def acquire(self,timeout):
            assert timeout==120
            return False
    ops=LibraryOps(SimpleNamespace(lock=Lock()))
    with telemetry.sync_run():
        with pytest.raises(Busy):
            with ops._writing():
                pytest.fail('lock was not acquired')
    record=json.loads(capsys.readouterr().err.split(' ',1)[1])
    assert record['phase_ms']['lock_wait']>=0


def test_nested_phases_aggregate(capsys):
    with telemetry.sync_run():
        with telemetry.phase('upstream'):
            with telemetry.phase('upstream'):
                pass
        with telemetry.phase('upstream'):
            pass
    record=json.loads(capsys.readouterr().err.split(' ',1)[1])
    assert 0<=record['phase_ms']['upstream']<=record['total_ms']


def test_diagnostic_content_never_enters_call_record(capsys):
    import asyncio
    from mcp import types
    from rmzotero.claude.server import build
    ops=SimpleNamespace(diagnostics=lambda: {'error':'PRIVATE_SENTINEL'})
    mcp=build(ops,[])
    async def call():
        request=types.CallToolRequest(method='tools/call',params=types.CallToolRequestParams(
            name='service_diagnostics',arguments={}))
        result=await mcp._mcp_server.request_handlers[types.CallToolRequest](request)
        assert 'PRIVATE_SENTINEL' in result.model_dump_json()
    asyncio.run(call())
    lines=[line for line in capsys.readouterr().err.splitlines() if line.startswith('mcp_call ')]
    assert len(lines)==1 and 'PRIVATE_SENTINEL' not in lines[0]
    record=json.loads(lines[0].split(' ',1)[1])
    assert record['tool']=='service_diagnostics' and record['outcome']=='ok'
    assert record['resp_bytes']>0 and record['phase_ms']['serialize']>=0
