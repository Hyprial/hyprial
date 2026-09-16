from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeVar


class PortAdmission(StrEnum):
    """Synchronous admission only; mutation results always arrive as events."""

    ACCEPTED = "accepted"
    OVERLOADED = "overloaded"
    CLOSING = "closing"


@dataclass(frozen=True, slots=True)
class PortCommandRejected:
    correlation_id: str
    domain: str
    generation: int
    version: int
    code: str
    detail: str
    admission: PortAdmission | None = None


CommandT = TypeVar("CommandT", contravariant=True)
EventT = TypeVar("EventT", contravariant=True)


class CommandSink(Protocol[CommandT]):
    def submit(self, command: CommandT) -> PortAdmission: ...


class EventSink(Protocol[EventT]):
    def publish(self, event: EventT) -> None: ...
