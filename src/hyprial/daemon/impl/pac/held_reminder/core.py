"""The held-reminder cadence: one PAC-owned timer on the kernel scheduler.

The scheduler is borrowed, not extended: this class schedules a single key and
reschedules itself, exactly as the maintenance cadence does.  Nothing in the
kernel or the HookBus learns that PAC exists.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import sqlite3
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hyprial.kernel import GenerationScheduler

from hyprial.daemon.impl.pac.held_reminder.detection import (
    plan_held_notice,
    record_held_notices,
    reminder_notice_at,
    scan_held_graphs,
)
from hyprial.daemon.impl.pac.held_reminder.types import (
    HELD_REMINDER_KEY,
    HeldReminderConfig,
)
from hyprial.daemon.impl.pac.storage.store import PacGraphStore

CreatorExists = Callable[[str], bool]


class HeldReminder:
    """Remind graph creators about held graphs nobody has acted on.

    Detection and delivery are durable; the only in-memory state is the timer
    generation, which is fenced so a stale callback cannot double-fire.
    """

    def __init__(
        self,
        *,
        database: Path,
        owner: str,
        config: HeldReminderConfig | None = None,
        scheduler: GenerationScheduler | None = None,
        creator_exists: CreatorExists | None = None,
        operator_route: str | None = None,
        wake_delivery: Callable[[], None] | None = None,
        clock_ms: Callable[[], int] | None = None,
        logger: Any = None,
    ) -> None:
        self.database = Path(database)
        self.owner = owner
        self.config = config or HeldReminderConfig()
        self._scheduler = scheduler
        self._creator_exists = creator_exists
        self._operator_route = operator_route
        self._wake_delivery = wake_delivery
        self._clock = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._logger = logger
        self._generation = 0
        self._closed = False

    # -- cadence ---------------------------------------------------------------

    def start_after_restore(self) -> list[str]:
        """Run once now (PAC is restored) and then own an hourly timer."""

        if not self.config.enabled or self._closed:
            return []
        recorded = self._safe_run()
        self._generation += 1
        self._schedule(self._generation, self.config.interval_seconds)
        return recorded

    def _schedule(self, generation: int, delay: float) -> None:
        if self._closed or self._scheduler is None:
            return
        self._scheduler.schedule(
            HELD_REMINDER_KEY, generation, delay, self._on_timer
        )

    def _on_timer(self, generation: int) -> None:
        if self._closed or generation != self._generation:
            return
        try:
            self._safe_run()
        finally:
            self._generation += 1
            self._schedule(self._generation, self.config.interval_seconds)

    def close(self) -> None:
        self._closed = True
        if self._scheduler is not None:
            self._scheduler.cancel(HELD_REMINDER_KEY)

    # -- one pass --------------------------------------------------------------

    def _safe_run(self) -> list[str]:
        try:
            return self.run_once()
        except (sqlite3.Error, OSError, RuntimeError) as error:
            if self._logger:
                self._logger(
                    "warn",
                    "pac",
                    "workflow.held_reminder_failed",
                    errorType=type(error).__name__,
                    detail=str(error)[:500],
                )
            return []

    def run_once(self) -> list[str]:
        """Record one reminder per held-overdue graph and wake delivery."""

        if not self.config.enabled or self._closed:
            return []
        if not self.database.exists():
            return []
        now_ms = int(self._clock())
        store = PacGraphStore(self.database)
        try:
            notices = self._plan(store, now_ms)
            if not notices:
                return []
            recorded = record_held_notices(store, notices, now_ms=now_ms)
        finally:
            store.close()
        if recorded and self._wake_delivery is not None:
            try:
                self._wake_delivery()
            except Exception as error:  # noqa: BLE001 - the row is already durable
                if self._logger:
                    self._logger(
                        "warn",
                        "pac",
                        "workflow.held_reminder_wake_failed",
                        errorType=type(error).__name__,
                        detail=str(error)[:500],
                    )
        return recorded

    def _plan(self, store: PacGraphStore, now_ms: int) -> list:
        notices = []
        for held in scan_held_graphs(
            store._db,
            now_ms=now_ms,
            threshold_ms=self.config.threshold_ms,
        ):
            previous = reminder_notice_at(store._db, held.graph_id)
            if previous is not None and now_ms - previous < self.config.repeat_window_ms:
                continue
            creator_missing = self._creator_missing(held.created_by)
            notices.append(
                plan_held_notice(
                    held,
                    now_ms=now_ms,
                    repeat_window_ms=self.config.repeat_window_ms,
                    threshold_ms=self.config.threshold_ms,
                    creator_missing=creator_missing,
                    operator_route=self._operator_route,
                )
            )
        return notices

    def _creator_missing(self, creator: str) -> bool:
        if self._creator_exists is None:
            return False
        try:
            return not bool(self._creator_exists(creator))
        except Exception:  # noqa: BLE001 - unprovable absence is not absence
            return False


__all__ = ["HeldReminder"]
