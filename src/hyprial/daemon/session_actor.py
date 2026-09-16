"""Actor-owned interactive sessions with durable correlated Agent effects."""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from queue import Empty, Full, Queue
from typing import Protocol

from hyprial.actor_runtime import (
    ActorRuntime,
    ActorSpec,
    ActorState,
    AdmissionResult,
    DrainReport,
)
from hyprial.agents.ports import (
    AgentCommand,
    AgentEvent,
    AgentMutationCompleted,
    BindAgentCommand,
    ReleaseAgentCommand,
)
from hyprial.contracts import ipc_errors
from hyprial.contracts.channel import (
    CHANNEL_LIVENESS_TTL_SECONDS,
    CHANNEL_PROTOCOL_VERSION,
)
from hyprial.contracts.ports import PortAdmission, PortCommandRejected
from hyprial.contracts.session import SESSION_CARRIER_SOURCES

from .desired_state import (
    DesiredStateStore,
    InteractiveSession,
    PendingSessionAgentEffect,
)
from hyprial.uri import parse_agent_uri
from .session_ports import (
    HeartbeatSessionCommand,
    RefreshSessionCommand,
    RegisterSessionCommand,
    SessionEvent,
    SessionLeaseElapsedCommand,
    SessionLeaseExpired,
    SessionLeaseSweepCompleted,
    SessionMutationCompleted,
    SessionMutationProjection,
    SessionProjection,
    SessionRuntimeProjection,
    UnregisterSessionCommand,
)

__all__ = ["SessionActor", "SessionOwnershipError"]


class _CommandSubmitter(Protocol):
    def submit(self, command: AgentCommand) -> PortAdmission: ...


class SessionOwnershipError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def owner_only_relocation(previous: str, current: str) -> bool:
    """True when two canonical agent URIs differ only in the owner segment.

    An owner migration rewrites every stored address in place, so one live
    session legitimately changes actor spelling mid-flight: same machine,
    same actor name, new owner. That move is the SAME session under its
    current canonical spelling -- treating it as a supersede fences off every
    carrier that survived the migration (#352: both interactive sessions
    went quiet on their first post-restart poll and lost inbound delivery).
    Anything else -- a different actor name, a different machine, a
    non-canonical spelling -- is not an owner migration and stays a move.
    """

    old = parse_agent_uri(previous)
    new = parse_agent_uri(current)
    if old is None or new is None:
        return False
    return old[0] != new[0] and old[1] == new[1] and old[2] == new[2]


class _Version:
    def __init__(self) -> None:
        self._value = 0
        self._lock = threading.Lock()

    def read(self) -> int:
        with self._lock:
            return self._value

    def bump(self) -> int:
        with self._lock:
            self._value += 1
            return self._value


class _SessionRuntimeState:
    """Thread-safe immutable snapshots of generation-local session signals."""

    def __init__(self, daemon_epoch: str) -> None:
        self._daemon_epoch = daemon_epoch
        self._lock = threading.Lock()
        self._confirmed: dict[str, str] = {}
        self._heartbeats: dict[str, tuple[str, int]] = {}

    def register(
        self, actor: str, session_ref: str, observed_at_ms: int, *, confirmed: bool
    ) -> None:
        with self._lock:
            if confirmed:
                self._confirmed[actor] = session_ref
            self._heartbeats[actor] = (session_ref, observed_at_ms)

    def drop(self, actor: str) -> None:
        with self._lock:
            self._confirmed.pop(actor, None)
            self._heartbeats.pop(actor, None)

    def confirmed(self, actor: str) -> str | None:
        with self._lock:
            return self._confirmed.get(actor)

    def heartbeat(self, actor: str) -> tuple[str, int] | None:
        with self._lock:
            return self._heartbeats.get(actor)

    def read(
        self,
        actor: str,
        session_ref: str | None,
        *,
        now_ms: int,
        ttl_seconds: float,
    ) -> SessionRuntimeProjection:
        with self._lock:
            confirmed = self._confirmed.get(actor)
            heartbeat = self._heartbeats.get(actor)
        matches = bool(
            session_ref is not None
            and heartbeat is not None
            and heartbeat[0] == session_ref
        )
        alive = bool(
            matches
            and max(0, now_ms - heartbeat[1]) <= int(ttl_seconds * 1000)
        )
        return SessionRuntimeProjection(
            actor=actor,
            session_ref=session_ref,
            current_epoch=(
                self._daemon_epoch
                if session_ref is not None and confirmed == session_ref
                else None
            ),
            last_heartbeat_ms=heartbeat[1] if matches else None,
            alive=alive,
        )

@dataclass(frozen=True, slots=True)
class _AgentEffectResult:
    effect_id: str
    custody_token: str
    event: AgentMutationCompleted | PortCommandRejected


@dataclass(frozen=True, slots=True)
class _AgentEffectUnavailable:
    effect_id: str
    custody_token: str
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class _EffectWork:
    effect: PendingSessionAgentEffect
    custody_token: str
    generation: int
    agent_correlation_id: str


@dataclass(frozen=True, slots=True)
class _EffectCustody:
    token: str
    generation: int
    agent_correlation_id: str


@dataclass(frozen=True, slots=True)
class _ReplayScan:
    submitted: int
    blocked_by_surviving_custody: bool


@dataclass(slots=True)
class _PendingMutation:
    correlation_id: str
    effect_ids: set[str]
    final_events: tuple[SessionEvent, ...]


class _AgentEffectWorker:
    """Bounded Agent admission lane; it never executes Agent state itself."""

    def __init__(
        self,
        commands: _CommandSubmitter | None,
        completion_sink: Callable[[object], bool],
        submission_failed: Callable[[str, str], None],
        *,
        capacity: int,
        deadline: float,
        backoff: tuple[float, ...],
    ) -> None:
        if capacity < 1:
            raise ValueError("agent effect capacity must be at least 1")
        if deadline <= 0:
            raise ValueError("agent effect deadline must be positive")
        if not backoff or any(delay <= 0 for delay in backoff):
            raise ValueError("agent effect backoff must contain positive delays")
        self._commands = commands
        self._completion_sink = completion_sink
        self._submission_failed = submission_failed
        self._deadline = deadline
        self._backoff = backoff
        self._queue: Queue[_EffectWork | None] = Queue(maxsize=capacity)
        self._condition = threading.Condition()
        self._pending = 0
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name="hyprial-session-agent-effects",
            daemon=True,
        )
        self._thread.start()

    def submit(
        self,
        effect: PendingSessionAgentEffect,
        *,
        custody_token: str,
        generation: int,
        agent_correlation_id: str,
    ) -> bool:
        work = _EffectWork(
            effect, custody_token, generation, agent_correlation_id
        )
        with self._condition:
            if self._closed:
                return False
            try:
                self._queue.put_nowait(work)
            except Full:
                self._submission_failed(effect.effect_id, custody_token)
                self._completion_sink(
                    _AgentEffectUnavailable(
                        effect.effect_id,
                        custody_token,
                        "AGENT_EFFECT_QUEUE_OVERLOADED",
                        "Agent effect queue is full; durable effect remains pending",
                    )
                )
                return False
            self._pending += 1
            return True

    def drain(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            self._closed = True
            while self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        try:
            self._queue.put_nowait(None)
        except Full:
            return False
        self._thread.join(max(0.0, deadline - time.monotonic()))
        return not self._thread.is_alive()

    def _run(self) -> None:
        while True:
            try:
                work = self._queue.get(timeout=0.1)
            except Empty:
                with self._condition:
                    if self._closed and self._pending == 0:
                        return
                continue
            if work is None:
                return
            self._admit(work)
            with self._condition:
                self._pending -= 1
                self._condition.notify_all()

    def _admit(self, work: _EffectWork) -> None:
        if self._commands is None:
            self._fail(
                work,
                "AGENT_EFFECT_UNAVAILABLE",
                "SessionActor has no Agent command sink; durable effect remains pending",
            )
            return
        command = _agent_command(work)
        deadline = time.monotonic() + self._deadline
        attempt = 0
        while True:
            try:
                admission = self._commands.submit(command)
            except Exception as error:
                self._fail(
                    work,
                    "AGENT_EFFECT_SUBMIT_FAILED",
                    f"Agent command submit raised {type(error).__name__}",
                )
                return
            if admission is PortAdmission.ACCEPTED:
                return
            if admission is PortAdmission.CLOSING:
                self._fail(
                    work,
                    "AGENT_EFFECT_PORT_CLOSED",
                    "Agent command port is closed; durable effect remains pending",
                )
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._fail(
                    work,
                    "AGENT_EFFECT_ADMISSION_DEADLINE",
                    "Agent command port stayed overloaded; durable effect remains pending",
                )
                return
            delay = self._backoff[min(attempt, len(self._backoff) - 1)]
            attempt += 1
            time.sleep(min(delay, remaining))

    def _fail(self, work: _EffectWork, code: str, detail: str) -> None:
        self._submission_failed(work.effect.effect_id, work.custody_token)
        self._completion_sink(
            _AgentEffectUnavailable(
                work.effect.effect_id, work.custody_token, code, detail
            )
        )


@dataclass(slots=True)
class _SessionGeneration:
    generation: int
    store: DesiredStateStore
    daemon_epoch: str
    events: object
    request_effect: Callable[[PendingSessionAgentEffect, int], bool]
    retire_effect: Callable[[str, str], None]
    version: _Version
    clock_ms: Callable[[], int]
    lease_ttl_seconds: float
    runtime_projection: _SessionRuntimeState
    _mutations: dict[str, _PendingMutation] = field(default_factory=dict, init=False)
    _effect_owners: dict[str, str] = field(default_factory=dict, init=False)

    def __call__(self, command: object) -> None:
        from .lifecycle_receipts import LifecycleMutationRequest

        if isinstance(command, LifecycleMutationRequest):
            self._lifecycle(command)
            return
        if isinstance(command, _AgentEffectResult):
            self._effect_result(command)
            return
        if isinstance(command, _AgentEffectUnavailable):
            self._effect_unavailable(command)
            return
        if not isinstance(
            command,
            (
                RegisterSessionCommand,
                RefreshSessionCommand,
                HeartbeatSessionCommand,
                UnregisterSessionCommand,
                SessionLeaseElapsedCommand,
            ),
        ):
            self._reject(
                command, ipc_errors.INVALID_ARGUMENT, "unsupported session command"
            )
            return
        try:
            if isinstance(command, RegisterSessionCommand):
                self._register(command)
            elif isinstance(command, RefreshSessionCommand):
                self._refresh(command)
            elif isinstance(command, HeartbeatSessionCommand):
                self._heartbeat(command)
            elif isinstance(command, UnregisterSessionCommand):
                self._unregister(command)
            else:
                self._lease_elapsed(command)
        except SessionOwnershipError as error:
            self._reject(command, error.code, error.detail)
        except ValueError as error:
            self._reject(command, ipc_errors.INVALID_ARGUMENT, str(error))

    def _lifecycle(self, request: object) -> None:
        from .lifecycle_receipts import (
            LifecycleMutationCompleted,
            LifecycleMutationRequest,
        )

        assert isinstance(request, LifecycleMutationRequest)
        payload = request.payload
        try:
            provenance, superseded = self.store.apply_session_lifecycle(request)
            if isinstance(payload, RegisterSessionCommand):
                if provenance.changed:
                    for actor in superseded:
                        self.runtime_projection.drop(actor)
                persisted = next(
                    (
                        item
                        for item in self.store.load().interactive_sessions
                        if item.actor == payload.actor
                    ),
                    None,
                )
                if persisted is not None and persisted.session_ref is not None:
                    self.runtime_projection.register(
                        persisted.actor,
                        persisted.session_ref,
                        self.clock_ms(),
                        confirmed=persisted.source in SESSION_CARRIER_SOURCES,
                    )
                projection = SessionMutationProjection(
                    actor=payload.actor,
                    session_ref=payload.session_ref,
                    daemon_epoch=self.daemon_epoch if provenance.changed else None,
                    registered=provenance.changed,
                    superseded_actors=superseded,
                )
            elif isinstance(payload, UnregisterSessionCommand):
                if provenance.changed:
                    self.runtime_projection.drop(payload.actor)
                projection = SessionMutationProjection(
                    actor=payload.actor,
                    session_ref=payload.session_ref,
                    daemon_epoch=None,
                    unregistered=provenance.changed,
                )
            else:
                raise TypeError(
                    f"unsupported Session lifecycle payload: {type(payload).__name__}"
                )
            version = (
                self.version.bump()
                if provenance.changed
                else self.version.read()
            )
            base = SessionMutationCompleted(
                request.correlation_id,
                self.generation,
                version,
                projection,
            )
            self._publish(
                LifecycleMutationCompleted(
                    request.correlation_id,
                    request.attempt_token,
                    self.generation,
                    version,
                    "session",
                    provenance,
                    base,
                )
            )
        except (TypeError, ValueError) as error:
            self._reject(request, ipc_errors.INVALID_ARGUMENT, str(error))

    def _register(self, command: RegisterSessionCommand) -> None:
        _required(command.correlation_id, "correlation_id")
        _required(command.actor, "actor")
        _required(command.cwd, "cwd")
        _required(command.source, "source")
        _required(command.session_ref, "session_ref")
        if not command.command or any(not item for item in command.command):
            raise ValueError("command must contain non-empty strings")
        digest = _lease_digest_for_registration(
            source=command.source,
            protocol_version=command.channel_protocol_version,
            token=command.channel_lease_token,
        )
        session = InteractiveSession(
            actor=command.actor,
            cwd=command.cwd,
            command=command.command,
            source=command.source,
            session_ref=command.session_ref,
            runtime=command.runtime,
            channel_confirmed=command.channel_confirmed,
            channel_build_version=command.channel_build_version,
            channel_protocol_version=command.channel_protocol_version,
            owner_fence=command.owner_fence,
            channel_lease_digest=digest,
            tmux_session=command.tmux_session,
            process_pid=command.process_pid,
            process_identity=command.process_identity,
        )
        if command.manage_agent:
            bind = _bind_effect(command.correlation_id, session)
            _state, superseded, effects = self.store.claim_interactive_with_agent_effects(
                session, bind
            )
        else:
            _state, superseded = self.store.claim_interactive(session)
            effects = ()
        for actor in superseded:
            self.runtime_projection.drop(actor)
        self.runtime_projection.register(
            command.actor,
            command.session_ref,
            self.clock_ms(),
            confirmed=command.source in SESSION_CARRIER_SOURCES,
        )
        version = self.version.bump()
        result = SessionMutationCompleted(
            correlation_id=command.correlation_id,
            generation=self.generation,
            version=version,
            result=SessionMutationProjection(
                actor=command.actor,
                session_ref=command.session_ref,
                registered=True,
                daemon_epoch=self.daemon_epoch,
                channel_current_epoch=(
                    self.daemon_epoch
                    if command.source in SESSION_CARRIER_SOURCES
                    else None
                ),
                superseded_actors=superseded,
            ),
        )
        self._stage(command.correlation_id, effects, (result,))

    def _refresh(self, command: RefreshSessionCommand) -> None:
        current = self._owned_session(command.actor, command.session_ref)
        # ``current.actor`` is the canonical spelling of this session: after an
        # owner-only relocation it is the migrated URI, and every downstream
        # key (runtime liveness, result projection) must follow it so the
        # carrier continues as the canonical actor, not its stale spelling.
        if current.source not in SESSION_CARRIER_SOURCES:
            raise SessionOwnershipError(
                ipc_errors.INVALID_SESSION_SOURCE,
                "only a persisted session-carrier session can refresh its daemon generation",
            )
        if current.channel_lease_digest is not None:
            _verify_lease(current, command.channel_lease_token)
        effects = (
            (_bind_effect(command.correlation_id, current),)
            if command.manage_agent
            else ()
        )
        self.store.record_session_agent_effects(effects)
        self.runtime_projection.register(
            current.actor, command.session_ref, self.clock_ms(), confirmed=True
        )
        version = self.version.bump()
        result = SessionMutationCompleted(
            correlation_id=command.correlation_id,
            generation=self.generation,
            version=version,
            result=SessionMutationProjection(
                actor=current.actor,
                session_ref=command.session_ref,
                refreshed=True,
                daemon_epoch=self.daemon_epoch,
                channel_current_epoch=self.daemon_epoch,
            ),
        )
        self._stage(command.correlation_id, effects, (result,))

    def _heartbeat(self, command: HeartbeatSessionCommand) -> None:
        current = self._owned_session(command.actor, command.session_ref)
        # Same canonical-actor rule as _refresh: after an owner-only
        # relocation the lease, liveness, and verdict all key on the
        # session's current spelling.
        if current.source not in SESSION_CARRIER_SOURCES:
            raise SessionOwnershipError(
                ipc_errors.INVALID_SESSION_SOURCE,
                "only a persisted session-carrier session can renew liveness",
            )
        if current.channel_lease_digest is not None:
            _verify_lease(current, command.channel_lease_token)
        if self.runtime_projection.confirmed(current.actor) != command.session_ref:
            raise SessionOwnershipError(
                ipc_errors.STALE_DAEMON_GENERATION,
                "channel must refresh this daemon generation before heartbeat",
            )
        effects = (
            (_bind_effect(command.correlation_id, current),)
            if command.manage_agent
            else ()
        )
        self.store.record_session_agent_effects(effects)
        self.runtime_projection.register(
            current.actor, command.session_ref, self.clock_ms(), confirmed=True
        )
        version = self.version.bump()
        result = SessionMutationCompleted(
            correlation_id=command.correlation_id,
            generation=self.generation,
            version=version,
            result=SessionMutationProjection(
                actor=current.actor,
                session_ref=command.session_ref,
                alive=True,
                daemon_epoch=self.daemon_epoch,
            ),
        )
        self._stage(command.correlation_id, effects, (result,))

    def _unregister(self, command: UnregisterSessionCommand) -> None:
        effects = (
            (_release_effect(command.correlation_id, command.actor),)
            if command.manage_agent
            else ()
        )
        _state, changed = self.store.unregister_interactive_if_current(
            command.actor, command.session_ref, effects
        )
        effects = effects if changed else ()
        if changed:
            self.runtime_projection.drop(command.actor)
        version = self.version.bump() if changed else self.version.read()
        result = SessionMutationCompleted(
            correlation_id=command.correlation_id,
            generation=self.generation,
            version=version,
            result=SessionMutationProjection(
                actor=command.actor,
                session_ref=command.session_ref,
                unregistered=changed,
                daemon_epoch=None,
            ),
        )
        self._stage(command.correlation_id, effects, (result,))

    def _lease_elapsed(self, command: SessionLeaseElapsedCommand) -> None:
        if command.generation != self.generation:
            raise SessionOwnershipError(
                "STALE_GENERATION",
                f"lease sweep generation {command.generation} does not own generation {self.generation}",
            )
        current_version = self.version.read()
        if command.version != current_version:
            raise SessionOwnershipError(
                "STALE_VERSION",
                f"lease sweep version {command.version} does not match {current_version}",
            )
        if command.observed_at_ms < 0:
            raise ValueError("observed_at_ms must not be negative")
        sessions = self.store.load().interactive_sessions
        effects: list[PendingSessionAgentEffect] = []
        finals: list[SessionEvent] = []
        for session in sessions:
            if session.channel_lease_digest is None or session.session_ref is None:
                continue
            heartbeat = self.runtime_projection.heartbeat(session.actor)
            alive = (
                heartbeat is not None
                and heartbeat[0] == session.session_ref
                and max(0, command.observed_at_ms - heartbeat[1])
                <= int(self.lease_ttl_seconds * 1000)
            )
            if alive:
                continue
            self.runtime_projection.drop(session.actor)
            effect = _release_effect(command.correlation_id, session.actor)
            effects.append(effect)
            version = self.version.bump()
            finals.append(
                SessionLeaseExpired(
                    correlation_id=command.correlation_id,
                    generation=self.generation,
                    version=version,
                    actor=session.actor,
                    session_ref=session.session_ref,
                )
            )
        if effects:
            self.store.record_session_agent_effects(tuple(effects))
        finals.append(
            SessionLeaseSweepCompleted(
                correlation_id=command.correlation_id,
                generation=self.generation,
                version=self.version.read(),
                sessions_checked=len(sessions),
            )
        )
        self._stage(command.correlation_id, tuple(effects), tuple(finals))

    def _stage(
        self,
        correlation_id: str,
        effects: tuple[PendingSessionAgentEffect, ...],
        final_events: tuple[SessionEvent, ...],
    ) -> None:
        if not effects:
            for event in final_events:
                self._publish(event)
            return
        mutation = _PendingMutation(
            correlation_id=correlation_id,
            effect_ids={effect.effect_id for effect in effects},
            final_events=final_events,
        )
        self._mutations[correlation_id] = mutation
        for effect in effects:
            self._effect_owners[effect.effect_id] = correlation_id
            self.request_effect(effect, self.generation)

    def _effect_result(self, result: _AgentEffectResult) -> None:
        effect = self._pending_effect(result.effect_id)
        if effect is None:
            self.retire_effect(result.effect_id, result.custody_token)
            return
        event = result.event
        if isinstance(event, PortCommandRejected):
            self._fail_effect(
                effect,
                result.custody_token,
                "AGENT_EFFECT_REJECTED",
                f"Agent rejected {effect.operation}: {event.code}: {event.detail}",
            )
            return
        if event.operation != effect.operation:
            self._fail_effect(
                effect,
                result.custody_token,
                "AGENT_EFFECT_RESULT_MISMATCH",
                f"expected {effect.operation} result, received {event.operation}",
            )
            return
        self.store.complete_session_agent_effect(effect.effect_id)
        self.retire_effect(effect.effect_id, result.custody_token)
        owner = self._effect_owners.pop(effect.effect_id, None)
        if owner is None:
            return
        mutation = self._mutations.get(owner)
        if mutation is None:
            return
        mutation.effect_ids.discard(effect.effect_id)
        if mutation.effect_ids:
            return
        self._mutations.pop(owner, None)
        for final in mutation.final_events:
            self._publish(final)

    def _effect_unavailable(self, failure: _AgentEffectUnavailable) -> None:
        effect = self._pending_effect(failure.effect_id)
        if effect is not None:
            self._fail_effect(
                effect,
                failure.custody_token,
                failure.code,
                failure.detail,
            )
        else:
            self.retire_effect(failure.effect_id, failure.custody_token)

    def _fail_effect(
        self,
        effect: PendingSessionAgentEffect,
        custody_token: str,
        code: str,
        detail: str,
    ) -> None:
        # A correlated Agent failure is a terminal result for the surviving
        # worker custody.  The durable effect intentionally remains, but a
        # newer Session generation may now take it over.
        self.retire_effect(effect.effect_id, custody_token)
        owner = self._effect_owners.get(effect.effect_id)
        correlation_id = effect.correlation_id if owner is None else owner
        mutation = self._mutations.pop(correlation_id, None)
        if mutation is not None:
            for effect_id in mutation.effect_ids:
                self._effect_owners.pop(effect_id, None)
        self._publish(
            PortCommandRejected(
                correlation_id=correlation_id,
                domain="session",
                generation=self.generation,
                version=self.version.read(),
                code=code,
                detail=detail,
            )
        )

    def _pending_effect(self, effect_id: str) -> PendingSessionAgentEffect | None:
        return next(
            (
                effect
                for effect in self.store.load().pending_session_agent_effects
                if effect.effect_id == effect_id
            ),
            None,
        )

    def _owned_session(self, actor: str, session_ref: str) -> InteractiveSession:
        sessions = self.store.load().interactive_sessions
        current = next((item for item in sessions if item.actor == actor), None)
        if current is not None and current.session_ref == session_ref:
            return current
        if current is not None:
            raise SessionOwnershipError(
                ipc_errors.SESSION_SUPERSEDED,
                f"actor {actor} is now owned by interactive session {current.session_ref}, not {session_ref}",
            )
        relocated = next(
            (item for item in sessions if item.session_ref == session_ref), None
        )
        if relocated is not None:
            if owner_only_relocation(actor, relocated.actor):
                # An owner migration rewrote this session's stored address; the
                # same carrier is calling under its pre-migration spelling.
                # Same session: the caller continues under the canonical URI.
                return relocated
            raise SessionOwnershipError(
                ipc_errors.SESSION_SUPERSEDED,
                f"interactive session {session_ref} moved from actor {actor} to {relocated.actor}",
            )
        raise SessionOwnershipError(
            ipc_errors.STALE_SESSION,
            f"interactive session {session_ref} no longer owns actor {actor}",
        )

    def _reject(self, command: object, code: str, detail: str) -> None:
        self._publish(
            PortCommandRejected(
                correlation_id=str(getattr(command, "correlation_id", "")),
                domain="session",
                generation=self.generation,
                version=self.version.read(),
                code=code,
                detail=detail,
            )
        )

    def _publish(self, event: SessionEvent) -> None:
        _publish(self.events, event)


class SessionActor:
    """Typed command/projection boundary for interactive-session ownership.

    Wiring seam: the Agent actor's event sink must fan out every ``AgentEvent``
    to :meth:`accept_agent_event`; admission alone is never session success.
    """

    def __init__(
        self,
        store: DesiredStateStore,
        *,
        daemon_epoch: str,
        event_sink: object,
        agent_commands: _CommandSubmitter | None = None,
        clock: Callable[[], float] | None = None,
        clock_ms: Callable[[], int] | None = None,
        lease_ttl_seconds: float = CHANNEL_LIVENESS_TTL_SECONDS,
        mailbox_capacity: int = 128,
        effect_capacity: int = 64,
        effect_deadline: float = 1.0,
        effect_backoff: tuple[float, ...] = (0.005, 0.01, 0.02, 0.05),
        runtime: ActorRuntime | None = None,
    ) -> None:
        if not daemon_epoch:
            raise ValueError("daemon_epoch must not be empty")
        if clock is not None and clock_ms is not None:
            raise ValueError("pass clock or clock_ms, not both")
        self._store = store
        self._events = event_sink
        self._clock_ms = (
            clock_ms
            if clock_ms is not None
            else (
                (lambda: int(clock() * 1000))
                if clock is not None
                else (lambda: time.time_ns() // 1_000_000)
            )
        )
        self._lease_ttl_seconds = lease_ttl_seconds
        self._version = _Version()
        self._runtime_projection = _SessionRuntimeState(daemon_epoch)
        self._runtime = runtime or ActorRuntime()
        self._generation = 0
        self._generation_lock = threading.Lock()
        self._effect_lock = threading.Lock()
        self._effect_custody: dict[str, _EffectCustody] = {}
        self._agent_attempts: dict[str, tuple[str, str]] = {}
        self._replay_lock = threading.Lock()
        self._replayed_generations: set[int] = set()
        self._replay_attempted: dict[int, threading.Event] = {}
        self._draining = False

        def factory() -> _SessionGeneration:
            with self._generation_lock:
                self._generation += 1
                generation = self._generation
            handler = _SessionGeneration(
                generation=generation,
                store=self._store,
                daemon_epoch=daemon_epoch,
                events=self._events,
                request_effect=self._request_effect,
                retire_effect=self._retire_effect,
                version=self._version,
                clock_ms=self._clock_ms,
                lease_ttl_seconds=self._lease_ttl_seconds,
                runtime_projection=self._runtime_projection,
            )
            # Generation 1 is reconciled synchronously after the worker and
            # stable handle exist.  Guardian-created generations need their
            # own hook: handler_factory runs before the backend endpoint is
            # RUNNING, so a small fenced waiter performs replay only after the
            # guardian publishes that exact generation as runnable.
            if generation > 1:
                with self._replay_lock:
                    self._replay_attempted[generation] = threading.Event()
                self._schedule_generation_replay(generation)
            return handler

        self._handle = self._runtime.start(
            ActorSpec(
                name="session-authority",
                handler_factory=factory,
                mailbox_capacity=mailbox_capacity,
            )
        )
        self._effect_worker = _AgentEffectWorker(
            agent_commands,
            self._deliver_internal,
            self._submission_failed,
            capacity=effect_capacity,
            deadline=effect_deadline,
            backoff=effect_backoff,
        )
        self.reconcile_pending_effects()

    @property
    def generation(self) -> int:
        snapshot = self._runtime.snapshot(self._handle)
        if snapshot.generation > 1:
            deadline = time.monotonic() + 1.0
            attempted = None
            while attempted is None and time.monotonic() < deadline:
                with self._replay_lock:
                    attempted = self._replay_attempted.get(snapshot.generation)
                if attempted is None:
                    current = self._runtime.snapshot(self._handle)
                    if (
                        current.generation != snapshot.generation
                        or current.state
                        in {ActorState.QUARANTINED, ActorState.STOPPED}
                    ):
                        return current.generation
                    time.sleep(0.005)
            if attempted is not None:
                # Expose a restarted generation only after its automatic
                # replay hook has audited durable custody at least once.  This
                # keeps RUNNING observable without letting an external reader
                # race ahead and consume the very recovery fault the hook must
                # handle.
                attempted.wait(max(0.0, deadline - time.monotonic()))
        return snapshot.generation

    @property
    def version(self) -> int:
        return self._version.read()

    def submit(self, command: object) -> PortAdmission:
        admission = self._runtime.tell(self._handle, command)
        if admission is AdmissionResult.ACCEPTED:
            return PortAdmission.ACCEPTED
        port_admission = (
            PortAdmission.OVERLOADED
            if admission is AdmissionResult.OVERLOADED
            else PortAdmission.CLOSING
        )
        _publish(
            self._events,
            PortCommandRejected(
                correlation_id=str(getattr(command, "correlation_id", "")),
                domain="session",
                generation=self.generation,
                version=self.version,
                code=(
                    "PORT_OVERLOADED"
                    if port_admission is PortAdmission.OVERLOADED
                    else "PORT_CLOSING"
                ),
                detail=f"session command admission is {port_admission.value}",
                admission=port_admission,
            ),
        )
        return port_admission

    def accept_agent_event(self, event: AgentEvent) -> bool:
        """Accept one correlated Agent result from the composition fan-out."""

        if not isinstance(event, (AgentMutationCompleted, PortCommandRejected)):
            return False
        if isinstance(event, PortCommandRejected):
            if event.domain != "agent" or event.admission is not None:
                return False
        with self._effect_lock:
            attempt = self._agent_attempts.get(event.correlation_id)
        if attempt is None:
            return False
        effect_id, custody_token = attempt
        delivered = self._deliver_internal(
            _AgentEffectResult(effect_id, custody_token, event)
        )
        if not delivered:
            self._submission_failed(effect_id, custody_token)
        return delivered

    def reconcile_pending_effects(self) -> int:
        """Explicit Agent-recovery nudge; Session restarts replay automatically."""

        replayed = self._reconcile_pending_effects(expected_generation=None)
        return 0 if replayed is None else replayed.submitted

    def _reconcile_pending_effects(
        self, *, expected_generation: int | None
    ) -> _ReplayScan | None:
        if expected_generation is not None:
            snapshot = self._runtime.snapshot(self._handle)
            if (
                snapshot.generation != expected_generation
                or snapshot.state is not ActorState.RUNNING
            ):
                return None

        effects = self._store.load().pending_session_agent_effects
        if expected_generation is not None:
            snapshot = self._runtime.snapshot(self._handle)
            if (
                snapshot.generation != expected_generation
                or snapshot.state is not ActorState.RUNNING
            ):
                return None
        generation = (
            expected_generation
            if expected_generation is not None
            else self._runtime.snapshot(self._handle).generation
        )
        submitted = 0
        blocked = False
        for effect in effects:
            with self._effect_lock:
                surviving = self._effect_custody.get(effect.effect_id)
            if surviving is not None:
                blocked = True
                continue
            # The first snapshot may have been taken while a surviving worker
            # was completing.  Successful completion removes the durable row
            # before releasing its custody token; once we observe no token, a
            # second durable read closes that handoff race and prevents replay
            # from enqueuing a row that was just retired.
            if not any(
                current.effect_id == effect.effect_id
                for current in self._store.load().pending_session_agent_effects
            ):
                continue
            if self._request_effect(effect, generation):
                submitted += 1
                continue
            with self._effect_lock:
                blocked = blocked or effect.effect_id in self._effect_custody
        return _ReplayScan(submitted, blocked)

    def _schedule_generation_replay(self, generation: int) -> None:
        threading.Thread(
            target=self._replay_when_running,
            args=(generation,),
            name=f"hyprial-session-replay-{generation}",
            daemon=True,
        ).start()

    def _replay_when_running(self, generation: int) -> None:
        while not self._draining:
            try:
                snapshot = self._runtime.snapshot(self._handle)
            except (KeyError, RuntimeError):
                time.sleep(0.005)
                continue
            if snapshot.generation > generation:
                return
            if snapshot.state in {ActorState.QUARANTINED, ActorState.STOPPED}:
                return
            if snapshot.generation != generation or snapshot.state is not ActorState.RUNNING:
                time.sleep(0.005)
                continue
            with self._replay_lock:
                if generation in self._replayed_generations:
                    return
            try:
                replayed = self._reconcile_pending_effects(
                    expected_generation=generation
                )
            except Exception:
                # Desired-state I/O can be the reason the previous generation
                # failed.  Keep this generation's restart hook alive; durable
                # custody makes retry safe and the generation fence prevents a
                # delayed hook from acting after a subsequent crash.
                with self._replay_lock:
                    attempted = self._replay_attempted.get(generation)
                if attempted is not None:
                    attempted.set()
                time.sleep(0.01)
                continue
            with self._replay_lock:
                attempted = self._replay_attempted.get(generation)
            if attempted is not None:
                attempted.set()
            if replayed is None:
                continue
            if replayed.blocked_by_surviving_custody:
                time.sleep(0.005)
                continue
            with self._replay_lock:
                self._replayed_generations.add(generation)
            return

    def read_session(self, actor: str) -> SessionProjection | None:
        session = next(
            (
                item
                for item in self._store.load().interactive_sessions
                if item.actor == actor
            ),
            None,
        )
        return None if session is None else _projection(session, self.version)

    def read_sessions(self) -> tuple[SessionProjection, ...]:
        version = self.version
        return tuple(
            _projection(session, version)
            for session in self._store.load().interactive_sessions
        )

    def read_runtime(self, actor: str) -> SessionRuntimeProjection | None:
        session = next(
            (item for item in self._store.load().interactive_sessions if item.actor == actor),
            None,
        )
        if session is None:
            return None
        return self._runtime_projection.read(
            actor,
            session.session_ref,
            now_ms=self._clock_ms(),
            ttl_seconds=self._lease_ttl_seconds,
        )

    def assert_owner(self, actor: str, session_ref: str) -> None:
        sessions = self._store.load().interactive_sessions
        current = next((item for item in sessions if item.actor == actor), None)
        if current is not None and current.session_ref == session_ref:
            return
        if current is not None:
            raise SessionOwnershipError(
                ipc_errors.SESSION_SUPERSEDED,
                f"interactive session {session_ref} no longer owns actor {actor}",
            )
        relocated = next(
            (item for item in sessions if item.session_ref == session_ref), None
        )
        if relocated is not None:
            if owner_only_relocation(actor, relocated.actor):
                # Owner migration moved the same session's address; not a
                # supersede (mirrors _owned_session's relocated branch).
                return
            raise SessionOwnershipError(
                ipc_errors.SESSION_SUPERSEDED,
                f"interactive session {session_ref} no longer owns actor {actor}",
            )
        raise SessionOwnershipError(
            ipc_errors.STALE_SESSION,
            f"interactive session {session_ref} no longer owns actor {actor}",
        )

    def drain(self, timeout: float = 5.0) -> DrainReport:
        started = time.monotonic()
        self._draining = True
        worker_complete = self._effect_worker.drain(timeout / 2)
        remaining = max(0.0, timeout - (time.monotonic() - started))
        report = self._runtime.drain(remaining)
        if worker_complete:
            return DrainReport(
                complete=report.complete,
                elapsed=time.monotonic() - started,
                remaining=report.remaining,
            )
        return DrainReport(
            complete=False,
            elapsed=time.monotonic() - started,
            remaining=(self._handle,),
        )

    def _request_effect(
        self, effect: PendingSessionAgentEffect, generation: int
    ) -> bool:
        custody_token = uuid.uuid4().hex
        agent_correlation_id = f"{effect.effect_id}.{custody_token}"
        custody = _EffectCustody(
            custody_token, generation, agent_correlation_id
        )
        with self._effect_lock:
            if self._draining or effect.effect_id in self._effect_custody:
                return False
            self._effect_custody[effect.effect_id] = custody
            self._agent_attempts[agent_correlation_id] = (
                effect.effect_id,
                custody_token,
            )
        accepted = self._effect_worker.submit(
            effect,
            custody_token=custody.token,
            generation=generation,
            agent_correlation_id=agent_correlation_id,
        )
        if not accepted:
            self._submission_failed(effect.effect_id, custody.token)
        return accepted

    def _submission_failed(self, effect_id: str, custody_token: str) -> None:
        with self._effect_lock:
            custody = self._effect_custody.get(effect_id)
            if custody is not None and custody.token == custody_token:
                self._agent_attempts.pop(custody.agent_correlation_id, None)
                self._effect_custody.pop(effect_id, None)

    def _retire_effect(self, effect_id: str, custody_token: str) -> None:
        with self._effect_lock:
            custody = self._effect_custody.get(effect_id)
            if custody is not None and custody.token == custody_token:
                self._agent_attempts.pop(custody.agent_correlation_id, None)
                self._effect_custody.pop(effect_id, None)

    def _deliver_internal(self, command: object) -> bool:
        deadline = time.monotonic() + 1.0
        delay = 0.001
        while True:
            admission = self._runtime.tell(self._handle, command)
            if admission is AdmissionResult.ACCEPTED:
                return True
            if admission is AdmissionResult.CLOSED:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 0.05)


def _agent_command(work: _EffectWork) -> AgentCommand:
    effect = work.effect
    if effect.operation == "bind":
        assert effect.harness is not None
        assert effect.runtime is not None
        return BindAgentCommand(
            correlation_id=work.agent_correlation_id,
            actor=effect.actor,
            harness=effect.harness,
            runtime=effect.runtime,
            session_id=effect.session_id,
        )
    return ReleaseAgentCommand(work.agent_correlation_id, effect.actor)


def _bind_effect(
    correlation_id: str, session: InteractiveSession
) -> PendingSessionAgentEffect:
    return PendingSessionAgentEffect(
        effect_id=uuid.uuid4().hex,
        correlation_id=correlation_id,
        operation="bind",
        actor=session.actor,
        harness=_session_harness(session),
        runtime="interactive",
        session_id=session.session_ref,
    )


def _release_effect(correlation_id: str, actor: str) -> PendingSessionAgentEffect:
    return PendingSessionAgentEffect(
        effect_id=uuid.uuid4().hex,
        correlation_id=correlation_id,
        operation="release",
        actor=actor,
    )


def _required(value: str, label: str) -> str:
    if not value:
        raise ValueError(f"{label} must not be empty")
    return value


def _lease_token(value: object) -> str:
    if not isinstance(value, str) or not 32 <= len(value) <= 256:
        raise SessionOwnershipError(
            ipc_errors.INVALID_CHANNEL_LEASE,
            "channelLeaseToken must be an opaque 32-256 character token",
        )
    return value


def _lease_digest_for_registration(
    *, source: str, protocol_version: int | None, token: object
) -> str | None:
    if source != "claude-channel":
        if token is not None:
            raise SessionOwnershipError(
                ipc_errors.INVALID_SESSION_SOURCE,
                "channelLeaseToken is only valid for claude-channel sessions",
            )
        return None
    if protocol_version != CHANNEL_PROTOCOL_VERSION:
        return None
    return hashlib.sha256(_lease_token(token).encode()).hexdigest()


def _verify_lease(session: InteractiveSession, token: object) -> None:
    expected = session.channel_lease_digest
    if expected is None:
        raise SessionOwnershipError(
            ipc_errors.CHANNEL_LIVENESS_UNAVAILABLE,
            "channel registration has no operational liveness lease",
        )
    supplied = hashlib.sha256(_lease_token(token).encode()).hexdigest()
    if not hmac.compare_digest(supplied, expected):
        raise SessionOwnershipError(
            ipc_errors.INVALID_CHANNEL_LEASE,
            "channel liveness lease did not match the registered session",
        )


def _session_harness(session: InteractiveSession) -> str:
    runtime = (session.runtime or "").strip()
    if runtime:
        return runtime.split("_", 1)[0]
    source = session.source.strip()
    return source.split("-", 1)[0] if source else "claude"


def _projection(session: InteractiveSession, version: int) -> SessionProjection:
    return SessionProjection(
        version=version,
        actor=session.actor,
        cwd=session.cwd,
        command=session.command,
        source=session.source,
        session_ref=session.session_ref,
        runtime=session.runtime,
        channel_confirmed=session.channel_confirmed,
        channel_build_version=session.channel_build_version,
        channel_protocol_version=session.channel_protocol_version,
        owner_fence=session.owner_fence,
        tmux_session=session.tmux_session,
        channel_lease_backed=session.channel_lease_digest is not None,
        process_pid=session.process_pid,
        process_identity=session.process_identity,
    )


def _publish(sink: object, event: object) -> None:
    publish = getattr(sink, "publish", None)
    if callable(publish):
        publish(event)
        return
    if callable(sink):
        sink(event)
        return
    raise TypeError("event_sink must be callable or expose publish(event)")
