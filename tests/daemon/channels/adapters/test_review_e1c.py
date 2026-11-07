"""Independent E1b regression probes; no production runtime access."""
import time

import pytest

from hyprial.daemon.impl.inbox_watch import Heartbeat, Sleep
from hyprial.daemon.impl.mcp.api import DaemonRequestRejected
from tests.daemon.channels.adapters.test_channel_carrier_contract import _fixture
from tests.daemon.harnesses.carrier.turns.test_turn_refresh import (
    _app, _register, _refresh, _facts, _open_contact_gap, _published,
)


def test_rejected_old_sequence_cannot_acknowledge_new_contact_gap(tmp_path):
    app = _app(tmp_path, {"now": 5000})
    try:
        _register(app)
        _refresh(app, seq=5, open_runs=[{"reportId": "lost-end", "phase": "executing"}])
        _open_contact_gap(app)
        assert not _facts(app).known
        answer = _refresh(app, seq=4)
        assert answer["applied"] is False
        assert answer["observationGapCleared"] is False, (answer, _published(app))
        assert not _facts(app).known
    finally:
        app.close()


@pytest.mark.anyio
async def test_unsupported_cache_expires_on_generation_refresh_without_register(tmp_path):
    app, factory, adapter, turns, clock = _fixture(tmp_path)
    state = {"old": True}
    original = adapter._call

    async def rolling_upgrade(method, params, *, mutation):
        if method == "session.turn.refresh" and state["old"]:
            raise DaemonRequestRejected("METHOD_NOT_FOUND", "old daemon")
        answer = await original(method, params, mutation=mutation)
        return {**answer, "daemonEpoch": "old" if state["old"] else "new"}

    adapter._call = rolling_upgrade
    try:
        await adapter.start()
        assert adapter.watch.daemon_epoch == "old"
        assert not factory.of("session.turn.refresh")
        state["old"] = False
        adapter.watch.use_wait = False
        now = int(time.monotonic() * 1000)
        for now in range(now, now + 60000, 500):
            action = adapter.watch.poll_next(now)
            if not isinstance(action, Sleep):
                outcome = await adapter.perform(action)
                adapter.watch.poll_done(action, outcome, now)
        assert adapter.watch.daemon_epoch == "new"
        assert factory.of("session.refresh")
        assert len(factory.of("session.register")) == 1
        assert factory.of("session.turn.refresh"), "cache survived a confirmed new generation"
    finally:
        app.close()


@pytest.mark.anyio
async def test_same_generation_heartbeat_recovery_schedules_current_state(tmp_path):
    app, factory, adapter, turns, clock = _fixture(tmp_path)
    try:
        await adapter.start()
        before = len(factory.of("session.turn.refresh"))
        _open_contact_gap(app)
        factory.fail_once["message.pending.list"] = 2
        await adapter.perform(Heartbeat())
        adapter.watch.use_wait = False
        now = int(time.monotonic() * 1000)
        for now in range(now, now + 60000, 500):
            action = adapter.watch.poll_next(now)
            if not isinstance(action, Sleep):
                outcome = await adapter.perform(action)
                adapter.watch.poll_done(action, outcome, now)
        assert len(factory.of("session.turn.refresh")) > before
        assert next(iter(app.app._cell_interactive_facts().values())).known
    finally:
        app.close()


def test_pi_unsupported_cache_expires_on_new_daemon_epoch(tmp_path):
    from tests.daemon.harnesses.pi.extension._pi_attach_support import SDK_AVAILABLE, carrier_for
    from tests.daemon.harnesses.pi.extension.test_pi_turn_refresh_extension import _wait_until

    assert SDK_AVAILABLE, "Pi protocol rig unavailable; report BLOCKED, not PASS"
    with carrier_for(tmp_path) as carrier:
        carrier.daemon.fail_next("session.turn.refresh", "METHOD_NOT_FOUND")
        carrier.command({"cmd": "start"})
        carrier.wait_for_frames("session.turn.refresh", 1)
        carrier.daemon.epoch = "epoch-2"
        carrier.wait_for_frames("session.refresh", 1)
        polls = len(carrier.daemon.frames_for("message.pending.list"))
        _wait_until(lambda: len(carrier.daemon.frames_for("message.pending.list")) >= polls + 8)
        assert len(carrier.daemon.frames_for("session.register")) == 1
        assert len(carrier.daemon.frames_for("session.turn.refresh")) >= 2
