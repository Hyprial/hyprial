"""Synthetic OS probes; the real identity reader/comparator and session actor run."""

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.mcp.channel import ownership

REFERENCE = json.loads(Path(__file__).with_name('legacy_055_markers.json').read_text())


def reset_cache():
    reset = getattr(ownership, '_reset_darwin_identity_cache', None)
    if reset is not None:
        reset()


@pytest.fixture
def probes(monkeypatch):
    reset_cache()
    world = SimpleNamespace(
        markers=REFERENCE['markers'],
        births={row['pid']: tuple(row['start']) for row in REFERENCE['markers']},
        ps_text={row['pid']: row['psText'] for row in REFERENCE['markers']},
        calls=[], fail=False, delay=0.0, on_ps=None,
    )

    class PsResult:
        returncode = 0

        def __init__(self, argv, **kwargs):
            assert argv[:3] == ['ps', '-o', 'lstart=']
            self.pid = int(argv[-1])
            world.calls.append(self.pid)
            if world.fail:
                raise OSError('injected unavailable ps')

        def communicate(self, timeout):
            if world.delay:
                time.sleep(world.delay)
            if world.on_ps is not None:
                world.on_ps(self.pid)
            return world.ps_text.get(self.pid, '') + '\n', ''

    original_status = ownership._owner_process_status

    def status(pid, expected_identity, **kwargs):
        # Synthetic high PIDs are never sent to the host's signal/proc APIs.
        kwargs.setdefault('signal_process', lambda _pid, _signal: None)
        kwargs.setdefault('read_identity', ownership._read_process_identity)
        return original_status(pid, expected_identity, **kwargs)

    monkeypatch.setattr(ownership.sys, 'platform', 'darwin')
    monkeypatch.setattr(ownership, '_read_darwin_process_start', world.births.get)
    monkeypatch.setattr(ownership, '_PROCESS_POPEN', PsResult)
    monkeypatch.setattr(ownership, '_owner_process_status', status)
    world.status = status
    yield world
    reset_cache()
