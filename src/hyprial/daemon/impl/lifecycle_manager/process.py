"""LifecycleProcessManager and its step planners/parsers."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
import sqlite3
import threading
import time
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Callable, cast
from hyprial.kernel import PortAdmission, PortCommandRejected
from hyprial.kernel import (
    LIFECYCLE_OPERATION_DEADLINE_SECONDS, LIFECYCLE_WAIT_MARGIN_SECONDS,
)
from hyprial.daemon.impl.correlation.correlation  import (
    CorrelationEventRouter,
    CorrelationRouterOverloaded,
)
from hyprial.daemon.impl.harnesses.runtime.ports  import (
    EnsureHarnessCommand,
    RemoveHarnessCommand,
)
from hyprial.daemon.impl.state_db  import StateDatabase
from hyprial.daemon.impl.lifecycle.lifecycle_receipts  import (
    LifecycleMutationFailed,
)
from hyprial.kernel import LifecycleMutationCompleted
from hyprial.kernel import LifecycleMutationRequest
from hyprial.daemon.impl.network.route_ports  import (
    DropRouteCommand,
    EnsureRouteCommand,
    RouteMutationFailed,
)

from .steps import (
    _command,
    _completion_provenance,
    _plan,
)
from .store import (
    _InjectedManagerCrash,
    _LifecycleStore,
    _RECOVER_FAULT_EVENT_EVERY,
    _RECOVER_FAULT_EVENT_MIN_INTERVAL_S,
)
from .vocabulary import (
    LifecycleOperation,
    LifecyclePorts,
    LifecycleResult,
    LifecycleState,
    LifecycleStepFailed,
    LifecycleStepUnresolved,
    _Step,
)


class LifecycleProcessManager:
    """Bounded durable saga runner intended for the daemon composition root."""

    def __init__(
        self,
        state: Path | StateDatabase,
        ports: LifecyclePorts,
        router: CorrelationEventRouter,
        *,
        capacity: int = 32,
        completion_timeout: float = 2.0,
        admission_deadline: float = 1.0,
        operation_deadline: float = LIFECYCLE_OPERATION_DEADLINE_SECONDS,
        admission_backoff: tuple[float, ...] = (0.005, 0.01, 0.02),
        fault_after_effect: Callable[[str, str], None] | None = None,
        fault_after_receipt_retire: Callable[[str, str], None] | None = None,
        event_sink: Callable[..., None] | None = None,
        journal_store: object | None = None,
        combined_shared_receipts: bool = False,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._store: _LifecycleStore = (
            cast(_LifecycleStore, journal_store)
            if journal_store is not None
            else _LifecycleStore(
                state if isinstance(state, StateDatabase) else StateDatabase(Path(state))
            )
        )
        self._generation = self._store.next_generation()
        # U0c (Allen 2026-09-03): a saga that did not finish in its daemon
        # generation starts over after a restart -- it is never resumed.
        # Before the worker thread exists, every RUNNING journal row belongs
        # to a previous generation, so it is marked for compensation here;
        # ``recover`` then schedules the teardown, and desired-state re-runs
        # whatever is still wanted.  This must happen before ``_thread``
        # starts so the new generation can never observe a stale RUNNING
        # row as forward progress.
        self._restart_interrupted = self._store.interrupt_running_operations(
            "saga did not finish in its daemon generation; "
            "restart compensates instead of resuming",
            "SAGA_INTERRUPTED_BY_RESTART",
        )
        self._ports = ports
        self._router = router
        self._capacity = capacity
        self._completion_timeout = completion_timeout
        self._admission_deadline = admission_deadline
        self._operation_deadline = operation_deadline
        #: Monotonic start of each operations current re-drive life, used only
        #: to bound an unresolvable step against ``_operation_deadline``.
        self._operation_started_at: dict[str, float] = {}
        self._deadline_exceeded: set[str] = set()
        self._backoff = admission_backoff
        self._fault_after_effect = fault_after_effect
        self._fault_after_receipt_retire = fault_after_receipt_retire
        self._combined_shared_receipts = combined_shared_receipts
        self._queue: Queue[str | None] = Queue(maxsize=capacity)
        self._condition = threading.Condition()
        self._scheduled: set[str] = set()
        self._active: str | None = None
        # Reservations run outside the manager condition.  Drain must wait
        # for every accepted reservation to finish before declaring the
        # journal empty or stopping the consumer generation.
        self._reserving = 0
        self._closed = False
        self._crashed = False
        self._store_closed = False
        #: Optional observability sink (the daemon's event log in production).
        #: Thread faults are state first (``_crashed``/``_last_error``) and
        #: events second, so a failing sink can never mask or cause a death.
        self._event_sink = event_sink
        self._last_error: str | None = None
        self._recover_faults = 0
        # Consumer-thread-only bookkeeping for the event throttle's clock
        # floor (see _RECOVER_FAULT_EVENT_MIN_INTERVAL_S); monotonic, reset
        # per manager generation together with the fault count.
        self._last_recover_event_at = 0.0
        self._thread = threading.Thread(
            target=self._run, name="hyprial-lifecycle-process-manager", daemon=True
        )
        self._thread.start()
        self.recover()

    @property
    def crashed(self) -> bool:
        """Whether the consumer thread died; ``submit`` refuses in this state."""

        with self._condition:
            return self._crashed

    @property
    def last_error(self) -> str | None:
        """The fault that killed (or most recently stung) the consumer thread."""

        with self._condition:
            return self._last_error

    @property
    def is_running(self) -> bool:
        """Whether the consumer thread is alive (drained or crashed ⇒ False)."""

        return self._thread.is_alive()

    @property
    def state_db(self) -> StateDatabase:
        """The StateDatabase this journal was wired with.

        Reachability seam for the assembly invariant: the daemon must wire
        the SAME instance into the journal and the desired-state store, so
        "one transaction across both" is a fact about constructed objects,
        not a convention (tests/test_final_composition_lifecycle.py walks
        the assembled application down to this attribute).
        """

        return self._store._state_db

    def submit(self, operation: LifecycleOperation) -> PortAdmission:
        if not operation.operation_id.strip():
            raise ValueError("operation_id must not be blank")
        with self._condition:
            if self._closed or self._crashed:
                return PortAdmission.CLOSING
            self._reserving += 1
        try:
            _created, state = self._store.reserve(operation)
        finally:
            with self._condition:
                self._reserving -= 1
                self._condition.notify_all()
        if state not in {
            LifecycleState.COMPLETED,
            LifecycleState.COMPENSATED,
            LifecycleState.FAILED,
        }:
            # Closing prevents new reservations, but an operation accepted
            # before close retains durable custody and must still be driven.
            self._schedule(operation.operation_id)
        # Durable reserve is the custody transfer.  Queue saturation cannot
        # turn it back into an ephemeral rejection; recover/scanning retries.
        return PortAdmission.ACCEPTED

    def recover(self) -> None:
        for operation_id in self._store.pending():
            self._schedule(operation_id)

    def result(self, operation_id: str) -> LifecycleResult:
        return self._store.result(operation_id)

    def wait(self, operation_id: str, timeout: float) -> LifecycleResult:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            result = self.result(operation_id)
            if result.state not in {
                LifecycleState.RUNNING,
                LifecycleState.COMPENSATING,
            }:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"lifecycle operation still running: {operation_id}")
            with self._condition:
                if self._crashed:
                    # Nobody will drive this operation in the current manager
                    # generation; saying so now beats waiting out the full
                    # timeout for a settlement that cannot arrive.
                    raise TimeoutError(
                        "lifecycle manager thread is dead; "
                        f"{operation_id} will not settle in this generation"
                    )
                self._condition.wait(min(0.02, remaining))

    def drain(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            self._closed = True
        while True:
            # Reads can wait on SQLite and must not hold the condition needed
            # by the consumer to settle active work or an in-flight reserve.
            pending = self._store.pending()
            with self._condition:
                if self._active is None and self._reserving == 0 and not pending:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._crashed:
                    return False
                self._condition.wait(min(0.05, remaining))
        try:
            self._queue.put_nowait(None)
        except Full:
            return False
        self._thread.join(max(0.0, deadline - time.monotonic()))
        complete = not self._thread.is_alive()
        if complete and not self._store_closed:
            self._store.close()
            self._store_closed = True
        return complete

    def _schedule(self, operation_id: str) -> None:
        with self._condition:
            if operation_id in self._scheduled or self._active == operation_id:
                return
            try:
                self._queue.put_nowait(operation_id)
            except Full:
                return
            self._scheduled.add(operation_id)
            self._condition.notify_all()

    def _run(self) -> None:
        while True:
            try:
                operation_id = self._queue.get(timeout=0.05)
            except Empty:
                if not self._guarded_recover():
                    return
                if self._close_requested_and_drained():
                    return
                continue
            if operation_id is None:
                self._queue.task_done()
                return
            with self._condition:
                self._scheduled.discard(operation_id)
                self._active = operation_id
            self._operation_started_at.setdefault(operation_id, time.monotonic())
            try:
                self._execute(operation_id)
            except _InjectedManagerCrash as error:
                self._mark_crashed(
                    f"injected manager crash: {error.__cause__ or 'fault hook'}"
                )
                return
            except BaseException as error:
                # Preserve RUNNING/COMPENSATING journal state for a new
                # process-manager generation; unexpected faults are not
                # translated into a business rejection.  The exit is loud:
                # ``_mark_crashed`` records state, wakes waiters and emits
                # the thread_exited event, and ``submit`` refuses from here.
                self._mark_crashed(f"{type(error).__name__}: {error}")
                return
            finally:
                self._queue.task_done()
                try:
                    state = self._store.state(operation_id)
                except (OSError, sqlite3.OperationalError):
                    # An unreadable journal is not a terminal state; keep the
                    # re-drive marker so the operation deadline still bounds it.
                    state = None
                if state is not None and state not in {
                    LifecycleState.RUNNING,
                    LifecycleState.COMPENSATING,
                }:
                    self._operation_started_at.pop(operation_id, None)
                    self._deadline_exceeded.discard(operation_id)
                with self._condition:
                    self._active = None
                    self._condition.notify_all()
            if not self._guarded_recover():
                return

    def _guarded_recover(self) -> bool:
        """One recovery scan that cannot silently kill the consumer thread.

        Retryable store faults -- a locked database under write contention
        (the 2026-09-14 production thread death) or a transient I/O error --
        are journaled, backed off and retried on the next pass; the consumer
        thread only leaves via drain/close, so ``submit`` never returns
        ACCEPTED into a queue nobody drains.  Any other exception is a real
        crash: recorded, emitted, and exited loudly (``_crashed`` set).

        Returns False when the thread must exit.
        """

        try:
            self.recover()
        except (OSError, sqlite3.OperationalError) as error:
            self._note_recover_fault(error)
            return True
        except BaseException as error:
            self._mark_crashed(f"recover: {type(error).__name__}: {error}")
            return False
        if self._recover_faults:
            # Close the episode loudly too: a recovered line with the total
            # fault count, so a log read can tell a blip from a storm.
            self._emit("thread_recovered", consecutiveFaults=self._recover_faults)
        self._recover_faults = 0
        return True

    def _close_requested_and_drained(self) -> bool:
        """The Empty-branch exit check, with the same fault discipline."""

        with self._condition:
            if not self._closed:
                return False
        try:
            pending = bool(self._store.pending())
        except (OSError, sqlite3.OperationalError) as error:
            self._note_recover_fault(error)
            return False
        except BaseException as error:
            self._mark_crashed(f"close-drain check: {type(error).__name__}: {error}")
            return True
        return not pending

    def _note_recover_fault(self, error: BaseException) -> None:
        self._recover_faults += 1
        detail = f"{type(error).__name__}: {error}"
        with self._condition:
            self._last_error = detail
        now = time.monotonic()
        if (
            self._recover_faults == 1
            or (
                self._recover_faults % _RECOVER_FAULT_EVENT_EVERY == 0
                and now - self._last_recover_event_at
                >= _RECOVER_FAULT_EVENT_MIN_INTERVAL_S
            )
        ):
            self._last_recover_event_at = now
            self._emit(
                "thread_error",
                detail=detail,
                consecutiveFaults=self._recover_faults,
            )
        # Reuse the admission backoff cadence (bounded, already tuned); the
        # queue poll above keeps the loop responsive to real work meanwhile.
        time.sleep(self._backoff[min(self._recover_faults - 1, len(self._backoff) - 1)])

    def _mark_crashed(self, detail: str) -> None:
        with self._condition:
            self._crashed = True
            self._last_error = detail
            self._condition.notify_all()
        self._emit("thread_exited", detail=detail)

    def _emit(self, event: str, **fields: object) -> None:
        sink = self._event_sink
        if sink is None:
            return
        try:
            sink(event, **fields)
        except Exception:
            # The sink failing must never take the consumer thread down; the
            # state half (_crashed / _last_error) is already recorded, so the
            # fault stays visible through ps even when the log write fails.
            pass

    def _execute(self, operation_id: str) -> None:
        operation = self._store.load(operation_id)
        steps = _plan(operation)
        if self._store.state(operation_id) is LifecycleState.COMPENSATING:
            prior = self._store.result(operation_id)
            self._compensate(
                operation_id,
                steps,
                prior.error or "recovered compensation",
                prior.error_code,
            )
            return
        try:
            for ordinal, step in enumerate(steps):
                if self._store.effect_done(operation_id, step.name, "forward"):
                    self._retire_completed_receipt(operation_id, step, "forward")
                    continue
                receipt = self._store.effect_receipt(
                    operation_id, step.name, "forward"
                )
                if operation_id in self._deadline_exceeded and receipt is None:
                    detail = (
                        "lifecycle operation exceeded its "
                        f"{self._operation_deadline:g}s deadline; settled work is being compensated"
                    )
                    self._store.set_state(
                        operation_id, LifecycleState.COMPENSATING, detail,
                        "LIFECYCLE_OPERATION_TIMEOUT",
                    )
                    self._compensate(
                        operation_id, steps, detail, "LIFECYCLE_OPERATION_TIMEOUT"
                    )
                    return
                self._perform(operation_id, ordinal, step, "forward")
                if operation_id in self._deadline_exceeded:
                    detail = (
                        "lifecycle operation exceeded its "
                        f"{self._operation_deadline:g}s deadline; settled work is being compensated"
                    )
                    self._store.set_state(
                        operation_id, LifecycleState.COMPENSATING, detail,
                        "LIFECYCLE_OPERATION_TIMEOUT",
                    )
                    self._compensate(
                        operation_id, steps, detail, "LIFECYCLE_OPERATION_TIMEOUT"
                    )
                    return
            self._store.set_state(operation_id, LifecycleState.COMPLETED)
        except LifecycleStepFailed as error:
            self._store.set_state(
                operation_id,
                LifecycleState.COMPENSATING,
                str(error),
                error.code,
            )
            self._compensate(operation_id, steps, str(error), error.code)
        except LifecycleStepUnresolved:
            # The domain or I/O worker still owns an admitted effect.  It is
            # unsafe to compensate or declare success until a fenced receipt
            # arrives; a later recovery scan resubmits/reassociates it -- unless
            # the operation has outlived its deadline, in which case the effect
            # is not "slow" but stuck (card 104164aa (c)). Harness effects get
            # one domain-owned settlement attempt: an unacknowledged control
            # keeps custody, a completed start resumes forward progress, and a
            # fenced failed start may safely compensate. An unresolved stop
            # still fails terminally because reversing it would be unsafe.
            started = self._operation_started_at.get(operation_id)
            if (
                started is not None
                and time.monotonic() - started >= self._operation_deadline
            ):
                detail = (
                    "lifecycle operation exceeded its "
                    f"{self._operation_deadline:g}s deadline with a step still unresolved"
                )
                code = "LIFECYCLE_OPERATION_TIMEOUT"
                self._deadline_exceeded.add(operation_id)
                if (
                    step.domain == "harness"
                    and step.forward in {"ensure", "remove"}
                    and not self._store.effect_done(
                        operation_id, step.name, "forward"
                    )
                ):
                    receipt = self._store.effect_receipt(operation_id, step.name, "forward")
                    assert receipt is not None
                    payload = (
                        EnsureHarnessCommand(
                            receipt.correlation_id,
                            step.spec.harness,
                        )
                        if step.forward == "ensure"
                        else RemoveHarnessCommand(
                            receipt.correlation_id,
                            step.spec.harness.harness,
                            step.spec.harness.name,
                        )
                    )
                    request = LifecycleMutationRequest(
                        receipt.correlation_id,
                        receipt.attempt_token,
                        operation_id,
                        None,
                        payload,
                    )
                    try:
                        settled = self._ports.harness.fail_lifecycle(
                            request, code=code, detail=detail,
                            timeout=LIFECYCLE_WAIT_MARGIN_SECONDS / 2,
                        )
                    except TimeoutError:
                        # No actor acknowledgement is NOT a domain settlement.
                        # Keep custody and retry, never mutate its rows here.
                        return
                    if isinstance(settled, LifecycleMutationCompleted):
                        self._store.complete_effect(
                            operation_id, step.name, "forward", receipt.correlation_id,
                            provenance=settled.provenance,
                        )
                        self._retire_completed_receipt(operation_id, step, "forward")
                        if step.forward == "ensure":
                            self._store.set_state(
                                operation_id, LifecycleState.COMPENSATING,
                                detail, code,
                            )
                            self._compensate(operation_id, steps, detail, code)
                        else:
                            # A completed remove cannot be reversed; preserve
                            # success that won before the actor's failure fence.
                            self._deadline_exceeded.discard(operation_id)
                        return
                    code, detail = settled.code, settled.detail
                    if step.forward == "ensure":
                        # The harness actor has fenced the start and confirmed
                        # that no process effect can still complete.  Only that
                        # domain settlement makes it safe to reverse bind and
                        # the earlier create effects.
                        self._store.set_state(
                            operation_id,
                            LifecycleState.COMPENSATING,
                            detail,
                            code,
                        )
                        self._compensate(operation_id, steps, detail, code)
                        return
                    self._store.set_state(
                        operation_id, LifecycleState.FAILED, detail, code,
                    )
                    self._operation_started_at.pop(operation_id, None)
                    return
                # Non-Harness domains retain custody until their exact late
                # receipt settles. The deadline marker fences new forward work.
                return
            time.sleep(min(0.02, self._completion_timeout))

    def _compensate(
        self,
        operation_id: str,
        steps: tuple[_Step, ...],
        cause: str,
        error_code: str | None,
    ) -> None:
        completed = set(self._store.compensable_forward(operation_id))
        try:
            for ordinal, step in reversed(tuple(enumerate(steps))):
                if step.name not in completed:
                    continue
                if self._store.effect_done(operation_id, step.name, "compensation"):
                    self._retire_completed_receipt(operation_id, step, "compensation")
                    continue
                self._perform(operation_id, ordinal, step, "compensation")
        except LifecycleStepFailed as error:
            self._store.set_state(
                operation_id,
                LifecycleState.FAILED,
                f"{cause}; compensation failed: {error}",
                error_code,
            )
            return
        except LifecycleStepUnresolved:
            time.sleep(min(0.02, self._completion_timeout))
            return
        self._store.set_state(
            operation_id, LifecycleState.COMPENSATED, cause, error_code
        )

    def _perform(
        self,
        operation_id: str,
        ordinal: int,
        step: _Step,
        direction: str,
    ) -> None:
        attempt_token = f"{operation_id}:{direction}:{ordinal}"
        correlation = f"lifecycle:{attempt_token}"
        port = getattr(self._ports, step.domain)
        generation = self._generation if step.domain == "route" else port.generation
        version = ordinal + 1 if step.domain == "route" else port.version
        command = _command(
            step,
            direction,
            correlation=correlation,
            attempt_token=attempt_token,
            generation=generation,
            version=version,
        )
        expected_resource_token = (
            None
            if direction == "forward"
            else self._store.forward_resource_token(operation_id, step.name)
        )
        receipt = self._store.effect_receipt(operation_id, step.name, direction)
        if receipt is None:
            receipt = self._store.prepare_effect(
                operation_id,
                ordinal,
                step.name,
                direction,
                correlation,
                attempt_token,
            )
        request = LifecycleMutationRequest(
            receipt.correlation_id,
            receipt.attempt_token,
            operation_id,
            expected_resource_token,
            command,
        )
        try:
            waiter = self._router.register(
                receipt.correlation_id,
                attempt_token=receipt.attempt_token,
                generation=generation,
                versions=(
                    frozenset({version})
                    if step.domain == "route"
                    else frozenset({version, version + 1})
                ),
            )
        except CorrelationRouterOverloaded as error:
            raise LifecycleStepFailed(str(error)) from error
        try:
            self._store.mark_dispatched(operation_id, step.name, direction)
            self._admit(step.domain, request)
            event = waiter.wait(self._completion_timeout)
        except TimeoutError as error:
            waiter.cancel()
            raise LifecycleStepUnresolved(
                f"{step.name} completion deadline elapsed"
            ) from error
        except BaseException:
            waiter.cancel()
            raise
        if (
            isinstance(event, RouteMutationFailed)
            and event.code == "ROUTE_PARTIAL_CLEANUP"
        ):
            raise LifecycleStepUnresolved(f"{step.name} route cleanup remains partial")
        if isinstance(event, LifecycleMutationFailed):
            raise LifecycleStepFailed(
                f"{step.name} failed: {event.code}: {event.detail}",
                code=event.code,
            )
        if isinstance(event, (PortCommandRejected, RouteMutationFailed)):
            raise LifecycleStepFailed(
                f"{step.name} rejected: {event.code}: {event.detail}",
                code=event.code,
            )
        provenance = _completion_provenance(step.domain, event)
        self._store.complete_effect(
            operation_id,
            step.name,
            direction,
            correlation,
            provenance=provenance,
        )
        if self._fault_after_effect is not None:
            try:
                self._fault_after_effect(operation_id, step.name)
            except Exception as error:
                raise _InjectedManagerCrash() from error
        self._retire_completed_receipt(operation_id, step, direction)

    def _retire_completed_receipt(
        self, operation_id: str, step: _Step, direction: str
    ) -> None:
        retirement = self._store.receipt_retirement(operation_id, step.name, direction)
        port = getattr(self._ports, step.domain)
        if retirement is not None:
            if self._combined_shared_receipts and step.domain in {"session", "harness"}:
                combined = getattr(self._store, "retire_shared_receipt", None)
                if not callable(combined):
                    raise RuntimeError("shared receipt authority is unavailable")
                if not combined(
                    step.domain, operation_id, step.name, direction,
                    retirement.attempt_token, retirement.resource_token,
                ):
                    raise LifecycleStepUnresolved(
                        f"{step.name} shared receipt is not yet retireable"
                    )
                completed = self._store.completed_receipt(
                    operation_id, step.name, direction
                )
                # The durable row is already gone. This no-op domain call
                # releases its in-memory replay claim after the atomic
                # StatePersistence commit; a crash here is replayable.
                port.confirm_receipt_retired(
                    completed.attempt_token, completed.resource_token
                )
                return
            if not port.retire_receipt(
                retirement.attempt_token, retirement.resource_token
            ):
                raise LifecycleStepUnresolved(
                    f"{step.name} domain receipt is not yet retireable"
                )
            if self._fault_after_receipt_retire is not None:
                try:
                    self._fault_after_receipt_retire(operation_id, step.name)
                except Exception as error:
                    raise _InjectedManagerCrash() from error
            self._store.mark_receipt_retired(operation_id, step.name, direction)
        completed = self._store.completed_receipt(operation_id, step.name, direction)
        port.confirm_receipt_retired(completed.attempt_token, completed.resource_token)

    def _admit(self, domain: str, command: LifecycleMutationRequest) -> None:
        port = getattr(self._ports, domain)
        deadline = time.monotonic() + self._admission_deadline
        attempt = 0
        while True:
            admission = port.submit(command)
            if admission is PortAdmission.ACCEPTED:
                return
            if domain == "route" and admission is PortAdmission.OVERLOADED:
                route_command = command.payload
                if isinstance(route_command, (EnsureRouteCommand, DropRouteCommand)):
                    if self._ports.route.reassociate(
                        route_command.attempt_token,
                        correlation_id=route_command.correlation_id,
                        generation=route_command.generation,
                        version=route_command.version,
                    ):
                        return
            if admission is PortAdmission.CLOSING:
                raise LifecycleStepFailed(
                    f"{domain} command port is closing", code="PORT_CLOSING"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LifecycleStepFailed(
                    f"{domain} command port stayed overloaded",
                    code="PORT_OVERLOADED",
                )
            delay = self._backoff[min(attempt, len(self._backoff) - 1)]
            attempt += 1
            time.sleep(min(delay, remaining))
