from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Protocol


class AdmissionResult(StrEnum):
    """Result of attempting to hand a command to an actor."""

    ACCEPTED = "accepted"
    OVERLOADED = "overloaded"
    CLOSED = "closed"


class ActorState(StrEnum):
    RUNNING = "running"
    RESTART_PENDING = "restart_pending"
    QUARANTINED = "quarantined"
    STOPPED = "stopped"


class ActorEventKind(StrEnum):
    COMMAND_COMPLETED = "command_completed"
    COMMAND_REJECTED = "command_rejected"
    CHILD_FAILED = "child_failed"
    RESTART_SCHEDULED = "restart_scheduled"
    CHILD_RESTARTED = "child_restarted"
    CHILD_QUARANTINED = "child_quarantined"
    CHILD_STOPPED = "child_stopped"


class CommandHandler(Protocol):
    def __call__(self, command: object) -> object: ...


class ExpectedActorError(Exception):
    """A typed, expected command rejection which must not crash an actor."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ActorHandle:
    """Opaque stable identity; it intentionally contains no backend reference."""

    actor_id: str
    name: str


@dataclass(frozen=True, slots=True)
class ActorSpec:
    name: str
    handler_factory: Callable[[], CommandHandler]
    mailbox_capacity: int = 128
    supervision_profile: str = "state_authority"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("actor name must not be blank")
        if self.mailbox_capacity < 1:
            raise ValueError("mailbox_capacity must be at least 1")


@dataclass(frozen=True, slots=True)
class ActorEvent:
    kind: ActorEventKind
    handle: ActorHandle
    generation: int
    command_type: str | None = None
    code: str | None = None
    detail: str | None = None
    restart_delay: float | None = None


@dataclass(frozen=True, slots=True)
class ActorSnapshot:
    handle: ActorHandle
    state: ActorState
    generation: int
    queued: int
    in_flight: int
    failures_in_window: int


@dataclass(frozen=True, slots=True)
class DrainReport:
    complete: bool
    elapsed: float
    remaining: tuple[ActorHandle, ...]


EventSink = Callable[[ActorEvent], None]
FailureSink = Callable[[str, int, BaseException], None]


class ActorBackend(Protocol):
    """Backend-neutral port implemented only by the internal Pykka adapter."""

    def start(
        self,
        *,
        handle: ActorHandle,
        generation: int,
        handler: CommandHandler,
        mailbox_capacity: int,
        event_sink: EventSink,
        failure_callback: FailureSink,
    ) -> object: ...

    def tell(self, endpoint: object, command: object) -> AdmissionResult: ...

    def close_admission(self, endpoint: object) -> None: ...

    def load(self, endpoint: object) -> tuple[int, int]: ...

    def stop(self, endpoint: object, timeout: float) -> bool: ...
