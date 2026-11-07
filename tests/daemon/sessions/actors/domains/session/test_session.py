from __future__ import annotations

from dataclasses import replace
from tests.support.runtime.waiting_support import await_recorded
from pathlib import Path
from hyprial.kernel.impl.contracts.ports import PortAdmission, PortCommandRejected
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.session_actor import SessionActor
from hyprial.daemon.impl.operations.session_ports import HeartbeatSessionCommand, RefreshSessionCommand, SessionLeaseElapsedCommand, SessionLeaseExpired

from ..support import (
    ACTOR,
    LEASE,
    _ORCHESTRATION_SLACK_SECONDS,
    _SESSION_EVENT_BUDGET,
    _ScriptedAgentCommands,
    _SlowSessionEffectStore,
    _register,
    _registered,
    _wait_until,
    _wired_session,
)


def test_session_generation_fences_timer_and_expires_a_stale_lease(
    tmp_path: Path,
) -> None:
    now = {"value": 0.0}
    events: list[object] = []
    actor, agent = _wired_session(tmp_path, events, clock=lambda: now["value"])
    assert actor.submit(_register("register", "session-1")) is PortAdmission.ACCEPTED
    await_recorded(
        lambda: _registered(events, "register"),
        budget=_SESSION_EVENT_BUDGET,
        proves="this agent's registration command is recorded",
        monotonic_because="events is append-only in this file; a recorded event entry is never removed"
    )

    stale = SessionLeaseElapsedCommand(
        correlation_id="stale-generation",
        generation=actor.generation + 1,
        version=actor.version,
        observed_at_ms=20_000,
    )
    assert actor.submit(stale) is PortAdmission.ACCEPTED
    await_recorded(
        lambda: any( isinstance(event, PortCommandRejected) and event.correlation_id == "stale-generation" for event in events ),
        budget=_SESSION_EVENT_BUDGET,
        proves="the PortCommandRejected event is recorded",
        monotonic_because="events is append-only in this file; a recorded PortCommandRejected entry is never removed"
    )
    rejected = next(
        event
        for event in events
        if isinstance(event, PortCommandRejected)
        and event.correlation_id == "stale-generation"
    )
    assert rejected.code == "STALE_GENERATION"
    assert not any(isinstance(event, SessionLeaseExpired) for event in events)

    current = SessionLeaseElapsedCommand(
        correlation_id="session:heartbeat:background-lease",
        generation=actor.generation,
        version=actor.version,
        observed_at_ms=20_000,
    )
    assert actor.submit(current) is PortAdmission.ACCEPTED
    await_recorded(
        lambda: any( isinstance(event, SessionLeaseExpired) and event.correlation_id == "session:heartbeat:background-lease" for event in events ),
        budget=_SESSION_EVENT_BUDGET,
        proves="the SessionLeaseExpired event is recorded",
        monotonic_because="events is append-only in this file; a recorded SessionLeaseExpired entry is never removed"
    )
    expired = next(
        event
        for event in events
        if isinstance(event, SessionLeaseExpired)
        and event.correlation_id == "session:heartbeat:background-lease"
    )
    assert (expired.actor, expired.session_ref) == (ACTOR, "session-1")
    assert actor.read_session(ACTOR) is not None  # expiry is not unregister
    persistence_costs = actor.persistence_io_costs.snapshot()
    assert persistence_costs["record_session_agent_effects/background"]["calls"] == 1
    assert persistence_costs["complete_session_agent_effect/background"]["calls"] == 1
    assert "record_session_agent_effects/ipc" not in persistence_costs
    assert actor.drain(1.0).complete is True
    assert agent.drain(1.0).complete is True


def test_arbitrary_external_heartbeat_id_retains_ipc_origin(tmp_path: Path) -> None:
    events: list[object] = []
    actor, agent = _wired_session(tmp_path, events)
    try:
        # Registered without Agent management, so the heartbeat performs the
        # first (durable) bind and its persistence is what this test reads; a
        # heartbeat on an already-bound session persists nothing.
        assert actor.submit(
            replace(_register("opaque-registration", "session-1"), manage_agent=False)
        ) is PortAdmission.ACCEPTED
        await_recorded(
            lambda: _registered(events, "opaque-registration"),
            budget=_SESSION_EVENT_BUDGET,
            proves="the arbitrary-id registration completion is recorded",
            monotonic_because="events is append-only",
        )
        command = HeartbeatSessionCommand(
            "opaque-heartbeat", ACTOR, "session-1", LEASE
        )
        assert actor.submit(command) is PortAdmission.ACCEPTED
        await_recorded(
            lambda: _registered(events, "opaque-heartbeat"),
            budget=_SESSION_EVENT_BUDGET,
            proves="the arbitrary-id heartbeat completion is recorded",
            monotonic_because="events is append-only",
        )
        persistence_costs = actor.persistence_io_costs.snapshot()
        assert persistence_costs["record_session_agent_effects/ipc"]["calls"] == 1
        assert persistence_costs["complete_session_agent_effect/ipc"]["calls"] == 1
        assert "record_session_agent_effects/background" not in persistence_costs
    finally:
        assert actor.drain(1.0).complete
        assert agent.drain(1.0).complete


def test_session_projection_reads_do_not_open_desired_state(
    tmp_path: Path, monkeypatch,
) -> None:
    events: list[object] = []
    actor, agent = _wired_session(tmp_path, events)
    try:
        assert actor.submit(_register("projection", "session-1")) is PortAdmission.ACCEPTED
        await_recorded(
            lambda: _registered(events, "projection"),
            budget=_SESSION_EVENT_BUDGET,
            proves="the session registration completion is recorded",
            monotonic_because="events is append-only",
        )

        def forbidden_load():
            raise AssertionError("caller-side session projection opened SQLite")

        monkeypatch.setattr(actor._store, "load", forbidden_load)
        assert actor.read_session(ACTOR).session_ref == "session-1"  # type: ignore[union-attr]
        assert len(actor.read_sessions()) == 1
        assert actor.read_runtime(ACTOR) is not None
        actor.assert_owner(ACTOR, "session-1")
    finally:
        monkeypatch.undo()
        assert actor.drain(1.0).complete
        assert agent.drain(1.0).complete


def test_superseded_session_heartbeat_is_terminal(tmp_path: Path) -> None:
    events: list[object] = []
    actor, agent = _wired_session(tmp_path, events)
    actor.submit(_register("first", "session-1"))
    await_recorded(
        lambda: _registered(events, "first"),
        budget=_SESSION_EVENT_BUDGET,
        proves="the first launch's registration is recorded",
        monotonic_because="events is append-only in this file; a recorded event entry is never removed"
    )
    actor.submit(_register("second", "session-2"))
    await_recorded(
        lambda: _registered(events, "second"),
        budget=_SESSION_EVENT_BUDGET,
        proves="the second launch's registration is recorded",
        monotonic_because="events is append-only in this file; a recorded event entry is never removed"
    )

    actor.submit(
        HeartbeatSessionCommand(
            correlation_id="stale-heartbeat",
            actor=ACTOR,
            session_ref="session-1",
            channel_lease_token=LEASE,
        )
    )
    await_recorded(
        lambda: any( isinstance(event, PortCommandRejected) and event.correlation_id == "stale-heartbeat" for event in events ),
        budget=_SESSION_EVENT_BUDGET,
        proves="the PortCommandRejected event is recorded",
        monotonic_because="events is append-only in this file; a recorded PortCommandRejected entry is never removed"
    )
    rejection = next(
        event
        for event in events
        if isinstance(event, PortCommandRejected)
        and event.correlation_id == "stale-heartbeat"
    )
    assert rejection.code == "SESSION_SUPERSEDED"
    assert actor.read_session(ACTOR).session_ref == "session-2"  # type: ignore[union-attr]
    assert actor.drain(1.0).complete is True
    assert agent.drain(1.0).complete is True


def test_slow_persistence_does_not_block_unrelated_heartbeat(tmp_path: Path) -> None:
    events: list[object] = []
    store = _SlowSessionEffectStore(tmp_path / "desired.json")
    commands = _ScriptedAgentCommands(())
    actor = SessionActor(
        store,
        daemon_epoch="epoch-1",
        event_sink=events.append,
        agent_commands=commands,
    )
    commands.attach(actor)
    try:
        assert actor.submit(
            replace(_register("register", "session-1"), manage_agent=False)
        ) is PortAdmission.ACCEPTED
        _wait_until(lambda: _registered(events, "register"))

        assert actor.submit(
            RefreshSessionCommand(
                correlation_id="slow-refresh",
                actor=ACTOR,
                session_ref="session-1",
                channel_lease_token=LEASE,
            )
        ) is PortAdmission.ACCEPTED
        assert store.entered.wait(_ORCHESTRATION_SLACK_SECONDS)

        assert actor.submit(
            HeartbeatSessionCommand(
                correlation_id="independent-heartbeat",
                actor=ACTOR,
                session_ref="session-1",
                channel_lease_token=LEASE,
                manage_agent=False,
            )
        ) is PortAdmission.ACCEPTED
        _wait_until(lambda: _registered(events, "independent-heartbeat"))
        assert not _registered(events, "slow-refresh")
    finally:
        store.release.set()
        _wait_until(lambda: _registered(events, "slow-refresh"))
        assert actor.drain(1.0).complete


def test_session_retries_agent_overload_and_waits_for_correlated_result(
    tmp_path: Path,
) -> None:
    commands = _ScriptedAgentCommands(
        (PortAdmission.OVERLOADED, PortAdmission.OVERLOADED)
    )
    events: list[object] = []
    store = DesiredStateStore(tmp_path / "desired.json")
    actor = SessionActor(
        store,
        daemon_epoch="epoch-1",
        event_sink=events.append,
        agent_commands=commands,
        effect_deadline=0.2,
        effect_backoff=(0.001,),
    )
    commands.attach(actor)

    assert actor.submit(_register("overload", "session-1")) is PortAdmission.ACCEPTED
    await_recorded(
        lambda: _registered(events, "overload"),
        budget=_SESSION_EVENT_BUDGET,
        proves="the overload admission is recorded",
        monotonic_because="events is append-only in this file; a recorded overload admission entry is never removed"
    )

    assert len(commands.commands) == 3
    assert store.load().pending_session_agent_effects == ()
    assert actor.drain(1.0).complete is True
