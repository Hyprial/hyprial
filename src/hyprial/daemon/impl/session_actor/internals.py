"""Session actor internals: cost keys, ownership errors, versions and effect records."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
import hashlib
import hmac
import threading
import uuid
from dataclasses import dataclass
from typing import Protocol
from hyprial.identity import (
    AgentCommand,
    AgentMutationCompleted,
    BindAgentCommand,
    ReleaseAgentCommand,
)
from hyprial.kernel import ipc_errors
from hyprial.kernel import (
    CHANNEL_PROTOCOL_VERSION,
)
from hyprial.kernel import PortAdmission, PortCommandRejected
from hyprial.daemon.impl.desired_state  import (
    InteractiveSession,
    PendingSessionAgentEffect,
)
from hyprial.kernel import parse_agent_uri
from hyprial.daemon.impl.operations.session_ports  import (
    SessionEvent,
    SessionProjection,
    SessionRuntimeProjection,
)
from hyprial.daemon.impl.state_persistence  import StateCostOrigin


_SESSION_COST_KEYS = frozenset(
    {
        "RegisterSessionCommand",
        "RefreshSessionCommand",
        "HeartbeatSessionCommand",
        "UnregisterSessionCommand",
        "SessionLeaseElapsedCommand",
        "LifecycleMutationRequest",
        "_AgentEffectResult",
        "_AgentEffectUnavailable",
    }
)


_EFFECT_ADMISSION_COST_KEYS = frozenset({"bind", "release"})


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


class _SessionProjectionState:
    """One immutable committed session view for caller-side reads."""

    def __init__(self, sessions: tuple[InteractiveSession, ...]) -> None:
        self._lock = threading.Lock()
        self._sessions = sessions
        self._version = 0

    def replace(self, sessions: tuple[InteractiveSession, ...], version: int) -> None:
        with self._lock:
            self._sessions = sessions
            self._version = version

    def read(self) -> tuple[tuple[InteractiveSession, ...], int]:
        with self._lock:
            return self._sessions, self._version


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
    cost_origin: StateCostOrigin


@dataclass(frozen=True, slots=True)
class _ReplayScan:
    submitted: int
    blocked_by_surviving_custody: bool


@dataclass(slots=True)
class _PendingMutation:
    correlation_id: str
    effect_ids: set[str]
    final_events: tuple[SessionEvent, ...]


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


def _publish(sink: object, event: object) -> None:
    publish = getattr(sink, "publish", None)
    if callable(publish):
        publish(event)
        return
    if callable(sink):
        sink(event)
        return
    raise TypeError("event_sink must be callable or expose publish(event)")


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


def _lease_token(value: object) -> str:
    if not isinstance(value, str) or not 32 <= len(value) <= 256:
        raise SessionOwnershipError(
            ipc_errors.INVALID_CHANNEL_LEASE,
            "channelLeaseToken must be an opaque 32-256 character token",
        )
    return value


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
