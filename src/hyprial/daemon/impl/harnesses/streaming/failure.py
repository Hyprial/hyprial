"""SequentialTurnProcess failure reporting behaviors (transitional mixin)."""

import queue
import threading
import time
from typing import TYPE_CHECKING

from hyprial.kernel import AdmissionResult
from hyprial.kernel import PortAdmission


if TYPE_CHECKING:
    pass



from hyprial.daemon.impl.harnesses.streaming.protocol import (
    _OBSERVER_STOP,
)


class _SequentialFailureMixin:
        def _dispatch_turn_failure(self, failure: str) -> None:
            """Enqueue a turn failure for the observer; never blocks, never raises.
    
            The observer (provider_auth coordinator) runs on its own thread, so
            its latency (Lark HTTP + file writes on the B path) cannot stall the
            turn's completion.  A full queue is backpressure: the drop is logged
            and the pump keeps going (review r2 final).
            """
    
            observer_queue = self._observer_queue
            if observer_queue is None:
                return
            reason = None
            with self._observer_admission_lock:
                if self._observer_stop_requested.is_set():
                    reason = "observer closed"
                else:
                    try:
                        observer_queue.put_nowait(failure)
                    except queue.Full:
                        reason = "queue full"
            if reason is not None:
                self._log_observer_event("turn-failure-observer.dropped", reason=reason)

        def _observer_drain(self) -> None:
            """The observer's single dedicated thread; swallows every raise."""
    
            observer_queue = self._observer_queue
            assert observer_queue is not None
            while True:
                item = observer_queue.get()
                if item is _OBSERVER_STOP:
                    return
                observer = self._on_turn_failure
                if observer is not None and isinstance(item, str):
                    while True:
                        try:
                            admission = observer(item)
                        except Exception as error:  # noqa: BLE001 -- never-raises contract
                            self._log_observer_event(
                                "turn-failure-observer.error",
                                errorType=type(error).__name__,
                            )
                            break
                        if admission is AdmissionResult.OVERLOADED:
                            # This bounded lane owns the already-enqueued observation.
                            # Keep its exact spec-bound payload until the auth owner can
                            # accept it. Only this I/O thread waits; turns keep settling.
                            time.sleep(0.05)
                            continue
                        if admission is AdmissionResult.CLOSED:
                            self._log_observer_event(
                                "turn-failure-observer.rejected", reason="owner closed"
                            )
                        break  # ACCEPTED, or the legacy void-observer contract
                # The stop sentinel may have been refused while the queue was full.
                # Admission and stop use the same lock, so an empty queue plus the
                # latched stop request is a stable terminal state for this observer.
                with self._observer_admission_lock:
                    if self._observer_stop_requested.is_set() and observer_queue.empty():
                        return

        def _reclaim_owned_runtime(self) -> None:
            """Reclaim auxiliary custody after every native stop outcome."""

            deadline = time.monotonic() + self._stop_timeout_seconds
            reclaim_error: BaseException | None = None
            if not self._turn_runtime_drained.is_set():
                try:
                    drained = self._turn_runtime.drain(
                        max(0.0, deadline - time.monotonic())
                    )
                    if not drained:
                        raise RuntimeError(f"{self.label} turn runtime did not drain")
                    self._turn_runtime_drained.set()
                except BaseException as error:
                    reclaim_error = error
            observer_thread = self._observer_thread
            if observer_thread is not None and observer_thread.is_alive():
                # Latch the stop under the admission lock so no new observation is
                # admitted, then offer the sentinel OUTSIDE the lock: the observer's
                # own exit check takes this lock, so blocking on a full queue while
                # holding it would deadlock with the observer until the deadline. A
                # refused sentinel is fine: the observer exits on "stop requested and
                # queue empty".
                with self._observer_admission_lock:
                    self._observer_stop_requested.set()
                observer_queue = self._observer_queue
                assert observer_queue is not None
                try:
                    observer_queue.put_nowait(_OBSERVER_STOP)
                except queue.Full:
                    pass
                observer_thread.join(max(0.0, deadline - time.monotonic()))
                if observer_thread.is_alive():
                    observer_error = RuntimeError(
                        f"{self.label} failure observer did not drain"
                    )
                    if reclaim_error is None:
                        reclaim_error = observer_error
            # Accepted observer custody has settled. Only now retire the remaining
            # process-local retry/wait handles; a failed stop can be called again
            # even though the native process thread already exited.
            with self._lock:
                for timer in self._abort_timers.values():
                    timer.cancel()
                self._abort_timers.clear()
                self._aborted_generations.clear()
                self._abort_callbacks.clear()
                failure_waiters = tuple(self._failure_waiters.values())
                self._failure_waiters.clear()
                interrupt_waiters = tuple(self._interrupt_waiters.values())
                self._interrupt_waiters.clear()
            for completed, outcome in failure_waiters:
                outcome.append((False, 0))
                completed.set()
            for completed, outcome in interrupt_waiters:
                outcome.append(False)
                completed.set()
            if reclaim_error is not None:
                raise reclaim_error

        def _log_observer_event(self, event: str, **fields: object) -> None:
            logger = self._logger
            if logger is None:
                return
            try:
                logger.info(event, **fields)
            except (NameError, ImportError):
                raise
            except OSError:
                return

        def _turn_failure_decision(
            self,
            generation: int,
            version: int,
            delivery_id: str,
            retry: bool,
            attempt: int,
            error: str,
        ) -> None:
            del version, error
            with self._lock:
                waiter = self._failure_waiters.pop((generation, delivery_id), None)
            if waiter is None:
                return
            completed, outcome = waiter
            outcome.append((retry, attempt))
            completed.set()

        def _report_turn_failure(
            self,
            generation: int,
            version: int,
            delivery_id: str,
            failure: str,
        ) -> tuple[bool, int]:
            allow_retry = True
            channel = getattr(self, "worker_channel", None)
            if channel is not None and delivery_id.startswith("workflow-"):
                import sqlite3
                from hyprial.daemon.impl.pac.contracts.delivery import (
                    is_workflow_delivery,
                )
                try:
                    allow_retry = not is_workflow_delivery(channel.state_dir, delivery_id)
                except (OSError, sqlite3.Error):
                    # An unavailable authority is not permission to repeat work.
                    allow_retry = False
            completed = threading.Event()
            outcome: list[tuple[bool, int]] = []
            with self._lock:
                self._failure_waiters[(generation, delivery_id)] = (completed, outcome)
            admission = self._turn_runtime.fail_io(
                generation=generation,
                version=version,
                delivery_id=delivery_id,
                error=failure,
                allow_retry=allow_retry,
            )
            if admission is not PortAdmission.ACCEPTED:
                with self._lock:
                    self._failure_waiters.pop((generation, delivery_id), None)
                raise RuntimeError(
                    f"turn actor failure decision was {admission.value}"
                )
            if not completed.wait(max(0.25, self._stop_timeout_seconds)):
                with self._lock:
                    self._failure_waiters.pop((generation, delivery_id), None)
                raise RuntimeError("turn actor failure decision timed out")
            return outcome[0]
