from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias

from hyprial.contracts.ports import CommandSink, EventSink, PortCommandRejected


@dataclass(frozen=True, slots=True)
class RegisterSessionCommand:
    correlation_id: str
    actor: str
    cwd: str
    command: tuple[str, ...]
    source: str
    session_ref: str
    runtime: str = "claude_interactive"
    channel_confirmed: bool = False
    channel_build_version: str | None = None
    channel_protocol_version: int | None = None
    owner_fence: bool | None = None
    channel_lease_token: str | None = None
    tmux_session: str | None = None
    process_pid: int | None = None
    process_identity: str | None = None
    manage_agent: bool = True


@dataclass(frozen=True, slots=True)
class RefreshSessionCommand:
    correlation_id: str
    actor: str
    session_ref: str
    channel_lease_token: str | None = None
    manage_agent: bool = True


@dataclass(frozen=True, slots=True)
class HeartbeatSessionCommand:
    correlation_id: str
    actor: str
    session_ref: str
    channel_lease_token: str | None = None
    manage_agent: bool = True


@dataclass(frozen=True, slots=True)
class UnregisterSessionCommand:
    correlation_id: str
    actor: str
    session_ref: str
    manage_agent: bool = True


@dataclass(frozen=True, slots=True)
class SessionLeaseElapsedCommand:
    correlation_id: str
    generation: int
    version: int
    observed_at_ms: int


SessionCommand: TypeAlias = (
    RegisterSessionCommand
    | RefreshSessionCommand
    | HeartbeatSessionCommand
    | UnregisterSessionCommand
    | SessionLeaseElapsedCommand
)


@dataclass(frozen=True, slots=True)
class SessionProjection:
    version: int
    actor: str
    cwd: str
    command: tuple[str, ...]
    source: str
    session_ref: str | None = None
    runtime: str | None = None
    channel_confirmed: bool = False
    channel_build_version: str | None = None
    channel_protocol_version: int | None = None
    owner_fence: bool | None = None
    tmux_session: str | None = None
    channel_lease_backed: bool = False
    process_pid: int | None = None
    process_identity: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "actor": self.actor,
            "cwd": self.cwd,
            "command": list(self.command),
            "source": self.source,
            **(
                {"sessionRef": self.session_ref} if self.session_ref is not None else {}
            ),
            **({"runtime": self.runtime} if self.runtime is not None else {}),
            **({"channelConfirmed": True} if self.channel_confirmed else {}),
            **(
                {"channelBuildVersion": self.channel_build_version}
                if self.channel_build_version is not None
                else {}
            ),
            **(
                {"channelProtocolVersion": self.channel_protocol_version}
                if self.channel_protocol_version is not None
                else {}
            ),
            **(
                {"ownerFence": self.owner_fence} if self.owner_fence is not None else {}
            ),
            **(
                {"tmuxSession": self.tmux_session}
                if self.tmux_session is not None
                else {}
            ),
            **(
                {"processPid": self.process_pid}
                if self.process_pid is not None
                else {}
            ),
        }


@dataclass(frozen=True, slots=True)
class SessionRuntimeProjection:
    actor: str
    session_ref: str | None
    current_epoch: str | None
    last_heartbeat_ms: int | None
    alive: bool


@dataclass(frozen=True, slots=True)
class HarnessHandoverProjection:
    actor: str
    previous_harness: str
    previous_session_id: str | None
    next_harness: str
    notice: str

    def to_payload(self) -> dict[str, object]:
        return {
            "actor": self.actor,
            "previousHarness": self.previous_harness,
            "previousSessionId": self.previous_session_id,
            "nextHarness": self.next_harness,
            "contextCarriedOver": False,
            "notice": self.notice,
        }


@dataclass(frozen=True, slots=True)
class SessionMutationProjection:
    actor: str
    session_ref: str | None
    daemon_epoch: str | None
    registered: bool = False
    refreshed: bool = False
    alive: bool = False
    unregistered: bool = False
    channel_current_epoch: str | None = None
    harness_handover: HarnessHandoverProjection | None = None
    superseded_actors: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "ok": True,
            **({"registered": True} if self.registered else {}),
            **({"refreshed": True} if self.refreshed else {}),
            **({"alive": True} if self.alive else {}),
            "unregistered": self.unregistered,
            "actor": self.actor,
            **(
                {"sessionRef": self.session_ref} if self.session_ref is not None else {}
            ),
            **(
                {
                    "channelCurrentThisGeneration": True,
                    "channelCurrentEpoch": self.channel_current_epoch,
                }
                if self.channel_current_epoch is not None
                else {}
            ),
            **(
                {"daemonEpoch": self.daemon_epoch}
                if self.daemon_epoch is not None
                else {}
            ),
            **(
                {"harnessHandover": self.harness_handover.to_payload()}
                if self.harness_handover is not None
                else {}
            ),
            **(
                {"supersededActors": list(self.superseded_actors)}
                if self.superseded_actors
                else {}
            ),
        }


@dataclass(frozen=True, slots=True)
class SessionMutationCompleted:
    correlation_id: str
    generation: int
    version: int
    result: SessionMutationProjection


@dataclass(frozen=True, slots=True)
class SessionLeaseExpired:
    correlation_id: str
    generation: int
    version: int
    actor: str
    session_ref: str


@dataclass(frozen=True, slots=True)
class SessionLeaseSweepCompleted:
    correlation_id: str
    generation: int
    version: int
    sessions_checked: int


SessionEvent: TypeAlias = (
    SessionMutationCompleted
    | SessionLeaseExpired
    | SessionLeaseSweepCompleted
    | PortCommandRejected
)
SessionCommandSink: TypeAlias = CommandSink[SessionCommand]
SessionEventSink: TypeAlias = EventSink[SessionEvent]


class SessionProjectionPort(Protocol):
    def read_session(self, actor: str) -> SessionProjection | None: ...

    def read_sessions(self) -> tuple[SessionProjection, ...]: ...

    def read_runtime(self, actor: str) -> SessionRuntimeProjection | None: ...
