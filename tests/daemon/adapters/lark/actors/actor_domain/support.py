from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


from hyprial.kernel import LarkGatewayConfig


def _gateway(name: str) -> LarkGatewayConfig:
    return LarkGatewayConfig(
        name=name,
        app_id=f"app-{name}",
        credential_ref=name,
        routes=(),
    )


@dataclass
class _Process:
    pid: int
    readiness: str = "online"
    running: bool = True
    error: str | None = None
    health: dict[str, object] = field(default_factory=dict)
    stopped: bool = False

    def wait_ready(self, timeout: float | None = None) -> str:
        del timeout
        return self.readiness

    def drain_health_events(self) -> tuple[dict[str, object], ...]:
        return ()

    def deliver_reply(self, delivery: object) -> bool:
        del delivery
        return True

    def deliver_alarm(
        self, correlation_id: str, text: str, *, idempotency_key: str
    ) -> bool:
        del correlation_id, text, idempotency_key
        return True

    def stop(self, timeout: float = 5.0) -> None:
        del timeout
        self.running = False
        self.stopped = True


class _Launcher:
    startup_timeout = 1.0

    def __init__(self) -> None:
        self.calls = 0

    def spawn(self, gateway: LarkGatewayConfig) -> _Process:
        del gateway
        self.calls += 1
        return _Process(self.calls)


class _DesiredState:
    def __init__(self) -> None:
        self.active: set[str] = set()
        self.operations: list[tuple[str, str]] = []

    def activate(self, name: str) -> bool:
        self.operations.append(("activate", name))
        changed = name not in self.active
        self.active.add(name)
        return changed

    def deactivate(self, name: str) -> bool:
        self.operations.append(("deactivate", name))
        changed = name in self.active
        self.active.discard(name)
        return changed


class _Events:
    def __init__(self) -> None:
        self.values: list[Any] = []
        self.changed = threading.Event()

    def publish(self, event: Any) -> None:
        self.values.append(event)
        self.changed.set()












































#: Every field the live actor publishes. Asserting the four that went missing
#: would not have caught the other five, so the whole shape is pinned.
_STATUS_SCHEMA = frozenset(
    {
        "id",
        "provider",
        "name",
        "status",
        "online",
        "configured",
        "desired",
        "processRunning",
        "generation",
        "version",
        "knownChatsRecovery",
        "unknownFirstChatRecovery",
        "chatEnumeration",
    }
)




# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    '_DesiredState',
    '_Events',
    '_Launcher',
    '_Process',
    '_STATUS_SCHEMA',
    '_gateway',
]
