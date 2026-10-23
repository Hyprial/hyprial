from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias

from hyprial.kernel import CommandSink, EventSink, PortCommandRejected


@dataclass(frozen=True, slots=True)
class TurnDeliveryProjection:
    delivery_id: str
    conversation_id: str
    sender: str
    recipient: str
    message: str
    hook_text: str | None = None
    hook_request: bool = False
    notice: bool = False
    #: Set when ``message`` is a complete ``inbox-batch-v1`` rendering; the
    #: final model boundary passes it verbatim instead of adding the scalar
    #: delivery header.  ``message_ids`` are the ordered members it claims.
    batch_cursor: str | None = None
    message_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EnqueueTurnCommand:
    correlation_id: str
    delivery: TurnDeliveryProjection


@dataclass(frozen=True, slots=True)
class InterruptTurnCommand:
    correlation_id: str
    delivery_id: str
    deadline_ms: int


@dataclass(frozen=True, slots=True)
class SteerTurnCommand:
    """Deliver a QUEUED delivery into the turn that is running right now.

    Carries only the queued delivery id; the message text stays in the
    pending command the runtime already owns, so the steering path never
    mints a second copy of message custody.  ``close`` rejects rather than
    queues: a steer with no running turn has nothing to steer.
    """

    correlation_id: str
    delivery_id: str


@dataclass(frozen=True, slots=True)
class CloseTurnPumpCommand:
    correlation_id: str
    deadline_ms: int


TurnCommand: TypeAlias = (
    EnqueueTurnCommand
    | InterruptTurnCommand
    | SteerTurnCommand
    | CloseTurnPumpCommand
)


@dataclass(frozen=True, slots=True)
class TurnResultProjection:
    delivery_id: str
    recipient: str
    status: str
    output: str = ""
    error: str | None = None
    failure_code: str | None = None
    message_ids: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "deliveryId": self.delivery_id,
            "recipient": self.recipient,
            "status": self.status,
            "output": self.output,
            **({"error": self.error} if self.error is not None else {}),
            **(
                {"failureCode": self.failure_code}
                if self.failure_code is not None
                else {}
            ),
        }


@dataclass(frozen=True, slots=True)
class TurnStarted:
    correlation_id: str
    generation: int
    version: int
    delivery_id: str
    delivery: TurnDeliveryProjection
    emitted_at_ms: int


@dataclass(frozen=True, slots=True)
class TurnProgressObserved:
    correlation_id: str
    generation: int
    version: int
    delivery_id: str
    sequence: int
    phase: str
    summary: str
    detail_json: bytes | None = None


@dataclass(frozen=True, slots=True)
class TurnIoCompleted:
    correlation_id: str
    generation: int
    version: int
    result: TurnResultProjection


@dataclass(frozen=True, slots=True)
class TurnInterruptIoCompleted:
    correlation_id: str
    generation: int
    version: int
    delivery_id: str
    interrupted: bool
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class TurnSteerIoCompleted:
    """One queued delivery's steer outcome, keyed to the queued delivery.

    ``accepted`` is the harness's native acceptance only, never proof the
    model consumed the text.  ``consumed`` is true when the native mechanism
    folded the message INTO the running turn (the queued delivery must not
    run again); it is false for an advance mechanism (Claude: interrupt the
    running turn so the queued delivery starts next) where the queued
    delivery stays pending.
    """

    correlation_id: str
    generation: int
    version: int
    delivery_id: str
    accepted: bool
    consumed: bool
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class TurnPumpClosed:
    correlation_id: str
    generation: int
    version: int
    remaining_delivery_id: str | None = None


TurnEvent: TypeAlias = (
    TurnStarted
    | TurnProgressObserved
    | TurnIoCompleted
    | TurnInterruptIoCompleted
    | TurnSteerIoCompleted
    | TurnPumpClosed
    | PortCommandRejected
)
TurnCommandSink: TypeAlias = CommandSink[TurnCommand]
TurnEventSink: TypeAlias = EventSink[TurnEvent]


class TurnProjectionPort(Protocol):
    def read_in_flight(self) -> TurnDeliveryProjection | None: ...

    def read_results(self) -> tuple[TurnResultProjection, ...]: ...
