"""Callback and query worker dispatch lanes with overflow accounting."""
from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


from hyprial.kernel import AdmissionResult

from hyprial.daemon.impl.transport.api  import TransportSample
from hyprial.daemon.impl.transport.zenoh.config import (
    CALLBACK_LANES,
    OVERFLOW_LOG_EVERY,
    QUERY_WORKERS,
)

logger = logging.getLogger(__name__)

class _CallbackDispatcher:
    """Runs subscriber callbacks off the thread zenoh delivers on.

    zenoh-python's default indirect callback runs one ``pyo3-closure`` thread
    per declaration, parked in an unbounded Rust receive.  Direct callbacks
    avoid that receive and do only a non-blocking hand-off here.  Each
    subscription is pinned to one lane, preserving its order without making
    thread count scale with declaration count.
    """

    def __init__(self, capacity: int, lanes: int = CALLBACK_LANES) -> None:
        self._capacity = capacity
        self._lanes: list[queue.Queue[Any]] = [
            queue.Queue(maxsize=capacity) for _ in range(lanes)
        ]
        self._threads: list[threading.Thread | None] = [None] * lanes
        self._next_lane = itertools.count()
        self._dropped: dict[str, int] = {}
        self._guard = threading.Lock()
        self._stopped = False

    def wrap(
        self, key_expr: str, callback: Callable[[TransportSample], None]
    ) -> Callable[[Any], None]:
        index = next(self._next_lane) % len(self._lanes)
        lane = self._lanes[index]

        def deliver(sample: Any) -> None:
            if self._stopped:
                return
            self._ensure_worker(index)
            try:
                lane.put_nowait((key_expr, callback, _sample(sample)))
            except queue.Full:
                self._overflow(key_expr)

        return deliver

    def _ensure_worker(self, index: int) -> None:
        if self._threads[index] is not None:
            return
        with self._guard:
            if self._threads[index] is None:
                worker = threading.Thread(
                    target=self._drain,
                    args=(self._lanes[index],),
                    name=f"hyprial-zenoh-callback-{index}",
                    daemon=True,
                )
                self._threads[index] = worker
                worker.start()

    def _drain(self, lane: queue.Queue[Any]) -> None:
        while True:
            item = lane.get()
            if item is None:
                return
            key_expr, callback, sample = item
            try:
                callback(sample)
            except Exception:  # noqa: BLE001 - one bad callback must not stop the lane
                logger.exception(
                    "zenoh subscriber callback failed for %s",
                    key_expr,
                    extra={"event": "zenoh.subscriber.callback_failed"},
                )

    def _overflow(self, key_expr: str) -> None:
        with self._guard:
            dropped = self._dropped.get(key_expr, 0) + 1
            self._dropped[key_expr] = dropped
        if dropped == 1 or dropped % OVERFLOW_LOG_EVERY == 0:
            logger.error(
                "zenoh.subscriber.overflow key_expr=%s dropped=%d capacity=%d: "
                "the callback is not keeping up; samples are being dropped",
                key_expr,
                dropped,
                self._capacity,
                extra={"event": "zenoh.subscriber.overflow"},
            )

    def stop(self) -> None:
        self._stopped = True
        for index, lane in enumerate(self._lanes):
            if self._threads[index] is None:
                continue
            while True:
                try:
                    lane.put_nowait(None)
                    break
                except queue.Full:
                    # Stop owns the queue now (``_stopped`` rejects new
                    # deliveries).  Drop queued callbacks until the sentinel
                    # fits so a once-full lane cannot park forever in get().
                    try:
                        lane.get_nowait()
                    except queue.Empty:
                        continue

@dataclass(frozen=True, slots=True)
class _QueryJob:
    key_expr: str
    callback: Callable[[Any], None]
    payload: Any
    query: Any
    retire: Callable[[str], None] | None = None
    token: str = ""

@dataclass(frozen=True, slots=True)
class _QueryCleanup:
    job: _QueryJob
    dropped: bool = False

def _drop_query(query: Any, key_expr: str) -> bool:
    """A failed native drop must not end a shared query worker."""

    try:
        query.drop()
        return True
    except Exception:  # noqa: BLE001 - preserve the remaining pool workers
        logger.exception(
            "zenoh.queryable.drop_failed key_expr=%s: a query could not be finalized",
            key_expr,
            extra={"event": "zenoh.queryable.drop_failed"},
        )
        return False

def _sample(sample: Any) -> TransportSample:
    key = str(sample.key_expr)
    payload = sample.payload.to_bytes() if hasattr(sample, "payload") else b""
    kind = str(getattr(sample, "kind", "put")).lower()
    if "." in kind:
        kind = kind.rsplit(".", 1)[-1]
    return TransportSample(key=key, payload=payload, kind=kind)

class _QueryDispatcher:
    """One bounded native-query effect pool shared by all query declarations.

    The queue plus active workers own at most ``capacity + workers`` queries.
    Direct Zenoh callbacks only reserve and enqueue; accepted native handles
    remain pinned until the worker replies, drops and retires their token.
    """

    def __init__(self, capacity: int, workers: int = QUERY_WORKERS) -> None:
        if capacity < 1 or workers < 1:
            raise ValueError("query dispatcher capacity and worker count must be positive")
        self._capacity = capacity
        self._worker_count = workers
        self._limit = capacity + workers
        self._queue: queue.Queue[_QueryJob | None] = queue.Queue(maxsize=self._limit)
        self._threads: list[threading.Thread] = []
        self._dropped: dict[str, int] = {}
        self._guard = threading.Condition()
        self._cleanup_retry_lock = threading.Lock()
        self._outstanding = 0
        self._cleanup_pending: dict[int, _QueryCleanup] = {}
        self._paused = False
        self._stopped = False
        self._sentinels_sent = False

    def wrap(self, key_expr: str, callback: Callable[[Any], None]) -> Callable[[Any], None]:
        def deliver(query: Any) -> None:
            self.submit(key_expr, callback, query, query)

        return deliver

    @staticmethod
    def reject(key_expr: str, query: Any) -> None:
        _drop_query(query, key_expr)

    def submit(
        self,
        key_expr: str,
        callback: Callable[[Any], None],
        payload: Any,
        query: Any,
        *,
        retire: Callable[[str], None] | None = None,
        token: str = "",
    ) -> AdmissionResult:
        dropped = 0
        with self._guard:
            if self._stopped:
                admission = AdmissionResult.CLOSED
            elif self._paused or self._outstanding >= self._limit:
                dropped = self._dropped.get(key_expr, 0) + 1
                self._dropped[key_expr] = dropped
                admission = AdmissionResult.OVERLOADED
            else:
                self._ensure_workers()
                self._outstanding += 1
                self._queue.put_nowait(_QueryJob(key_expr, callback, payload, query, retire, token))
                return AdmissionResult.ACCEPTED
        _drop_query(query, key_expr)
        self._retire(retire, token, key_expr)
        if dropped == 1 or (dropped and dropped % OVERFLOW_LOG_EVERY == 0):
            logger.error(
                "zenoh.queryable.overflow key_expr=%s dropped=%d capacity=%d: "
                "the query handlers are not keeping up; query was dropped",
                key_expr, dropped, self._capacity,
                extra={"event": "zenoh.queryable.overflow"},
            )
        return admission

    def _ensure_workers(self) -> None:
        if self._threads:
            return
        for index in range(self._worker_count):
            worker = threading.Thread(
                target=self._drain, name=f"hyprial-zenoh-query-{index}", daemon=True
            )
            self._threads.append(worker)
            worker.start()

    @staticmethod
    def _retire(retire: Callable[[str], None] | None, token: str, key_expr: str) -> bool:
        if retire is None:
            return True
        try:
            retire(token)
            return True
        except Exception:  # noqa: BLE001 - do not lose a shared worker on retirement
            logger.exception(
                "zenoh.queryable.retire_failed key_expr=%s", key_expr,
                extra={"event": "zenoh.queryable.retire_failed"},
            )
            return False

    def _finish(self, job: _QueryJob) -> None:
        dropped = _drop_query(job.query, job.key_expr)
        retired = dropped and self._retire(job.retire, job.token, job.key_expr)
        with self._guard:
            if retired:
                self._outstanding -= 1
            else:
                self._cleanup_pending[id(job)] = _QueryCleanup(job, dropped)
            self._guard.notify_all()

    def retry_cleanup(self) -> bool:
        """Try each retained native cleanup once; never rerun its handler."""

        # A close retry and a worker's explicit caller may arrive together.
        # Claim the whole pass, while native cleanup stays outside the global
        # admission condition so direct Zenoh callbacks remain nonblocking.
        with self._cleanup_retry_lock:
            with self._guard:
                pending = tuple(self._cleanup_pending.items())
            for key, state in pending:
                dropped = state.dropped or _drop_query(state.job.query, state.job.key_expr)
                retired = dropped and self._retire(
                    state.job.retire, state.job.token, state.job.key_expr
                )
                with self._guard:
                    if self._cleanup_pending.get(key) is not state:
                        continue
                    if retired:
                        self._cleanup_pending.pop(key)
                        self._outstanding -= 1
                    elif dropped and not state.dropped:
                        self._cleanup_pending[key] = _QueryCleanup(state.job, True)
                    self._guard.notify_all()
            with self._guard:
                return not self._cleanup_pending

    def _drain(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            try:
                job.callback(job.payload)
            except Exception:  # noqa: BLE001 - isolate one query from the pool
                logger.exception(
                    "zenoh queryable callback failed for %s", job.key_expr,
                    extra={"event": "zenoh.queryable.callback_failed"},
                )
            finally:
                self._finish(job)

    def quiesce(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            if self._stopped:
                return False
            self._paused = True
            while self._outstanding:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._guard.wait(remaining)
            return True

    def resume(self) -> None:
        with self._guard:
            if not self._stopped:
                self._paused = False
                self._guard.notify_all()

    def stop(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._guard:
            self._stopped = True
            self._paused = True
            threads = tuple(self._threads)
            send_sentinels = not self._sentinels_sent
            self._sentinels_sent = True
        self.retry_cleanup()
        if send_sentinels:
            while True:
                try:
                    job = self._queue.get_nowait()
                except queue.Empty:
                    break
                if job is not None:
                    self._finish(job)
            for _ in threads:
                self._queue.put_nowait(None)
        current = threading.current_thread()
        for thread in threads:
            if thread is not current:
                thread.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
        with self._guard:
            return all(not thread.is_alive() for thread in threads) and not self._cleanup_pending
