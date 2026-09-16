from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias

from hyprial.contracts.ports import CommandSink, EventSink, PortCommandRejected


@dataclass(frozen=True, slots=True)
class TurnDeliveryProjection:
    delivery_id: str
    conversation_id: str
    sender: str
    recipient: str
    message: str


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
class CloseTurnPumpCommand:
    correlation_id: str
    deadline_ms: int


TurnCommand: TypeAlias = (
    EnqueueTurnCommand | InterruptTurnCommand | CloseTurnPumpCommand
)


@dataclass(frozen=True, slots=True)
class TurnResultProjection:
    delivery_id: str
    recipient: str
    status: str
    output: str = ""
    error: str | None = None
    failure_code: str | None = None

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
    | TurnPumpClosed
    | PortCommandRejected
)
TurnCommandSink: TypeAlias = CommandSink[TurnCommand]
TurnEventSink: TypeAlias = EventSink[TurnEvent]


class TurnProjectionPort(Protocol):
    def read_in_flight(self) -> TurnDeliveryProjection | None: ...

    def read_results(self) -> tuple[TurnResultProjection, ...]: ...
