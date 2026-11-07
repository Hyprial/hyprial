"""E1: the Claude channel publishes its current run set on register and recovery.

The refresh is an observation call.  It rides the existing registration and
daemon-generation refresh edges, so recovery needs no inbox traffic, and a
daemon that does not know the method simply keeps its previous coverage.
"""

from __future__ import annotations

import time

import pytest

from hyprial.daemon.impl.inbox_watch import Ok, Refresh
from hyprial.daemon.impl.mcp.api import DaemonRequestRejected
from hyprial.daemon.impl.mcp.channel.loops import _forward_turn_signals

from tests.daemon.channels.adapters.test_channel_carrier_contract import (
    _fixture,
    _prompt,
    _stop,
)

TURN_REFRESH = "session.turn.refresh"


@pytest.mark.anyio
async def test_registration_publishes_an_empty_current_run_set(tmp_path) -> None:
    app, factory, adapter, _turns, _clock = _fixture(tmp_path)
    try:
        await adapter.start()

        frames = factory.of(TURN_REFRESH)
        assert len(frames) == 1
        params = frames[0]
        assert params["schemaVersion"] == 1
        assert params["daemonEpoch"] == app.app.epoch
        assert params["openRuns"] == []
        assert params["seq"] == 0
        assert isinstance(params["producerEpoch"], str) and params["producerEpoch"]
        assert params["capabilities"] == {
            "wholeSessionTurns": True,
            "preparation": True,
            "permissionWait": False,
            "producerActivity": False,
        }
        assert params["channelLeaseToken"]
        facts = app.app._cell_interactive_facts()
        assert next(iter(facts.values())).known is True
        assert next(iter(facts.values())).open_count == 0
        # Observation only: no batch read and no business turn event.
        assert factory.of("message.pending.batch") == []
        assert app.recorder.events == []
    finally:
        app.close()


@pytest.mark.anyio
async def test_reconnect_refreshes_the_open_turn_without_inbox_traffic(
    tmp_path,
) -> None:
    app, factory, adapter, turns, _clock = _fixture(tmp_path)
    try:
        await adapter.start()
        _prompt(turns)
        await _forward_turn_signals(adapter, turns)
        assert adapter.open_turn is not None

        outcome = await adapter.perform(Refresh())
        assert isinstance(outcome, Ok) and outcome.response["ok"] is True

        frames = factory.of(TURN_REFRESH)
        assert len(frames) == 2
        assert len(frames[-1]["openRuns"]) == 1
        row = frames[-1]["openRuns"][0]
        assert row["reportId"] == adapter.open_turn
        assert row["phase"] == "executing"
        assert row["elapsedMs"] >= 0
        assert frames[-1]["seq"] == frames[0]["seq"] + 1
        assert frames[-1]["producerEpoch"] == frames[0]["producerEpoch"]
        assert frames[-1]["daemonEpoch"] == app.app.epoch

        facts = app.app._cell_interactive_facts()
        assert next(iter(facts.values())).open_count == 1
        assert factory.of("message.pending.batch") == []
    finally:
        app.close()


@pytest.mark.anyio
async def test_reconnect_after_the_turn_ended_publishes_idle(tmp_path) -> None:
    app, factory, adapter, turns, clock = _fixture(tmp_path)
    try:
        await adapter.start()
        _prompt(turns)
        await _forward_turn_signals(adapter, turns)
        clock["now"] += 1_000
        _stop(turns)
        await _forward_turn_signals(adapter, turns)
        assert adapter.open_turn is None

        await adapter.perform(Refresh())
        assert factory.of(TURN_REFRESH)[-1]["openRuns"] == []
        facts = app.app._cell_interactive_facts()
        assert next(iter(facts.values())).open_count == 0
    finally:
        app.close()


@pytest.mark.anyio
async def test_an_unknown_method_never_breaks_registration_or_turns(
    tmp_path, monkeypatch
) -> None:
    app, factory, adapter, turns, clock = _fixture(tmp_path)
    try:
        original = adapter._call

        async def reject_refresh(method, params, *, mutation):
            if method == TURN_REFRESH:
                raise DaemonRequestRejected("METHOD_NOT_FOUND", "older daemon")
            return await original(method, params, mutation=mutation)

        monkeypatch.setattr(adapter, "_call", reject_refresh)
        await adapter.start()
        assert adapter.started_once is True

        _prompt(turns)
        await _forward_turn_signals(adapter, turns)
        clock["now"] += 1_000
        _stop(turns)
        await _forward_turn_signals(adapter, turns)
        assert app.recorder.wait_for(
            lambda: len(app.recorder.of("turn.completed")) == 1
        )
        started = factory.of("session.turn.started")
        assert len(started) == 1
    finally:
        app.close()


@pytest.mark.anyio
async def test_a_transport_loss_on_refresh_is_not_a_registration_failure(
    tmp_path, monkeypatch
) -> None:
    from hyprial.daemon.impl.mcp.api import DaemonDisconnected

    app, factory, adapter, turns, _clock = _fixture(tmp_path)
    try:
        original = adapter._call

        async def lose_refresh(method, params, *, mutation):
            if method == TURN_REFRESH:
                raise DaemonDisconnected("channel closed")
            return await original(method, params, mutation=mutation)

        monkeypatch.setattr(adapter, "_call", lose_refresh)
        await adapter.start()
        assert adapter.started_once is True
        _prompt(turns)
        await _forward_turn_signals(adapter, turns)
        assert len(factory.of("session.turn.started")) == 1
    finally:
        app.close()


@pytest.mark.anyio
async def test_refresh_does_not_consume_the_pending_message(tmp_path) -> None:
    app, factory, adapter, _turns, _clock = _fixture(tmp_path)
    try:
        from tests.daemon.channels.adapters.test_channel_carrier_contract import (
            _pending_ids,
            _seed,
        )

        _seed(app, "r1", "request")
        await adapter.start()
        before = _pending_ids(app)
        await adapter.perform(Refresh())
        assert _pending_ids(app) == before == ["r1"]
        assert factory.of("message.settle_hint") == []
        assert factory.of("message.reply") == []
        assert factory.of("message.ack") == []
    finally:
        app.close()


@pytest.mark.anyio
async def test_reconnect_refresh_repairs_a_recovered_contact_gap(tmp_path) -> None:
    """A real owner-recorded lease gap is repaired by the current run set."""

    from hyprial.kernel import CHANNEL_LIVENESS_TTL_SECONDS
    from tests.daemon.harnesses.carrier.turns.test_turn_reports import A_URI

    app, _factory, adapter, _turns, _clock = _fixture(tmp_path)
    try:
        await adapter.start()
        session = app.app._agent_session_domains.session
        runtime = session._runtime_projection
        now_ms = session._clock_ms()
        runtime.register(
            A_URI,
            adapter.session_ref,
            now_ms - int(CHANNEL_LIVENESS_TTL_SECONDS * 1_000) - 1,
            confirmed=True,
        )
        runtime.register(A_URI, adapter.session_ref, now_ms, confirmed=True)
        facts = next(iter(app.app._cell_interactive_facts().values()))
        assert facts.known is False and facts.resync_required is True

        outcome = await adapter.perform(Refresh())
        assert isinstance(outcome, Ok)
        facts = next(iter(app.app._cell_interactive_facts().values()))
        assert facts.known is True and facts.resync_required is False
    finally:
        app.close()


@pytest.mark.anyio
async def test_heartbeat_only_ttl_recovery_refreshes_once_per_gap(tmp_path) -> None:
    from hyprial.daemon.impl.inbox_watch import Heartbeat
    from tests.daemon.harnesses.carrier.turns.test_turn_refresh import _open_contact_gap

    app, factory, adapter, _turns, _clock = _fixture(tmp_path)
    try:
        await adapter.start()
        for count in (2, 3):
            _open_contact_gap(app)
            await adapter.perform(Heartbeat())
            assert len(factory.of(TURN_REFRESH)) == count
            assert next(iter(app.app._cell_interactive_facts().values())).known
            for _ in range(4):
                await adapter.perform(Heartbeat())
            await _drive_poll(adapter, actions=20)
            assert len(factory.of(TURN_REFRESH)) == count
        assert len(factory.of("session.register")) == 1
        assert factory.of("session.refresh") == []
        assert factory.of("message.pending.batch") == []
        assert factory.of("session.turn.started") == []
    finally:
        app.close()


@pytest.mark.anyio
async def test_poll_does_not_duplicate_or_wait_for_heartbeat_refresh(tmp_path, monkeypatch) -> None:
    import anyio

    from hyprial.daemon.impl.inbox_watch import Heartbeat, ListInbox
    from tests.daemon.harnesses.carrier.turns.test_turn_refresh import _open_contact_gap

    app, factory, adapter, _turns, _clock = _fixture(tmp_path)
    try:
        await adapter.start()
        _open_contact_gap(app)
        entered, release, polled = anyio.Event(), anyio.Event(), anyio.Event()
        original = adapter._call
        attempts = 0

        async def hold_snapshot(method, params, *, mutation):
            nonlocal attempts
            if method == TURN_REFRESH:
                attempts += 1
                entered.set()
                with anyio.fail_after(3):
                    await release.wait()
            return await original(method, params, mutation=mutation)

        async def poll():
            await adapter.perform(ListInbox())
            polled.set()

        monkeypatch.setattr(adapter, "_call", hold_snapshot)
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(adapter.perform, Heartbeat())
            with anyio.fail_after(3):
                await entered.wait()
            tasks.start_soon(poll)
            with anyio.move_on_after(0.2):
                await polled.wait()
            poll_completed_during_snapshot = polled.is_set()
            release.set()
        assert attempts == 1
        assert poll_completed_during_snapshot
        await _drive_poll(adapter, actions=20)
        assert len(factory.of(TURN_REFRESH)) == 2
        assert next(iter(app.app._cell_interactive_facts().values())).known
    finally:
        app.close()


@pytest.mark.anyio
@pytest.mark.parametrize("rejected", [False, True])
async def test_old_flight_cannot_settle_or_refuse_new_epoch(tmp_path, monkeypatch, rejected) -> None:
    import anyio

    from hyprial.daemon.impl.inbox_watch import Heartbeat

    app, factory, adapter, _turns, _clock = _fixture(tmp_path)
    try:
        await adapter.start()
        entered, release = anyio.Event(), anyio.Event()
        adapter._turn_refresh_epoch = "previous-epoch"
        original = adapter._call
        attempts = 0

        async def delayed(method, params, *, mutation):
            nonlocal attempts
            if method == TURN_REFRESH:
                attempts += 1
                if attempts == 1:
                    entered.set()
                    with anyio.fail_after(3):
                        await release.wait()
                    if rejected:
                        raise DaemonRequestRejected("METHOD_NOT_FOUND", "old epoch")
            return await original(method, params, mutation=mutation)

        monkeypatch.setattr(adapter, "_call", delayed)
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(adapter.refresh_turns)
            with anyio.fail_after(3):
                await entered.wait()
                await adapter.perform(Refresh())
            attempts_before_release = attempts
            release.set()
        assert attempts_before_release == 1
        assert adapter._turn_refresh_owed
        assert not adapter._turn_refresh_unsupported
        await adapter.perform(Heartbeat())
        assert attempts == 2
        assert not adapter._turn_refresh_owed
    finally:
        app.close()


@pytest.mark.anyio
async def test_stale_daemon_epoch_refusal_keeps_refresh_owed(tmp_path, monkeypatch):
    from hyprial.daemon.impl.inbox_watch import Heartbeat

    app, factory, adapter, _turns, _clock = _fixture(tmp_path)
    try:
        await adapter.start()
        original = adapter._call
        attempts = 0

        async def refuse_once(method, params, *, mutation):
            nonlocal attempts
            if method == TURN_REFRESH:
                attempts += 1
                if attempts == 1:
                    params = {**params, "daemonEpoch": "old-epoch"}
            return await original(method, params, mutation=mutation)

        monkeypatch.setattr(adapter, "_call", refuse_once)
        assert not await adapter.refresh_turns()
        assert adapter._turn_refresh_owed
        await adapter.perform(Heartbeat())
        assert attempts == 2
        assert not adapter._turn_refresh_owed
        assert factory.of(TURN_REFRESH)[-1]["daemonEpoch"] == app.app.epoch
        assert factory.of("message.pending.batch") == []
    finally:
        app.close()


async def _drive_poll(adapter, *, actions: int) -> None:
    """Drive the real poll state machine like the shipped poll lane."""

    from hyprial.daemon.impl.inbox_watch import Sleep

    adapter.watch.use_wait = False
    now = int(time.monotonic() * 1_000)
    for _ in range(actions):
        action = adapter.watch.poll_next(now)
        now += 500
        if isinstance(action, Sleep):
            continue
        outcome = await adapter.perform(action)
        adapter.watch.poll_done(action, outcome, now)


@pytest.mark.anyio
async def test_a_lost_initial_refresh_is_recovered_without_a_new_epoch(
    tmp_path,
) -> None:
    """Both proxy attempts fail at registration; the next contact recovers."""

    app, factory, adapter, _turns, _clock = _fixture(tmp_path)
    try:
        factory.fail_once[TURN_REFRESH] = 2
        await adapter.start()
        assert adapter.started_once
        # The immediate registration refresh failed twice; the ordinary startup
        # list contact carried the owed retry in the same daemon generation.
        assert len(factory.of(TURN_REFRESH)) == 3
        assert next(iter(app.app._cell_interactive_facts().values())).scope == "whole-session"
        assert factory.of("message.pending.batch") == []
    finally:
        app.close()


@pytest.mark.anyio
async def test_a_transient_refresh_loss_is_retried_then_settles(
    tmp_path,
) -> None:
    app, factory, adapter, _turns, _clock = _fixture(tmp_path)
    try:
        await adapter.start()
        settled = len(factory.of(TURN_REFRESH))
        factory.fail_once[TURN_REFRESH] = 2
        await adapter.perform(Refresh())
        assert len(factory.of(TURN_REFRESH)) == settled + 2

        await _drive_poll(adapter, actions=20)
        assert len(factory.of(TURN_REFRESH)) == settled + 3
        assert next(iter(app.app._cell_interactive_facts().values())).scope == "whole-session"

        # The obligation settled: no further observation call per poll.
        await _drive_poll(adapter, actions=20)
        assert len(factory.of(TURN_REFRESH)) == settled + 3
        assert factory.of("message.pending.batch") == []
    finally:
        app.close()


@pytest.mark.anyio
async def test_an_unknown_method_is_cached_instead_of_retried(
    tmp_path,
) -> None:
    app, factory, adapter, _turns, _clock = _fixture(tmp_path)
    try:
        original = adapter._call
        attempts = {"count": 0}

        async def unknown_method(method, params, *, mutation):
            if method == TURN_REFRESH:
                attempts["count"] += 1
                raise DaemonRequestRejected("METHOD_NOT_FOUND", "older daemon")
            return await original(method, params, mutation=mutation)

        adapter._call = unknown_method
        try:
            await adapter.start()
            await _drive_poll(adapter, actions=20)
            # Exactly one attempt: a deterministic refusal is not a transient loss.
            assert attempts["count"] == 1
            assert factory.of(TURN_REFRESH) == []
        finally:
            adapter._call = original
    finally:
        app.close()


@pytest.mark.anyio
async def test_a_new_registration_retries_a_cached_unsupported_method(
    tmp_path,
) -> None:
    app, factory, adapter, _turns, _clock = _fixture(tmp_path)
    try:
        original = adapter._call
        state = {"unknown": True}

        async def unknown_method(method, params, *, mutation):
            if method == TURN_REFRESH and state["unknown"]:
                raise DaemonRequestRejected("METHOD_NOT_FOUND", "older daemon")
            return await original(method, params, mutation=mutation)

        adapter._call = unknown_method
        try:
            await adapter.start()
            assert factory.of(TURN_REFRESH) == []
            state["unknown"] = False
            # A fresh registration is a new daemon generation edge.
            await adapter._register()
            assert len(factory.of(TURN_REFRESH)) == 1
        finally:
            adapter._call = original
    finally:
        app.close()
