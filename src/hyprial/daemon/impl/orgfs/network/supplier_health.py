"""Shared supplier-sync health across every orgfs space on one daemon.

The hold exists because an unreachable supplier fails in milliseconds, so
retrying at reconcile-tick rate charges a known-doomed attempt to the shared
budget on every tick.  Health is keyed by supplier node, not by space: the
same offline holder otherwise needs three failures per space before any
space holds it.  A success halves the count instead of zeroing it, so a
flapping supplier converges toward the cap instead of resetting forever.
"""

from __future__ import annotations

import time
from threading import Lock

from hyprial.daemon.impl.orgfs.network.protocol import (
    ORGFS_SCHEDULED_SYNC_ATTEMPT_SECONDS,
    ORGFS_SCHEDULED_SYNC_BUDGET_SECONDS,
    ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD,
    ORGFS_SYNC_QUEUE_LIMIT,
    ORGFS_SYNC_RETRY_BACKOFF_SECONDS,
    ORGFS_SUPPLIER_SYNC_HOLD_BASE_SECONDS,
    ORGFS_SUPPLIER_SYNC_HOLD_MAX_SECONDS,
)


class SupplierSyncHealth:
    """Thread-safe failure/hold state shared by all spaces on this node."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._failures: dict[str, int] = {}
        self._hold_until: dict[str, float] = {}

    def failure_count(self, supplier: str) -> int:
        with self._lock:
            return self._failures.get(supplier, 0)

    def hold_remaining(self, supplier: str) -> float:
        with self._lock:
            return max(0.0, self._hold_until.get(supplier, 0.0) - time.monotonic())

    def record_failure(self, supplier: str, reason: str) -> tuple[int, float | None]:
        """Count one failure; return ``(failures, hold_seconds)`` if held.

        ``hold_seconds`` is None below the threshold, so the caller only logs
        a hold when one actually engages.
        """

        del reason
        with self._lock:
            failures = self._failures.get(supplier, 0) + 1
            self._failures[supplier] = failures
            if failures < ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD:
                return failures, None
            exponent = failures - ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD
            hold = min(
                ORGFS_SUPPLIER_SYNC_HOLD_BASE_SECONDS * (2 ** min(exponent, 16)),
                ORGFS_SUPPLIER_SYNC_HOLD_MAX_SECONDS,
            )
            self._hold_until[supplier] = time.monotonic() + hold
            return failures, hold

    def record_success(self, supplier: str) -> int:
        """Halve the failure count; return the new count.

        A hold is dropped only once the decayed count falls below the
        threshold.  Otherwise it is recomputed from the smaller count, so a
        flapping supplier keeps a growing/backed-off hold instead of resetting
        to zero on every success.
        """

        with self._lock:
            previous = self._failures.get(supplier, 0)
            failures = previous // 2
            if failures:
                self._failures[supplier] = failures
            else:
                self._failures.pop(supplier, None)
            if failures < ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD:
                self._hold_until.pop(supplier, None)
            else:
                exponent = failures - ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD
                hold = min(
                    ORGFS_SUPPLIER_SYNC_HOLD_BASE_SECONDS
                    * (2 ** min(exponent, 16)),
                    ORGFS_SUPPLIER_SYNC_HOLD_MAX_SECONDS,
                )
                self._hold_until[supplier] = time.monotonic() + hold
            return failures


class SupplierSyncMixin:
    """Scheduled supplier sync plus global/local failure classification."""

    #: Positive allowlist of proven node-global reachability failures.
    #: ``no-holder-online`` is raised when the supplier does not answer the
    #: sync/log-range/probe query (network/sync.py:177,197,322,371);
    #: TimeoutError/ConnectionError come from the transport query lane
    #: (transport/session_actor.py:511,774,844).  Everything else -- including
    #: unknown-space, not-a-member, authorization, invalid-argument and any
    #: unrecognized code -- is space-local until proven otherwise.
    _GLOBAL_FAILURE_CODES = frozenset({"no-holder-online"})
    _GLOBAL_FAILURE_TYPES = (TimeoutError, ConnectionError)

    def _supplier_failure_is_global(self, error: Exception) -> bool:
        """True only for proven node-global reachability failures."""

        code = getattr(error, "code", None)
        if code in self._GLOBAL_FAILURE_CODES:
            return True
        return isinstance(error, self._GLOBAL_FAILURE_TYPES)

    def schedule_sync_from(self, supplier: str) -> bool:
        """F6: coalesce one bounded anti-entropy job per live peer."""

        dropped_count: int | None = None
        held_for = 0.0
        pending = False
        with self._worker_lock:
            if self._closed:
                return False
            held_for = max(
                self._supplier_sync_health.hold_remaining(supplier),
                self._supplier_sync_local_health.hold_remaining(supplier),
            )
            if held_for <= 0:
                if supplier in self._sync_pending or supplier in self._sync_active:
                    rerun_added = supplier not in self._sync_rerun
                    self._sync_rerun.add(supplier)
                    return rerun_added
                if len(self._sync_pending) >= ORGFS_SYNC_QUEUE_LIMIT:
                    self._sync_dropped += 1
                    dropped_count = self._sync_dropped
                else:
                    self._sync_pending.add(supplier)
                    pending = True
            else:
                self._supplier_sync_held_until_total += 1
                consecutive = max(
                    self._supplier_sync_health.failure_count(supplier),
                    self._supplier_sync_local_health.failure_count(supplier),
                )
        if held_for > 0:
            # A held supplier is not queued: the whole point of the hold is that
            # the next attempt cannot succeed, so charging it to the reconcile
            # budget only delays delivery and harness launch for everybody.
            self._log(
                "warn",
                "orgfs.sync.deferred",
                reason="supplier-hold",
                supplier=supplier,
                spaceId=self.store.space_id,
                holdSeconds=round(held_for, 3),
                consecutiveFailures=consecutive,
            )
            return False
        if dropped_count is not None:
            self._log(
                "warn",
                "orgfs.sync.dropped",
                reason="queue-full",
                supplier=supplier,
                droppedCount=dropped_count,
                queueDepth=ORGFS_SYNC_QUEUE_LIMIT,
                queueLimit=ORGFS_SYNC_QUEUE_LIMIT,
            )
            return False
        if not pending:
            return False
        if not self._submit_worker(self._run_scheduled_sync, supplier):
            with self._worker_lock:
                self._sync_pending.discard(supplier)
            return False
        return True



    def _record_supplier_sync_failure(
        self, supplier: str, reason: str, *, shared: bool = True
    ) -> None:
        """Count one failure and, past the threshold, hold the supplier off.

        The hold exists because an unreachable supplier fails in milliseconds:
        retrying at reconcile-tick rate charges a known-doomed attempt to the
        shared budget every tick.  Holding it turns an unbounded stream of
        failures into a few records plus a growing gap.
        """

        target = (
            self._supplier_sync_health if shared else self._supplier_sync_local_health
        )
        failures, hold = target.record_failure(supplier, reason)
        if hold is None:
            return
        self._log(
            "warn",
            "orgfs.sync.held",
            reason=reason,
            supplier=supplier,
            spaceId=self.store.space_id,
            consecutiveFailures=failures,
            holdSeconds=round(hold, 3),
        )

    def _clear_supplier_sync_failure(self, supplier: str) -> None:
        had = max(
            self._supplier_sync_health.failure_count(supplier),
            self._supplier_sync_local_health.failure_count(supplier),
        )
        self._supplier_sync_health.record_success(supplier)
        self._supplier_sync_local_health.record_success(supplier)
        after = max(
            self._supplier_sync_health.failure_count(supplier),
            self._supplier_sync_local_health.failure_count(supplier),
        )
        if had:
            self._log(
                "info",
                "orgfs.sync.recovered",
                supplier=supplier,
                spaceId=self.store.space_id,
                previousFailures=had,
                afterFailures=had,
                remainingFailures=after,
            )

    def _run_scheduled_sync(self, supplier: str) -> None:
        def run_once() -> None:
            deadline = time.monotonic() + ORGFS_SCHEDULED_SYNC_BUDGET_SECONDS
            delays = (0.0, *ORGFS_SYNC_RETRY_BACKOFF_SECONDS)
            for attempt, delay in enumerate(delays, start=1):
                if delay:
                    time.sleep(delay)
                with self._worker_lock:
                    if self._closed:
                        return
                if not self._supplier_allowed(supplier):
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                try:
                    self.sync_from(
                        supplier,
                        timeout=min(ORGFS_SCHEDULED_SYNC_ATTEMPT_SECONDS, remaining),
                        deadline_monotonic=deadline,
                    )
                    self._clear_supplier_sync_failure(supplier)
                    return
                except Exception as exc:  # noqa: BLE001 - best-effort lane
                    # A non-member supplier never becomes one inside this
                    # budget; retrying only burns the sync window.
                    details = getattr(exc, "details", None) or {}
                    code = getattr(exc, "code", "sync-failed")
                    reason = details.get("reason", code)
                    member_refusal = reason == "supplier-not-member"
                    budget_exhausted = reason == "deadline-expired-mid-sync"
                    retry_scheduled = (
                        attempt < len(delays)
                        and not member_refusal
                        and not budget_exhausted
                        and self._supplier_allowed(supplier)
                        and time.monotonic() + delays[attempt] < deadline
                    )
                    self._log(
                        "warn",
                        "orgfs.sync.failed",
                        reason=reason,
                        code=code,
                        message=str(exc),
                        supplier=supplier,
                        spaceId=self.store.space_id,
                        attempt=attempt,
                        retryScheduled=retry_scheduled,
                        pages=details.get("pages"),
                        requestBytes=details.get("requestBytes"),
                        responseBytes=details.get("responseBytes"),
                        elapsedMs=details.get("elapsedMs"),
                    )
                    if budget_exhausted:
                        self._clear_supplier_sync_failure(supplier)
                    else:
                        self._record_supplier_sync_failure(
                            supplier,
                            code,
                            shared=self._supplier_failure_is_global(exc),
                        )
                    if not retry_scheduled:
                        return

        resubmit = False
        try:
            for run in range(2):
                run_once()
                with self._worker_lock:
                    if run == 0 and supplier in self._sync_rerun:
                        self._sync_rerun.discard(supplier)
                        continue
                break
        finally:
            with self._worker_lock:
                self._sync_pending.discard(supplier)
                resubmit = supplier in self._sync_rerun and not self._closed
                self._sync_rerun.discard(supplier)
        if resubmit:
            self.schedule_sync_from(supplier)


