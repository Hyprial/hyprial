# Decision record (file header; no inline comments by repository rule):
# - One generation rule (ABI v1 5.1): the generation increments exactly when
#   an actor enters running. A failure, a failed restart factory (row 4b),
#   stop and reset leave it unchanged; restart_scheduled carries the failed
#   generation g and child_restarted carries g + 1.
# - Kept mailbox (D2, ABI v1 6.3.5): the guardian owns each cell's mailbox
#   ledger, an insertion-ordered map of custody tokens. A failure drops only
#   the command that failed; every other admitted command stays queued and
#   the next generation executes it. restart_pending accepts tells, and the
#   backend thread parks on the cell condition (bounded waits) until the
#   replacement handler is installed. The pre-v1 restart dead-letter path
#   (ACTOR_RESTARTED_UNDELIVERED) no longer exists.
# - Dead letters keep the legacy reason codes on this facade engine until P8
#   (ACTOR_STOPPED_UNDELIVERED / ACTOR_QUARANTINED_UNDELIVERED), so no
#   published code changes while owners migrate (待 HQ 追认).
# - Accepted admissions are recorded in the thread-local admission trace so
#   an effect lane re-sends a completion only after its earlier copy left
#   the owner's mailbox.
# - Stop (ABI v1 5.4): acceptance happens exactly once per actor: state
#   stopping, pending restart cancelled, admission closed, one backend stop.
#   Every later or concurrent stop joins the same confirmation. Confirmation
#   is the backend's on_stop: queued commands are dead-lettered in admission
#   order, then child_stopped is emitted, then the state becomes stopped. A
#   timed-out stop stays stopping and sends no dead letters. A handler that
#   stops its own actor accepts or joins and returns True without waiting.
# - stop_gracefully() closes external admission and lets the queued commands
#   run (state stopping), and accepts the v1 stop once the cell is idle; an
#   actor that is not running is stopped at once. The ActorRuntime facade
#   that exposed this path was removed in P8.
# - Drain (5.5) is per target set: external admission closes for the
#   targets only, the engine waits until every target is idle at the same
#   instant and then stops them through the single stop path, so drained
#   actors emit child_stopped. Only an engine-wide drain refuses start(),
#   and it keeps refusing once drained. A failure while draining follows the
#   normal supervision machine.
# - Events (11, CL-1): child_failed carries the failed command's type, or
#   None for a restart-factory failure (row 4b). A failure that crosses the
#   restart budget emits child_failed, then child_quarantined, then the
#   quarantine dead letters; never restart_scheduled. reset emits
#   restart_scheduled with delay 0 before the replacement re-enters running.
# - ask (10): the reply slot rides on the custody entry and is settled
#   after the command's events were emitted: the handler result, the
#   expected error itself, or ActorFailedError for a child failure. An ask
#   whose command is dead-lettered by stop or quarantine is settled with
#   ActorAdmissionError("closed") instead of waiting for its timeout
#   (待 HQ 追认; the reference oracle leaves it to the timeout).
# - failures_in_window (CL-9) is the length of the restart-budget deque: it
#   is appended and pruned only at a failure, frozen at quarantine and
#   cleared by reset. A snapshot never re-prunes it.
# - BaseException containment (6.2, CL-21): a restart factory raising any
#   BaseException is a row-4b child failure. Event and dead-letter sinks
#   that raise are isolated, including non-Exception BaseExceptions (CL-11;
#   swallowing those as well is the conservative choice, 待 HQ 追认).
# - begin_drain() is the "at start" half of drain on its own: it closes
#   external admission of the targets and returns, so a caller that owns
#   other drain targets (effect lanes) can wait for joint idleness first.
# - Internal admission (tell(..., internal=True)) bypasses drain closure and
#   the graceful-stop closure, never stopping, stopped or quarantined: it is
#   the path for engine-internal completion custody (DRN-09).
from __future__ import annotations

import random
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from hyprial.kernel.impl.actor_runtime.contracts  import (
    ActorBackend,
    ActorEvent,
    ActorEventKind,
    ActorHandle,
    ActorSnapshot,
    ActorSpec,
    ActorState,
    ActorAdmissionError,
    ActorFailedError,
    AdmissionResult,
    CommandHandler,
    DrainReport,
    EventSink,
    ExpectedActorError,
    trace_admission,
)
from hyprial.kernel.impl.actor_runtime.policies  import SupervisionPolicy
from hyprial.kernel.impl.actor_runtime.scheduler  import GenerationScheduler
from hyprial.kernel.impl.contracts.daemon import ipc_errors

STOPPED_UNDELIVERED = ipc_errors.ACTOR_STOPPED_UNDELIVERED
QUARANTINED_UNDELIVERED = ipc_errors.ACTOR_QUARANTINED_UNDELIVERED
_PARK_SECONDS = 0.25


class _Reply:
    __slots__ = ("done", "value", "error")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: object = None
        self.error: BaseException | None = None

    def settle(self, value: object = None, error: BaseException | None = None) -> None:
        self.value = value
        self.error = error
        self.done.set()


@dataclass(slots=True)
class _Custody:
    command: object
    reply: _Reply | None = None


@dataclass(frozen=True, slots=True)
class _Lease:
    handler: CommandHandler
    command: object
    generation: int
    reply: _Reply | None = None


@dataclass(slots=True)
class _ChildRecord:
    handle: ActorHandle
    spec: ActorSpec
    policy: SupervisionPolicy
    condition: threading.Condition
    handler: CommandHandler | None
    endpoint: object | None = None
    state: ActorState = ActorState.RUNNING
    generation: int = 1
    failures: deque[float] = field(default_factory=deque)
    mailbox: dict[str, _Custody] = field(default_factory=dict)
    executing: int | None = None
    draining: bool = False
    graceful: bool = False
    stop_accepted: bool = False
    confirmed: threading.Event = field(default_factory=threading.Event)


class _CellDispatch:
    __slots__ = ("_guardian", "_actor_id")

    def __init__(self, guardian: ActorGuardian, actor_id: str) -> None:
        self._guardian = guardian
        self._actor_id = actor_id

    def begin(self, custody_token: str) -> _Lease | None:
        return self._guardian._begin(self._actor_id, custody_token)

    def finish(
        self, lease: _Lease, result: object, failure: BaseException | None
    ) -> None:
        self._guardian._finish(self._actor_id, lease, result, failure)

    def stopped(self) -> None:
        self._guardian._confirm_stop(self._actor_id)


class ActorGuardian:
    """Daemon-owned authority for child lifecycle and restart decisions."""

    def __init__(
        self,
        *,
        policies: Mapping[str, SupervisionPolicy],
        event_sink: EventSink,
        scheduler: GenerationScheduler,
        random_source: Callable[[], float] = random.random,
        backend: ActorBackend,
    ) -> None:
        self._policies = policies
        self._event_sink = event_sink
        self._scheduler = scheduler
        self._random_source = random_source
        self._backend = backend
        self._lock = threading.RLock()
        self._children: dict[str, _ChildRecord] = {}
        self._draining = False

    def start(self, spec: ActorSpec) -> ActorHandle:
        try:
            policy = self._policies[spec.supervision_profile]
        except KeyError as exc:
            raise ValueError(
                f"unknown supervision profile: {spec.supervision_profile}"
            ) from exc
        with self._lock:
            if self._draining:
                raise RuntimeError("actor runtime is draining")
        handle = ActorHandle(actor_id=uuid.uuid4().hex, name=spec.name)
        handler = spec.handler_factory()
        record = _ChildRecord(
            handle=handle,
            spec=spec,
            policy=policy,
            condition=threading.Condition(self._lock),
            handler=handler,
        )
        endpoint = self._backend.open(
            handle=handle, dispatch=_CellDispatch(self, handle.actor_id)
        )
        with self._lock:
            record.endpoint = endpoint
            if not self._draining:
                self._children[handle.actor_id] = record
                return handle
        self._backend.request_stop(endpoint)
        raise RuntimeError("actor runtime started draining during start")

    def tell(
        self, handle: ActorHandle, command: object, *, internal: bool = False
    ) -> AdmissionResult:
        return self._enqueue(handle, _Custody(command), internal=internal)

    def ask(self, handle: ActorHandle, command: object, timeout: float) -> object:
        """Run ``command`` and return the handler's result (ABI v1 10)."""

        reply = _Reply()
        admission = self._enqueue(handle, _Custody(command, reply), internal=False)
        if admission is AdmissionResult.OVERLOADED:
            raise ActorAdmissionError("overloaded")
        if admission is not AdmissionResult.ACCEPTED:
            raise ActorAdmissionError("closed")
        if not reply.done.wait(max(0.0, timeout)):
            raise TimeoutError("ask timed out")
        if reply.error is not None:
            raise reply.error
        return reply.value

    def _enqueue(
        self, handle: ActorHandle, custody: _Custody, *, internal: bool
    ) -> AdmissionResult:
        with self._lock:
            record = self._resolve(handle)
            if record is None or not self._admits(record, internal=internal):
                return AdmissionResult.CLOSED
            if len(record.mailbox) >= record.spec.mailbox_capacity:
                return AdmissionResult.OVERLOADED
            custody_token = uuid.uuid4().hex
            record.mailbox[custody_token] = custody
            admission = self._backend.tell(record.endpoint, custody_token)
            if admission is not AdmissionResult.ACCEPTED:
                record.mailbox.pop(custody_token, None)
                return admission
        actor_id = handle.actor_id
        trace_admission(lambda: self._still_queued(actor_id, custody_token))
        return admission

    def snapshot(self, handle: ActorHandle) -> ActorSnapshot:
        with self._lock:
            record = self._require_child(handle)
            return ActorSnapshot(
                handle=record.handle,
                state=record.state,
                generation=record.generation,
                queued=len(record.mailbox),
                in_flight=0 if record.executing is None else 1,
                failures_in_window=len(record.failures),
            )

    def stop(self, handle: ActorHandle, timeout: float) -> bool:
        """ABI v1 stop: True once confirmed, or at once for a self-stop."""

        with self._lock:
            record = self._resolve(handle)
            if record is None:
                return False
            if record.confirmed.is_set():
                return True
            self_call = record.executing == threading.get_ident()
            endpoint = None if record.stop_accepted else self._accept_stop(record)
        self._request_stop(endpoint)
        if self_call:
            return True
        return record.confirmed.wait(max(0.0, timeout))

    def stop_gracefully(self, handle: ActorHandle, timeout: float) -> bool:
        """Facade stop: run the queued commands first, then the v1 stop."""

        with self._lock:
            record = self._require_child(handle)
            if record.confirmed.is_set():
                return True
            self_call = record.executing == threading.get_ident()
            endpoint = None
            if not record.stop_accepted and not record.graceful:
                busy = bool(record.mailbox) or record.executing is not None
                if record.state is ActorState.RUNNING and busy:
                    record.graceful = True
                    record.state = ActorState.STOPPING
                    record.condition.notify_all()
                else:
                    endpoint = self._accept_stop(record)
        self._request_stop(endpoint)
        if self_call:
            return True
        return record.confirmed.wait(max(0.0, timeout))

    def reset(self, handle: ActorHandle) -> bool:
        with self._lock:
            record = self._require_child(handle)
            if (
                self._draining
                or record.draining
                or record.state is not ActorState.QUARANTINED
            ):
                return False
            record.failures.clear()
            generation = record.generation
            record.state = ActorState.RESTART_PENDING
        self._emit(
            ActorEvent(
                kind=ActorEventKind.RESTART_SCHEDULED,
                handle=handle,
                generation=generation,
                restart_delay=0.0,
            )
        )
        return self._scheduler.schedule(
            handle.actor_id,
            generation,
            0.0,
            lambda scheduled_generation: self._restart(
                handle.actor_id, scheduled_generation
            ),
        )

    def drain(
        self, timeout: float, handles: Sequence[ActorHandle] | None = None
    ) -> DrainReport:
        """Drain every actor (``handles`` None) or exactly the listed cell."""

        started = time.monotonic()
        deadline = started + max(0.0, timeout)
        targets = self._close_for_drain(handles)
        endpoints: list[object | None] = []
        while True:
            with self._lock:
                if all(
                    record.confirmed.is_set() or self._idle(record)
                    for record in targets
                ):
                    endpoints = [
                        self._accept_stop(record)
                        for record in targets
                        if not record.stop_accepted and not record.confirmed.is_set()
                    ]
                    break
            if time.monotonic() >= deadline:
                return self._drain_report(started, targets)
            time.sleep(min(0.002, max(0.0, deadline - time.monotonic())))
        for endpoint in endpoints:
            self._request_stop(endpoint)
        for record in targets:
            record.confirmed.wait(max(0.0, deadline - time.monotonic()))
        return self._drain_report(started, targets)

    def begin_drain(self, handles: Sequence[ActorHandle]) -> None:
        """Close external admission of ``handles`` without waiting (5.5 at start)."""

        self._close_for_drain(handles)

    def _close_for_drain(
        self, handles: Sequence[ActorHandle] | None
    ) -> tuple[_ChildRecord, ...]:
        with self._lock:
            if handles is None:
                self._draining = True
                targets = tuple(self._children.values())
            else:
                targets = tuple(self._require_child(handle) for handle in handles)
            for record in targets:
                record.draining = True
            return targets

    def _begin(self, actor_id: str, custody_token: str) -> _Lease | None:
        with self._lock:
            while True:
                record = self._children.get(actor_id)
                if record is None:
                    return None
                custody = record.mailbox.get(custody_token)
                if custody is None:
                    return None
                runnable = not record.stop_accepted and (
                    record.state is ActorState.RUNNING
                    or (record.state is ActorState.STOPPING and record.graceful)
                )
                if runnable and record.handler is not None:
                    del record.mailbox[custody_token]
                    record.executing = threading.get_ident()
                    return _Lease(
                        record.handler,
                        custody.command,
                        record.generation,
                        custody.reply,
                    )
                if (
                    record.state is not ActorState.RESTART_PENDING
                    or record.stop_accepted
                ):
                    return None
                record.condition.wait(_PARK_SECONDS)

    def _finish(
        self,
        actor_id: str,
        lease: _Lease,
        result: object,
        failure: BaseException | None,
    ) -> None:
        record = self._children.get(actor_id)
        if record is None:
            return
        command_type = type(lease.command).__name__
        if failure is None:
            self._emit(
                ActorEvent(
                    kind=ActorEventKind.COMMAND_COMPLETED,
                    handle=record.handle,
                    generation=lease.generation,
                    command_type=command_type,
                )
            )
        elif isinstance(failure, ExpectedActorError):
            self._emit(
                ActorEvent(
                    kind=ActorEventKind.COMMAND_REJECTED,
                    handle=record.handle,
                    generation=lease.generation,
                    command_type=command_type,
                    code=failure.code,
                    detail=failure.detail,
                )
            )
        unexpected = failure is not None and not isinstance(
            failure, ExpectedActorError
        )
        endpoint = None
        with self._lock:
            record.executing = None
            record.condition.notify_all()
            if (
                not unexpected
                and record.graceful
                and not record.stop_accepted
                and not record.mailbox
            ):
                endpoint = self._accept_stop(record)
        self._request_stop(endpoint)
        if unexpected:
            assert failure is not None
            self._process_failure(
                actor_id,
                lease.generation,
                failure,
                expected_state=ActorState.RUNNING,
                command_type=type(lease.command).__name__,
            )
        if lease.reply is None:
            return
        if failure is None:
            lease.reply.settle(value=result)
        elif unexpected:
            lease.reply.settle(
                error=ActorFailedError(f"{type(failure).__name__}: {failure}")
            )
        else:
            lease.reply.settle(error=failure)

    def _confirm_stop(self, actor_id: str) -> None:
        with self._lock:
            record = self._children.get(actor_id)
            if record is None or record.confirmed.is_set():
                return
            undelivered = tuple(record.mailbox.values())
            record.mailbox.clear()
            record.handler = None
            record.stop_accepted = True
            generation = record.generation
        self._dead_letter(record.spec, undelivered, STOPPED_UNDELIVERED)
        self._emit(
            ActorEvent(
                kind=ActorEventKind.CHILD_STOPPED,
                handle=record.handle,
                generation=generation,
            )
        )
        with self._lock:
            record.state = ActorState.STOPPED
            record.confirmed.set()
            record.condition.notify_all()

    def _restart_attempt_failed(
        self,
        actor_id: str,
        generation: int,
        failure: BaseException,
    ) -> None:
        self._process_failure(
            actor_id,
            generation,
            failure,
            expected_state=ActorState.RESTART_PENDING,
        )

    def _process_failure(
        self,
        actor_id: str,
        generation: int,
        failure: BaseException,
        *,
        expected_state: ActorState,
        command_type: str | None = None,
    ) -> None:
        now = time.monotonic()
        scheduled: tuple[int, float] | None = None
        scheduled_event: ActorEvent | None = None
        quarantine_event: ActorEvent | None = None
        undelivered: tuple[_Custody, ...] = ()
        stop_endpoint: object | None = None
        with self._lock:
            record = self._children.get(actor_id)
            if record is None or record.generation != generation:
                return
            failed_event = ActorEvent(
                kind=ActorEventKind.CHILD_FAILED,
                handle=record.handle,
                generation=generation,
                command_type=command_type,
                code=type(failure).__name__,
                detail=str(failure),
            )
            stopping = record.stop_accepted or record.graceful
            if not stopping and record.state is not expected_state:
                return
            record.handler = None
            if stopping:
                if not record.stop_accepted:
                    stop_endpoint = self._accept_stop(record)
            elif self._record_failure(record, now) > record.policy.max_restarts:
                record.state = ActorState.QUARANTINED
                undelivered = tuple(record.mailbox.values())
                record.mailbox.clear()
                quarantine_event = ActorEvent(
                    kind=ActorEventKind.CHILD_QUARANTINED,
                    handle=record.handle,
                    generation=generation,
                    code=type(failure).__name__,
                    detail=str(failure),
                )
            else:
                record.state = ActorState.RESTART_PENDING
                delay = record.policy.delay(len(record.failures), self._random_source())
                scheduled = (record.generation, delay)
                scheduled_event = ActorEvent(
                    kind=ActorEventKind.RESTART_SCHEDULED,
                    handle=record.handle,
                    generation=record.generation,
                    restart_delay=delay,
                )
            record.condition.notify_all()
        self._emit(failed_event)
        self._request_stop(stop_endpoint)
        if quarantine_event is not None:
            self._emit(quarantine_event)
        self._dead_letter(record.spec, undelivered, QUARANTINED_UNDELIVERED)
        if scheduled is not None:
            next_generation, delay = scheduled
            assert scheduled_event is not None
            self._emit(scheduled_event)
            self._scheduler.schedule(
                actor_id,
                next_generation,
                delay,
                lambda scheduled_generation: self._restart(
                    actor_id, scheduled_generation
                ),
            )

    def _restart(self, actor_id: str, generation: int) -> None:
        with self._lock:
            record = self._children.get(actor_id)
            if (
                record is None
                or record.stop_accepted
                or record.generation != generation
                or record.state is not ActorState.RESTART_PENDING
            ):
                return
            spec = record.spec
            handle = record.handle
        try:
            handler = spec.handler_factory()
        except BaseException as exc:
            self._restart_attempt_failed(actor_id, generation, exc)
            return
        with self._lock:
            if (
                record.stop_accepted
                or record.generation != generation
                or record.state is not ActorState.RESTART_PENDING
            ):
                return
            record.handler = handler
            record.generation = generation + 1
            record.state = ActorState.RUNNING
            record.condition.notify_all()
        self._emit(
            ActorEvent(
                kind=ActorEventKind.CHILD_RESTARTED,
                handle=handle,
                generation=generation + 1,
            )
        )

    def _still_queued(self, actor_id: str, custody_token: str) -> bool:
        with self._lock:
            record = self._children.get(actor_id)
            return record is not None and custody_token in record.mailbox

    def _admits(self, record: _ChildRecord, *, internal: bool) -> bool:
        if record.endpoint is None or record.stop_accepted:
            return False
        if record.state is ActorState.STOPPING:
            return internal and record.graceful
        if record.state not in (ActorState.RUNNING, ActorState.RESTART_PENDING):
            return False
        return internal or not record.draining

    def _accept_stop(self, record: _ChildRecord) -> object | None:
        """Accept the one stop of ``record``; caller holds the lock."""

        record.stop_accepted = True
        record.state = ActorState.STOPPING
        self._scheduler.cancel(record.handle.actor_id)
        record.condition.notify_all()
        return record.endpoint

    def _request_stop(self, endpoint: object | None) -> None:
        if endpoint is not None:
            self._backend.request_stop(endpoint)

    def _drain_report(
        self, started: float, targets: tuple[_ChildRecord, ...]
    ) -> DrainReport:
        remaining = tuple(
            record.handle for record in targets if not record.confirmed.is_set()
        )
        return DrainReport(
            complete=not remaining,
            elapsed=time.monotonic() - started,
            remaining=remaining,
        )

    def _idle(self, record: _ChildRecord) -> bool:
        return record.executing is None and not record.mailbox

    def _record_failure(self, record: _ChildRecord, now: float) -> int:
        record.failures.append(now)
        self._prune_failures(record, now)
        return len(record.failures)

    def _prune_failures(self, record: _ChildRecord, now: float) -> None:
        cutoff = now - record.policy.restart_window
        while record.failures and record.failures[0] < cutoff:
            record.failures.popleft()

    def _resolve(self, handle: ActorHandle) -> _ChildRecord | None:
        record = self._children.get(handle.actor_id)
        if record is None or record.handle != handle:
            return None
        return record

    def _require_child(self, handle: ActorHandle) -> _ChildRecord:
        record = self._resolve(handle)
        if record is None:
            raise KeyError(f"unknown actor handle: {handle.actor_id}")
        return record

    def _emit(self, event: ActorEvent) -> None:
        record = self._children.get(event.handle.actor_id)
        spec_sink = None if record is None else record.spec.event_sink
        if spec_sink is not None:
            try:
                spec_sink(event)
            except BaseException:
                pass
        try:
            self._event_sink(event)
        except BaseException:
            return

    @staticmethod
    def _dead_letter(
        spec: ActorSpec, custodies: tuple[_Custody, ...], reason_code: str
    ) -> None:
        sink = spec.undelivered_sink
        for custody in custodies:
            if sink is not None:
                try:
                    sink(custody.command, reason_code)
                except BaseException:
                    pass
            if custody.reply is not None:
                custody.reply.settle(error=ActorAdmissionError("closed"))
