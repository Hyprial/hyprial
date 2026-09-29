"""Per-process I/O ownership with cache-only facts for the Harness authority."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import uuid4

from hyprial.actor_runtime import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.actor_runtime.effects import EffectLane, EffectRequest, EffectCompleted
from .api import (
    HarnessDelivery,
    StreamingHarnessProcess,
    ProcessLiveness,
    ProcessLivenessState,
    ProcessLivenessProbeError,
)


@dataclass(frozen=True, slots=True)
class PollFacts:
    pass


@dataclass(frozen=True, slots=True)
class StopProcess:
    pass


@dataclass(frozen=True, slots=True)
class ReadLiveness:
    pass


@dataclass(frozen=True, slots=True)
class EnqueueTurn:
    delivery: HarnessDelivery


@dataclass(frozen=True, slots=True)
class DrainResults:
    limit: int | None = None


@dataclass(frozen=True, slots=True)
class DrainProgress:
    pass


@dataclass(frozen=True, slots=True)
class InterruptTurn:
    delivery_id: str
    timeout: float


@dataclass(frozen=True, slots=True)
class WaitReady:
    timeout: float


@dataclass(frozen=True, slots=True)
class PrepareInterruption:
    reason: str


@dataclass(frozen=True, slots=True)
class WorkerChannelFacts:
    session_ref: str | None


@dataclass(frozen=True, slots=True)
class ProcessFacts:
    running: bool
    pid: int | None
    last_error: str | None
    session_ref: str | None
    worker_channel: WorkerChannelFacts
    endpoint: str | None
    dsh_home: Path | None
    max_in_flight: int | None
    in_flight: int | None
    queue_depth: int | None
    liveness: ProcessLiveness | None


@dataclass(frozen=True, slots=True)
class ProcessOperation:
    operation_id: str
    generation: int
    command: (
        PollFacts
        | StopProcess
        | ReadLiveness
        | EnqueueTurn
        | DrainResults
        | DrainProgress
        | InterruptTurn
        | WaitReady
        | PrepareInterruption
    )


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    value: object
    facts: ProcessFacts


@dataclass(frozen=True, slots=True)
class ProcessFactsObserved:
    harness_id: str
    process_token: str
    running: bool


@dataclass
class _Reply:
    ready: threading.Event = field(default_factory=threading.Event)
    value: object = None
    error: BaseException | str | None = None
    facts: ProcessFacts | None = None


class _ProcessOwnerClosed(RuntimeError):
    """Admission is closed, distinct from capacity or runtime failures."""


class ProcessOwner:
    def __init__(
        self, process, *, capacity=32, timeout=65.0, poll_interval=0.5, observer=None
    ):
        self.process_token = uuid4().hex
        self._observer = observer
        self._process = process  # private to bootstrap and the I/O executor
        self._facts = self._read_facts()
        self._guard = threading.Lock()
        self._pending: dict[str, tuple[object, _Reply]] = {}
        self._errors: dict[str, BaseException] = {}
        self._capacity = capacity
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._generation = 1
        self._closing = False
        self._detached = False
        self._poll_pending = False
        self._stop_reply = None
        self._runtime = ActorRuntime()
        self._handle = self._runtime.start(
            ActorSpec(
                name="process-io-owner",
                handler_factory=lambda: self._receive,
                mailbox_capacity=capacity,
                undelivered_sink=self._undelivered,
            )
        )
        self._effects = EffectLane(
            name="process-native-io",
            execute=self._execute,
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=capacity,
        )

    def _undelivered(self, command: object, reason_code: str) -> None:
        if isinstance(command, ProcessOperation):
            self._settle(command.operation_id, None, reason_code)

    def _read_facts(self):
        process = self._process

        def integer(name):
            value = getattr(process, name, None)
            return value if isinstance(value, int) else None

        def string(name):
            value = getattr(process, name, None)
            return str(value) if value is not None else None

        channel = getattr(process, "worker_channel", None)
        home = getattr(process, "dsh_home", None)
        liveness = getattr(process, "liveness", None)
        return ProcessFacts(
            bool(process.running),
            integer("pid"),
            string("last_error"),
            string("session_ref"),
            WorkerChannelFacts(getattr(channel, "session_ref", None)),
            string("endpoint"),
            home if isinstance(home, Path) else None,
            integer("max_in_flight"),
            integer("in_flight"),
            integer("queue_depth"),
            liveness() if callable(liveness) else None,
        )

    def _execute(self, operation):
        try:
            return self._operate(operation.command)
        except Exception as error:
            # Preserve the driver's public exception contract without putting
            # mutable exception/traceback objects in completion messages.
            with self._guard:
                self._errors[operation.operation_id] = error
            raise

    def _operate(self, command):
        process = self._process
        if isinstance(command, PollFacts):
            value = None
        elif isinstance(command, StopProcess):
            value = process.stop()
        elif isinstance(command, ReadLiveness):
            value = process.liveness()
        elif isinstance(command, EnqueueTurn):
            value = process.enqueue(command.delivery)
        elif isinstance(command, DrainResults):
            value = tuple(
                process.drain_results()
                if command.limit is None
                else process.drain_results(command.limit)
            )
        elif isinstance(command, DrainProgress):
            method = getattr(process, "drain_progress", None)
            value = tuple(method()) if callable(method) else ()
        elif isinstance(command, InterruptTurn):
            value = process.interrupt(command.delivery_id, timeout=command.timeout)
        elif isinstance(command, WaitReady):
            method = getattr(process, "wait_ready", None)
            value = (
                bool(method(timeout=command.timeout))
                if callable(method)
                else bool(process.running)
            )
        elif isinstance(command, PrepareInterruption):
            method = getattr(process, "prepare_daemon_interruption", None)
            value = method(command.reason) if callable(method) else None
        else:
            raise TypeError("unsupported process operation")
        try:
            facts = self._read_facts()
        except Exception as error:
            if not isinstance(command, (EnqueueTurn, DrainResults, DrainProgress)):
                raise
            # The native operation already transferred custody. A subsequent
            # diagnostic probe must not turn its success/result into failure.
            with self._guard:
                prior = self._facts
            detail = f"process facts probe failed: {type(error).__name__}"
            facts = replace(
                prior,
                last_error=prior.last_error or detail,
                liveness=ProcessLiveness(
                    ProcessLivenessState.UNKNOWN,
                    observed=False,
                    pid=prior.pid,
                    detail=detail,
                ),
            )
        if isinstance(command, PollFacts):
            # A caller that joins this exact observation must receive its
            # immutable facts, rather than race a later cache read.
            value = facts
        return ProcessOutcome(value, facts)

    def _receive(self, event):
        if isinstance(event, ProcessOperation):
            admission = self._effects.submit(
                EffectRequest(event.operation_id, event.generation, event)
            )
            if admission is not AdmissionResult.ACCEPTED:
                self._settle(event.operation_id, None, f"process I/O {admission.value}")
            return
        if not isinstance(event, EffectCompleted):
            raise TypeError("unsupported process completion")
        if event.generation == self._generation:
            self._settle(event.operation_id, event.result, event.error)
        self._effects.acknowledge(event.operation_id, event.generation)

    def _settle(self, operation_id, outcome, error):
        with self._guard:
            pending = self._pending.pop(operation_id, None)
            if pending is None:
                return
            command, reply = pending
            if outcome is not None:
                self._facts = outcome.facts
                reply.value = outcome.value
                reply.facts = outcome.facts
            reply.error = self._errors.pop(operation_id, error)
            if isinstance(command, PollFacts):
                self._poll_pending = False
            reply.ready.set()
        if outcome is not None and self._observer is not None:
            try:
                self._observer(self.process_token, outcome.facts)
            except Exception:
                pass  # recoverable projection will be repeated by the next cadence

    def _submit(self, command):
        reply = _Reply()
        with self._guard:
            if isinstance(command, StopProcess) and self._stop_reply is not None:
                if (
                    not self._stop_reply.ready.is_set()
                    or self._stop_reply.error is None
                ):
                    return self._stop_reply
            if self._detached or (self._closing and not isinstance(command, StopProcess)):
                raise _ProcessOwnerClosed("process owner closed or overloaded")
            if len(self._pending) >= self._capacity:
                raise RuntimeError("process owner closed or overloaded")
            operation_id = uuid4().hex
            self._pending[operation_id] = (command, reply)
            result = self._runtime.tell(
                self._handle, ProcessOperation(operation_id, self._generation, command)
            )
            if result is not AdmissionResult.ACCEPTED:
                self._pending.pop(operation_id)
                raise RuntimeError(f"process owner {result.value}")
            if isinstance(command, StopProcess):
                self._closing = True
                self._stop_reply = reply
        return reply

    def _wait(self, reply, timeout=None, *, settled=False):
        budget = None if settled else self._timeout if timeout is None else timeout
        if not reply.ready.wait(budget):
            raise TimeoutError("accepted process operation has not settled")
        if reply.error is not None:
            if isinstance(reply.error, BaseException):
                raise reply.error
            raise RuntimeError(reply.error)
        return reply.value

    def _call(self, command, timeout=None, *, settled=False):
        return self._wait(self._submit(command), timeout, settled=settled)

    def refresh(self):
        with self._guard:
            if self._closing or self._poll_pending:
                return False
            self._poll_pending = True
        try:
            self._submit(PollFacts())
            return True
        except RuntimeError:
            with self._guard:
                self._poll_pending = False
            return False

    _poll = refresh

    def observe_facts(self) -> ProcessFacts:
        """Join one admitted native facts read off the Harness mailbox."""

        try:
            reply = self._submit(PollFacts())
        except _ProcessOwnerClosed:
            # A removal can close poll admission after the Harness captured
            # this owner. Join its already-admitted stop instead: that receipt
            # carries the final immutable facts, without reopening native I/O.
            with self._guard:
                stop_reply = None if self._detached else self._stop_reply
            if stop_reply is None:
                raise
            self._wait(stop_reply, settled=True)
            if stop_reply.facts is None:
                raise RuntimeError("accepted process stop did not return facts")
            return stop_reply.facts
        observed = self._wait(reply, settled=True)
        assert isinstance(observed, ProcessFacts)
        return observed

    def facts(self):
        with self._guard:
            return self._facts

    @property
    def running(self):
        return self.facts().running

    @property
    def pid(self):
        return self.facts().pid

    @property
    def last_error(self):
        return self.facts().last_error

    @property
    def session_ref(self):
        return self.facts().session_ref

    @property
    def worker_channel(self):
        return self.facts().worker_channel

    @property
    def endpoint(self):
        return self.facts().endpoint

    @property
    def dsh_home(self):
        return self.facts().dsh_home

    @property
    def max_in_flight(self):
        return self.facts().max_in_flight

    @property
    def in_flight(self):
        return self.facts().in_flight

    @property
    def queue_depth(self):
        return self.facts().queue_depth

    def liveness(self):
        observation = self.facts().liveness
        if observation is None:
            raise ProcessLivenessProbeError(
                "process has no observed liveness projection"
            )
        return observation

    def wait_ready(self, *, timeout=1.0):
        return self._call(WaitReady(timeout), timeout + 1.0)

    def prepare_daemon_interruption(self, reason):
        return self._call(PrepareInterruption(reason))

    def stop(self):
        reply = self._submit(StopProcess())
        self._wait(reply)
        if not self._effects.close(1.0) or not self._runtime.stop(self._handle, 1.0):
            raise TimeoutError("process owner did not drain after stop")

    def detach(self, timeout=1.0):
        """Drain adapter custody without signaling a native process.

        Used when the Harness authority closes or PID identity fencing refuses
        a stop. It must never turn an identity mismatch into a native stop.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            self._closing = True
            self._detached = True
        while True:
            with self._guard:
                pending = bool(self._pending)
            if not pending:
                break
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        if not self._effects.close(max(0.0, deadline - time.monotonic())):
            return False
        return self._runtime.stop(self._handle, max(0.0, deadline - time.monotonic()))


class StreamingProcessOwner(ProcessOwner):
    def enqueue(self, delivery):
        # These ports run on the outer bounded ProcessIoPort/result-scan lanes.
        # Their callers may observe a deadline, but this resource-moving I/O
        # must hand its actual completion back before custody can be released.
        return self._call(EnqueueTurn(delivery), settled=True)

    def drain_results(self, limit: int | None = None):
        return self._call(DrainResults(limit), settled=True)

    def drain_progress(self):
        return self._call(DrainProgress(), settled=True)

    def interrupt(self, delivery_id, *, timeout=1.0):
        return self._call(InterruptTurn(delivery_id, timeout), timeout + 1.0)


def own_process(process, *, observer=None):
    if isinstance(process, ProcessOwner):
        return process
    cls = (
        StreamingProcessOwner
        if isinstance(process, StreamingHarnessProcess)
        else ProcessOwner
    )
    return cls(process, observer=observer)
