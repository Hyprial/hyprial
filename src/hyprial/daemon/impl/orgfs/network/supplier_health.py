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
from dataclasses import dataclass
from functools import partial
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


@dataclass(slots=True)
class _SupplierState:
    failures: int = 0
    hold_started: float = 0.0
    hold_until: float = 0.0
    deferred_count: int = 0
    probe_token: int | None = None
    next_probe_token: int = 0


@dataclass(frozen=True, slots=True)
class _SyncAdmission:
    allowed: bool
    remaining: float = 0.0
    reason: str | None = None
    probe_token: int | None = None


@dataclass(frozen=True, slots=True)
class _DeferredSummary:
    suppressed_count: int
    hold_seconds: float
    consecutive_failures: int


@dataclass(frozen=True, slots=True)
class _FailureOutcome:
    failures: int
    hold_seconds: float | None
    hold_started: bool
    deferred: _DeferredSummary | None


@dataclass(frozen=True, slots=True)
class _SuccessOutcome:
    failures: int
    deferred: _DeferredSummary | None


class SupplierSyncHealth:
    """Thread-safe failure/hold state shared by all spaces on this node."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._states: dict[str, _SupplierState] = {}

    def failure_count(self, supplier: str) -> int:
        with self._lock:
            state = self._states.get(supplier)
            return state.failures if state is not None else 0

    def hold_remaining(self, supplier: str) -> float:
        with self._lock:
            state = self._states.get(supplier)
            return (
                max(0.0, state.hold_until - time.monotonic())
                if state is not None
                else 0.0
            )

    def admit(self, supplier: str) -> _SyncAdmission:
        """Atomically admit at most one probe after a shared hold expires."""

        now = time.monotonic()
        with self._lock:
            state = self._states.get(supplier)
            if (
                state is None
                or state.failures < ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD
            ):
                return _SyncAdmission(True)
            remaining = state.hold_until - now
            if remaining > 0:
                state.deferred_count += 1
                return _SyncAdmission(False, remaining, "supplier-hold")
            if state.probe_token is not None:
                state.deferred_count += 1
                return _SyncAdmission(False, 0.0, "supplier-probe")
            state.next_probe_token += 1
            state.probe_token = state.next_probe_token
            return _SyncAdmission(True, probe_token=state.probe_token)

    def cancel_admission(self, supplier: str, probe_token: int | None) -> None:
        """Release a probe whose worker could not be submitted."""

        if probe_token is None:
            return
        with self._lock:
            state = self._states.get(supplier)
            if state is not None and state.probe_token == probe_token:
                state.probe_token = None

    @staticmethod
    def _hold_seconds(failures: int) -> float:
        exponent = failures - ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD
        return min(
            ORGFS_SUPPLIER_SYNC_HOLD_BASE_SECONDS * (2 ** min(exponent, 16)),
            ORGFS_SUPPLIER_SYNC_HOLD_MAX_SECONDS,
        )

    @staticmethod
    def _take_deferred(
        state: _SupplierState, now: float
    ) -> _DeferredSummary | None:
        if state.deferred_count <= 0:
            return None
        summary = _DeferredSummary(
            suppressed_count=state.deferred_count,
            hold_seconds=max(0.0, now - state.hold_started),
            consecutive_failures=state.failures,
        )
        state.deferred_count = 0
        return summary

    def record_failure_detail(
        self,
        supplier: str,
        reason: str,
        *,
        probe_token: int | None = None,
    ) -> _FailureOutcome:
        del reason
        now = time.monotonic()
        with self._lock:
            state = self._states.setdefault(supplier, _SupplierState())
            settling_probe = state.probe_token is not None and (
                probe_token is None or probe_token == state.probe_token
            )
            deferred = self._take_deferred(state, now) if settling_probe else None
            if settling_probe:
                state.probe_token = None
            was_held = state.hold_until > now and not settling_probe
            state.failures += 1
            if state.failures < ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD:
                return _FailureOutcome(state.failures, None, False, deferred)
            hold = self._hold_seconds(state.failures)
            state.hold_until = now + hold
            if not was_held:
                state.hold_started = now
            return _FailureOutcome(state.failures, hold, not was_held, deferred)

    def record_failure(self, supplier: str, reason: str) -> tuple[int, float | None]:
        """Compatibility surface returning the failure count and hold length."""

        outcome = self.record_failure_detail(supplier, reason)
        return outcome.failures, outcome.hold_seconds

    def record_success_detail(
        self, supplier: str, *, probe_token: int | None = None
    ) -> _SuccessOutcome:
        """Decay failures and settle the current aggregated hold window."""

        now = time.monotonic()
        with self._lock:
            state = self._states.get(supplier)
            if state is None:
                return _SuccessOutcome(0, None)
            settling_probe = state.probe_token is not None and (
                probe_token is None or probe_token == state.probe_token
            )
            deferred = self._take_deferred(state, now)
            if settling_probe:
                state.probe_token = None
            state.failures //= 2
            if state.failures == 0:
                self._states.pop(supplier, None)
            elif state.failures < ORGFS_SUPPLIER_SYNC_FAILURE_THRESHOLD:
                state.hold_started = 0.0
                state.hold_until = 0.0
            else:
                hold = self._hold_seconds(state.failures)
                state.hold_started = now
                state.hold_until = now + hold
            return _SuccessOutcome(state.failures, deferred)

    def record_success(self, supplier: str) -> int:
        """Halve the failure count; return the new count."""

        return self.record_success_detail(supplier).failures


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
        pending = False
        probe_token: int | None = None
        local_remaining = self._supplier_sync_local_health.hold_remaining(supplier)
        if local_remaining > 0:
            self._supplier_sync_local_health.admit(supplier)
            with self._worker_lock:
                self._supplier_sync_held_until_total += 1
            return False
        shared_admission = self._supplier_sync_health.admit(supplier)
        if not shared_admission.allowed:
            with self._worker_lock:
                self._supplier_sync_held_until_total += 1
            return False
        probe_token = shared_admission.probe_token
        with self._worker_lock:
            if self._closed:
                self._supplier_sync_health.cancel_admission(supplier, probe_token)
                return False
            if supplier in self._sync_pending or supplier in self._sync_active:
                self._supplier_sync_health.cancel_admission(supplier, probe_token)
                rerun_added = supplier not in self._sync_rerun
                self._sync_rerun.add(supplier)
                return rerun_added
            if len(self._sync_pending) >= ORGFS_SYNC_QUEUE_LIMIT:
                self._supplier_sync_health.cancel_admission(supplier, probe_token)
                self._sync_dropped += 1
                dropped_count = self._sync_dropped
            else:
                self._sync_pending.add(supplier)
                pending = True
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
        operation = (
            self._run_scheduled_sync
            if probe_token is None
            else partial(self._run_scheduled_sync, probe_token=probe_token)
        )
        if not self._submit_worker(operation, supplier):
            self._supplier_sync_health.cancel_admission(supplier, probe_token)
            with self._worker_lock:
                self._sync_pending.discard(supplier)
            return False
        return True



    def _log_deferred_summary(
        self, supplier: str, summary: _DeferredSummary | None
    ) -> None:
        if summary is None:
            return
        self._log(
            "warn",
            "orgfs.sync.deferred",
            reason="supplier-hold",
            supplier=supplier,
            spaceId=self.store.space_id,
            holdSeconds=round(summary.hold_seconds, 3),
            consecutiveFailures=summary.consecutive_failures,
            suppressedCount=summary.suppressed_count,
        )

    def _record_supplier_sync_failure(
        self,
        supplier: str,
        reason: str,
        *,
        shared: bool = True,
        probe_token: int | None = None,
    ) -> bool:
        """Count a failure and return whether a supplier hold is now active."""

        target = (
            self._supplier_sync_health if shared else self._supplier_sync_local_health
        )
        outcome = target.record_failure_detail(
            supplier, reason, probe_token=probe_token
        )
        self._log_deferred_summary(supplier, outcome.deferred)
        if outcome.hold_seconds is None:
            return False
        if outcome.hold_started:
            self._log(
                "warn",
                "orgfs.sync.held",
                reason=reason,
                supplier=supplier,
                spaceId=self.store.space_id,
                consecutiveFailures=outcome.failures,
                holdSeconds=round(outcome.hold_seconds, 3),
            )
        return True

    def _settle_shared_supplier_success(
        self, supplier: str, *, probe_token: int | None = None
    ) -> _SuccessOutcome:
        outcome = self._supplier_sync_health.record_success_detail(
            supplier, probe_token=probe_token
        )
        self._log_deferred_summary(supplier, outcome.deferred)
        return outcome

    def _clear_supplier_sync_failure(
        self, supplier: str, *, probe_token: int | None = None
    ) -> None:
        had = max(
            self._supplier_sync_health.failure_count(supplier),
            self._supplier_sync_local_health.failure_count(supplier),
        )
        shared = self._settle_shared_supplier_success(
            supplier, probe_token=probe_token
        )
        local = self._supplier_sync_local_health.record_success_detail(supplier)
        self._log_deferred_summary(supplier, local.deferred)
        after = max(shared.failures, local.failures)
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

    def _run_scheduled_sync(
        self, supplier: str, *, probe_token: int | None = None
    ) -> None:
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
                    self._record_supplier_sync_failure(
                        supplier,
                        "supplier-offline",
                        probe_token=probe_token,
                    )
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
                    self._clear_supplier_sync_failure(
                        supplier, probe_token=probe_token
                    )
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
                    if budget_exhausted:
                        self._clear_supplier_sync_failure(
                            supplier, probe_token=probe_token
                        )
                        held = False
                    else:
                        shared = self._supplier_failure_is_global(exc)
                        if not shared:
                            self._settle_shared_supplier_success(
                                supplier, probe_token=probe_token
                            )
                        held = self._record_supplier_sync_failure(
                            supplier,
                            code,
                            shared=shared,
                            probe_token=probe_token if shared else None,
                        )
                    retry_scheduled = retry_scheduled and not held
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
                    if not retry_scheduled:
                        return

        resubmit = False
        try:
            for run in range(2):
                run_once()
                with self._worker_lock:
                    rerun = run == 0 and supplier in self._sync_rerun
                    if rerun:
                        self._sync_rerun.discard(supplier)
                if rerun and max(
                    self._supplier_sync_health.hold_remaining(supplier),
                    self._supplier_sync_local_health.hold_remaining(supplier),
                ) <= 0:
                    continue
                break
        finally:
            self._supplier_sync_health.cancel_admission(supplier, probe_token)
            with self._worker_lock:
                self._sync_pending.discard(supplier)
                resubmit = supplier in self._sync_rerun and not self._closed
                self._sync_rerun.discard(supplier)
        if resubmit:
            self.schedule_sync_from(supplier)


