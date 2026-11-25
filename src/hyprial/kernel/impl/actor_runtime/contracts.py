# Decision record (file header; no inline comments by repository rule):
# - The backend port is one endpoint per cell for the cell's whole life. The
#   guardian owns the mailbox ledger (custody tokens in admission order) and
#   the handler of the current generation; the backend only carries tokens
#   to a single execution thread and asks the guardian, through
#   CellDispatch, which handler runs a token. The mailbox therefore survives
#   a restart (ABI v1 6.3.5) without the backend knowing about generations.
# - The admission trace lets an effect lane learn, without a new public
#   argument on 50 lanes, whether the completion it handed to an owner is
#   still waiting in that owner's mailbox. It is thread-local and only
#   records while a lane has opened it around its own complete() call.
# - I2 keyed actors: ActorSpec carries an optional (group, key) pair and
#   ActorHandle reports it when present. Both halves or neither, non-blank
#   strings, no case folding (validate_actor_key is the single rule, shared
#   with the backends); a keyless spawn keeps the exact pre-key shapes.
# - A keyless ActorHandle keeps the pre-key two-field hash and repr (round 2
#   S1); keyed handles hash and print with all four fields.
from __future__ import annotations

import threading
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
    STOPPING = "stopping"
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


class ActorAdmissionError(Exception):
    """An ask was not admitted; ``args[0]`` is ``"overloaded"`` or ``"closed"``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ActorFailedError(Exception):
    """The asked command failed the actor (a supervised child failure)."""


class ExpectedActorError(Exception):
    """A typed, expected command rejection which must not crash an actor."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ActorHandle:
    """Opaque stable identity; it intentionally contains no backend reference.

    ``group`` and ``key`` are present exactly when the actor was spawned under
    a stable key (I2); keyless handles keep the pre-key two-field hash and
    repr byte-for-byte.
    """

    actor_id: str
    name: str
    group: str | None = None
    key: str | None = None

    def __repr__(self) -> str:
        if self.group is None and self.key is None:
            return f"ActorHandle(actor_id={self.actor_id!r}, name={self.name!r})"
        return (
            f"ActorHandle(actor_id={self.actor_id!r}, name={self.name!r}, "
            f"group={self.group!r}, key={self.key!r})"
        )

    def __hash__(self) -> int:
        if self.group is None and self.key is None:
            return hash((self.actor_id, self.name))
        return hash((self.actor_id, self.name, self.group, self.key))


def validate_actor_key(group: object, key: object) -> tuple[str, str] | None:
    """Validate an optional (group, key) pair; None means a keyless spawn.

    Both halves must be given together, as non-blank strings, without case
    folding; anything else is an explicit error, never silently accepted.
    """

    if group is None and key is None:
        return None
    if group is None or key is None:
        raise ValueError("group and key must be given together")
    if not isinstance(group, str) or not isinstance(key, str):
        raise ValueError("group and key must be strings")
    for label, value in (("group", group), ("key", key)):
        if not value.strip():
            raise ValueError(f"{label} must be a non-blank string")
    return (group, key)


@dataclass(frozen=True, slots=True)
class ActorSpec:
    name: str
    handler_factory: Callable[[], CommandHandler]
    mailbox_capacity: int = 128
    supervision_profile: str = "state_authority"
    undelivered_sink: Callable[[object, str], None] | None = None
    event_sink: Callable[["ActorEvent"], None] | None = None
    group: str | None = None
    key: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("actor name must not be blank")
        if self.mailbox_capacity < 1:
            raise ValueError("mailbox_capacity must be at least 1")
        validate_actor_key(self.group, self.key)


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


class CommandLease(Protocol):
    """One admitted command handed to the backend thread for execution."""

    @property
    def handler(self) -> CommandHandler: ...

    @property
    def command(self) -> object: ...


class CellDispatch(Protocol):
    """Guardian side of one cell, called only from the cell's backend thread."""

    def begin(self, custody_token: str) -> CommandLease | None: ...

    def finish(
        self, lease: CommandLease, result: object, failure: BaseException | None
    ) -> None: ...

    def stopped(self) -> None: ...


class ActorBackend(Protocol):
    """Backend-neutral port retained with the legacy guardian.

    P8 removed the internal Pykka adapter that was its only implementation.
    """

    def open(self, *, handle: ActorHandle, dispatch: CellDispatch) -> object: ...

    def tell(self, endpoint: object, custody_token: str) -> AdmissionResult: ...

    def request_stop(self, endpoint: object) -> None: ...


_ADMISSION_TRACE = threading.local()


def open_admission_trace() -> None:
    """Start recording the last accepted admission made by this thread."""

    _ADMISSION_TRACE.active = True
    _ADMISSION_TRACE.probe = None


def close_admission_trace() -> Callable[[], bool] | None:
    """Stop recording; return a probe that is True while that command is queued."""

    probe = getattr(_ADMISSION_TRACE, "probe", None)
    _ADMISSION_TRACE.active = False
    _ADMISSION_TRACE.probe = None
    return probe


def trace_admission(probe: Callable[[], bool]) -> None:
    """Record an accepted admission when the current thread is tracing."""

    if getattr(_ADMISSION_TRACE, "active", False):
        _ADMISSION_TRACE.probe = probe
