"""SequentialTurnProcess turn-I/O submission behaviors (transitional mixin).

State ownership stays with SequentialTurnProcess itself; this mixin only
carries the turn input/output submission cluster.
"""

import asyncio
import queue
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from hyprial.daemon.impl.api import (
    HarnessDelivery,
    HarnessResult,
    HarnessResultStatus,
)
from hyprial.daemon.impl.inbox.tracking.progress import ProgressEvent, ProgressEventError


if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.harnesses.turn_delivery.turn.turn_ports import (
    TurnDeliveryProjection,
    TurnInterruptIoCompleted,
    TurnStarted,
)


from hyprial.daemon.impl.harnesses.streaming.protocol import (
    _STOP,
    ProgressObservation,
)


class _SequentialTurnIoMixin:
        def _record_turn_outcome(self, result: HarnessResult) -> int:
            """Track consecutive failures of one delivery; return the run length.
    
            Counted per delivery rather than measured as a rate.  Worker turns run
            anywhere from seconds to tens of minutes, so any "failures per second"
            threshold has to either fire on a slow-but-healthy worker or miss a
            fast-looping one -- slow is not stopped.  "The same delivery failed N
            times in a row" is anomalous under every duration distribution.
            """
    
            with self._lock:
                if result.status is not HarnessResultStatus.FAILED:
                    self._repeated_failures.pop(result.delivery_id, None)
                    self.repeated_failure_delivery_id = None
                    self.repeated_failure_count = 0
                    return 0
                repeats = self._repeated_failures.get(result.delivery_id, 0) + 1
                self._repeated_failures[result.delivery_id] = repeats
                # Published for `hyprial top`: this incident's tell was one delivery
                # failing over and over, and no surface showed it for 45 hours.
                # Detection, not diagnosis -- doctor only helps once suspicious.
                self.repeated_failure_delivery_id = result.delivery_id
                self.repeated_failure_count = repeats
                return repeats

        def _record_progress(
            self, observation: ProgressObservation, delivery: HarnessDelivery
        ) -> None:
            """Stamp one observation with delivery context and enqueue it.
    
            A malformed observation never becomes an event (it does not consume
            a seq either -- contract-invalid producer output is a bug, not a
            gap): progress is a side channel and must never fail, delay, or
            distort the real turn.  The queue is bounded; a full queue drops
            the OLDEST event and the drop count is reported on the next drain.
            """
    
            if self._on_turn_completed is not None and observation.tool_name:
                with self._lock:
                    names = self._turn_tool_names.setdefault(delivery.delivery_id, [])
                    if observation.tool_name not in names:
                        names.append(observation.tool_name)
            try:
                event = ProgressEvent(
                    delivery_id=delivery.delivery_id,
                    conversation_id=delivery.conversation_id,
                    actor=delivery.recipient,
                    harness=self.harness,
                    phase=observation.phase,
                    seq=self._progress_seq,
                    emitted_at_ms=time.time_ns() // 1_000_000,
                    summary=observation.summary,
                    tool_call_id=observation.tool_call_id,
                    tool_name=observation.tool_name,
                    detail=observation.detail,
                    terminal=observation.terminal,
                )
            except ProgressEventError as error:
                logger = self._logger
                if logger is not None:
                    try:
                        logger.info(
                            "worker.progress.invalid",
                            deliveryId=delivery.delivery_id,
                            phase=observation.phase,
                            detail=str(error),
                        )
                    except OSError:
                        pass
                return
            self._progress_seq += 1
            while True:
                try:
                    self._progress.put_nowait(event)
                    return
                except queue.Full:
                    try:
                        self._progress.get_nowait()
                    except queue.Empty:
                        continue
                    with self._lock:
                        self._progress_dropped += 1

        def _submit_turn_io(
            self,
            generation: int,
            version: int,
            delivery: TurnDeliveryProjection,
        ) -> None:
            self._io_requests.put_nowait((generation, version, delivery))

        def _submit_interrupt_io(
            self,
            generation: int,
            version: int,
            correlation_id: str,
            delivery_id: str,
        ) -> None:
            with self._lock:
                loop = self._loop
                client = self._client
                active = self._active_delivery_id
            if loop is None or client is None or active != delivery_id:
                self._turn_runtime.complete_interrupt(
                    generation=generation,
                    version=version,
                    correlation_id=correlation_id,
                    delivery_id=delivery_id,
                    interrupted=False,
                    detail="turn I/O was not active",
                )
                return
            future = asyncio.run_coroutine_threadsafe(client.interrupt(), loop)
    
            def completed(done: object) -> None:
                interrupted = True
                detail = None
                try:
                    assert hasattr(done, "result")
                    done.result()  # type: ignore[union-attr]
                except Exception as error:  # noqa: BLE001 - reported as typed event
                    interrupted = False
                    detail = str(error) or type(error).__name__
                self._turn_runtime.complete_interrupt(
                    generation=generation,
                    version=version,
                    correlation_id=correlation_id,
                    delivery_id=delivery_id,
                    interrupted=interrupted,
                    detail=detail,
                )
    
            future.add_done_callback(completed)

        def _submit_close_io(
            self, generation: int, version: int, correlation_id: str
        ) -> None:
            del generation, version, correlation_id
            with self._lock:
                first_request = not self._stopping.is_set()
                self._stopping.set()
                loop = self._loop
                client = self._client
                active = self._active_delivery_id
                if first_request and active is None and loop is not None and client is not None:
                    # query() may be in flight; interrupt only after it has minted
                    # the native turn, preserving prompt-before-abort ordering.
                    self._stop_interrupt_pending = True
            if first_request and active is not None and loop is not None and client is not None:
                asyncio.run_coroutine_threadsafe(client.interrupt(), loop)
            try:
                self._io_requests.put_nowait(_STOP)
            except queue.Full:
                # One current delivery is already owned by the I/O worker.  The
                # stop flag plus interrupt ends it; no sentinel is then required.
                pass

        def _abort_generation_io(
            self, generation: int, ready: Callable[[], None]
        ) -> None:
            with self._lock:
                self._aborted_generations.add(generation)
                if self._active_generation != generation:
                    ready_now = True
                    loop = None
                    client = None
                else:
                    ready_now = False
                    loop = self._loop
                    client = self._client
                    if self._active_delivery_id is None:
                        self._abort_callbacks[generation] = ready
                        timer = threading.Timer(
                            min(1.0, self._stop_timeout_seconds),
                            self._force_abort_ready,
                            args=(generation,),
                        )
                        timer.daemon = True
                        self._abort_timers[generation] = timer
                        timer.start()
                        return
            if ready_now or loop is None or client is None:
                ready()
                return
            future = asyncio.run_coroutine_threadsafe(client.interrupt(), loop)
    
            def aborted(done: object) -> None:
                try:
                    assert hasattr(done, "result")
                    done.result()  # type: ignore[union-attr]
                except Exception:
                    pass
                ready()
    
            future.add_done_callback(aborted)

        def _force_abort_ready(self, generation: int) -> None:
            with self._lock:
                callback = self._abort_callbacks.pop(generation, None)
                self._abort_timers.pop(generation, None)
            if callback is None:
                return
            if self._force_stop is not None:
                try:
                    self._force_stop()
                except Exception:
                    pass
            callback()

        def _on_turn_event(self, event: object) -> None:
            if isinstance(event, TurnStarted):
                observer = self._on_turn_completed
                started = getattr(observer, "turn_started", None)
                if callable(started):
                    try:
                        started(
                            event.delivery,
                            started_at_ms=event.emitted_at_ms,
                        )
                    except Exception:
                        # Turn admission is authoritative; hooks are advisory.
                        pass
                return
            if not isinstance(event, TurnInterruptIoCompleted):
                return
            with self._lock:
                waiter = self._interrupt_waiters.pop(event.correlation_id, None)
            if waiter is None:
                return
            completed, outcome = waiter
            outcome.append(event.interrupted)
            completed.set()
