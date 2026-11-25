"""The real sessions cleanup path keeps legacy persisted fences and bounds ps."""

import pytest

from tests.daemon.org_membership.context.support import org_daemon as _org_daemon  # noqa: F401

from .support import probes as probes, reset_cache


def register(app, row, index, tmp_path):
    """Register one carrier and return the official address it is stored at."""

    actor = f'agent:allen:node-1:identity-cache-{index}'
    cwd = tmp_path / f'cwd-{index}'
    cwd.mkdir()
    result = app.handle('session.register', {
        'actor': actor, 'sessionRef': f'cache-session-{index}', 'cwd': str(cwd),
        'command': ['codex'], 'source': 'codex-app-server',
        'processPid': row['pid'], 'processIdentity': row['processIdentity'],
    })
    assert result['registered'] is True
    address = app.agents.get(f'identity-cache-{index}').uri
    assert result['actor'] == address != actor
    return address


@pytest.mark.parametrize('count', [1, 8, 32])
def test_cleanup_reuses_ps_identity_across_ticks(org_daemon, probes, count, tmp_path):
    app, _ = org_daemon
    expected = {}
    for index, row in enumerate(probes.markers[:count]):
        expected[register(app, row, index, tmp_path)] = row['processIdentity']
    reset_cache()
    probes.calls.clear()
    per_tick = []
    for _ in range(4):
        before = len(probes.calls)
        alive = app._clean_dead_interactive_sessions()
        assert {session.actor: session.process_identity for session in alive} == expected
        per_tick.append(len(probes.calls) - before)
    persisted = app.desired_state.load().interactive_sessions
    assert {row.actor: row.process_identity for row in persisted} == expected
    assert per_tick == [count, 0, 0, 0]


def test_same_second_pid_reuse_retires_only_the_old_session(org_daemon, probes, tmp_path):
    app, _ = org_daemon
    old, live = probes.markers[:2]
    old_actor = register(app, old, 0, tmp_path)
    live_actor = register(app, live, 1, tmp_path)
    assert len(app._clean_dead_interactive_sessions()) == 2
    seconds, micros = probes.births[old['pid']]
    probes.births[old['pid']] = seconds, micros + 1
    survivors = app._clean_dead_interactive_sessions()
    assert [session.actor for session in survivors] == [live_actor]
    assert app._agent_recovery_cleanups[old_actor]['liveness'] == 'identity-mismatch'
    assert [row.actor for row in app.desired_state.load().interactive_sessions] == [live_actor]
