"""Stamp and publish one source's observe events onto a HookBus.

The bus routes facts; it does not stamp ``seq`` or know a source's identity.
A publisher owns one source's FIFO counter and freezes the event envelope, so
an emitting component (which knows *when* a fact is durable) only supplies the
event name, payload and timestamp.  ``publish`` mirrors ``HookBus.publish``:
non-blocking, best-effort, never runs observer code on the caller's thread.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from typing import Any

from hyprial.kernel.impl.hooks.bus import HookBus
from hyprial.kernel.impl.hooks.events import HookEvent


class HookEventPublisher:
    """Publish one source's observe events; a caller without one is a no-op.

    A component takes an optional publisher.  ``None`` means publish nothing,
    which keeps hooks-off behaviour byte-identical to the pre-hooks code path.
    """

    def __init__(
        self,
        bus: HookBus,
        *,
        source: str,
        clock_ms: Any = None,
    ) -> None:
        if not source.strip():
            raise ValueError("hook publisher source must not be blank")
        self._bus = bus
        self._source = source
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._sequence = 0
        self._lock = threading.Lock()

    @property
    def source(self) -> str:
        return self._source

    def publish(
        self,
        event: str,
        *,
        payload: Mapping[str, Any],
        emitted_at_ms: int | None = None,
        actor: str | None = None,
        graph_id: str | None = None,
    ) -> int:
        """Offer one observe event to its subscribers; return lanes reached."""

        with self._lock:
            sequence = self._sequence
            self._sequence += 1
        at = self._clock_ms() if emitted_at_ms is None else int(emitted_at_ms)
        return self._bus.publish(
            HookEvent(
                event=event,
                kind="observe",
                emitted_at_ms=at,
                seq=sequence,
                source=self._source,
                actor=actor,
                graph_id=graph_id,
                payload=payload,
            )
        )


__all__ = ["HookEventPublisher"]
