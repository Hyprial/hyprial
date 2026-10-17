"""The recap hook consumer: ``turn.*`` events into ``state/recaps.jsonl``.

Design §3 (unified hooks): subscribe to ``turn.completed``/``turn.failed`` and
write ``<home>/state/recaps.jsonl`` exactly as the retired turn-hook service
did - one neutral record per turn, append-only, redaction inherited from the
kernel event payload, never on the delivery pump's thread.  The record stays
schema 1 so existing logs and the ``recentRecaps`` reader keep reading it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from hyprial.biz.impl.hooks.recap.writer import (
    DEFAULT_RECAP_QUEUE_SIZE,
    RecapWrite,
    RecapWriter,
    recent_recaps as read_recent_recaps,
)
from hyprial.kernel import HookEvent

RECAP_EVENTS = frozenset({"turn.completed", "turn.failed"})
_HOME_ERROR_LOG_INTERVAL_SECONDS = 60.0

#: Fields the neutral turn record copies from the kernel event payload.
_RECORD_FIELDS = (
    "deliveryId",
    "sender",
    "conversation",
    "startedAtMs",
    "endedAtMs",
    "status",
    "toolNames",
    "outputExcerpt",
)


class RecapHookConsumer:
    """Consumer registered by the shell as ``recap`` on the daemon bus."""

    def __init__(
        self,
        *,
        home_for_actor: Callable[[str], Path | None],
        logger: Callable[..., None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        queue_size: int = DEFAULT_RECAP_QUEUE_SIZE,
    ) -> None:
        self._home_for_actor = home_for_actor
        self._logger = logger
        self._monotonic = monotonic
        self._home_error_logged_at: dict[str, float] = {}
        self._home_error_lock = threading.Lock()
        self._writer = RecapWriter(
            logger=logger, monotonic=monotonic, queue_size=queue_size
        )

    def observe(self, event: HookEvent, options: Mapping[str, Any]) -> None:
        """One bus event; runs on the consumer lane, never a delivery pump."""

        del options  # recap writing is uniform; no per-entry options yet
        if event.event not in RECAP_EVENTS or event.actor is None:
            return
        home = self._home(event.actor)
        if home is None:
            return
        payload: Mapping[str, Any] = event.payload
        record: dict[str, Any] = {"schemaVersion": 1, "event": "turn"}
        for field in _RECORD_FIELDS:
            record[field] = payload[field]
        self._writer.enqueue(
            RecapWrite(
                home=home,
                value=record,
                actor=event.actor,
                delivery_id=str(payload.get("deliveryId", "")),
            )
        )

    def recent_recaps(self, actor: str, count: int) -> list[object]:
        """Serve recent records for an intercept handler request (design §3)."""

        home = self._home(actor)
        if home is None:
            return []
        return read_recent_recaps(home, count)

    def close(self, timeout: float = 5.0) -> bool:
        """Drain queued writes and stop the writer within ``timeout``."""

        return self._writer.close(timeout)

    @property
    def overflow_count(self) -> int:
        return self._writer.overflow_count

    def _home(self, actor: str) -> Path | None:
        try:
            home = self._home_for_actor(actor)
        except Exception as error:  # noqa: BLE001 - a hook never owns a turn
            self._log_home_error(actor, error)
            return None
        if home is None:
            self._log_home_error(actor, ValueError("agent home is unavailable"))
            return None
        return home

    def _log_home_error(self, actor: str, error: BaseException) -> None:
        now = self._monotonic()
        with self._home_error_lock:
            previous = self._home_error_logged_at.get(actor)
            if previous is not None and now - previous < _HOME_ERROR_LOG_INTERVAL_SECONDS:
                return
            self._home_error_logged_at[actor] = now
        if self._logger is not None:
            self._logger(
                "warn",
                "daemon",
                "hooks.recap_home_error",
                agent=actor,
                errorType=type(error).__name__,
                detail=str(error)[:300],
            )


__all__ = ["RECAP_EVENTS", "RecapHookConsumer"]
