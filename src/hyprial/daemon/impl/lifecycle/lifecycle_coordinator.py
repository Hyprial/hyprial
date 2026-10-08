"""Mailbox-owned lifecycle scheduling with bounded, independent domain effects.

The journal and domain ports retain durable custody. The actor only owns the
in-memory admission, resource scheduling and completion projections; neither
SQLite nor a domain ``submit`` executes in its mailbox.
"""

from __future__ import annotations

import threading
import time
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from queue import Full, Queue
from typing import Callable, cast
from uuid import uuid4

from hyprial.kernel import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.kernel import PortAdmission
from hyprial.kernel import (
    LIFECYCLE_OPERATION_DEADLINE_SECONDS, LIFECYCLE_WAIT_MARGIN_SECONDS,
)

from hyprial.daemon.impl.correlation.correlation  import CorrelationEventRouter
from hyprial.daemon.impl.harnesses.runtime.ports  import EnsureHarnessCommand, RemoveHarnessCommand
from hyprial.kernel import LifecycleMutationCompleted
from hyprial.kernel import LifecycleMutationRequest
from hyprial.daemon.impl.lifecycle_manager  import (
    LifecycleOperation,
    LifecyclePorts, LifecycleProcessManager, LifecycleResult, LifecycleState,
    LifecycleStepFailed, LifecycleStepUnresolved,
    _LifecycleStore, _Step, _plan,
)
from hyprial.daemon.impl.lifecycle_manager.steps import _command
from hyprial.daemon.impl.state_db  import StateDatabase

#: A compensation step still unsettled at the operation deadline; the
#: operation ends FAILED and its resources are released.
LIFECYCLE_COMPENSATION_TIMEOUT = "LIFECYCLE_COMPENSATION_TIMEOUT"

#: How often a waiting caller re-reads the journal when no completion was
#: observed in the coordinator's cache (a safety net, not the wake path).
LIFECYCLE_WAIT_STORE_RECHECK_SECONDS = 1.0

# Resource scheduling key namespace; these keys are not canonical Agent URIs.
_AGENT_RESOURCE_NAMESPACE = "agent"


@dataclass(frozen=True, slots=True)
class BeginOperation:
    operation: LifecycleOperation
    request_id: str


@dataclass(frozen=True, slots=True)
class ReservationCompleted:
    operation: LifecycleOperation
    request_id: str
    state: LifecycleState | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class DomainCompleted:
    operation_id: str
    result: LifecycleResult | None = None
    error_code: str | None = None
    error_detail: str | None = None


@dataclass(frozen=True, slots=True)
class Recovered:
    operations: tuple[LifecycleOperation, ...]


@dataclass(frozen=True, slots=True)
class _ReserveWork:
    operation: LifecycleOperation
    request_id: str


@dataclass(frozen=True, slots=True)
class _AdvanceWork:
    operation_id: str


def _resources(operation: LifecycleOperation) -> frozenset[str]:
    specs = (operation.target,) if operation.source is None else (operation.target, operation.source)
    return frozenset(
        key
        for spec in specs
        for key in (f"actor:{spec.actor}", f"{_AGENT_RESOURCE_NAMESPACE}:{spec.agent_name}",
                    f"harness:{spec.harness.harness}:{spec.harness.name}")
    )


class _StepEffects(LifecycleProcessManager):
    """Reuse the existing receipt and domain-command protocol, one step at a time."""

    def __init__(
        self, store: _LifecycleStore, ports: LifecyclePorts,
        router: CorrelationEventRouter, generation: int,
        completion_timeout: float, admission_deadline: float,
        admission_backoff: tuple[float, ...], combined_shared_receipts: bool,
        operation_deadline: float,
        event_sink: Callable[..., None] | None = None,
        live_resource: Callable[[str, str], bool] | None = None,
    ) -> None:
        self._event_sink = event_sink
        self._live_resource = live_resource
        self._store = store
        self._ports = ports
        self._router = router
        self._generation = generation
        self._completion_timeout = completion_timeout
        self._admission_deadline = admission_deadline
        self._backoff = admission_backoff
        self._combined_shared_receipts = combined_shared_receipts
        self._fault_after_effect = None
        self._fault_after_receipt_retire = None
        self._operation_deadline = operation_deadline
        self._started: dict[str, float] = {}
        # When each operation entered compensation in this generation; the
        # compensation gets its own deadline from that moment.
        self._compensation_started: dict[str, float] = {}
        self._deadline_exceeded: set[str] = set()
        self._started_lock = threading.Lock()

    def advance(self, operation_id: str) -> LifecycleResult:
        with self._started_lock:
            self._started.setdefault(operation_id, time.monotonic())
        operation = self._store.load(operation_id)
        steps = _plan(operation)
        state = self._store.state(operation_id)
        if state not in {LifecycleState.RUNNING, LifecycleState.COMPENSATING}:
            return self._store.result(operation_id)
        if state is LifecycleState.RUNNING:
            try:
                for ordinal, step in enumerate(steps):
                    if self._store.effect_done(operation_id, step.name, "forward"):
                        self._retire_completed_receipt(operation_id, step, "forward")
                        continue
                    receipt = self._store.effect_receipt(
                        operation_id, step.name, "forward"
                    )
                    if operation_id in self._deadline_exceeded and receipt is None:
                        self._begin_deadline_compensation(operation_id)
                        break
                    self._perform(operation_id, ordinal, step, "forward")
                    if operation_id in self._deadline_exceeded:
                        self._begin_deadline_compensation(operation_id)
                    break
                else:
                    self._store.set_state(operation_id, LifecycleState.COMPLETED)
            except LifecycleStepFailed as error:
                self._store.set_state(
                    operation_id, LifecycleState.COMPENSATING, str(error), error.code
                )
            except LifecycleStepUnresolved:
                self._settle_unresolved(operation_id, step)
        else:
            prior = self._store.result(operation_id)
            compensable = set(self._store.compensable_forward(operation_id))
            compensable -= self._live_harness_prerequisites(operation_id, steps)
            with self._started_lock:
                compensation_started = self._compensation_started.setdefault(
                    operation_id, time.monotonic()
                )
            try:
                for ordinal, step in reversed(tuple(enumerate(steps))):
                    if step.name not in compensable:
                        continue
                    if self._store.effect_done(operation_id, step.name, "compensation"):
                        self._retire_completed_receipt(operation_id, step, "compensation")
                        continue
                    self._perform(operation_id, ordinal, step, "compensation")
                    break
                else:
                    self._store.set_state(
                        operation_id, LifecycleState.COMPENSATED,
                        prior.error or "recovered compensation", prior.error_code,
                    )
            except LifecycleStepFailed as error:
                self._store.set_state(
                    operation_id, LifecycleState.FAILED,
                    f"{prior.error or 'compensation'}; compensation failed: {error}",
                    prior.error_code,
                )
            except LifecycleStepUnresolved:
                if time.monotonic() - compensation_started >= self._operation_deadline:
                    self._settle_unresolved_compensation(operation_id, ordinal, step, prior)
        result = self._store.result(operation_id)
        if result.state not in {LifecycleState.RUNNING, LifecycleState.COMPENSATING}:
            with self._started_lock:
                self._started.pop(operation_id, None)
                self._compensation_started.pop(operation_id, None)
                self._deadline_exceeded.discard(operation_id)
        return result

    def _settle_unresolved_compensation(
        self, operation_id: str, ordinal: int, step: _Step, prior: LifecycleResult,
    ) -> None:
        """End a compensation whose step never settled, instead of waiting forever.

        Before this, an unresolved compensation step was re-driven with no
        time limit while the operation kept its resources, so every later
        operation on the same actor queued behind it (production 0.5.0,
        2026-10-05: kanban's start stuck in compensating with its harness
        removal dispatched).  A harness step is first offered to the Harness
        owner's fenced settlement, as the forward deadline already does;
        whatever is still unconfirmed at the deadline ends FAILED, loudly, and
        the coordinator releases the operation's resources.
        """

        detail = (
            "lifecycle compensation exceeded its "
            f"{self._operation_deadline:g}s deadline with {step.name} unresolved"
        )
        code = LIFECYCLE_COMPENSATION_TIMEOUT
        receipt = self._store.effect_receipt(operation_id, step.name, "compensation")
        # A completed effect whose receipt cannot be retired yet needs no
        # settlement from the owner -- asking again would only retry the same
        # retirement and raise from here, re-driving the operation forever.
        effect_done = self._store.effect_done(operation_id, step.name, "compensation")
        if step.domain == "harness" and receipt is not None and not effect_done:
            payload = _command(
                step, "compensation",
                correlation=receipt.correlation_id,
                attempt_token=receipt.attempt_token,
                generation=self._ports.harness.generation,
                version=self._ports.harness.version,
            )
            request = LifecycleMutationRequest(
                receipt.correlation_id, receipt.attempt_token, operation_id,
                self._store.forward_resource_token(operation_id, step.name),
                payload,
            )
            try:
                settled = self._ports.harness.fail_lifecycle(
                    request, code=code, detail=detail,
                    timeout=LIFECYCLE_WAIT_MARGIN_SECONDS / 2,
                )
            except TimeoutError:
                settled = None
            if isinstance(settled, LifecycleMutationCompleted):
                # The Harness owner settled it after all: record the result
                # and let the next pass finish compensating.
                self._store.complete_effect(
                    operation_id, step.name, "compensation",
                    receipt.correlation_id, provenance=settled.provenance,
                )
                try:
                    self._retire_completed_receipt(operation_id, step, "compensation")
                except LifecycleStepUnresolved:
                    pass  # settled, but its receipt is still held: end below
                else:
                    return
        self._store.set_state(
            operation_id, LifecycleState.FAILED,
            f"{prior.error or 'compensation'}; {detail}",
            code,
        )
        self._emit(
            "compensation_deadline_failed",
            operationId=operation_id,
            step=step.name,
            ordinal=ordinal,
            errorCode=code,
        )
    def _live_harness_prerequisites(
        self, operation_id: str, steps: tuple[_Step, ...],
    ) -> frozenset[str]:
        """Steps that must stay because the harness they serve is still live.

        A harness removal can change nothing for two reasons: the harness is
        already gone, or a newer owner holds it under a newer token.  Only
        the second leaves a running harness, and the journal alone cannot
        tell them apart -- the deciding fact is whether the token the removal
        recorded is the harness's active token *now*, which the desired state
        owns.  When it is, everything ordered before ``harness.ensure`` in the
        same saga half (Agent record, binding, persona route) is a
        prerequisite of that live harness and is not compensated.  Production
        0.5.0, 2026-10-05: four late-settled starts released the bindings of
        harnesses that kept running.  Note this reads current state, so a
        replay later can decide differently if the live token has moved.
        """

        if self._live_resource is None:
            return frozenset()
        for ordinal, step in enumerate(steps):
            if step.domain != "harness" or step.forward != "ensure":
                continue
            if not self._store.effect_done(operation_id, step.name, "compensation"):
                continue
            try:
                token = self._store.completed_receipt(
                    operation_id, step.name, "compensation"
                ).resource_token
            except RuntimeError:
                continue
            key = f"harness:{step.spec.harness.harness}:{step.spec.harness.name}"
            if not self._live_resource(key, token):
                continue
            prefix = step.name[: -len("harness.ensure")]
            return frozenset(
                earlier.name
                for earlier in steps[:ordinal]
                if earlier.name.startswith(prefix)
            )
        return frozenset()

    def _begin_deadline_compensation(self, operation_id: str) -> None:
        self._store.set_state(
            operation_id,
            LifecycleState.COMPENSATING,
            (
                "lifecycle operation exceeded its "
                f"{self._operation_deadline:g}s deadline; settled work is being compensated"
            ),
            "LIFECYCLE_OPERATION_TIMEOUT",
        )

    def _settle_unresolved(self, operation_id: str, step: _Step) -> None:
        with self._started_lock:
            started = self._started[operation_id]
        if time.monotonic() - started < self._operation_deadline:
            return
        detail = (
            "lifecycle operation exceeded its "
            f"{self._operation_deadline:g}s deadline with a step still unresolved"
        )
        code = "LIFECYCLE_OPERATION_TIMEOUT"
        with self._started_lock:
            self._deadline_exceeded.add(operation_id)
        # A harness start may still own a process effect. Only the harness
        # actor's fenced settlement permits compensation of earlier steps.
        if (
            step.domain == "harness" and step.forward in {"ensure", "remove"}
            and not self._store.effect_done(operation_id, step.name, "forward")
        ):
            receipt = self._store.effect_receipt(operation_id, step.name, "forward")
            assert receipt is not None
            payload = (
                EnsureHarnessCommand(receipt.correlation_id, step.spec.harness)
                if step.forward == "ensure" else
                RemoveHarnessCommand(
                    receipt.correlation_id, step.spec.harness.harness,
                    step.spec.harness.name,
                )
            )
            request = LifecycleMutationRequest(
                receipt.correlation_id, receipt.attempt_token, operation_id,
                None, payload,
            )
            try:
                settled = self._ports.harness.fail_lifecycle(
                    request, code=code, detail=detail,
                    timeout=LIFECYCLE_WAIT_MARGIN_SECONDS / 2,
                )
            except TimeoutError:
                return  # No actor ack; admitted work still has custody.
            if isinstance(settled, LifecycleMutationCompleted):
                self._store.complete_effect(
                    operation_id, step.name, "forward", receipt.correlation_id,
                    provenance=settled.provenance,
                )
                self._retire_completed_receipt(operation_id, step, "forward")
                if step.forward == "ensure":
                    # A late start owns a new resource: settle it before
                    # compensating the expired create operation.
                    self._begin_deadline_compensation(operation_id)
                else:
                    # Removal success won before the owner's deadline control.
                    # The resource is gone; preserve that truthful result and
                    # finish cleanup rather than inventing a failed removal.
                    with self._started_lock:
                        self._deadline_exceeded.discard(operation_id)
                return
            code, detail = settled.code, settled.detail
            if step.forward == "ensure":
                self._store.set_state(
                    operation_id, LifecycleState.COMPENSATING, detail, code
                )
                return
            # A fenced failed remove cannot be reversed safely, but custody is
            # released only after the Harness owner acknowledged settlement.
            self._store.set_state(operation_id, LifecycleState.FAILED, detail, code)
            return
        # Agent, Session and Route expose no deadline-as-cancel control. Their
        # accepted effect retains custody until its exact receipt is recovered;
        # the deadline marker prevents any subsequent forward step.


class _Mailbox:
    def __init__(self, owner: LifecycleCoordinator) -> None:
        self.owner = owner

    def __call__(self, command: object) -> None:
        owner = self.owner
        with owner._condition:
            if isinstance(command, BeginOperation):
                if owner._closing:
                    owner._incoming.discard(command.request_id)
                    owner._answer_locked(command.request_id, PortAdmission.CLOSING)
                else:
                    try:
                        owner._effects.put_nowait(_ReserveWork(command.operation, command.request_id))
                    except Full:
                        owner._incoming.discard(command.request_id)
                        owner._overloaded += 1
                        owner._answer_locked(command.request_id, PortAdmission.OVERLOADED)
                    else:
                        owner._reserving += 1
            elif isinstance(command, ReservationCompleted):
                owner._reserving -= 1
                owner._incoming.discard(command.request_id)
                if command.error_code is not None:
                    error = owner._reply_errors.pop(command.request_id)
                    owner._answer_locked(command.request_id, error)
                else:
                    assert command.state is not None
                    if command.state in {LifecycleState.RUNNING, LifecycleState.COMPENSATING}:
                        owner._retain_operation_locked(command.operation)
                        owner._dispatch_locked()
                    owner._answer_locked(command.request_id, PortAdmission.ACCEPTED)
            elif isinstance(command, DomainCompleted):
                owner._running.discard(command.operation_id)
                operation = owner._active.get(command.operation_id)
                if command.error_code is not None:
                    owner._last_error = f"{command.error_code}: {command.error_detail}"
                    owner._errors += 1
                elif command.result is not None:
                    owner._results[command.operation_id] = command.result
                    if command.result.state not in {LifecycleState.RUNNING, LifecycleState.COMPENSATING}:
                        owner._pending.pop(command.operation_id, None)
                        owner._active.pop(command.operation_id, None)
                        owner._owned.discard(command.operation_id)
                        if operation is not None:
                            owner._held.difference_update(_resources(operation))
                owner._dispatch_locked()
            elif isinstance(command, Recovered):
                for operation in command.operations:
                    if (
                        operation.operation_id not in owner._owned
                        and len(owner._owned) + len(owner._incoming) >= owner._capacity
                    ):
                        break
                    owner._retain_operation_locked(operation)
                owner._dispatch_locked()
            else:
                raise TypeError(type(command).__name__)
            owner._condition.notify_all()


class LifecycleCoordinator:
    """Per-resource saga scheduler; accepted reservations are durable."""

    def __init__(
        self, state: Path | StateDatabase, ports: LifecyclePorts,
        router: CorrelationEventRouter, *, journal_store: object | None = None,
        capacity: int = 32, effect_workers: int = 4,
        completion_timeout: float = 2.0, admission_deadline: float = 1.0,
        operation_deadline: float = LIFECYCLE_OPERATION_DEADLINE_SECONDS,
        admission_backoff: tuple[float, ...] = (0.005, 0.01, 0.02),
        combined_shared_receipts: bool = False,
        event_sink: Callable[..., None] | None = None,
        live_resource: Callable[[str, str], bool] | None = None,
    ) -> None:
        if capacity < 1 or effect_workers < 1:
            raise ValueError("capacity and effect_workers must be positive")
        self._store = cast(_LifecycleStore, journal_store) if journal_store is not None else _LifecycleStore(
            state if isinstance(state, StateDatabase) else StateDatabase(Path(state))
        )
        generation = self._store.next_generation()
        self._store.interrupt_running_operations(
            "saga did not finish in its daemon generation; restart compensates instead of resuming",
            "SAGA_INTERRUPTED_BY_RESTART",
        )
        self._effects: Queue[_ReserveWork | _AdvanceWork | None] = Queue(maxsize=capacity)
        self._capacity = capacity
        self._condition = threading.Condition()
        self._pending: dict[str, LifecycleOperation] = {}
        self._active: dict[str, LifecycleOperation] = {}
        self._owned: set[str] = set()
        self._incoming: set[str] = set()
        self._running: set[str] = set()
        self._reserving = 0
        self._held: set[str] = set()
        self._results: dict[str, LifecycleResult] = {}
        self._answers: dict[str, Queue[PortAdmission | BaseException]] = {}
        self._reply_errors: dict[str, BaseException] = {}
        self._closing = False
        self._closed = False
        self._crashed = False
        self._overloaded = 0
        self._errors = 0
        self._last_error: str | None = None
        self._effect_workers = effect_workers
        self._effects_engine = _StepEffects(
            self._store, ports, router, generation, completion_timeout,
            admission_deadline, admission_backoff, combined_shared_receipts,
            operation_deadline, event_sink=event_sink, live_resource=live_resource,
        )
        self._runtime = ActorRuntime()
        self._handle = self._runtime.start(ActorSpec(
            name="lifecycle-coordinator", handler_factory=lambda: _Mailbox(self),
            mailbox_capacity=capacity,
        ))
        self._workers = tuple(threading.Thread(
            target=self._work, name=f"lifecycle-effect-{index}", daemon=True
        ) for index in range(effect_workers))
        for worker in self._workers:
            worker.start()
        self._recover_thread = threading.Thread(
            target=self._recover_loop, name="lifecycle-recover", daemon=True
        )
        self._recover_thread.start()

    @property
    def state_db(self) -> StateDatabase:
        return self._store._state_db

    @property
    def crashed(self) -> bool:
        with self._condition:
            return self._crashed or self._runtime.snapshot(self._handle).state.value == "quarantined"

    @property
    def is_running(self) -> bool:
        return any(worker.is_alive() for worker in self._workers)

    @property
    def last_error(self) -> str | None:
        with self._condition:
            return self._last_error

    def stats(self) -> dict[str, int]:
        with self._condition:
            return {"pending": len(self._pending), "active": len(self._active),
                    "reserving": self._reserving, "overloaded": self._overloaded,
                    "errors": self._errors,
                    "custody": len(self._owned) + len(self._incoming),
                    "capacity": self._capacity}

    def submit(self, operation: LifecycleOperation) -> PortAdmission:
        if not operation.operation_id.strip():
            raise ValueError("operation_id must not be blank")
        if self.crashed:
            return PortAdmission.CLOSING
        answer: Queue[PortAdmission | BaseException] = Queue(maxsize=1)
        request_id = uuid4().hex
        with self._condition:
            if self._closing:
                return PortAdmission.CLOSING
            if len(self._owned) + len(self._incoming) >= self._capacity:
                self._overloaded += 1
                return PortAdmission.OVERLOADED
            self._incoming.add(request_id)
            self._answers[request_id] = answer
        admission = self._runtime.tell(self._handle, BeginOperation(operation, request_id))
        if admission is AdmissionResult.OVERLOADED:
            with self._condition:
                self._incoming.discard(request_id)
                self._answers.pop(request_id, None)
            return PortAdmission.OVERLOADED
        if admission is not AdmissionResult.ACCEPTED:
            with self._condition:
                self._incoming.discard(request_id)
                self._answers.pop(request_id, None)
            return PortAdmission.CLOSING
        outcome = answer.get()  # Durable reservation is the acceptance point.
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def _answer_locked(
        self, request_id: str, outcome: PortAdmission | BaseException,
    ) -> None:
        answer = self._answers.pop(request_id, None)
        if answer is not None:
            answer.put_nowait(outcome)

    def recover(self) -> None:
        operations = tuple(self._store.load(item) for item in self._store.pending())
        self._runtime.tell(self._handle, Recovered(operations))

    def result(self, operation_id: str) -> LifecycleResult:
        return self._store.result(operation_id)

    def wait(self, operation_id: str, timeout: float) -> LifecycleResult:
        """Block until the operation is terminal, without polling the journal.

        A journal read is a store round trip (through the single state writer
        until reads move off it), so the old 50 ms re-read added each waiting
        caller's load to the work the operation needed to finish (lifecycle
        root-fix plan S5).  The coordinator's own result cache is updated and
        the condition notified on every ``DomainCompleted``; the journal is
        read once up front, once when the cache turns terminal, and otherwise
        at most every ``LIFECYCLE_WAIT_STORE_RECHECK_SECONDS`` -- measured
        from when the previous read *returned*, so a slow read is never
        followed straight away by the next one.
        """

        active = {LifecycleState.RUNNING, LifecycleState.COMPENSATING}
        deadline = time.monotonic() + max(0.0, timeout)
        result = self.result(operation_id)
        recheck_at = time.monotonic() + LIFECYCLE_WAIT_STORE_RECHECK_SECONDS
        reread_on_cache = False
        while result.state in active:
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError(f"lifecycle operation still running: {operation_id}")
            with self._condition:
                cached = self._results.get(operation_id)
                cache_terminal = cached is not None and cached.state not in active
                if not cache_terminal or reread_on_cache:
                    self._condition.wait(max(0.0, min(deadline, recheck_at) - now))
                    cached = self._results.get(operation_id)
                    cache_terminal = cached is not None and cached.state not in active
            if (cache_terminal and not reread_on_cache) or time.monotonic() >= recheck_at:
                reread_on_cache = reread_on_cache or cache_terminal
                result = self.result(operation_id)
                recheck_at = time.monotonic() + LIFECYCLE_WAIT_STORE_RECHECK_SECONDS
        return result

    def drain(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            self._closing = True
        while True:
            with self._condition:
                if (
                    not self._pending and not self._active and not self._reserving
                    and not self._incoming and not self._owned
                    and self._effects.unfinished_tasks == 0
                ):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(min(0.05, remaining))
        self._closed = True
        for _ in self._workers:
            self._effects.put(None)
        for worker in self._workers:
            worker.join(max(0.0, deadline - time.monotonic()))
        self._recover_thread.join(max(0.0, deadline - time.monotonic()))
        complete = all(not worker.is_alive() for worker in self._workers)
        complete &= not self._recover_thread.is_alive()
        complete &= self._runtime.stop(self._handle, timeout=max(0.0, deadline - time.monotonic()))
        if complete:
            self._store.close()
        return complete

    def _retain_operation_locked(self, operation: LifecycleOperation) -> None:
        operation_id = operation.operation_id
        result = self._results.get(operation_id)
        # Recovery reads and durable reservation replies can cross in flight.
        # A late snapshot must not enqueue an already active operation again,
        # or resurrect custody after this owner processed its terminal receipt.
        if operation_id in self._owned or (
            result is not None
            and result.state not in {LifecycleState.RUNNING, LifecycleState.COMPENSATING}
        ):
            return
        self._owned.add(operation_id)
        self._pending.setdefault(operation_id, operation)

    def _dispatch_locked(self) -> None:
        for operation_id in self._active:
            if operation_id in self._running:
                continue
            try:
                self._effects.put_nowait(_AdvanceWork(operation_id))
            except Full:
                self._overloaded += 1
                return
            self._running.add(operation_id)
        for operation_id, operation in tuple(self._pending.items()):
            if operation_id in self._active or _resources(operation) & self._held:
                continue
            try:
                self._effects.put_nowait(_AdvanceWork(operation_id))
            except Full:
                self._overloaded += 1
                break
            self._active[operation_id] = operation
            self._running.add(operation_id)
            self._held.update(_resources(operation))
            del self._pending[operation_id]

    def _work(self) -> None:
        while True:
            work = self._effects.get()
            try:
                if work is None:
                    return
                if isinstance(work, _ReserveWork):
                    try:
                        _, state = self._store.reserve(work.operation)
                        completion = ReservationCompleted(work.operation, work.request_id, state)
                    except BaseException as error:
                        with self._condition:
                            self._reply_errors[work.request_id] = error
                        completion = ReservationCompleted(
                            work.operation, work.request_id,
                            error_code=type(error).__name__,
                        )
                    self._deliver(completion)
                else:
                    try:
                        result = self._effects_engine.advance(work.operation_id)
                        completion = DomainCompleted(work.operation_id, result=result)
                        if result.state in {LifecycleState.RUNNING, LifecycleState.COMPENSATING}:
                            time.sleep(0.02)
                    except BaseException as error:
                        completion = DomainCompleted(
                            work.operation_id, error_code=type(error).__name__,
                            error_detail=str(error)[:500],
                        )
                    self._deliver(completion)
            finally:
                self._effects.task_done()

    def _deliver(self, command: ReservationCompleted | DomainCompleted | Recovered) -> None:
        while not self._closed:
            admission = self._runtime.tell(self._handle, command)
            if admission is AdmissionResult.ACCEPTED:
                return
            if admission is AdmissionResult.CLOSED:
                return
            time.sleep(0.005)

    def _recover_loop(self) -> None:
        while not self._closed:
            try:
                with self._condition:
                    available = max(
                        0, self._capacity - len(self._owned) - len(self._incoming)
                    )
                    owned = frozenset(self._owned)
                operations = tuple(
                    self._store.load(item)
                    for item in self._store.pending()
                    if item not in owned
                )[:available]
                if operations:
                    self._deliver(Recovered(operations))
            except (OSError, sqlite3.OperationalError) as error:
                with self._condition:
                    self._last_error = f"recover: {type(error).__name__}: {error}"
                    self._errors += 1
            except BaseException as error:
                with self._condition:
                    self._last_error = f"recover: {type(error).__name__}: {error}"
                    self._errors += 1
                    self._crashed = True
                    self._condition.notify_all()
                return
            for _ in range(10):
                if self._closed:
                    return
                time.sleep(0.02)
