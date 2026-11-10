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
