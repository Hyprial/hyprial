"""A heartbeat on an already-bound session must not rewrite desired state.

Production 0.5.0, 2026-10-05: every managed session heartbeats once a second
and each heartbeat recorded, then completed, a bind effect -- two full
desired-state rewrites (~0.5 s each) to re-touch a binding that had not
moved.  With ~11 sessions the writes needed more time than exists; session
register/refresh/heartbeat calls timed out at 10 s (register failed 35 of 39)
and reconnected Channels never woke anyone.  Once this generation durably
bound the session, steady refreshes and heartbeats stage no Agent bind at all.
The application advances liveness through the bounded activity command;
register/reconnect and post-expiry recovery retain the durable bind path.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.operations.session_ports import (
    HeartbeatSessionCommand,
    RefreshSessionCommand,
    UnregisterSessionCommand,
)
from hyprial.daemon.impl.session_actor import SessionActor
from hyprial.identity.impl.agents.actor.ports import (
    AgentBindingProjection,
    BindAgentCommand,
)
from hyprial.kernel import PortCommandRejected
from hyprial.kernel.impl.contracts.ports import PortAdmission

from ..support import (
    ACTOR,
    LEASE,
    _ScriptedAgentCommands,
    _register,
    _registered,
    _wait_until,
)


class _CountingStore(DesiredStateStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.records = 0
        self.completes = 0

    def record_session_agent_effects(self, effects):  # type: ignore[no-untyped-def]
        self.records += 1
        return super().record_session_agent_effects(effects)

    def complete_session_agent_effect(self, effect_id):  # type: ignore[no-untyped-def]
        self.completes += 1
        return super().complete_session_agent_effect(effect_id)


def _heartbeat(correlation_id: str) -> HeartbeatSessionCommand:
    return HeartbeatSessionCommand(
        correlation_id=correlation_id,
        actor=ACTOR,
        session_ref="session-1",
        channel_lease_token=LEASE,
        manage_agent=True,
    )


def _binds(commands: _ScriptedAgentCommands) -> int:
    return sum(isinstance(command, BindAgentCommand) for command in commands.commands)


def test_heartbeats_on_a_bound_session_stage_no_bind_or_state_write(tmp_path: Path) -> None:
    events: list[object] = []
    store = _CountingStore(tmp_path / "desired.json")
    commands = _ScriptedAgentCommands(())
    actor = SessionActor(
        store, daemon_epoch="epoch-1", event_sink=events.append, agent_commands=commands
    )
    commands.attach(actor)
    try:
        assert actor.submit(_register("register", "session-1")) is PortAdmission.ACCEPTED
        _wait_until(lambda: _registered(events, "register"))
        records, completes, binds = store.records, store.completes, _binds(commands)

        for index in range(10):
            assert actor.submit(_heartbeat(f"hb-{index}")) is PortAdmission.ACCEPTED
            _wait_until(lambda index=index: _registered(events, f"hb-{index}"))

        # The application refreshes liveness separately; the Session authority
        # must not stage an unchanged binding on its steady heartbeat path.
        assert _binds(commands) - binds == 0
        assert (store.records - records, store.completes - completes) == (0, 0)
        assert store.load().pending_session_agent_effects == ()
    finally:
        assert actor.drain(1.0).complete



class _BindingIdentity:
    token = "incarnation-1"
    current: AgentBindingProjection | None = None

    def entity_token(self, _actor: str) -> str:
        return self.token

    def binding_key(
        self, _actor: str
    ) -> tuple[int, str | None, str | None, str | None]:
        if self.current is None:
            return 2, None, None, None
        return (
            self.current.revision,
            self.current.harness,
            self.current.runtime,
            self.current.session_id,
        )


def _interactive_binding() -> AgentBindingProjection:
    return AgentBindingProjection(
        actor=ACTOR,
        harness="claude",
        runtime="interactive",
        session_id="session-1",
        bound_at_ms=1,
        revision=1,
    )


def test_changed_agent_incarnation_triggers_a_new_durable_bind(tmp_path: Path) -> None:
    identity = _BindingIdentity()
    events: list[object] = []
    store = _CountingStore(tmp_path / "desired.json")
    commands = _ScriptedAgentCommands(())
    actor = SessionActor(
        store,
        daemon_epoch="epoch-1",
        event_sink=events.append,
        agent_commands=commands,
        agent_identity=identity,
    )
    commands.attach(actor)
    try:
        actor.submit(_register("register", "session-1"))
        _wait_until(lambda: _registered(events, "register"))
        identity.current = _interactive_binding()
        records = store.records

        identity.token = "incarnation-2"
        actor.submit(_heartbeat("changed-agent"))
        _wait_until(lambda: _registered(events, "changed-agent"))
        assert store.records - records == 1
    finally:
        assert actor.drain(1.0).complete


def test_same_incarnation_release_triggers_a_durable_rebind(tmp_path: Path) -> None:
    identity = _BindingIdentity()
    events: list[object] = []
    store = _CountingStore(tmp_path / "desired.json")
    commands = _ScriptedAgentCommands(())
    actor = SessionActor(
        store,
        daemon_epoch="epoch-1",
        event_sink=events.append,
        agent_commands=commands,
        agent_identity=identity,
    )
    commands.attach(actor)
    try:
        actor.submit(_register("register", "session-1"))
        _wait_until(lambda: _registered(events, "register"))
        identity.current = _interactive_binding()
        actor.submit(_heartbeat("settled"))
        _wait_until(lambda: _registered(events, "settled"))
        records, binds = store.records, _binds(commands)

        identity.current = None
        actor.submit(_heartbeat("released"))
        _wait_until(lambda: _registered(events, "released"))

        assert store.records - records == 1
        assert _binds(commands) - binds == 1
    finally:
        assert actor.drain(1.0).complete


class _RejectTakeoverBind(_ScriptedAgentCommands):
    reject = False

    def submit(self, command):  # type: ignore[no-untyped-def]
        if self.reject and isinstance(command, BindAgentCommand):
            self.commands.append(command)
            assert self.session is not None
            self.session.accept_agent_event(
                PortCommandRejected(
                    correlation_id=command.correlation_id,
                    domain="agent",
                    generation=1,
                    version=len(self.commands),
                    code="AGENT_ALREADY_RUNNING",
                    detail="binding moved to another connector",
                )
            )
            return PortAdmission.ACCEPTED
        return super().submit(command)


def test_same_incarnation_takeover_rejects_the_old_session_heartbeat(
    tmp_path: Path,
) -> None:
    identity = _BindingIdentity()
    events: list[object] = []
    store = _CountingStore(tmp_path / "desired.json")
    commands = _RejectTakeoverBind(())
    actor = SessionActor(
        store,
        daemon_epoch="epoch-1",
        event_sink=events.append,
        agent_commands=commands,
        agent_identity=identity,
    )
    commands.attach(actor)
    try:
        actor.submit(_register("register", "session-1"))
        _wait_until(lambda: _registered(events, "register"))
        identity.current = _interactive_binding()
        commands.reject = True
        identity.current = AgentBindingProjection(
            actor=ACTOR,
            harness="codex",
            runtime="headless",
            session_id="other-session",
            bound_at_ms=2,
            revision=2,
        )
        binds = _binds(commands)

        actor.submit(_heartbeat("displaced"))
        _wait_until(
            lambda: any(
                isinstance(event, PortCommandRejected)
                and event.correlation_id == "displaced"
                for event in events
            )
        )

        assert _binds(commands) - binds == 1
        assert not _registered(events, "displaced")
        rejection = next(
            event
            for event in events
            if isinstance(event, PortCommandRejected)
            and event.correlation_id == "displaced"
        )
        assert rejection.code == "AGENT_ALREADY_RUNNING"
    finally:
        assert actor.drain(1.0).complete


def test_unregister_then_unmanaged_reregister_starts_from_a_durable_bind(tmp_path: Path) -> None:
    # Review 817: with the cache never cleared on unregister, the first
    # managed heartbeat after an unmanaged re-registration skipped its bind.
    events: list[object] = []
    store = _CountingStore(tmp_path / "desired.json")
    commands = _ScriptedAgentCommands(())
    actor = SessionActor(
        store, daemon_epoch="epoch-1", event_sink=events.append, agent_commands=commands
    )
    commands.attach(actor)
    try:
        actor.submit(_register("register", "session-1"))
        _wait_until(lambda: _registered(events, "register"))
        actor.submit(
            UnregisterSessionCommand(
                correlation_id="unregister", actor=ACTOR, session_ref="session-1",
                manage_agent=True,
            )
        )
        _wait_until(lambda: _registered(events, "unregister"))
        actor.submit(replace(_register("again", "session-1"), manage_agent=False))
        _wait_until(lambda: _registered(events, "again"))
        records = store.records

        actor.submit(_heartbeat("first-managed"))
        _wait_until(lambda: _registered(events, "first-managed"))
        assert store.records - records == 1
    finally:
        assert actor.drain(1.0).complete


def _refresh(correlation_id: str) -> RefreshSessionCommand:
    return RefreshSessionCommand(
        correlation_id=correlation_id,
        actor=ACTOR,
        session_ref="session-1",
        channel_lease_token=LEASE,
        manage_agent=True,
    )


def test_a_repeated_refresh_of_a_bound_session_does_not_rewrite_state(tmp_path: Path) -> None:
    # Production 2026-10-06: after a restart every channel refreshes; a refresh
    # slower than the client's 5 s budget was retried on every poll, and each
    # retry recorded and completed a bind again (~1.2 s of writer time), so
    # the writer never caught up. Only the first durable bind may write.
    events: list[object] = []
    store = _CountingStore(tmp_path / "desired.json")
    commands = _ScriptedAgentCommands(())
    actor = SessionActor(
        store, daemon_epoch="epoch-1", event_sink=events.append, agent_commands=commands
    )
    commands.attach(actor)
    try:
        actor.submit(_register("register", "session-1"))
        _wait_until(lambda: _registered(events, "register"))
        records, completes, binds = store.records, store.completes, _binds(commands)

        for index in range(5):
            assert actor.submit(_refresh(f"refresh-{index}")) is PortAdmission.ACCEPTED
            _wait_until(lambda index=index: _registered(events, f"refresh-{index}"))

        assert _binds(commands) - binds == 0
        assert (store.records - records, store.completes - completes) == (0, 0)
    finally:
        assert actor.drain(1.0).complete


def test_the_first_refresh_of_a_new_daemon_generation_still_binds_durably(tmp_path: Path) -> None:
    events: list[object] = []
    store = _CountingStore(tmp_path / "desired.json")
    commands = _ScriptedAgentCommands(())
    first = SessionActor(
        store, daemon_epoch="epoch-1", event_sink=events.append, agent_commands=commands
    )
    commands.attach(first)
    first.submit(_register("register", "session-1"))
    _wait_until(lambda: _registered(events, "register"))
    assert first.drain(1.0).complete

    # A restarted daemon: a new actor and epoch over the same durable state.
    restarted_commands = _ScriptedAgentCommands(())
    restarted = SessionActor(
        store, daemon_epoch="epoch-2", event_sink=events.append,
        agent_commands=restarted_commands,
    )
    restarted_commands.attach(restarted)
    try:
        records = store.records
        restarted.submit(_refresh("first"))
        _wait_until(lambda: _registered(events, "first"))
        assert store.records - records == 1  # durable: nothing bound yet
        restarted.submit(_refresh("retry"))
        _wait_until(lambda: _registered(events, "retry"))
        assert store.records - records == 1  # the retry is light
    finally:
        assert restarted.drain(1.0).complete


def test_a_refresh_after_lease_expiry_binds_durably_again(tmp_path: Path) -> None:
    # Review on #1171: an expired lease drops the runtime binding, so the
    # recovery refresh must re-decide it durably rather than go light.
    from hyprial.daemon.impl.operations.session_ports import (
        SessionLeaseElapsedCommand,
        SessionLeaseExpired,
    )

    now = {"value": 0.0}
    events: list[object] = []
    store = _CountingStore(tmp_path / "desired.json")
    commands = _ScriptedAgentCommands(())
    actor = SessionActor(
        store, daemon_epoch="epoch-1", event_sink=events.append,
        agent_commands=commands, clock=lambda: now["value"],
    )
    commands.attach(actor)
    try:
        actor.submit(_register("register", "session-1"))
        _wait_until(lambda: _registered(events, "register"))
        actor.submit(_refresh("light"))
        _wait_until(lambda: _registered(events, "light"))
        records = store.records

        actor.submit(
            SessionLeaseElapsedCommand(
                correlation_id="lease-sweep",
                generation=actor.generation,
                version=actor.version,
                observed_at_ms=20_000,
            )
        )
        _wait_until(
            lambda: any(
                isinstance(event, SessionLeaseExpired)
                and event.correlation_id == "lease-sweep"
                for event in events
            )
        )
        records = store.records
        actor.submit(_refresh("recovery"))
        _wait_until(lambda: _registered(events, "recovery"))
        assert store.records - records == 1  # durable again after expiry
    finally:
        assert actor.drain(1.0).complete
