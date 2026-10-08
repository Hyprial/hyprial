"""SequentialTurnProcess: the streaming turn-pump state owner.

Behavior clusters live in turn_io.py / failure.py as explicit mixins;
all instance state is created and owned by this class's ``__init__``.
"""

import asyncio
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

from hyprial.kernel import AdmissionResult
from hyprial.kernel import capped_exponential
from hyprial.kernel import PortAdmission
from hyprial.daemon.impl.api import (
    HarnessDelivery,
    HarnessResult,
    HarnessResultStatus,
    classify_harness_failure,
    delivery_prompt,
)
from hyprial.kernel import ProcessLiveness, ProcessLivenessState
from hyprial.daemon.impl.inbox.tracking.progress import ProgressEvent
from hyprial.kernel import Logger


if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.harnesses.turn_delivery.turn.turn_ports import (
    CloseTurnPumpCommand,
    EnqueueTurnCommand,
    InterruptTurnCommand,
    TurnDeliveryProjection,
    TurnResultProjection,
)
from hyprial.daemon.impl.harnesses.turn_delivery.turn.turn_runtime import TurnRuntime, provider_failure_is_terminal


from hyprial.daemon.impl.harnesses.streaming.base import BaseTurnProcess
from hyprial.daemon.impl.harnesses.streaming.protocol import (
    _STOP,
    _PROGRESS_QUEUE_MAX,
    _TURN_DELIVERY_MAX,
    _TURN_FAILURE_OBSERVER_QUEUE_MAX,
    _TURN_CONDITION_EVENTS,
    ProgressObservation,
    TurnClient,
    TurnClientFactory,
    TurnStartedObserver,
    TurnCompletedObserver,
)
from hyprial.daemon.impl.harnesses.streaming.failure import (
    _SequentialFailureMixin,
)
from hyprial.daemon.impl.harnesses.streaming.turn_io import (
    _SequentialTurnIoMixin,
)


class SequentialTurnProcess(
    _SequentialTurnIoMixin,
    _SequentialFailureMixin,
    BaseTurnProcess,
):
        def __init__(
            self,
            *,
            harness: str,
            label: str,
            client_factory: TurnClientFactory,
            thread_name: str,
            logger: Logger | None = None,
            reconnect_delay_seconds: float = 0.25,
            reconnect_delay_max_seconds: float = 30.0,
            max_delivery_attempts: int = 5,
            stop_timeout_seconds: float = 2.0,
            force_stop: Callable[[], None] | None = None,
            force_stopped: Callable[[], bool] | None = None,
            force_stop_join_seconds: float = 1.0,
            liveness_probe: Callable[[], ProcessLiveness] | None = None,
            on_turn_started: TurnStartedObserver | None = None,
            on_turn_failure: Callable[[str], AdmissionResult | None] | None = None,
            on_turn_completed: TurnCompletedObserver | None = None,
        ) -> None:
            if reconnect_delay_seconds < 0:
                raise ValueError("reconnect delay must not be negative")
            if reconnect_delay_max_seconds < reconnect_delay_seconds:
                raise ValueError("reconnect delay ceiling must not undercut the base")
            if max_delivery_attempts < 1:
                raise ValueError("a delivery needs at least one attempt")
            if stop_timeout_seconds <= 0:
                raise ValueError("stop timeout must be positive")
            if force_stop_join_seconds <= 0:
                raise ValueError("force-stop join timeout must be positive")
            if (force_stop is None) != (force_stopped is None):
                raise ValueError("force-stop action and completion check must be paired")
            self.harness = harness
            self.label = label
            self._client_factory = client_factory
            self._logger = logger
            self._reconnect_delay_seconds = reconnect_delay_seconds
            self._reconnect_delay_max_seconds = reconnect_delay_max_seconds
            self._max_delivery_attempts = max_delivery_attempts
            self._stop_timeout_seconds = stop_timeout_seconds
            self._force_stop = force_stop
            self._force_stopped = force_stopped
            self._force_stop_join_seconds = force_stop_join_seconds
            self._liveness_probe = liveness_probe
            self._on_turn_started = on_turn_started
            self._on_turn_completed = on_turn_completed
            self._turn_deliveries: dict[str, HarnessDelivery] = {}
            self._turn_started_ms: dict[str, int] = {}
            self._turn_tool_names: dict[str, list[str]] = {}
            # Every turn failure is reported to this observer with the verbatim
            # failure text; classification (terminal/auth/entitlement/transient)
            # lives in provider_auth, whose tables are the single closed list —
            # gating here on provider_failure_is_terminal would silently drop the
            # entitlement class, which matches none of those markers.  The
            # observer's contract is never-raises, same as the alarm delivery
            # path; the pump does not defend against it.
            self._on_turn_failure = on_turn_failure
            # Fire-and-forget dispatch for the observer: the pump enqueues and
            # never awaits, so the observer's own latency (Lark HTTP + file writes
            # on the B path) cannot stall the turn's completion.  Bounded queue +
            # one dedicated drain thread; excess is dropped and counted.
            self._observer_queue: queue.Queue[object] | None = None
            self._observer_thread: threading.Thread | None = None
            self._observer_admission_lock = threading.Lock()
            self._observer_stop_requested = threading.Event()
            if on_turn_failure is not None:
                self._observer_queue = queue.Queue(
                    maxsize=_TURN_FAILURE_OBSERVER_QUEUE_MAX
                )
                self._observer_thread = threading.Thread(
                    target=self._observer_drain,
                    name=f"{thread_name}-observer",
                    daemon=True,
                )
                self._observer_thread.start()
            # The actor owns FIFO, admission, in-flight identity, and result
            # publication.  This one-slot queue is only the handoff to blocking
            # harness I/O; it can never become a second unbounded work owner.
            self._io_requests: queue.Queue[
                tuple[int, int, TurnDeliveryProjection] | object
            ] = queue.Queue(maxsize=1)
            # Progress side channel (route C): bounded, drop-oldest, and the
            # drop count folds into the first event of the next drain batch.
            # The bound is deliberate backpressure -- progress never blocks the pump.
            self._progress: queue.Queue[ProgressEvent] = queue.Queue(
                maxsize=_PROGRESS_QUEUE_MAX
            )
            self._progress_dropped = 0
            self._progress_seq = 0
            # Verbatim providerError of the turn being received, when the
            # failure came from the model backend itself (assistant
            # stopReason=error).  Set by _receive_result and consumed by the
            # very next log call in the same pump iteration -- there is no
            # await between them, so a plain attribute needs no lock.  It
            # exists because HarnessResult has no field for it (its home is
            # the daemon contract), while worker.turn.failed must name the
            # shape and carry the model vendor's own words (card 260).
            self._turn_provider_error: str | None = None
            self._lock = threading.RLock()
            self._interrupt_waiters: dict[
                str, tuple[threading.Event, list[bool]]
            ] = {}
            self._failure_waiters: dict[
                tuple[int, str], tuple[threading.Event, list[tuple[bool, int]]]
            ] = {}
            self._ready = threading.Event()
            self._startup_settled = threading.Event()
            self._stopping = threading.Event()
            self._loop: asyncio.AbstractEventLoop | None = None
            self._client: TurnClient | None = None
            # Published before ``__aenter__`` so a bounded stop can reach a client
            # whose native process has spawned but whose ready probe is still in
            # flight.  It is deliberately separate from ``_client``: liveness must
            # not advertise a half-connected harness as running.
            self._connecting_client: TurnClient | None = None
            self._active_delivery_id: str | None = None
            self._active_generation: int | None = None
            self._daemon_interruption_reasons: dict[str, str] = {}
            self._aborted_generations: set[int] = set()
            self._abort_callbacks: dict[int, Callable[[], None]] = {}
            self._abort_timers: dict[int, threading.Timer] = {}
            self._stop_interrupt_pending = False
            self.last_error: str | None = None
            self._repeated_failures: dict[str, int] = {}
            # Published so a status surface can show a delivery that keeps failing.
            self.repeated_failure_delivery_id: str | None = None
            self.repeated_failure_count = 0
            self._turn_runtime = TurnRuntime(
                name=thread_name,
                io_submit=self._submit_turn_io,
                io_interrupt=self._submit_interrupt_io,
                io_close=self._submit_close_io,
                io_abort=self._abort_generation_io,
                io_failure_decision=self._turn_failure_decision,
                max_delivery_attempts=max_delivery_attempts,
                event_sink=self._on_turn_event,
                capacity=_TURN_DELIVERY_MAX,
            )
            self._turn_runtime_drained = threading.Event()
            self._thread = threading.Thread(
                target=self._thread_main,
                name=thread_name,
                daemon=True,
            )
            self._thread.start()

        @property
        def running(self) -> bool:
            if not self._thread.is_alive() or self._stopping.is_set():
                return False
            with self._lock:
                client = self._client
            if client is None:
                # Before the first connection this is a starting process.  After a
                # connection failure, the error is a real unhealthy state even if
                # the reconnect loop's management thread is still alive.
                return self.last_error is None
            client_running = getattr(client, "running", None)
            if client_running is False:
                detail = getattr(client, "exit_error", None)
                self.last_error = str(detail or f"{self.label} child process stopped")
                return False
            return True

        @property
        def idle(self) -> bool:
            """Whether the turn actor currently owns no in-flight delivery."""

            return self._turn_runtime.read_in_flight() is None

        @property
        def pid(self) -> int | None:
            """Pid of the current harness child process, if one is live.
    
            The real subprocess belongs to the connected turn client and changes
            on every reconnect; clients without a dedicated child (DSH) simply
            do not expose one.
            """
    
            with self._lock:
                client = self._client
            if client is None:
                return None
            pid = getattr(client, "pid", None)
            return pid if isinstance(pid, int) else None

        def liveness(self) -> ProcessLiveness:
            if self._liveness_probe is not None:
                return self._liveness_probe()
            if self._stopping.is_set() and not self._thread.is_alive():
                return ProcessLiveness(
                    ProcessLivenessState.DEAD,
                    observed=True,
                    pid=self.pid,
                )
            return ProcessLiveness(
                ProcessLivenessState.UNKNOWN,
                observed=self._ready.is_set(),
                pid=self.pid,
                detail="managed runtime has no OS liveness criterion",
            )

        def wait_ready(self, *, timeout: float | None = None) -> bool:
            self._startup_settled.wait(timeout)
            return self._ready.is_set()

        def enqueue(self, delivery: HarnessDelivery) -> bool:
            if not delivery.delivery_id or not delivery.message:
                raise ValueError("harness delivery requires an id and message")
            if not self.running:
                return False
            accepted = (
                self._turn_runtime.submit(
                    EnqueueTurnCommand(
                        correlation_id=delivery.delivery_id,
                        delivery=TurnDeliveryProjection(
                            delivery_id=delivery.delivery_id,
                            conversation_id=delivery.conversation_id,
                            sender=delivery.sender,
                            recipient=delivery.recipient,
                            message=delivery.message,
                            hook_text=delivery.hook_text,
                            hook_request=delivery.hook_request,
                        ),
                    )
                )
                is PortAdmission.ACCEPTED
            )
            if accepted and self._on_turn_completed is not None:
                with self._lock:
                    # A pending inbox row is offered again on every daemon tick.
                    # Admission is the ownership boundary: an already-reserved
                    # re-offer must not replace or remove the live turn's observer
                    # state, especially the tool names collected since acceptance.
                    self._turn_deliveries[delivery.delivery_id] = delivery
                    self._turn_tool_names.setdefault(delivery.delivery_id, [])
            return accepted

        def drain_results(self, limit: int | None = None) -> tuple[HarnessResult, ...]:
            results = tuple(
                HarnessResult(
                    result.delivery_id,
                    result.recipient,
                    HarnessResultStatus(result.status),
                    output=result.output,
                    error=result.error,
                    failure_code=result.failure_code,
                )
                for result in self._turn_runtime.drain_results(limit)
            )
            observer = self._on_turn_completed
            if observer is None:
                return results
            ended_at_ms = time.time_ns() // 1_000_000
            for result in results:
                with self._lock:
                    delivery = self._turn_deliveries.pop(result.delivery_id, None)
                    started_at_ms = self._turn_started_ms.pop(
                        result.delivery_id, ended_at_ms
                    )
                    tool_names = tuple(
                        self._turn_tool_names.pop(result.delivery_id, ())
                    )
                if delivery is None:
                    continue
                try:
                    observer(
                        delivery,
                        result,
                        started_at_ms=started_at_ms,
                        ended_at_ms=ended_at_ms,
                        tool_names=tool_names,
                    )
                except Exception:
                    # Turn settlement is authoritative; an observer is advisory.
                    pass
            return results

        def drain_progress(self) -> tuple[ProgressEvent, ...]:
            """Dequeue every pending progress event (daemon reconcile drains these).
    
            Drops accumulated since the previous drain ride the first event of
            this batch (``dropped_since_seq``), so the accounting is exact even
            when the drop happened at the queue's newest end.
            """
    
            events: list[ProgressEvent] = []
            while True:
                try:
                    events.append(self._progress.get_nowait())
                except queue.Empty:
                    break
            with self._lock:
                pending = self._progress_dropped
                self._progress_dropped = 0
            if pending and events:
                events[0] = replace(
                    events[0],
                    dropped_since_seq=events[0].dropped_since_seq + pending,
                )
            return tuple(events)

        def interrupt(self, delivery_id: str, *, timeout: float = 1.0) -> bool:
            in_flight = self._turn_runtime.read_in_flight()
            if in_flight is None or in_flight.delivery_id != delivery_id:
                return False
            correlation_id = f"interrupt-{delivery_id}-{uuid4().hex}"
            completed = threading.Event()
            outcome: list[bool] = []
            with self._lock:
                self._interrupt_waiters[correlation_id] = (completed, outcome)
            admission = self._turn_runtime.submit(
                InterruptTurnCommand(
                    correlation_id=correlation_id,
                    delivery_id=delivery_id,
                    deadline_ms=max(0, int(timeout * 1000)),
                )
            )
            if admission is not PortAdmission.ACCEPTED:
                with self._lock:
                    self._interrupt_waiters.pop(correlation_id, None)
                return False
            if not completed.wait(timeout):
                with self._lock:
                    self._interrupt_waiters.pop(correlation_id, None)
                return False
            return outcome == [True]

        def prepare_daemon_interruption(self, reason: str) -> None:
            """Record why the daemon is about to interrupt the open turn.
    
            The record is delivery-correlated and written before ``stop()`` asks
            the native turn to interrupt. Harness result text never participates
            in the classification: without this record, an interrupted outcome
            remains a failure.
            """
    
            if reason not in {"graph-settled", "graph-cancelled", "graph-cleanup"}:
                raise ValueError(f"unsupported daemon interruption reason: {reason}")
            in_flight = self._turn_runtime.read_in_flight()
            if in_flight is None:
                return
            with self._lock:
                self._daemon_interruption_reasons[in_flight.delivery_id] = reason

        def stop(self) -> None:
            correlation_id = f"close-{uuid4().hex}"
            deadline = time.monotonic() + self._stop_timeout_seconds
            stop_error: BaseException | None = None
            try:
                while not self._stopping.is_set():
                    admission = self._turn_runtime.submit(
                        CloseTurnPumpCommand(
                            correlation_id=correlation_id,
                            deadline_ms=int(self._stop_timeout_seconds * 1000),
                        )
                    )
                    if admission is not PortAdmission.OVERLOADED:
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            f"{self.label} close command was overloaded"
                        )
                    time.sleep(0.005)
                self._thread.join(timeout=self._stop_timeout_seconds)
                if self._thread.is_alive() and self._force_stop is None:
                    raise RuntimeError(
                        f"{self.label} did not stop within "
                        f"{self._stop_timeout_seconds}s and has no force-stop hook"
                    )
                if self._force_stop is not None:
                    # Retry forced cleanup even when an earlier stop request set the
                    # intent flag but could not finish.  This makes repeated stop()
                    # both idempotent after success and useful after a bounded failure.
                    self._force_stop()
                    self._thread.join(timeout=self._force_stop_join_seconds)
                    assert self._force_stopped is not None
                    if self._thread.is_alive() or not self._force_stopped():
                        raise RuntimeError(
                            f"{self.label} did not stop after forced cleanup"
                        )
            except BaseException as error:
                stop_error = error
            try:
                self._reclaim_owned_runtime()
            except BaseException as error:
                if stop_error is None:
                    stop_error = error
            if stop_error is not None:
                raise stop_error

        def _thread_main(self) -> None:
            asyncio.run(self._run())

        async def _run(self) -> None:
            self._loop = asyncio.get_running_loop()
            current: HarnessDelivery | None = None
            current_fence: tuple[int, int] | None = None
            turn_started = False
            connect_failures = 0
            while not self._stopping.is_set():
                try:
                    client = self._client_factory()
                    with self._lock:
                        self._connecting_client = client
                    if self._stopping.is_set():
                        with self._lock:
                            self._connecting_client = None
                        return
                    async with client:
                        with self._lock:
                            self._client = client
                            self._connecting_client = None
                        self.last_error = None
                        self._ready.set()
                        self._startup_settled.set()
                        connect_failures = 0
                        while not self._stopping.is_set():
                            if current is None:
                                queued = await asyncio.to_thread(self._io_requests.get)
                                if queued is _STOP:
                                    return
                                assert isinstance(queued, tuple)
                                generation, version, projection = queued
                                assert isinstance(projection, TurnDeliveryProjection)
                                current_fence = (generation, version)
                                with self._lock:
                                    self._active_generation = generation
                                    aborted_before_start = (
                                        generation in self._aborted_generations
                                    )
                                if aborted_before_start:
                                    with self._lock:
                                        self._active_generation = None
                                        self._aborted_generations.discard(generation)
                                    current_fence = None
                                    continue
                                current = HarnessDelivery(
                                    delivery_id=projection.delivery_id,
                                    conversation_id=projection.conversation_id,
                                    sender=projection.sender,
                                    recipient=projection.recipient,
                                    message=projection.message,
                                    hook_text=projection.hook_text,
                                    hook_request=projection.hook_request,
                                )
                            channel = getattr(self, "worker_channel", None)
                            if channel is not None:
                                from hyprial.daemon.impl.pac.contracts.delivery import (
                                    WITHDRAWN,
                                    delivery_current,
                                )
                                from hyprial.daemon.impl.pac.contracts.bindings import (
                                    RemoteWorkflowUnavailable,
                                )
                                try:
                                    authorized = delivery_current(channel.state_dir, current.delivery_id)
                                except RemoteWorkflowUnavailable:
                                    await asyncio.sleep(0.2)
                                    continue
                                if not authorized:
                                    assert current_fence is not None
                                    admission = self._turn_runtime.complete_io(
                                        generation=current_fence[0], version=current_fence[1],
                                        result=TurnResultProjection(delivery_id=current.delivery_id,
                                            recipient=current.recipient, status="interrupted",
                                            failure_code=WITHDRAWN),
                                    )
                                    if admission is not PortAdmission.ACCEPTED:
                                        self.last_error = "withdrawn turn completion relay was refused"
                                        self._stopping.set()
                                        return
                                    self._log_turn("worker.turn.withdrawn", current)
                                    with self._lock:
                                        self._active_delivery_id = None
                                        self._active_generation = None
                                        self._aborted_generations.discard(current_fence[0])
                                    current = None
                                    current_fence = None
                                    continue
                            self._log_turn("worker.turn.started", current)
                            if self._on_turn_completed is not None:
                                with self._lock:
                                    self._turn_started_ms[current.delivery_id] = (
                                        time.time_ns() // 1_000_000
                                    )
                            turn_started = True
                            await client.query(delivery_prompt(current))
                            # A selected delivery is not interruptible until the
                            # harness has accepted it as a native turn.  Publishing
                            # it before query() returns lets clients with no turn
                            # ID treat interrupt as a no-op while this process
                            # reports success and permanently marks the delivery.
                            with self._lock:
                                self._active_delivery_id = current.delivery_id
                                stop_interrupt_pending = self._stop_interrupt_pending
                                if stop_interrupt_pending:
                                    self._stop_interrupt_pending = False
                                abort_callback = self._abort_callbacks.pop(
                                    current_fence[0], None
                                )
                                abort_timer = self._abort_timers.pop(
                                    current_fence[0], None
                                )
                                if abort_timer is not None:
                                    abort_timer.cancel()
                            if self._on_turn_started is not None:
                                self._on_turn_started(current, client)
                            if stop_interrupt_pending:
                                # The turn exists now.  The latched stop interrupt
                                # is delivered strictly after prompt, never before.
                                await client.interrupt()
                            if abort_callback is not None:
                                await client.interrupt()
                                abort_callback()
                            result = await self._receive_result(client, current)
                            provider_error = self._turn_provider_error
                            self._turn_provider_error = None
                            with self._lock:
                                interruption_reason = self._daemon_interruption_reasons.pop(
                                    current.delivery_id, None
                                )
                            if (
                                interruption_reason is not None
                                and result.status is not HarnessResultStatus.COMPLETED
                            ):
                                result = replace(
                                    result,
                                    status=HarnessResultStatus.INTERRUPTED,
                                    output="",
                                    error=None,
                                    failure_code=None,
                                )
                            self._log_turn(
                                f"worker.turn.{result.status.value}",
                                current,
                                **(
                                    {"reason": interruption_reason}
                                    if result.status is HarnessResultStatus.INTERRUPTED
                                    and interruption_reason is not None
                                    else {
                                        # The event name already says "failed", so
                                        # repeating the status here carried no
                                        # information while the actual reason sat
                                        # unused in the same object.  Three pi
                                        # workers retried one delivery ~280k times
                                        # over 45 hours and every line read
                                        # failure="failed"; the cause had to be
                                        # reconstructed afterwards from the
                                        # harness's own session file.
                                        "failure": result.error
                                        or result.status.value,
                                        "failureCode": result.failure_code or "",
                                        # Card 260: when the failure IS the
                                        # model vendor's own error stop, the log
                                        # must say so and quote it verbatim --
                                        # pi's session file is not an acceptable
                                        # substitute for the worker log.
                                        **(
                                            {
                                                "reason": "provider stopReason=error",
                                                "providerError": provider_error,
                                            }
                                            if provider_error is not None
                                            else {}
                                        ),
                                    }
                                    if result.status is HarnessResultStatus.FAILED
                                    else {}
                                ),
                            )
                            if (
                                result.status is HarnessResultStatus.FAILED
                                and self._on_turn_failure is not None
                                and result.error
                            ):
                                # the turn-failure observer: the FAILED result path is
                                # where pi's stopReason=error and claude's isError
                                # surface -- a normal error result, not an
                                # exception.  Fire-and-forget: the observer's own
                                # thread drains a bounded queue, so its latency
                                # never stalls the turn's completion (review r2).
                                self._dispatch_turn_failure(result.error)
                            turn_started = False
                            assert current_fence is not None
                            completion_admission = self._turn_runtime.complete_io(
                                generation=current_fence[0],
                                version=current_fence[1],
                                result=TurnResultProjection(
                                    delivery_id=result.delivery_id,
                                    recipient=result.recipient,
                                    status=result.status.value,
                                    output=result.output,
                                    error=result.error,
                                    failure_code=result.failure_code,
                                ),
                            )
                            if completion_admission is not PortAdmission.ACCEPTED:
                                self.last_error = (
                                    "turn completion relay did not accept terminal result"
                                )
                                self._stopping.set()
                                return
                            with self._lock:
                                self._active_delivery_id = None
                                self._active_generation = None
                                self._aborted_generations.discard(current_fence[0])
                            repeats = self._record_turn_outcome(result)
                            current = None
                            current_fence = None
                            if repeats > 1:
                                # This delivery has now failed repeatedly.  The
                                # harness does not retry -- the inbox redelivers --
                                # so slowing down here is all this layer can do,
                                # and it is not "give it time to recover": these
                                # failures do not heal on their own.  It bounds the
                                # damage.  Three workers spent 45 hours failing one
                                # delivery every 1.6s, ~280k attempts, 1.55e9 input
                                # tokens, and nothing capped the rate.
                                #
                                # Deciding to abandon a delivery stays the inbox's
                                # authority.  This layer may slow down; it may not
                                # decide to give up.
                                #
                                # Nor can a context-sized failure be left to the
                                # harness's own compaction: across 11 automatic
                                # compactions in that incident, the input size of
                                # every later billed turn stayed near 1.18M -- it
                                # never returned to a workable range.  (Observed
                                # only; how much any single compaction reclaimed
                                # was not measured.)
                                await asyncio.sleep(self._backoff_delay(repeats))
                except (NameError, ImportError):
                    # Programming/import defects are never transient harness loss;
                    # surfacing them terminates the pump instead of retrying forever.
                    raise
                except Exception as error:  # noqa: BLE001 - harness reconnect boundary
                    # The mechanism, not just the exception class (issues #81 and
                    # #270: seven cap kills all labelled bare "ConnectionError").
                    failure = str(error) or type(error).__name__
                    aborted = (
                        current_fence is not None
                        and current_fence[0] in self._aborted_generations
                    )
                    if aborted:
                        with self._lock:
                            callback = self._abort_callbacks.pop(
                                current_fence[0], None
                            )
                            timer = self._abort_timers.pop(current_fence[0], None)
                            if timer is not None:
                                timer.cancel()
                            self._active_delivery_id = None
                            self._active_generation = None
                            self._aborted_generations.discard(current_fence[0])
                        if callback is not None:
                            callback()
                        current = None
                        current_fence = None
                        turn_started = False
                        continue
                    if current is not None and turn_started:
                        turn_started = False
                        assert current_fence is not None
                        retry, attempt = await asyncio.to_thread(
                            self._report_turn_failure,
                            current_fence[0],
                            current_fence[1],
                            current.delivery_id,
                            failure,
                        )
                        self._log_turn(
                            "worker.turn.failed",
                            current,
                            failure=failure,
                            attempt=attempt,
                            maxAttempts=self._max_delivery_attempts,
                            # Same closed marker table the retry decision uses
                            # (turn_runtime): when the failure text names a
                            # model-vendor auth/network cause, the event says so
                            # and quotes the text verbatim.  Any other failure --
                            # including every non-pi harness whose outcome has no
                            # provider_error -- omits the fields entirely; no
                            # None placeholder is ever written (card 260).
                            **(
                                {
                                    "reason": "provider error",
                                    "providerError": failure,
                                }
                                if provider_failure_is_terminal(failure)
                                else {}
                            ),
                        )
                        if self._on_turn_failure is not None:
                            # turn-failure observer on the crash/断连 path;
                            # fire-and-forget like the result path (review r2).
                            self._dispatch_turn_failure(failure)
                        if not retry:
                            self._log_turn(
                                "worker.turn.abandoned",
                                current,
                                failure=failure,
                                attempts=attempt,
                            )
                            current = None
                            current_fence = None
                        failures_for_delay = attempt if retry else 1
                    else:
                        connect_failures += 1
                        failures_for_delay = connect_failures
                    self.last_error = failure
                    with self._lock:
                        self._client = None
                        self._connecting_client = None
                        self._active_delivery_id = None
                    if (
                        not turn_started
                        and getattr(error, "permanent_start_failure", False) is True
                    ):
                        if self._logger is not None:
                            try:
                                self._logger.error(
                                    "worker.start.failed",
                                    harness=self.harness,
                                    failure=failure,
                                )
                            except OSError:
                                pass
                        self._stopping.set()
                        self._startup_settled.set()
                        break
                    if self._stopping.is_set():
                        break
                    await asyncio.sleep(self._backoff_delay(failures_for_delay))
            self._loop = None

        def _backoff_delay(self, failures: int) -> float:
            """Exponential retry spacing: base doubles per failure up to the cap.
    
            The pre-#270 constant 0.25s meant a turn that hits its wall-clock cap
            was replayed immediately and identically, forming the observed
            15-minute failure loop.
            """
    
            if failures <= 1:
                return self._reconnect_delay_seconds
            return min(
                capped_exponential(
                    self._reconnect_delay_seconds,
                    self._reconnect_delay_max_seconds,
                    failures - 1,
                ),
                self._reconnect_delay_max_seconds,
            )

        def _log_turn(
            self, event: str, delivery: HarnessDelivery, **fields: object
        ) -> None:
            logger = self._logger
            if logger is None:
                return
            try:
                logger.info(
                    event,
                    messageId=delivery.delivery_id,
                    correlationId=delivery.delivery_id,
                    node="worker-turn",
                    actorId=delivery.recipient,
                    conversationId=delivery.conversation_id,
                    sender=delivery.sender,
                    recipient=delivery.recipient,
                    deliveryId=delivery.delivery_id,
                    **fields,
                )
            except (NameError, ImportError):
                raise
            except OSError:
                # Turn execution is authoritative; local visibility I/O cannot
                # turn a successful harness response into a delivery failure.
                return

        async def _receive_result(
            self, client: TurnClient, delivery: HarnessDelivery
        ) -> HarnessResult:
            output = ""
            is_error = False
            provider_error: str | None = None
            explicit_failure_code: str | None = None
            self._turn_provider_error = None
            self._progress_seq = 0
            async for message in client.receive_response():
                value = getattr(message, "result", None)
                if isinstance(value, str):
                    output = value
                    is_error = bool(getattr(message, "is_error", False))
                    candidate = getattr(message, "provider_error", None)
                    provider_error = (
                        candidate if isinstance(candidate, str) and candidate else None
                    )
                    candidate_code = getattr(message, "failure_code", None)
                    explicit_failure_code = (
                        candidate_code
                        if isinstance(candidate_code, str) and candidate_code
                        else None
                    )
                elif isinstance(message, ProgressObservation):
                    condition_event = _TURN_CONDITION_EVENTS.get(message.phase)
                    if condition_event is not None:
                        self._log_turn(
                            condition_event, delivery, **(message.detail or {})
                        )
                    self._record_progress(message, delivery)
            if is_error:
                self._turn_provider_error = provider_error
                return HarnessResult(
                    delivery.delivery_id,
                    delivery.recipient,
                    HarnessResultStatus.FAILED,
                    error=output or f"{self.label} returned an error result",
                    failure_code=explicit_failure_code
                    or classify_harness_failure(
                        output or f"{self.label} returned an error result"
                    ),
                )
            if not output:
                return HarnessResult(
                    delivery.delivery_id,
                    delivery.recipient,
                    HarnessResultStatus.FAILED,
                    error=f"{self.label} response ended without a terminal result",
                    failure_code="HARNESS_TRANSIENT_FAILURE",
                )
            return HarnessResult(
                delivery.delivery_id,
                delivery.recipient,
                HarnessResultStatus.COMPLETED,
                output=output,
            )


# Compatibility name retained for every existing Pi/Claude/Codex/DSH caller.
# It intentionally resolves to the sequential family implementation.
StreamingTurnProcess = SequentialTurnProcess


__all__ = [
    "ProgressObservation",
    "SequentialTurnProcess",
    "StreamingTurnProcess",
    "TurnClient",
    "TurnClientFactory",
    "TurnStartedObserver",
    "TurnCompletedObserver",
]
