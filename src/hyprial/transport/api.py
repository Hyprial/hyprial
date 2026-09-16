"""Public transport seams consumed by daemon and inbox layers.

The protocols deliberately expose bytes rather than protocol objects.  The
``proto`` package owns IRC parsing/serialization; transport only moves frames.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class TransportSample:
    key: str
    payload: bytes
    kind: str = "put"


@runtime_checkable
class Registration(Protocol):
    def close(self) -> None: ...


@runtime_checkable
class TransportSession(Protocol):
    """Minimal Zenoh-shaped API used outside ``hyprial.transport``."""

    def put(self, key: str, payload: bytes) -> None: ...

    def get(
        self,
        key_expr: str,
        *,
        timeout: float = 3.0,
        errors: list[str] | None = None,
        all_replies: bool = False,
    ) -> list[TransportSample]: ...

    def subscribe(
        self, key_expr: str, callback: Callable[[TransportSample], None]
    ) -> Registration: ...

    def declare_queryable(
        self, key_expr: str, handler: Callable[[str], bytes | None]
    ) -> Registration: ...

    def declare_liveliness(self, key: str) -> Registration: ...

    def observe_liveliness(
        self,
        key_expr: str,
        callback: Callable[[TransportSample], None],
        *,
        history: bool = True,
    ) -> Registration: ...

    def close(self) -> None: ...


@runtime_checkable
class PresenceView(Protocol):
    def actor_online(self, actor: str) -> bool: ...

    def online_mailboxes(self) -> tuple[str, ...]: ...
