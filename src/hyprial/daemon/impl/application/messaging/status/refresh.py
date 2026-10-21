"""Bounded, coalesced refresh owner for status projections.

Status IPC handlers only read this owner's immutable snapshots.  Refresh work
runs on daemon threads and can block or fail without holding an IPC request.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
import queue
import threading
import time
from typing import Any
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class RefreshAdmission:
    operation_id: str
    accepted: bool
    queued: bool
    coalesced: bool


@dataclass(frozen=True, slots=True)
class _RefreshWork:
    target: str
    operation_id: str
    sample: Callable[[], Any]


@dataclass(frozen=True, slots=True)
class _Observation:
    value: Any
    observed_at_ms: int | None
    last_error: str | None
    operation_id: str | None
    refresh_state: str


@dataclass(frozen=True, slots=True)
class _Settlement:
    operation_id: str
    refresh_state: str
    settled_at_ms: int
    last_error: str | None


class CoalescedRefreshAuthority:
    """A generic refresh lane with one outstanding operation per target."""

    def __init__(
        self,
        *,
        capacity: int = 32,
        workers: int = 4,
        stale_after_ms: int = 30_000,
        expire_after_ms: int = 60_000,
        settlement_limit: int = 16,
        settlement_retention_ms: int = 120_000,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if expire_after_ms <= stale_after_ms:
            raise ValueError("expiration must be later than the stale threshold")
        if settlement_limit < 1:
            raise ValueError("settlement limit must be positive")
        if settlement_retention_ms < 1:
            raise ValueError("settlement retention must be positive")
        self._queue: queue.Queue[_RefreshWork] = queue.Queue(capacity)
        self._worker_count = workers
        self._stale_after_ms = stale_after_ms
        self._expire_after_ms = expire_after_ms
        self._settlement_limit = settlement_limit
        self._settlement_retention_ms = settlement_retention_ms
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._lock = threading.Lock()
        self._observations: dict[str, _Observation] = {}
        self._settlements: dict[str, deque[_Settlement]] = {}
        self._outstanding: dict[str, str] = {}
        self._threads: tuple[threading.Thread, ...] = ()
        self._closed = False

    def request(self, target: str, sample: Callable[[], Any]) -> RefreshAdmission:
        with self._lock:
            operation_id = self._outstanding.get(target)
            if operation_id is not None:
                return RefreshAdmission(operation_id, True, True, True)
            if self._closed:
                return RefreshAdmission(
                    f"status-refresh:{uuid4().hex}", False, False, False
                )
            operation_id = f"status-refresh:{uuid4().hex}"
            work = _RefreshWork(target, operation_id, sample)
            try:
                self._queue.put_nowait(work)
            except queue.Full:
                return RefreshAdmission(operation_id, False, False, False)
            self._outstanding[target] = operation_id
            self._ensure_workers_locked()
            return RefreshAdmission(operation_id, True, True, False)

    def _ensure_workers_locked(self) -> None:
        if self._threads:
            return
        threads = tuple(
            threading.Thread(
                target=self._run,
                name=f"status-refresh-{index}",
                daemon=True,
            )
            for index in range(self._worker_count)
        )
        self._threads = threads
        for thread in threads:
            thread.start()

    def _run(self) -> None:
        while True:
            try:
                work = self._queue.get(timeout=0.1)
            except queue.Empty:
                with self._lock:
                    if self._closed:
                        return
                continue
            try:
                try:
                    value = work.sample()
                except BaseException as error:  # keep the effect owner alive
                    self._settle_error(work, error)
                else:
                    self._settle_success(work, value)
            finally:
                self._queue.task_done()

    def _remember_settlement_locked(
        self,
        target: str,
        operation_id: str,
        refresh_state: str,
        settled_at_ms: int,
        last_error: str | None,
    ) -> None:
        self._prune_settlements_locked(target, settled_at_ms)
        settlements = self._settlements.setdefault(
            target, deque(maxlen=self._settlement_limit)
        )
        settlements.append(
            _Settlement(
                operation_id,
                refresh_state,
                settled_at_ms,
                last_error,
            )
        )

    def _prune_settlements_locked(self, target: str, now_ms: int) -> None:
        settlements = self._settlements.get(target)
        if settlements is None:
            return
        while settlements and (
            now_ms - settlements[0].settled_at_ms > self._settlement_retention_ms
        ):
            settlements.popleft()
        if not settlements:
            self._settlements.pop(target, None)

    def _settle_success(self, work: _RefreshWork, value: Any) -> None:
        with self._lock:
            settled_at_ms = self._clock_ms()
            self._observations[work.target] = _Observation(
                value, settled_at_ms, None, work.operation_id, "settled"
            )
            self._remember_settlement_locked(
                work.target, work.operation_id, "settled", settled_at_ms, None
            )
            if self._outstanding.get(work.target) == work.operation_id:
                self._outstanding.pop(work.target, None)

    def _settle_error(self, work: _RefreshWork, error: BaseException) -> None:
        detail = f"{type(error).__name__}: {error}"[:300]
        with self._lock:
            settled_at_ms = self._clock_ms()
            previous = self._observations.get(
                work.target, _Observation(None, None, None, None, "idle")
            )
            self._observations[work.target] = _Observation(
                previous.value,
                previous.observed_at_ms,
                detail,
                work.operation_id,
                "failed",
            )
            self._remember_settlement_locked(
                work.target, work.operation_id, "failed", settled_at_ms, detail
            )
            if self._outstanding.get(work.target) == work.operation_id:
                self._outstanding.pop(work.target, None)

    def record_observation(
        self,
        target: str,
        value: Any,
        *,
        observed_at_ms: int | None = None,
    ) -> None:
        """Record evidence at its arrival time, independently of status reads."""

        with self._lock:
            previous = self._observations.get(
                target, _Observation(None, None, None, None, "idle")
            )
            self._observations[target] = _Observation(
                value,
                self._clock_ms() if observed_at_ms is None else observed_at_ms,
                previous.last_error,
                previous.operation_id,
                previous.refresh_state,
            )

    def targets(self, prefix: str) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                sorted(target for target in self._observations if target.startswith(prefix))
            )

    def snapshot(self, target: str) -> Any:
        with self._lock:
            observation = self._observations.get(target)
            return None if observation is None else observation.value

    def status(
        self, target: str, *, operation_id: str | None = None
    ) -> dict[str, object]:
        now_ms = self._clock_ms()
        with self._lock:
            observation = self._observations.get(
                target, _Observation(None, None, None, None, "idle")
            )
            outstanding_operation_id = self._outstanding.get(target)
            refresh_operation_id = outstanding_operation_id or observation.operation_id
            refresh_state = (
                "queued"
                if outstanding_operation_id is not None
                else observation.refresh_state
            )
            last_error = observation.last_error
            if operation_id is not None:
                refresh_operation_id = operation_id
                if outstanding_operation_id == operation_id:
                    refresh_state = "queued"
                    last_error = None
                else:
                    self._prune_settlements_locked(target, now_ms)
                    settlement = next(
                        (
                            item
                            for item in reversed(self._settlements.get(target, ()))
                            if item.operation_id == operation_id
                        ),
                        None,
                    )
                    if settlement is None:
                        refresh_state = "unknown-expired"
                        last_error = None
                    else:
                        refresh_state = settlement.refresh_state
                        last_error = settlement.last_error
                observation = _Observation(
                    observation.value,
                    observation.observed_at_ms,
                    last_error,
                    observation.operation_id,
                    observation.refresh_state,
                )
        observed_at_ms = observation.observed_at_ms
        age_ms = (
            None if observed_at_ms is None else max(0, now_ms - observed_at_ms)
        )
        return {
            "observedAtMs": observed_at_ms,
            "ageMs": age_ms,
            "stale": age_ms is None or age_ms > self._stale_after_ms,
            "expired": age_ms is None or age_ms > self._expire_after_ms,
            "lastError": observation.last_error,
            "refreshQueued": outstanding_operation_id is not None,
            "refreshOperationId": refresh_operation_id,
            "refreshState": refresh_state,
        }

    def close(self, timeout: float = 1.0) -> bool:
        with self._lock:
            if self._closed:
                threads = self._threads
            else:
                self._closed = True
                threads = self._threads
        deadline = time.monotonic() + timeout
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in threads)


def sample_status_projection(application: Any) -> dict[str, object]:
    """Sample possibly blocking status sources from the refresh owner only."""

    authority = getattr(application, "_transport", None)
    health = (
        getattr(authority, "session_health", None)
        if authority is not None
        else None
    )
    session: dict[str, object] = {"state": "unknown"}
    if callable(health):
        session = dict(health())
    application._trace_presence_snapshot(None)
    return {"zenohSession": session}


def request_status_refresh(application: Any) -> RefreshAdmission:
    return application._status_refresh.request(
        "daemon-status", lambda: sample_status_projection(application)
    )


def status_refresh_fields(
    application: Any, operation_id: str | None = None
) -> dict[str, object]:
    return application._status_refresh.status(
        "daemon-status", operation_id=operation_id
    )


def zenoh_session_status(
    application: Any, operation_id: str | None = None
) -> dict[str, object]:
    snapshot = application._status_refresh.snapshot("daemon-status")
    session = snapshot.get("zenohSession") if isinstance(snapshot, dict) else None
    result = dict(session) if isinstance(session, dict) else {"state": "unknown"}
    result.update(status_refresh_fields(application, operation_id))
    return result


def storage_health_status(application: Any) -> dict[str, object]:
    report = (
        dict(application._database_health.status())
        if application._database_health is not None
        else {"healthy": None, "checkedAtMs": None}
    )
    checked_at_ms = report.get("checkedAtMs")
    observed_at_ms = checked_at_ms if isinstance(checked_at_ms, int) else None
    age_ms = (
        None
        if observed_at_ms is None
        else max(0, int(time.time_ns() // 1_000_000) - observed_at_ms)
    )
    failures = report.get("failures")
    last_error = report.get("heartbeatError")
    if last_error is None and isinstance(failures, list) and failures:
        first = failures[0]
        if isinstance(first, dict):
            last_error = first.get("errorType")
    report.update(
        observedAtMs=observed_at_ms,
        ageMs=age_ms,
        stale=age_ms is None or age_ms > 60_000,
        lastError=last_error,
    )
    return report


def refresh_response_fields(application: Any, params: Any) -> dict[str, object]:
    if params.get("refresh") is not True:
        if params.get("refreshTailnetStatus") is not True:
            return {}
        return {
            "refresh": {
                "accepted": False,
                "queued": False,
                "operationId": None,
                "coalesced": False,
                "reason": "TAILNET_STATUS_PRODUCER_UNAVAILABLE",
            }
        }
    admission = request_status_refresh(application)
    return {
        "refresh": {
            "accepted": admission.accepted,
            "queued": admission.queued,
            "operationId": admission.operation_id,
            "coalesced": admission.coalesced,
        }
    }
