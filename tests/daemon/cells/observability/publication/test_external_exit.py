"""Independent C review: positive external process exit must outrank idle."""
import json
import subprocess
import sys
import time

import pytest

from tests.daemon.cells.observability.mount.test_mount import composed as composed
from hyprial.daemon.impl.application.actors.sessions.registry import process_birth_identity
from hyprial.daemon.impl.operations.session_ports import RegisterSessionCommand, SessionMutationCompleted
from hyprial.kernel import CHANNEL_PROTOCOL_VERSION


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / 'review-home'
    home.mkdir(mode=0o700)
    (home / 'settings.json').write_text(json.dumps({'version': 1, 'owner': 'review'}))
    monkeypatch.setenv('HYPRIAL_HOME', str(home))
    monkeypatch.setenv('HARNESS_STATE_DIR', str(home / 'state'))
    monkeypatch.setenv('HYPRIAL_NETWORK_ISOLATED', '1')
    monkeypatch.setenv('HYPRIAL_OWNER', 'review')


@pytest.mark.parametrize("ended", [False, True])
def test_verified_external_exit_is_not_idle(composed, monkeypatch, ended):
    app, *_ = composed
    app.agents.create('terminal')
    actor = 'agent:owner:node:terminal'
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        identity = process_birth_identity(child.pid)
        assert identity is not None
        domains = app._agent_session_domains
        domains.call_session(RegisterSessionCommand(
            'review-exit', actor, '/tmp', ('claude',), 'claude-channel', 'review-exit-session',
            channel_confirmed=True, channel_protocol_version=CHANNEL_PROTOCOL_VERSION,
            channel_lease_token='review-exit-lease-01234567890123456789',
            process_pid=child.pid, process_identity=identity,
        ), SessionMutationCompleted)
        app._record_carrier_turn_started(actor, 'review-exit-session', 'run')
        if ended:
            app._record_carrier_turn_ended(actor, 'review-exit-session', 'run', int(time.time() * 1000))
        # Coverage is negotiated by the producer (E1), not inferred from its name.
        app._record_carrier_turn_refresh(
            actor,
            'review-exit-session',
            producer_epoch='review-exit-boot',
            seq=0,
            runs=[] if ended else [('run', 'executing', None)],
            capabilities={
                'wholeSessionTurns': True,
                'preparation': True,
                'permissionWait': False,
                'producerActivity': False,
            },
            at_ms=int(time.time() * 1000),
        )
        before = app._cell_run_state_rows()[actor]
        assert before['state'] == ('idle' if ended else 'in-turn')
        assert not before['coverage']['processExit']  # No sample yet, not a PID-presence claim.
        child.terminate()
        child.wait(timeout=10)
        assert app._interactive_process_alive(actor) is False
        row = app._cell_run_state_rows()[actor]
        print('positive exit, current run state:', row)
        assert row['state'] == 'not-running', row
        assert row['coverage']['processExit']
        app._record_carrier_turn_started(actor, 'review-exit-session', 'late')
        from hyprial.daemon.impl.mcp.channel import ownership
        def forbidden(*args):
            raise AssertionError('status must not run a native process probe')
        monkeypatch.setattr(ownership, '_owner_process_status', forbidden)
        for _ in range(3):
            assert app._cell_run_state_rows()[actor]['state'] == 'not-running'
        domains.liveness.release(actor)
        domains.liveness.bind(actor, harness='claude', runtime='interactive', session_id='review-exit-session')
        fresh = app._cell_run_state_rows()[actor]
        assert fresh['state'] == 'unknown' and not fresh['coverage']['processExit']
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
