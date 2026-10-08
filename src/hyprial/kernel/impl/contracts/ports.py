from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeVar


class RequestPortError(Exception):
    """A request callback failed with a stable code and optional public data.

    Presentation layers may subclass this without moving formatting or command
    execution into the kernel. The base intentionally keeps Exception (not
    RuntimeError) compatibility for existing presentation exceptions.
    """

    def __init__(self, code: str, message: str, data: object | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


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
