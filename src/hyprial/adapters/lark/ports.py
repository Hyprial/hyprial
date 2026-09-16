from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias

from hyprial.contracts.ports import CommandSink, EventSink, PortCommandRejected


@dataclass(frozen=True, slots=True)
class StartAdapterCommand:
    correlation_id: str
    name: str


@dataclass(frozen=True, slots=True)
class StopAdapterCommand:
    correlation_id: str
    name: str


@dataclass(frozen=True, slots=True)
class ReloadAdaptersCommand:
    correlation_id: str


@dataclass(frozen=True, slots=True)
class PinAdapterCommand:
    correlation_id: str
    name: str
    actor: str


@dataclass(frozen=True, slots=True)
class UnpinAdapterCommand:
    correlation_id: str
    name: str


@dataclass(frozen=True, slots=True)
class AdapterTimerElapsedCommand:
    correlation_id: str
    generation: int
    version: int
    observed_at_ms: int


@dataclass(frozen=True, slots=True)
class DrainAdapterHealthCommand:
    correlation_id: str


@dataclass(frozen=True, slots=True)
class DeliverLarkMessageCommand:
    correlation_id: str
    adapter: str
    chat_id: str
    message_json: bytes


@dataclass(frozen=True, slots=True)
class DeliverLarkAlarmCommand:
    correlation_id: str
    adapter: str
    message_id: str
    text: str
    idempotency_key: str


LarkCommand: TypeAlias = (
    StartAdapterCommand
    | StopAdapterCommand
    | ReloadAdaptersCommand
    | PinAdapterCommand
    | UnpinAdapterCommand
    | AdapterTimerElapsedCommand
    | DrainAdapterHealthCommand
    | DeliverLarkMessageCommand
    | DeliverLarkAlarmCommand
)


@dataclass(frozen=True, slots=True)
class AdapterProjection:
    version: int
    adapter_id: str
    name: str
    status: str
    online: bool
    configured: bool
    desired: bool
    process_running: bool
    pid: int | None = None
    error: str | None = None
    # G3 (fd73140a v2): while a lifecycle transition is in flight, the
    # locking correlation and its age -- "谁锁的、锁了多久".
    lifecycle_correlation_id: str | None = None
    lifecycle_age_seconds: int | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.adapter_id,
            "provider": "lark",
            "name": self.name,
            "status": self.status,
            "online": self.online,
            "configured": self.configured,
            "desired": self.desired,
            "processRunning": self.process_running,
            **({"pid": self.pid} if self.pid is not None else {}),
            **({"error": self.error} if self.error is not None else {}),
            **(
                {"lifecycleCorrelationId": self.lifecycle_correlation_id}
                if self.lifecycle_correlation_id is not None
                else {}
            ),
            **(
                {"lifecycleAgeSeconds": self.lifecycle_age_seconds}
                if self.lifecycle_age_seconds is not None
                else {}
            ),
        }


@dataclass(frozen=True, slots=True)
class AdapterReloadProjection:
    added: tuple[str, ...]
    updated: tuple[str, ...]
    removed: tuple[str, ...]
    removed_running: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "added": list(self.added),
            "updated": list(self.updated),
            "removed": list(self.removed),
            "removedRunning": list(self.removed_running),
        }


@dataclass(frozen=True, slots=True)
class AdaptersProjection:
    adapters: tuple[AdapterProjection, ...]

    def to_payload(self) -> dict[str, object]:
        return {"ok": True, "adapters": [item.to_payload() for item in self.adapters]}


@dataclass(frozen=True, slots=True)
class AdapterItemProjection:
    adapter: AdapterProjection

    def to_payload(self) -> dict[str, object]:
        return {"ok": True, "adapter": self.adapter.to_payload()}


@dataclass(frozen=True, slots=True)
class AdapterPinProjection:
    adapter: str
    actor: str | None
    previous: str | None
    changed: bool
    pins: tuple[tuple[str, str], ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "ok": True,
            "adapter": self.adapter,
            **({"actor": self.actor} if self.actor is not None else {}),
            "previous": self.previous,
            "changed": self.changed,
            "pins": dict(self.pins),
        }


@dataclass(frozen=True, slots=True)
class AdapterPinsProjection:
    pins: tuple[tuple[str, str], ...]

    def to_payload(self) -> dict[str, object]:
        return {"ok": True, "pins": dict(self.pins)}


@dataclass(frozen=True, slots=True)
class AdapterMutationCompleted:
    correlation_id: str
    generation: int
    version: int
    changed: bool
    adapter: AdapterProjection | None = None
    reload: AdapterReloadProjection | None = None
    pin: AdapterPinProjection | None = None


@dataclass(frozen=True, slots=True)
class AdapterIoCompleted:
    correlation_id: str
    generation: int
    version: int
    adapter: str
    operation: str
    succeeded: bool
    external_message_id: str | None = None
    code: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class AdapterHealthEventsCompleted:
    correlation_id: str
    generation: int
    version: int
    events: tuple[tuple[tuple[str, object], ...], ...]


LarkEvent: TypeAlias = (
    AdapterMutationCompleted
    | AdapterIoCompleted
    | AdapterHealthEventsCompleted
    | PortCommandRejected
)
LarkCommandSink: TypeAlias = CommandSink[LarkCommand]
LarkEventSink: TypeAlias = EventSink[LarkEvent]


class LarkProjectionPort(Protocol):
    def read_adapter(self, name: str) -> AdapterProjection | None: ...

    def read_adapters(self) -> tuple[AdapterProjection, ...]: ...

    def read_pins(self) -> tuple[tuple[str, str], ...]: ...
