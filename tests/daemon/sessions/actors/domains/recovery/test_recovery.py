from __future__ import annotations

import time
from tests.support.runtime.waiting_support import await_recorded
from pathlib import Path
import hyprial.daemon.impl.session_actor as session_module
from hyprial.identity.impl.agents import AgentActor, AgentRegistry
from hyprial.identity.impl.agents.actor.ports import AgentMutationCompleted, CreateAgentCommand
from hyprial.kernel.impl.contracts.ports import PortAdmission, PortCommandRejected
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.session_actor import SessionActor
from hyprial.daemon.impl.operations.session_ports import HeartbeatSessionCommand, RefreshSessionCommand, SessionLeaseElapsedCommand, SessionLeaseExpired, SessionLeaseSweepCompleted, SessionMutationCompleted, UnregisterSessionCommand

from ..support import (
    ACTOR,
    LEASE,
    NODE,
    OWNER,
    _AgentEventRelay,
    _BlockingAcceptedAgentCommands,
    _BlockingFailureSession,
    _ClosingThenBlockingAcceptedCommands,
    _FaultOnceDesiredStateStore,
    _fail_next_ownership_read,
    _ORCHESTRATION_SLACK_SECONDS,
    _SESSION_EVENT_BUDGET,
    _ScriptedAgentCommands,
    _register,
    _registered,
    _wait_until,
)


def test_session_restart_recovers_registration_but_not_generation_proof(
    tmp_path: Path,
) -> None:
    store = DesiredStateStore(tmp_path / "desired.json")
    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE)
    registry.create("foo")
    relay = _AgentEventRelay()
    agent = AgentActor(registry, event_sink=relay)
    first_events: list[object] = []
    first = SessionActor(
        store,
        daemon_epoch="epoch-1",
        event_sink=first_events.append,
        agent_commands=agent,
    )
    relay.session = first
    first.submit(_register("register", "session-1"))
    await_recorded(
        lambda: _registered(first_events, "register"),
        budget=_SESSION_EVENT_BUDGET,
        proves="the first worker's registration is recorded",
        monotonic_because="first_events is append-only in this file; a recorded event entry is never removed"
    )
    assert first.drain(1.0).complete is True

    events: list[object] = []
    restarted = SessionActor(
        store,
        daemon_epoch="epoch-2",
        event_sink=events.append,
        agent_commands=agent,
    )
    relay.session = restarted
    assert restarted.read_session(ACTOR).session_ref == "session-1"  # type: ignore[union-attr]
    restarted.submit(
        HeartbeatSessionCommand(
            correlation_id="heartbeat-before-refresh",
            actor=ACTOR,
            session_ref="session-1",
            channel_lease_token=LEASE,
        )
    )
    await_recorded(
        lambda: any( isinstance(event, PortCommandRejected) and event.correlation_id == "heartbeat-before-refresh" for event in events ),
        budget=_SESSION_EVENT_BUDGET,
        proves="the PortCommandRejected event is recorded",
        monotonic_because="events is append-only in this file; a recorded PortCommandRejected entry is never removed"
    )
    assert (
        next(
            event.code
            for event in events
            if isinstance(event, PortCommandRejected)
            and event.correlation_id == "heartbeat-before-refresh"
        )
        == "STALE_DAEMON_GENERATION"
    )

    restarted.submit(
        RefreshSessionCommand(
            correlation_id="refresh",
            actor=ACTOR,
            session_ref="session-1",
            channel_lease_token=LEASE,
        )
    )
    await_recorded(
        lambda: _registered(events, "refresh"),
        budget=_SESSION_EVENT_BUDGET,
        proves="the registration refresh is recorded",
        monotonic_because="events is append-only in this file; a recorded registration refresh entry is never removed"
    )
    restarted.submit(
        HeartbeatSessionCommand(
            correlation_id="heartbeat-after-refresh",
            actor=ACTOR,
            session_ref="session-1",
            channel_lease_token=LEASE,
        )
    )
    await_recorded(
        lambda: _registered(events, "heartbeat-after-refresh"),
        budget=_SESSION_EVENT_BUDGET,
        proves="the post-refresh heartbeat is recorded",
        monotonic_because="events is append-only in this file; a recorded post-refresh heartbeat entry is never removed"
    )
    result = next(
        event.result
        for event in events
        if isinstance(event, SessionMutationCompleted)
        and event.correlation_id == "heartbeat-after-refresh"
    )
    assert result.alive is True
    assert result.daemon_epoch == "epoch-2"
    assert restarted.drain(1.0).complete is True
    assert agent.drain(1.0).complete is True


def test_agent_restart_recovers_binding_and_drain_closes_admission(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agents.sqlite3"
    registry = AgentRegistry(database, owner=OWNER, machine=NODE)
    registry.create("foo")
    desired = DesiredStateStore(tmp_path / "desired.json")
    desired.upsert_harness(HarnessLaunchSpec("pi", "foo", True))

    events: list[object] = []
    actor = AgentActor(
        AgentRegistry(database, owner=OWNER, machine=NODE),
        event_sink=events.append,
        desired_state=desired,
        worker_running=lambda candidate: True if candidate == ACTOR else None,
    )
    binding = actor.read_binding(ACTOR)
    assert binding is not None
    assert (binding.harness, binding.runtime) == ("pi", "headless")
    assert actor.read_agent("foo").to_payload()["uri"] == ACTOR  # type: ignore[union-attr]

    assert actor.drain(1.0).complete is True
    admission = actor.submit(CreateAgentCommand("after-drain", "bar"))
    assert admission is PortAdmission.CLOSING
    rejection = next(
        event
        for event in events
        if isinstance(event, PortCommandRejected)
        and event.correlation_id == "after-drain"
    )
    assert rejection.admission is PortAdmission.CLOSING
    assert AgentRegistry(database, owner=OWNER, machine=NODE).get("bar") is None


def test_closed_agent_port_retains_durable_effect_and_restart_reconciles(
    tmp_path: Path,
) -> None:
    closed = _ScriptedAgentCommands(())
    closed.default = PortAdmission.CLOSING
    events: list[object] = []
    store = DesiredStateStore(tmp_path / "desired.json")
    actor = SessionActor(
        store,
        daemon_epoch="epoch-1",
        event_sink=events.append,
        agent_commands=closed,
        effect_deadline=0.02,
        effect_backoff=(0.001,),
    )
    closed.attach(actor)
    actor.submit(_register("closed", "session-1"))
    await_recorded(
        lambda: any( isinstance(event, PortCommandRejected) and event.correlation_id == "closed" and event.code == "AGENT_EFFECT_PORT_CLOSED" for event in events ),
        budget=_SESSION_EVENT_BUDGET,
        proves="the PortCommandRejected event is recorded",
        monotonic_because="events is append-only in this file; a recorded PortCommandRejected entry is never removed"
    )
    assert not _registered(events, "closed")
    assert len(store.load().pending_session_agent_effects) == 1
    # Durable custody permits bounded shutdown even though the effect remains.
    assert actor.drain(1.0).complete is True

    recovered_commands = _ScriptedAgentCommands(())
    recovered_events: list[object] = []
    recovered = SessionActor(
        store,
        daemon_epoch="epoch-2",
        event_sink=recovered_events.append,
        agent_commands=recovered_commands,
    )
    recovered_commands.attach(recovered)
    # NONMONOTONIC-WAIT: the store is re-read each poll and the pending-effects set can be repopulated -- reaching () is not monotone
    _wait_until(lambda: store.load().pending_session_agent_effects == ())
    assert recovered.read_session(ACTOR) is not None
    assert recovered.drain(1.0).complete is True


def test_guardian_restart_automatically_replays_durable_agent_effect(
    tmp_path: Path,
) -> None:
    store = _FaultOnceDesiredStateStore(tmp_path / "desired.json")
    commands = _ScriptedAgentCommands(())
    commands.default = PortAdmission.CLOSING
    events: list[object] = []
    actor = SessionActor(
        store,
        daemon_epoch="epoch-1",
        event_sink=events.append,
        agent_commands=commands,
        effect_deadline=0.02,
        effect_backoff=(0.001,),
    )
    commands.attach(actor)
    actor.submit(_register("restart-replay", "session-1"))
    await_recorded(
        lambda: any( isinstance(event, PortCommandRejected) and event.correlation_id == "restart-replay" and event.code == "AGENT_EFFECT_PORT_CLOSED" for event in events ),
        budget=_SESSION_EVENT_BUDGET,
        proves="the PortCommandRejected event is recorded",
        monotonic_because="events is append-only in this file; a recorded PortCommandRejected entry is never removed"
    )
    assert actor.generation == 1
    assert len(store.load().pending_session_agent_effects) == 1
    first_attempts = len(commands.commands)

    commands.default = PortAdmission.ACCEPTED
    _fail_next_ownership_read(actor)
    actor.submit(
        HeartbeatSessionCommand(
            "crash-generation-1",
            ACTOR,
            "session-1",
            LEASE,
        )
    )

    # NONMONOTONIC-WAIT: the generation can advance past 2 -- not a monotone record
    _wait_until(lambda: actor.generation == 2)
    # NONMONOTONIC-WAIT: the store is re-read each poll and the pending-effects set can be repopulated -- reaching () is not monotone
    _wait_until(lambda: store.load().pending_session_agent_effects == ())
    assert len(commands.commands) == first_attempts + 1
    assert actor.read_session(ACTOR) is not None
    # Durable deletion precedes I/O cost publication. Drain the completion
    # worker before inspecting its final counters; pending == () is too early.
    assert actor.drain(1.0).complete is True
    persistence_costs = actor.persistence_io_costs.snapshot()
    assert persistence_costs["claim_interactive_with_agent_effects/ipc"]["calls"] == 1
    assert persistence_costs["complete_session_agent_effect/background"]["calls"] == 1
    assert "complete_session_agent_effect/ipc" not in persistence_costs


def test_restart_does_not_replay_effect_with_surviving_worker_custody(
    tmp_path: Path,
) -> None:
    store = _FaultOnceDesiredStateStore(tmp_path / "desired.json")
    commands = _BlockingAcceptedAgentCommands()
    actor = SessionActor(
        store,
        daemon_epoch="epoch-1",
        event_sink=lambda _event: None,
        agent_commands=commands,
    )
    commands.session = actor
    actor.submit(_register("inflight-restart", "session-1"))
    assert commands.first_entered.wait(_ORCHESTRATION_SLACK_SECONDS)
    assert len(store.load().pending_session_agent_effects) == 1

    _fail_next_ownership_read(actor)
    actor.submit(
        HeartbeatSessionCommand("crash-inflight", ACTOR, "session-1", LEASE)
    )
    # NONMONOTONIC-WAIT: the generation can advance past 2 -- not a monotone record
    _wait_until(lambda: actor.generation == 2)
    time.sleep(0.05)
    assert len(commands.commands) == 1

    commands.release_first.set()
    # NONMONOTONIC-WAIT: the store is re-read each poll and the pending-effects set can be repopulated -- reaching () is not monotone
    _wait_until(lambda: store.load().pending_session_agent_effects == ())
    time.sleep(0.05)
    assert len(commands.commands) == 1
    persistence_costs = actor.persistence_io_costs.snapshot()
    assert persistence_costs["claim_interactive_with_agent_effects/ipc"]["calls"] == 1
    assert persistence_costs["complete_session_agent_effect/ipc"]["calls"] == 1
    assert "complete_session_agent_effect/background" not in persistence_costs
    assert actor.drain(1.0).complete is True


def test_stale_failure_and_completion_cannot_retire_new_custody_token(
    tmp_path: Path,
) -> None:
    store = _FaultOnceDesiredStateStore(tmp_path / "desired.json")
    commands = _ClosingThenBlockingAcceptedCommands()
    events: list[object] = []
    actor = _BlockingFailureSession(
        store,
        daemon_epoch="epoch-1",
        event_sink=events.append,
        agent_commands=commands,
    )
    commands.session = actor
    actor.submit(_register("token-race", "session-1"))
    assert actor.failure_entered.wait(_ORCHESTRATION_SLACK_SECONDS)
    stale = actor.blocked_failure
    assert stale is not None

    _fail_next_ownership_read(actor)
    actor.submit(
        HeartbeatSessionCommand("crash-token-race", ACTOR, "session-1", LEASE)
    )
    # NONMONOTONIC-WAIT: the generation can advance past 2 -- not a monotone record
    _wait_until(lambda: actor.generation == 2)
    actor.release_failure.set()
    assert commands.second_entered.wait(_ORCHESTRATION_SLACK_SECONDS)
    await_recorded(
        lambda: any( isinstance(event, PortCommandRejected) and event.code == "AGENT_EFFECT_PORT_CLOSED" for event in events ),
        budget=_SESSION_EVENT_BUDGET,
        proves="the PortCommandRejected event is recorded",
        monotonic_because="events is append-only in this file; a recorded PortCommandRejected entry is never removed"
    )

    with actor._effect_lock:
        current = actor._effect_custody[stale.effect_id]
    assert current.token != stale.custody_token
    # The old failure was processed above and did not retire generation 2.
    assert actor.reconcile_pending_effects() == 0

    # A delayed completion carrying the old token is equally powerless.
    actor._deliver_internal(
        session_module._AgentEffectResult(
            stale.effect_id,
            stale.custody_token,
            AgentMutationCompleted(
                correlation_id="stale-attempt",
                generation=1,
                version=1,
                operation="bind",
                changed=True,
            ),
        )
    )
    # NONMONOTONIC-WAIT: the store is re-read each poll and the pending-effects set can be repopulated -- reaching () is not monotone
    _wait_until(lambda: store.load().pending_session_agent_effects == ())
    with actor._effect_lock:
        assert actor._effect_custody[stale.effect_id].token == current.token

    commands.release_second.set()
    # NONMONOTONIC-WAIT: waits for an effect_id to be ABSENT -- absence can be re-broken by a new replay, so it is not monotone
    _wait_until(
        lambda: stale.effect_id not in actor._effect_custody
    )
    time.sleep(0.05)
    assert len(commands.commands) == 2
    assert actor.drain(1.0).complete is True


def test_only_binding_mutations_wait_for_agent_effect_admission(tmp_path: Path) -> None:
    commands = _ScriptedAgentCommands(())
    events: list[object] = []
    actor = SessionActor(
        DesiredStateStore(tmp_path / "desired.json"),
        daemon_epoch="epoch-1",
        event_sink=events.append,
        agent_commands=commands,
        clock=lambda: 0.0,
        effect_deadline=0.02,
        effect_backoff=(0.001,),
    )
    commands.attach(actor)
    actor.submit(_register("register-ok", "session-1"))
    await_recorded(
        lambda: _registered(events, "register-ok"),
        budget=_SESSION_EVENT_BUDGET,
        proves="the successful registration is recorded",
        monotonic_because="events is append-only in this file; a recorded successful registration entry is never removed"
    )
    commands.default = PortAdmission.CLOSING

    actor.submit(RefreshSessionCommand("refresh-closed", ACTOR, "session-1", LEASE))
    await_recorded(
        lambda: _registered(events, "refresh-closed"),
        budget=_SESSION_EVENT_BUDGET,
        proves="the unchanged refresh is recorded without an Agent effect",
        monotonic_because="events is append-only in this file; a recorded refresh is never removed"
    )
    actor.submit(HeartbeatSessionCommand("heartbeat-closed", ACTOR, "session-1", LEASE))
    await_recorded(
        lambda: _registered(events, "heartbeat-closed"),
        budget=_SESSION_EVENT_BUDGET,
        proves="the unchanged heartbeat is recorded without an Agent effect",
        monotonic_because="events is append-only in this file; a recorded heartbeat is never removed"
    )
    lease_version = actor.version
    actor.submit(
        SessionLeaseElapsedCommand(
            "lease-closed", actor.generation, lease_version, 20_000
        )
    )
    await_recorded(
        lambda: any( isinstance(event, PortCommandRejected) and event.correlation_id == "lease-closed" and event.code == "AGENT_EFFECT_PORT_CLOSED" for event in events ),
        budget=_SESSION_EVENT_BUDGET,
        proves="the PortCommandRejected event is recorded",
        monotonic_because="events is append-only in this file; a recorded PortCommandRejected entry is never removed"
    )
    actor.submit(UnregisterSessionCommand("unregister-closed", ACTOR, "session-1"))
    correlations = {"lease-closed", "unregister-closed"}
    await_recorded(
        lambda: ( correlations <= { event.correlation_id for event in events if isinstance(event, PortCommandRejected) and event.code == "AGENT_EFFECT_PORT_CLOSED" } ),
        budget=_SESSION_EVENT_BUDGET,
        proves="the PortCommandRejected event is recorded",
        monotonic_because="events is append-only in this file; a recorded PortCommandRejected entry is never removed"
    )
    assert {
        event.correlation_id
        for event in events
        if isinstance(event, SessionMutationCompleted)
        and event.correlation_id in {"refresh-closed", "heartbeat-closed"}
    } == {"refresh-closed", "heartbeat-closed"}
    assert not any(
        isinstance(event, SessionMutationCompleted)
        and event.correlation_id in correlations
        for event in events
    )
    assert not any(
        isinstance(event, (SessionLeaseExpired, SessionLeaseSweepCompleted))
        and event.correlation_id == "lease-closed"
        for event in events
    )
    assert actor.drain(1.0).complete is True


def test_agent_rejection_is_not_reported_as_session_success(tmp_path: Path) -> None:
    relay = _AgentEventRelay()
    agent = AgentActor(
        AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE),
        event_sink=relay,
    )
    events: list[object] = []
    store = DesiredStateStore(tmp_path / "desired.json")
    session = SessionActor(
        store,
        daemon_epoch="epoch-1",
        event_sink=events.append,
        agent_commands=agent,
    )
    relay.session = session
    session.submit(_register("missing-agent", "session-1"))
    await_recorded(
        lambda: any( isinstance(event, PortCommandRejected) and event.correlation_id == "missing-agent" and event.code == "AGENT_EFFECT_REJECTED" for event in events ),
        budget=_SESSION_EVENT_BUDGET,
        proves="the PortCommandRejected event is recorded",
        monotonic_because="events is append-only in this file; a recorded PortCommandRejected entry is never removed"
    )
    assert not _registered(events, "missing-agent")
    assert len(store.load().pending_session_agent_effects) == 1
    assert session.drain(1.0).complete is True
    assert agent.drain(1.0).complete is True
