"""Actor-owned managed-harness lifecycle state.

The actor is the single writer for desired specifications, process handles,
start generations, restart/failure accounting, and liveliness bindings.
Potentially blocking process operations are delegated to :class:`ProcessIoPort`;
the actor only consumes generation-fenced completion messages.  Public callers
wait on HYPRIAL reply cells outside the actor -- no Pykka proxy, ``ask`` or ``get``
crosses this module boundary.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Generic, TypeVar, cast, get_args

from hyprial.actor_runtime import ActorHandle, ActorRuntime, ActorSpec, AdmissionResult
from hyprial.actor_runtime.effects import EffectCompleted, EffectLane, EffectRequest
from hyprial.backoff import capped_exponential

from .api import (
    DaemonInterruptibleHarnessProcess,
    HarnessDelivery,
    HarnessLauncher,
    HarnessResult,
    ManagedHarnessProcess,
    StreamingHarnessProcess,
)
from .desired_state import DesiredStateError, DesiredStateStore, HarnessLaunchSpec
from .desired_state_io import (
    DesiredStateIoCompleted,
    DesiredStateIoPort,
    DesiredStateIoRequest,
    DesiredStateOperation,
)
from .state_persistence import StateCommandCompleted
from .orphan_processes import OrphanProcessRegistry, OrphanProcessAuthority
from .harness_ports import (
    BindHarnessLivenessCommand,
    DispatchHarnessDeliveryCommand,
    DrainHarnessFailedCommand,
    DrainHarnessProgressCommand,
    DrainHarnessReadinessCommand,
    DrainHarnessResultsCommand,
    ClaimHarnessResultsCommand,
    SettleHarnessResultCommand,
    ClaimedHarnessResult,
    HarnessResultsClaimed,
    HarnessResultSettled,
    EnsureHarnessCommand,
    HarnessCallIoCompleted,
    HarnessAdapterRegistrationProjection,
    HarnessCommand,
    HarnessDeliveryAdmitted,
    HarnessDesiredStateManaged,
    HarnessEvent,
    HarnessFailedDrained,
    HarnessLaunchProjection,
    HarnessLivenessBound,
    HarnessMutationCompleted,
    HarnessProcessStarted,
    HarnessProjectionsRefreshed,
    HarnessProgressObserved,
    HarnessReadinessDrained,
    HarnessReadyObserved,
    HarnessRestoreCompleted,
    HarnessRestoreProjection,
    HarnessResultObserved,
    HarnessSessionRefProjection,
    HarnessSessionRefsReconciled,
    HarnessSessionRefsProjection,
    HarnessStartTimerElapsedCommand,
    HarnessStatusProjection,
    HarnessStopIoCompleted,
    HarnessStreamingProjection,
    HarnessTimerCompleted,
    HarnessTimerElapsedCommand,
    HarnessesStopped,
    RemoveHarnessCommand,
    RemoveAdapterRegistrationCommand,
    RefreshHarnessProjectionsCommand,
    ReconcileHarnessSessionRefsCommand,
    RestoreEligibilityProjection,
    AgentIdentityProjectionPort,
    RestoreAdapterRegistrationCommand,
    RestoreHarnessesCommand,
    UpdateRestoreEligibilityCommand,
    StopHarnessesCommand,
    SnapshotAdapterRegistrationCommand,
    StageHarnessDesiredCommand,
    WaitHarnessReadyCommand,
    harness_desired_generation,
)
from .readiness_budget import (
    START_ADMISSION_WIDTH_DEFAULT,
    START_TIMEOUT_SECONDS_DEFAULT,
    restore_rounds,
)
from hyprial.contracts import ipc_errors
from hyprial.contracts.ports import PortAdmission, PortCommandRejected
from hyprial.contracts.readiness import ReadinessReport


T = TypeVar("T")

_LIFECYCLE_SETTLED_CAPACITY = 1024


class HarnessRuntimeClosed(RuntimeError):
    """A lifecycle command could not be admitted or completed."""


class _Reply(Generic[T]):
    """One-shot, backend-neutral reply used only outside actor handlers."""

    def __init__(self) -> None:
        self._ready = threading.Event()
        self._value: T | None = None
        self._error: BaseException | None = None
        self._lock = threading.Lock()
        self._cancelled = False
        self._cancel: Callable[[], None] | None = None

    def complete(self, value: T) -> None:
        with self._lock:
            if self._ready.is_set() or self._cancelled:
                return
            self._value = value
            self._ready.set()

    def fail(self, error: BaseException) -> None:
        with self._lock:
            if self._ready.is_set() or self._cancelled:
                return
            self._error = error
            self._ready.set()

    def wait(self, timeout: float | None = None) -> T:
        if not self._ready.wait(timeout):
            cancel: Callable[[], None] | None
            with self._lock:
                self._cancelled = True
                cancel = self._cancel
            if cancel is not None:
                cancel()
            raise TimeoutError("harness actor reply deadline elapsed")
        if self._error is not None:
            raise self._error
        return cast(T, self._value)

    def set_cancel(self, cancel: Callable[[], None]) -> None:
        run_now = False
        with self._lock:
            if self._cancelled:
                run_now = True
            elif not self._ready.is_set():
                self._cancel = cancel
        if run_now:
            cancel()


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int | None
    marker: str | None


class _CompletionReceipt:
    """Worker custody is released only after an actor processed the event."""

    def __init__(self) -> None:
        self._processed = threading.Event()
        self._lock = threading.Lock()
        self._claimed_generation: int | None = None

    def mark_processed(self) -> None:
        self._processed.set()

    def wait(self, timeout: float) -> bool:
        return self._processed.wait(timeout)

    def claim(self, generation: int) -> bool:
        """Atomically claim one mailbox admission for this generation."""

        with self._lock:
            if self._processed.is_set():
                return False
            if self._claimed_generation == generation:
                return False
            self._claimed_generation = generation
            return True

    def claimed_by(self, generation: int) -> bool:
        with self._lock:
            return (
                not self._processed.is_set()
                and self._claimed_generation == generation
            )

    @property
    def processed(self) -> bool:
        return self._processed.is_set()


@dataclass(frozen=True, slots=True)
class _HarnessActorGeneration:
    """Per-guardian-generation handler; never reused after a crash."""

    owner: HarnessRuntimeActor
    generation: int
    token: str

    def __call__(self, command: object) -> None:
        self.owner.receive(self.generation, command)


HarnessRestoreSummary = HarnessRestoreProjection


@dataclass(slots=True)
class _RestoreBatch:
    correlation_id: str
    attempted: int
    pending: set[str]
    restored: int = 0
    failed: int = 0
    # Keys dispositioned "deferred": claimed by a lifecycle effect, so no
    # start attempt of this round will ever settle them.  Counted separately
    # from failed -- the saga that owns the resource is still working, and
    # reconcile owns bring-up afterwards; "deferred" is a handoff, not a
    # verdict about the connector.
    deferred: int = 0
    # A persistence error rejects the caller once, but every other admitted
    # or queued target retains its own disposition and write custody.
    terminal_rejected: bool = False
    # Keys accepted into this restore but not yet handed to the I/O port.
    # Restore used to begin every start in one loop, which made a fleet
    # larger than the port's capacity fail in two ways at once: starts past
    # the semaphore were rejected outright ("process I/O port is
    # overloaded") and starts past the executor width sat in its queue with
    # a start timer already running.  Both then surfaced as "harness start
    # did not complete within Ns" for harnesses the launcher was never
    # called for.  Draining this queue one start per settlement keeps every
    # in-flight start owning a real worker thread.
    queued: deque[str] = field(default_factory=deque)


@dataclass(frozen=True, slots=True)
class _PendingCall:
    expected_generation: int
    operation: str
    harness_id: str | None = None
    delivery_id: str | None = None
    subject_id: str | None = None
    reply_correlation_id: str | None = None


@dataclass(frozen=True, slots=True)
class _SessionRefObservation:
    key: str
    incarnation: str
    generation: int
    process_token: str | None
    harness: str
    name: str
    session_ref: str | None


@dataclass(slots=True)
class _EnsureFailureFence:
    request: object
    provenance: object
    code: str
    detail: str
    control_correlations: list[str] = field(default_factory=list)
    unstopped: list[
        tuple[
            str,
            int,
            ManagedHarnessProcess,
            ProcessIdentity | None,
        ]
    ] = field(default_factory=list)


class _PendingCallRegistry:
    """Thread-safe custody for ephemeral facade correlations."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, _PendingCall] = {}

    def register(self, correlation: str, pending: _PendingCall) -> None:
        with self._lock:
            self._items[correlation] = pending

    def pop(self, correlation: str) -> _PendingCall | None:
        with self._lock:
            return self._items.pop(correlation, None)

    def cancel(self, correlation: str) -> None:
        with self._lock:
            self._items.pop(correlation, None)
            for internal, pending in tuple(self._items.items()):
                if pending.reply_correlation_id == correlation:
                    self._items.pop(internal)

    def clear(self) -> tuple[tuple[str, _PendingCall], ...]:
        with self._lock:
            pending = tuple(self._items.items())
            self._items.clear()
        return pending

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


@dataclass(slots=True)
class _Record:
    spec: HarnessLaunchSpec
    incarnation: str = field(default_factory=lambda: uuid.uuid4().hex)
    generation: int = 0
    process: ManagedHarnessProcess | None = None
    identity: ProcessIdentity | None = None
    starting: bool = False
    attempt_kind: str = ""
    explicit_correlation_id: str | None = None
    attempt_correlation_id: str | None = None
    restore_batch: str | None = None
    failures: int = 0
    # `failed` is NOT stored: it is `failures >= failure_budget`, and keeping
    # a boolean alongside the counter it is computed from is a second source
    # of truth that can disagree with the first.  Within budget the harness is
    # `retrying` under backoff; past it the harness is `failed` -- terminal,
    # no automatic path back into the start queue.  Only a start that
    # actually succeeds, or an explicit operator start, clears the counter.
    restart_after: float | None = None
    last_error: str | None = None
    liveness_binding: object | None = None
    completed_start: HarnessProcessStarted | None = None
    # U0b: terminal verdict for a restore-kind start that did not come back
    # (Allen 2026-09-03: one attempt, no auto-retry).  Needed alongside the
    # budget because budget 0 is the documented "never terminate" opt-in
    # for reconcile-kind restarts -- the restore ruling outranks it, so
    # this flag pins the verdict where the budget refuses to.
    restore_failed_terminal: bool = False


@dataclass(frozen=True, slots=True)
class _StopAllOutcome:
    stopped: tuple[str, ...]
    bindings_closed: tuple[str, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _DeliveryReservation:
    harness_id: str
    process_generation: int
    claim_token: str | None = None


@dataclass(frozen=True, slots=True)
class _ClaimScan:
    operation_id: str
    limit: int
    processes: tuple[tuple[str, int, StreamingHarnessProcess], ...]


@dataclass(frozen=True, slots=True)
class _ClaimScanOutcome:
    rows: tuple[tuple[str, int, HarnessResult], ...]
    errors: tuple[str, ...] = ()


class HarnessProjection:
    """Stable read model.  Only the lifecycle actor publishes snapshots."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: tuple[HarnessStatusProjection, ...] = ()
        self._last_errors: dict[str, str] = {}
        self._failed: frozenset[str] = frozenset()
        self._streaming: tuple[str, ...] = ()
        self._streaming_generations: tuple[tuple[str, int], ...] = ()
        self._session_refs: dict[tuple[str, str], str] = {}
        self._worker_session_refs: dict[tuple[str, str], str] = {}
        # Compatibility-only error face.  Session-ref/streaming projections
        # never consult these mutable process handles.
        self._processes: dict[str, ManagedHarnessProcess] = {}

    def publish(
        self,
        records: dict[str, _Record],
        version: int,
        *,
        failure_budget: int = 0,
    ) -> None:
        """Snapshot the records.

        ``state`` is derived here from ``failures`` against the actor's
        failure budget rather than read from a stored flag: a flag kept
        beside the counter it is computed from is a second source of truth,
        and the two can disagree.  Budget 0 means failure termination is
        disabled (retries continue forever under backoff), so nothing is
        ever reported failed.
        """

        def _failed(record: _Record) -> bool:
            # ``restore_failed_terminal`` (U0b) is the one verdict that can
            # be terminal at budget 0: restore-kind starts get a single
            # attempt by ruling, outranking the infinite-retry opt-in.
            return record.restore_failed_terminal or (
                failure_budget > 0 and record.failures >= failure_budget
            )

        def _state(record: _Record) -> str | None:
            if _failed(record):
                return "failed"
            # Within budget, recorded failures mean the retry loop owns this
            # harness: reconcile restarts it under backoff.
            return "retrying" if record.failures > 0 else None

        rows: list[HarnessStatusProjection] = []
        errors: dict[str, str] = {}
        failed: set[str] = set()
        streaming: list[str] = []
        streaming_generations: list[tuple[str, int]] = []
        refs: dict[tuple[str, str], str] = {}
        worker_refs: dict[tuple[str, str], str] = {}
        processes: dict[str, ManagedHarnessProcess] = {}
        for key, record in sorted(records.items()):
            process = record.process
            if process is not None:
                processes[key] = process
            running = bool(process is not None and process.running)
            runtime_error = getattr(process, "last_error", None) if process else None
            error = record.last_error or (str(runtime_error) if runtime_error else None)
            pid = record.identity.pid if running and record.identity is not None else None
            endpoint = None
            dsh_home = None
            if record.spec.harness == "dsh":
                # The endpoint is an OUTPUT of the generation that is alive now,
                # never an input: it comes from the port the child's banner
                # named.  A stale or absent process reports neither value.
                endpoint = getattr(process, "endpoint", None)
                home = getattr(process, "dsh_home", None)
                if isinstance(home, Path):
                    dsh_home = str(home)
            max_in_flight = getattr(process, "max_in_flight", None) if process else None
            in_flight = getattr(process, "in_flight", None) if process else None
            queue_depth = getattr(process, "queue_depth", None) if process else None
            rows.append(
                HarnessStatusProjection(
                    version=version,
                    harness_id=key,
                    runtime=record.spec.harness,
                    name=record.spec.name,
                    running=running,
                    pid=pid,
                    starting=record.starting,
                    state=_state(record),
                    error=error,
                    endpoint=endpoint,
                    dsh_home=dsh_home,
                    max_in_flight=(
                        max_in_flight if isinstance(max_in_flight, int) else None
                    ),
                    in_flight=in_flight if isinstance(in_flight, int) else None,
                    queue_depth=queue_depth if isinstance(queue_depth, int) else None,
                )
            )
            if error is not None:
                errors[key] = error
            if _failed(record):
                failed.add(key)
            if running and isinstance(process, StreamingHarnessProcess):
                streaming.append(record.spec.name)
                streaming_generations.append((record.spec.name, record.generation))
            ref = getattr(process, "session_ref", None) if process else None
            if isinstance(ref, str) and ref:
                refs[(record.spec.harness, record.spec.name)] = ref
            worker_channel = getattr(process, "worker_channel", None)
            worker_ref = getattr(worker_channel, "session_ref", None)
            if isinstance(worker_ref, str) and worker_ref:
                worker_refs[(record.spec.harness, record.spec.name)] = worker_ref
        with self._lock:
            self._rows = tuple(rows)
            self._last_errors = errors
            self._failed = frozenset(failed)
            self._streaming = tuple(streaming)
            self._streaming_generations = tuple(streaming_generations)
            self._session_refs = refs
            self._worker_session_refs = worker_refs
            self._processes = processes

    def status(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            rows = self._rows
            processes = dict(self._processes)
        payloads: list[dict[str, object]] = []
        for row in rows:
            payload = row.to_payload()
            process = processes.get(row.harness_id)
            runtime_error = getattr(process, "last_error", None) if process else None
            if runtime_error and "error" not in payload:
                payload["error"] = str(runtime_error)
            payloads.append(payload)
        return tuple(payloads)

    def read_harness(self, harness_id: str) -> HarnessStatusProjection | None:
        with self._lock:
            return next((row for row in self._rows if row.harness_id == harness_id), None)

    def read_harnesses(self) -> tuple[HarnessStatusProjection, ...]:
        with self._lock:
            return self._rows

    def last_errors(self) -> dict[str, str]:
        with self._lock:
            return dict(self._last_errors)

    def failed(self) -> frozenset[str]:
        with self._lock:
            return self._failed

    def streaming_actors(self) -> tuple[str, ...]:
        return self.read_streaming().actors

    def read_streaming(self) -> HarnessStreamingProjection:
        with self._lock:
            version = max((row.version for row in self._rows), default=0)
            return HarnessStreamingProjection(
                version, self._streaming, self._streaming_generations
            )

    def session_refs(self) -> dict[tuple[str, str], str]:
        return {
            (item.harness, item.name): item.session_ref
            for item in self.read_session_refs().refs
        }

    def read_session_refs(self) -> HarnessSessionRefsProjection:
        with self._lock:
            refs = dict(self._session_refs)
            rows = self._rows
        return HarnessSessionRefsProjection(
            max((row.version for row in rows), default=0),
            tuple(
                HarnessSessionRefProjection(harness, name, session_ref)
                for (harness, name), session_ref in sorted(refs.items())
            ),
        )

    def read_worker_session_refs(self) -> HarnessSessionRefsProjection:
        with self._lock:
            refs = dict(self._worker_session_refs)
            rows = self._rows
        return HarnessSessionRefsProjection(
            max((row.version for row in rows), default=0),
            tuple(
                HarnessSessionRefProjection(harness, name, session_ref)
                for (harness, name), session_ref in sorted(refs.items())
            ),
        )


def _default_identity_reader(pid: int) -> str | None:
    from hyprial.mcp.channel import _read_process_identity

    return _read_process_identity(pid)


class ProcessIoPort:
    """Bounded blocking-I/O pool returning typed, generation-fenced events."""

    def __init__(
        self,
        launcher: HarnessLauncher,
        *,
        emit: Callable[[object], tuple[AdmissionResult, int]],
        delivery_failed: Callable[[object, BaseException], None],
        generation_reader: Callable[[], int],
        identity_reader: Callable[[int], str | None] = _default_identity_reader,
        orphan_processes: OrphanProcessRegistry | None = None,
        max_workers: int = 4,
        observe: Callable[[object], AdmissionResult] | None = None,
    ) -> None:
        self._launcher = launcher
        self._emit = emit
        self._observe = observe or (lambda event: self._emit(event)[0])
        self._delivery_failed = delivery_failed
        self._generation_reader = generation_reader
        self._identity_reader = identity_reader
        self._owns_orphan_processes = orphan_processes is None
        self._orphan_processes = orphan_processes or OrphanProcessAuthority(
            identity_reader=identity_reader
        )
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers),
            thread_name_prefix="hyprial-harness-io",
        )
        self._closed = False
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._in_flight = 0
        self._process_owners: set[object] = set()
        self._slots = threading.BoundedSemaphore(max(1, max_workers) * 4)

    def start(
        self,
        correlation_id: str,
        harness_id: str,
        generation: int,
        version: int,
        spec: HarnessLaunchSpec,
        *,
        replace: tuple[ManagedHarnessProcess, ProcessIdentity | None] | None = None,
    ) -> None:
        def operation() -> HarnessProcessStarted:
            if replace is not None:
                old_process, old_identity = replace
                stopped, detail = self._stop_checked(
                    old_process, old_identity, harness_id=harness_id
                )
                if not stopped:
                    return HarnessProcessStarted(
                        correlation_id=correlation_id,
                        generation=generation,
                        version=version,
                        harness_id=harness_id,
                        pid=None,
                        error=RuntimeError(detail or "incumbent harness did not stop"),
                    )
            try:
                from .process_owner import own_process, ProcessFactsObserved

                raw_process = self._launcher.start(spec)
                identity = self._capture_identity(raw_process)
                self._orphan_processes.observe_start(
                    harness_id,
                    raw_process,
                    pid=identity.pid,
                    marker=identity.marker,
                )
                try:
                    process = own_process(
                        raw_process,
                        observer=lambda token, facts: self._observe(
                            ProcessFactsObserved(harness_id, token, facts.running)
                        ),
                    )
                except BaseException:
                    self._stop_checked(raw_process, identity, harness_id=harness_id)
                    raise
                self._orphan_processes.observe_start(
                    harness_id, process, pid=identity.pid, marker=identity.marker
                )
                with self._lock:
                    self._process_owners.add(process)
            except BaseException as error:  # completion carries failures to actor
                return HarnessProcessStarted(
                    correlation_id=correlation_id,
                    generation=generation,
                    version=version,
                    harness_id=harness_id,
                    pid=None,
                    error=error,
                )
            return HarnessProcessStarted(
                correlation_id=correlation_id,
                generation=generation,
                version=version,
                harness_id=harness_id,
                pid=identity.pid,
                process=process,
                identity_marker=identity.marker,
            )

        self._submit(
            operation,
            overload=HarnessProcessStarted(
                correlation_id=correlation_id,
                generation=generation,
                version=version,
                harness_id=harness_id,
                pid=None,
                error=HarnessRuntimeClosed("process I/O port is overloaded"),
            ),
            orphan_start=True,
        )

    def stop(
        self,
        correlation_id: str,
        harness_id: str,
        generation: int,
        version: int,
        process: ManagedHarnessProcess,
        identity: ProcessIdentity | None,
    ) -> None:
        def operation() -> HarnessStopIoCompleted:
            stopped, detail = self._stop_checked(
                process, identity, harness_id=harness_id
            )
            return HarnessStopIoCompleted(
                correlation_id=correlation_id,
                generation=generation,
                version=version,
                harness_id=harness_id,
                stopped=stopped,
                detail=detail,
            )

        self._submit(
            operation,
            overload=HarnessStopIoCompleted(
                correlation_id=correlation_id,
                generation=generation,
                version=version,
                harness_id=harness_id,
                stopped=False,
                detail="process I/O port is overloaded",
            ),
        )

    def call(
        self,
        correlation_id: str,
        generation: int,
        version: int,
        operation: Callable[[], object],
    ) -> None:
        def run() -> HarnessCallIoCompleted:
            try:
                return HarnessCallIoCompleted(
                    correlation_id, generation, version, operation()
                )
            except BaseException as error:
                return HarnessCallIoCompleted(
                    correlation_id, generation, version, error=error
                )

        self._submit(
            run,
            overload=HarnessCallIoCompleted(
                correlation_id,
                generation,
                version,
                error=HarnessRuntimeClosed("process I/O port is overloaded"),
            ),
        )

    def close(self, deadline: float | None = None) -> bool:
        with self._condition:
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
        if deadline is None:
            deadline = time.monotonic()
        with self._condition:
            while self._in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            owners = tuple(self._process_owners)
        complete = True
        for owner in owners:
            if owner.detach(max(0.0, deadline - time.monotonic())):
                with self._lock:
                    self._process_owners.discard(owner)
            else:
                complete = False
        if complete and self._owns_orphan_processes:
            complete = self._orphan_processes.close(max(0.0, deadline - time.monotonic()))
        return complete

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def _submit(
        self,
        operation: Callable[[], object],
        *,
        overload: object,
        orphan_start: bool = False,
    ) -> None:
        with self._lock:
            if self._closed:
                raise HarnessRuntimeClosed("process I/O port is closed")
            if not self._slots.acquire(blocking=False):
                self._delivery_failed(
                    overload,
                    HarnessRuntimeClosed("process I/O port is overloaded"),
                )
                return
            try:
                future = self._executor.submit(operation)
            except BaseException as error:
                self._slots.release()
                self._delivery_failed(overload, error)
                return
            self._in_flight += 1

        def settle() -> None:
            try:
                completion = future.result()
            except CancelledError:
                completion = _failed_completion(
                    overload,
                    HarnessRuntimeClosed("process I/O operation was cancelled"),
                )
            except BaseException as error:
                completion = _failed_completion(overload, error)
            receipt = _CompletionReceipt()
            completion = replace(completion, processed_receipt=receipt)
            try:
                processed = self._handoff(completion, receipt)
                if (
                    not processed
                    and orphan_start
                    and isinstance(completion, HarnessProcessStarted)
                    and completion.process is not None
                ):
                    self._stop_checked(
                        cast(ManagedHarnessProcess, completion.process),
                        ProcessIdentity(completion.pid, completion.identity_marker),
                        harness_id=completion.harness_id,
                    )
            finally:
                self._slots.release()
                with self._condition:
                    self._in_flight -= 1
                    self._condition.notify_all()

        def schedule_settlement(_future: object) -> None:
            # ``Future.add_done_callback`` runs inline when a fast operation
            # already finished.  Never let receipt waiting block the actor
            # thread that submitted the I/O work.
            threading.Thread(
                target=settle,
                name="hyprial-harness-completion",
                daemon=True,
            ).start()

        future.add_done_callback(schedule_settlement)

    def _handoff(self, completion: object, receipt: _CompletionReceipt) -> bool:
        while True:
            if receipt.processed:
                return True
            generation = self._generation_reader()
            if receipt.claimed_by(generation):
                # Exactly one mailbox copy may be pending in a generation.
                # A long actor stall is not evidence of failure and cannot
                # release external-I/O custody.
                receipt.wait(0.01)
                continue
            admission, admitted_generation = self._emit(completion)
            if admission is AdmissionResult.ACCEPTED:
                receipt.claim(admitted_generation)
                receipt.wait(0.01)
                continue
            if admission is AdmissionResult.CLOSED:
                with self._lock:
                    permanently_closed = self._closed
                if permanently_closed:
                    if (
                        isinstance(completion, HarnessProcessStarted)
                        and completion.process is not None
                    ):
                        # A late child start can be safely compensated: it was
                        # never adopted by actor state, so stop that exact
                        # process identity before releasing worker custody.
                        self._delivery_failed(
                            completion,
                            HarnessRuntimeClosed(
                                "late harness start was not adopted"
                            ),
                        )
                        return False
                    if _completion_succeeded(completion):
                        # A successful external effect has no truthful failure
                        # translation.  Keep worker custody and let bounded
                        # drain report incomplete rather than inviting replay.
                        receipt.wait(0.05)
                        continue
                    self._delivery_failed(
                        completion,
                        HarnessRuntimeClosed(
                            "harness actor closed before I/O completion"
                        ),
                    )
                    return False
                # Guardian replacement has a short CLOSED interval.  Custody
                # stays with this worker and the exact completion is retried
                # against the replacement generation.
            time.sleep(0.002)

    def _capture_identity(self, process: ManagedHarnessProcess) -> ProcessIdentity:
        pid = getattr(process, "pid", None)
        marker = self._identity_reader(pid) if isinstance(pid, int) else None
        return ProcessIdentity(pid if isinstance(pid, int) else None, marker)

    def _recorded_identity_verdict(self, identity: ProcessIdentity) -> str:
        """Classify a recorded identity before stop signals anything.

        ``"reused"`` -- a live PID whose marker disagrees -- is the only
        refusal; ``"dead"`` (PID missing) must proceed, because a harness may
        have replaced its own child generation and a missing PID is not reuse.
        The component-wise owner fence is shared with ``mcp.channel`` so a
        marker-format skew is not read as reuse.

        ⚠️ Stop-path exception: the fence's own fail-safe for ``UNKNOWN``
        (PID alive but its marker cannot be read: EPERM, a vanished ps/procfs
        source, or no shared scheme) is *do not act*.  Here ``UNKNOWN`` maps to
        ``"alive"`` and stop proceeds.  That is safe ONLY because the actual
        signal gate is :meth:`OwnedProcessGroup.signal`, which re-reads the
        generation's PID + birth identity immediately before ``killpg`` and
        refuses a mismatch; ``_stop_checked`` is bookkeeping around that gate,
        not the gate itself.  If stop ever stops routing through
        ``OwnedProcessGroup``, UNKNOWN must become a refusal again.
        """

        from hyprial.mcp.channel import _OwnerProcessStatus, _owner_process_status

        status = _owner_process_status(
            identity.pid,
            identity.marker,
            read_identity=self._identity_reader,
        )
        if status is _OwnerProcessStatus.IDENTITY_MISMATCH:
            return "reused"
        if status is _OwnerProcessStatus.PID_MISSING:
            return "dead"
        return "alive"

    def _stop_checked(
        self,
        process: ManagedHarnessProcess,
        identity: ProcessIdentity | None,
        *,
        harness_id: str,
        interruption_reason: str | None = None,
    ) -> tuple[bool, str | None]:
        from .process_owner import ProcessOwner

        if (
            identity is not None
            and identity.pid is not None
            and identity.marker is not None
        ):
            verdict = self._recorded_identity_verdict(identity)
            if verdict == "reused":
                if isinstance(process, ProcessOwner):
                    if process.detach(1.0):
                        with self._lock:
                            self._process_owners.discard(process)
                return False, "PID_REUSED: refusing to stop a different process identity"
            if verdict == "dead":
                # The recorded generation is gone (it may have been replaced by
                # a respawn inside the same harness process).  A missing PID is
                # not PID reuse: stop whatever this process owns now.  The
                # generation's own PID+birth-identity fence remains the
                # authority on which PID it may signal.
                identity = self._capture_identity(process)
            # "alive" AND the UNKNOWN case both fall through to process.stop().
            # UNKNOWN means "the marker could not be read", which the shared
            # fence treats as fail-safe-do-not-act; proceeding is safe only
            # because OwnedProcessGroup.signal() re-checks the birth identity
            # before killpg (see _recorded_identity_verdict).
        self._orphan_processes.observe_stop(
            harness_id,
            process,
            pid=None if identity is None else identity.pid,
            marker=None if identity is None else identity.marker,
        )
        try:
            if interruption_reason is not None and isinstance(
                process, DaemonInterruptibleHarnessProcess
            ):
                process.prepare_daemon_interruption(interruption_reason)
            process.stop()
            if isinstance(process, ProcessOwner):
                with self._lock:
                    self._process_owners.discard(process)
        except BaseException as error:
            self._orphan_processes.collect_once()
            return False, str(error)
        self._orphan_processes.collect_once()
        return True, None


def _failed_completion(completion: object, error: BaseException) -> object:
    if isinstance(completion, HarnessProcessStarted):
        return replace(completion, error=error)
    if isinstance(completion, HarnessCallIoCompleted):
        return replace(completion, error=error)
    if isinstance(completion, HarnessStopIoCompleted):
        return replace(completion, stopped=False, detail=str(error))
    raise TypeError(f"unsupported process I/O completion: {type(completion).__name__}")


def _completion_succeeded(completion: object) -> bool:
    if isinstance(completion, HarnessProcessStarted):
        return completion.error is None and completion.process is not None
    if isinstance(completion, HarnessCallIoCompleted):
        return completion.error is None
    if isinstance(completion, HarnessStopIoCompleted):
        return completion.stopped
    return False


def _is_io_completion(command: object) -> bool:
    return isinstance(
        command,
        (HarnessProcessStarted, HarnessCallIoCompleted, HarnessStopIoCompleted),
    )


class HarnessRuntimeActor:
    """Single-writer coordinator for the complete harness lifecycle domain."""

    def __init__(
        self,
        launcher: HarnessLauncher,
        *,
        runtime: ActorRuntime | None = None,
        projection: HarnessProjection | None = None,
        clock: Callable[[], float] = time.monotonic,
        restart_backoff_seconds: float = 0.0,
        start_timeout_seconds: float = START_TIMEOUT_SECONDS_DEFAULT,
        start_backoff_max_seconds: float = 60.0,
        failure_budget: int = 3,
        start_max_workers: int = START_ADMISSION_WIDTH_DEFAULT,
        mailbox_capacity: int = 128,
        lifecycle_retry_base_seconds: float = 0.05,
        lifecycle_replay_capacity: int = _LIFECYCLE_SETTLED_CAPACITY,
        identity_reader: Callable[[int], str | None] = _default_identity_reader,
        orphan_processes: OrphanProcessRegistry | None = None,
        orphan_state_path: Path | None = None,
        orphan_logger: Callable[..., None] | None = None,
        event_sink: object | None = None,
        desired_state: DesiredStateStore | None = None,
        automatic_restore_allowed: Callable[[HarnessLaunchSpec], bool] | None = None,
        persistence_late_result: (
            Callable[[str], StateCommandCompleted | None] | None
        ) = None,
        agent_identity: AgentIdentityProjectionPort | None = None,
    ) -> None:
        self._runtime = runtime or ActorRuntime()
        self._projection = projection or HarnessProjection()
        self._clock = clock
        self._restart_backoff_seconds = restart_backoff_seconds
        self._start_timeout_seconds = start_timeout_seconds
        self._start_backoff_max_seconds = start_backoff_max_seconds
        self._failure_budget = failure_budget
        self._lifecycle_retry_base_seconds = max(
            0.001, lifecycle_retry_base_seconds
        )
        if lifecycle_replay_capacity < 1:
            raise ValueError("lifecycle_replay_capacity must be positive")
        self._lifecycle_replay_capacity = lifecycle_replay_capacity
        self._records: dict[str, _Record] = {}
        self._stop_retry_processes: dict[
            str, tuple[ManagedHarnessProcess, ProcessIdentity | None]
        ] = {}
        self._stop_retry_bindings: dict[str, object] = {}
        self._restore_batches: dict[str, _RestoreBatch] = {}
        self._calls = _PendingCallRegistry()
        self._result_claim_capacity = 256
        self._delivery_reservations: dict[str, _DeliveryReservation] = {}
        self._claimed_results: dict[str, ClaimedHarnessResult] = {}
        self._result_reservation_snapshot: frozenset[str] = frozenset()
        self._claim_scan_id: str | None = None
        self._claim_scan_generations: frozenset[tuple[str, int]] = frozenset()
        self._retire_after_claim_scan: set[tuple[str, int]] = set()
        self._claim_waiters: list[tuple[str, int]] = []
        self._timers: dict[tuple[str, int], threading.Timer] = {}
        self._failed_events: list[str] = []
        # Readiness reports (phase ③): one per disposition that happened --
        # a settled start attempt, an already-running ensure, a deferred
        # restore target.  Deliberately the same drain shape as
        # `_failed_events` but NOT the same semantics: those announce a
        # state entry (failed, edge-triggered on the budget tripping),
        # these announce that a disposition occurred, so a restore round
        # leaves exactly one report per desired target and every later
        # settlement adds one more.  The daemon aggregates arrival and
        # verdict; it never interprets report content.
        self._readiness_reports: list[ReadinessReport] = []
        self._version = 0
        self._last_timer_sequence = 0
        self._settled_completion_ids: set[str] = set()
        self._settled_completion_order: deque[str] = deque()
        self._event_sinks: list[object] = []
        if event_sink is not None:
            self._event_sinks.append(event_sink)
        self._closing = False
        self._desired_state = desired_state
        self._automatic_restore_allowed = automatic_restore_allowed
        self._restore_eligibility: dict[str, RestoreEligibilityProjection] = {}
        self._restore_eligibility_capacity = 10_000
        self._agent_identity = agent_identity
        self._lifecycle_pending: dict[str, tuple[object, object]] = {}
        self._lifecycle_retry_timers: dict[str, threading.Timer] = {}
        self._lifecycle_effect_resources: set[str] = (
            set()
            if desired_state is None
            else set(desired_state.incomplete_harness_lifecycle_resources())
        )
        self._lifecycle_effect_requests: dict[str, tuple[object, object]] = {}
        # Exact internal correlations whose native start/stop request crossed
        # into ProcessIoPort custody, mapped to their logical attempt token.
        # A later correlation under the same attempt must earn its own phase.
        self._lifecycle_native_admitted: dict[str, str] = {}
        # A lifecycle ensure can own multiple launcher calls when actor-level
        # retries overlap late I/O completions.  Keep every call fenced until
        # it reports and any process it created is stopped.
        self._lifecycle_ensure_io: dict[str, tuple[str, str, int]] = {}
        self._lifecycle_ensure_failure_fences: dict[
            str, _EnsureFailureFence
        ] = {}
        self._lifecycle_ensure_settling: set[str] = set()
        self._lifecycle_ensure_failure_stops: dict[
            str,
            tuple[
                str,
                str,
                int,
                ManagedHarnessProcess,
                ProcessIdentity | None,
            ],
        ] = {}
        # Terminal failures replay to re-admissions just like successful receipts.
        # Evicted with the existing bounded settled-attempt ledger.
        self._lifecycle_failures: dict[str, object] = {}
        self._settled_lifecycle_attempts: set[str] = set()
        self._settled_lifecycle_order: deque[str] = deque()
        self._lifecycle_replay_claims: set[str] = set()
        self._handler_generation = 0
        self._generation_lock = threading.Lock()
        self._handler_token = ""
        self._start_admission_width = max(1, start_max_workers)
        self._handle: ActorHandle | None = None
        self._io: ProcessIoPort | None = None
        self._persistence: DesiredStateIoPort | None = None
        self._owns_orphan_processes = orphan_processes is None
        self._orphan_processes = orphan_processes or OrphanProcessAuthority(
            orphan_state_path,
            identity_reader=identity_reader,
            logger=orphan_logger,
        )
        self._handle = self._runtime.start(
            ActorSpec(
                name="harness-runtime",
                handler_factory=self._new_generation_handler,
                mailbox_capacity=mailbox_capacity,
                supervision_profile="process_lifecycle",
                undelivered_sink=self._on_undelivered,
            )
        )
        if self._desired_state is not None:
            self._persistence = DesiredStateIoPort(
                self._desired_state,
                complete=lambda event: self._admit(event),
                late_result=persistence_late_result,
                capacity=mailbox_capacity,
            )
        self._result_effects: EffectLane[_ClaimScan, _ClaimScanOutcome] | None = None
        self._io = ProcessIoPort(
            launcher,
            emit=self._emit_completion,
            observe=self._admit,
            delivery_failed=self._fail_completion_delivery,
            generation_reader=self._read_generation,
            identity_reader=identity_reader,
            orphan_processes=self._orphan_processes,
            max_workers=start_max_workers,
        )
        self._publish()

    def _on_undelivered(self, command: object, reason_code: str) -> None:
        # ProcessIoPort and EffectLane retain the exact completion until the
        # replacement generation processes/acknowledges it.  Reporting a
        # second failure here would release or duplicate that custody.
        if isinstance(command, EffectCompleted):
            if isinstance(command.result, DesiredStateIoCompleted):
                persistence = self._persistence
                if persistence is not None:
                    persistence.redeliver(command.result.request)
            return
        if _is_io_completion(command):
            return
        correlation_id = str(getattr(command, "correlation_id", ""))
        if correlation_id:
            self._reject_correlation(
                correlation_id,
                reason_code,
                "accepted harness command did not begin before actor restart",
            )

    @property
    def projection(self) -> HarnessProjection:
        return self._projection

    def collect_orphans(self) -> int:
        return self._orphan_processes.collect_once()

    def orphan_status(self) -> tuple[dict[str, object], ...]:
        return self._orphan_processes.status()

    def restore_settlement_budget(self, target_count: int) -> float | None:
        """Wall-clock backstop for one restore round's settlement wait.

        Replaces the flat 45s wall-clock ceiling U7 removed (the constant
        whose name is now tripwired out of the tree).  The primary bound is
        structural, and the fix keeps it true: every desired target either
        settles immediately as deferred or is an admitted start whose
        settlement timer bounds it, so the batch cannot outlive
        ``ceil(targets / width)`` rounds of one start timeout each.  This
        method returns that bound plus one round of margin for admission
        and scheduler jitter -- a derived value that scales with the fleet
        instead of a constant that guessed at one.  Its only job is to
        convert a *lost settlement* (a bug, by definition -- no key without
        a disposition owner) into a ``TimeoutError`` on the caller's
        degraded-start path instead of a gate that never opens.

        ``None`` when start timers are disabled (start timeout 0): that is
        the operator's explicit opt-out of settlement deadlines, and no
        ceiling is invented for them -- the wait stays
        ``_AWAIT_SETTLEMENT``.
        """
        if self._start_timeout_seconds <= 0:
            return None
        rounds = restore_rounds(target_count, self._start_admission_width)
        return (rounds + 1) * self._start_timeout_seconds

    def read_harness(self, harness_id: str) -> HarnessStatusProjection | None:
        return self._projection.read_harness(harness_id)

    def read_harnesses(self) -> tuple[HarnessStatusProjection, ...]:
        return self._projection.read_harnesses()

    def read_streaming(self) -> HarnessStreamingProjection:
        return self._projection.read_streaming()

    def read_session_refs(self) -> HarnessSessionRefsProjection:
        return self._projection.read_session_refs()

    def read_worker_session_refs(self) -> HarnessSessionRefsProjection:
        return self._projection.read_worker_session_refs()

    def read_pending_result_delivery_ids(self) -> frozenset[str]:
        """Immutable reservation projection; native drain cannot reopen a row."""

        return self._result_reservation_snapshot

    @property
    def generation(self) -> int:
        return self._read_generation()

    @property
    def start_admission_width(self) -> int:
        """Concurrent restore starts, so callers can size their deadlines."""

        return self._start_admission_width

    @property
    def start_timeout(self) -> float:
        return self._start_timeout_seconds

    def _read_generation(self) -> int:
        with self._generation_lock:
            return self._handler_generation

    @property
    def version(self) -> int:
        return self._version

    def subscribe_events(self, sink: object) -> None:
        self._event_sinks.append(sink)

    def cancel_correlation(self, correlation_id: str) -> None:
        """Cancel only edge reply custody; domain state remains actor-owned."""

        self._calls.cancel(correlation_id)

    def submit(self, command: object) -> PortAdmission:
        with self._generation_lock:
            closing = self._closing and not isinstance(
                command,
                (StopHarnessesCommand, ClaimHarnessResultsCommand, SettleHarnessResultCommand),
            )
            admission = AdmissionResult.CLOSED if closing else self._admit(command)
        if closing:
            self._emit_event(
                PortCommandRejected(
                    correlation_id=command.correlation_id,
                    domain="harness",
                    generation=self._handler_generation,
                    version=self._version,
                    code="PORT_CLOSING",
                    detail="harness runtime is closing",
                    admission=PortAdmission.CLOSING,
                )
            )
            return PortAdmission.CLOSING
        if admission is AdmissionResult.ACCEPTED:
            return PortAdmission.ACCEPTED
        mapped = (
            PortAdmission.OVERLOADED
            if admission is AdmissionResult.OVERLOADED
            else PortAdmission.CLOSING
        )
        self._emit_event(
            PortCommandRejected(
                correlation_id=str(getattr(command, "correlation_id", "")),
                domain="harness",
                generation=self._handler_generation,
                version=self._version,
                code=(
                    "PORT_OVERLOADED"
                    if mapped is PortAdmission.OVERLOADED
                    else "PORT_CLOSING"
                ),
                detail=f"harness command admission is {mapped.value}",
                admission=mapped,
            )
        )
        return mapped

    def _admit(self, command: object) -> AdmissionResult:
        handle = self._handle
        if handle is None:
            return AdmissionResult.CLOSED
        return self._runtime.tell(handle, command)

    def receive(self, handler_generation: int, command: object) -> None:
        from .process_owner import ProcessFactsObserved
        from .lifecycle_receipts import (
            FailHarnessLifecycleCommand, LifecycleMutationRequest,
            TerminalizeHarnessLifecycleCommand,
            ConfirmLifecycleReceiptCommand, RetireLifecycleReceiptCommand,
        )

        if handler_generation != self._handler_generation:
            return
        if (
            _is_io_completion(command)
            and command.correlation_id in self._settled_completion_ids
        ):
            receipt = command.processed_receipt
            if isinstance(receipt, _CompletionReceipt):
                receipt.mark_processed()
            return
        self._version += 1
        if isinstance(command, RetireLifecycleReceiptCommand):
            self._persist_desired(
                DesiredStateOperation.RETIRE_LIFECYCLE_RECEIPT,
                command,
                ("harness", command.attempt_token, command.resource_token),
                context=("receipt", command, "retire"),
            )
            return
        if isinstance(command, ConfirmLifecycleReceiptCommand):
            self._persist_desired(
                DesiredStateOperation.CONFIRM_LIFECYCLE_RECEIPT_RETIRED,
                command,
                ("harness", command.attempt_token, command.resource_token),
                context=("receipt", command, "confirm"),
            )
            return
        if isinstance(command, TerminalizeHarnessLifecycleCommand):
            self._terminalize_incomplete_lifecycle(command)
            return
        if isinstance(command, FailHarnessLifecycleCommand):
            with self._generation_lock:
                self._on_fail_lifecycle(command)
            self._publish()
            return
        if isinstance(command, LifecycleMutationRequest):
            with self._generation_lock:
                durable_receipt = self._durable_lifecycle_receipt(
                    command.attempt_token
                )
                if durable_receipt is not None and durable_receipt.completed:
                    replay_claim = self._claim_lifecycle_replay(
                        command.attempt_token
                    )
                    if replay_claim == "wait":
                        return
                    if replay_claim == "full":
                        self._emit_event(
                            PortCommandRejected(
                                correlation_id=command.correlation_id,
                                domain="harness",
                                generation=self._handler_generation,
                                version=self._version,
                                code="PORT_OVERLOADED",
                                detail="Harness lifecycle replay capacity is full",
                                admission=PortAdmission.OVERLOADED,
                            )
                        )
                        return
                    self._replay_lifecycle_completion(command, durable_receipt)
                    return
                if (
                    command.attempt_token in self._settled_lifecycle_attempts
                    and durable_receipt is None
                ):
                    failure = self._settled_lifecycle_failure(
                        command.attempt_token
                    )
                    if failure is not None:
                        self._emit_event(failure)
                    return
                if self._closing:
                    self._remember_settled_lifecycle(command.attempt_token)
                    from .lifecycle_receipts import LifecycleMutationFailed

                    self._emit_event(
                        LifecycleMutationFailed(
                            command.correlation_id,
                            command.attempt_token,
                            self._handler_generation,
                            self._version,
                            "harness",
                            "HARNESS_RUNTIME_CLOSING",
                            "Harness runtime is closing before lifecycle admission",
                            True,
                        )
                    )
                    return
                self._on_lifecycle(command)
            self._publish()
            return
        if (
            self._closing
            and isinstance(command, get_args(HarnessCommand))
            and type(command) is not StopHarnessesCommand
        ):
            self._reject(command, "PORT_CLOSING", "harness runtime is closing")
            self._publish()
            return
        if isinstance(command, RestoreHarnessesCommand):
            self._on_restore(command)
        elif isinstance(command, EnsureHarnessCommand):
            self._on_ensure(command)
        elif isinstance(command, RemoveHarnessCommand):
            self._restore_eligibility.pop(command.name, None)
            self._on_remove(command)
        elif isinstance(command, HarnessTimerElapsedCommand):
            self._on_reconcile(command)
        elif isinstance(command, UpdateRestoreEligibilityCommand):
            candidate = command.eligibility
            if not self._restore_entity_current(candidate):
                current = (
                    self._restore_eligibility[candidate.actor]
                    if candidate.actor in self._restore_eligibility
                    else None
                )
                if current is not None and not self._restore_entity_current(current):
                    self._restore_eligibility.pop(candidate.actor, None)
                    self._version += 1
                return
            current = (
                self._restore_eligibility[candidate.actor]
                if candidate.actor in self._restore_eligibility
                else None
            )
            if current is None and len(self._restore_eligibility) >= (
                self._restore_eligibility_capacity
            ):
                return
            if current is None or (
                candidate.source_generation,
                candidate.source_version,
            ) > (current.source_generation, current.source_version):
                self._restore_eligibility[candidate.actor] = candidate
                self._version += 1
        elif isinstance(command, ProcessFactsObserved):
            self._on_process_facts(command)
        elif isinstance(command, DrainHarnessFailedCommand):
            events = tuple(self._failed_events)
            self._failed_events.clear()
            self._emit_event(
                HarnessFailedDrained(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    events,
                )
            )
        elif isinstance(command, DrainHarnessReadinessCommand):
            reports = tuple(self._readiness_reports)
            self._readiness_reports.clear()
            self._emit_event(
                HarnessReadinessDrained(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    reports,
                )
            )
        elif isinstance(command, WaitHarnessReadyCommand):
            self._on_wait_ready(command)
        elif isinstance(command, DispatchHarnessDeliveryCommand):
            self._on_dispatch(command)
        elif isinstance(command, DrainHarnessResultsCommand):
            self._on_drain_results(command)
        elif isinstance(command, ClaimHarnessResultsCommand):
            self._on_claim_results(command)
        elif isinstance(command, SettleHarnessResultCommand):
            self._on_settle_result(command)
        elif isinstance(command, EffectCompleted) and isinstance(
            command.result, DesiredStateIoCompleted
        ):
            persistence = self._persistence
            try:
                self._on_desired_completed(command.result)
            except BaseException:
                if persistence is not None:
                    persistence.redeliver(command.result.request)
                raise
            if persistence is not None:
                persistence.acknowledge(command.result.request)
        elif isinstance(command, EffectCompleted):
            self._on_claim_scan_completed(command)
        elif isinstance(command, DrainHarnessProgressCommand):
            self._on_drain_progress(command)
        elif isinstance(command, BindHarnessLivenessCommand):
            self._on_bind_liveness(command)
        elif isinstance(command, StopHarnessesCommand):
            self._on_stop_all(command)
        elif isinstance(command, HarnessProcessStarted):
            self._on_start_completed(command)
        elif isinstance(command, HarnessStopIoCompleted):
            self._on_stop_completed(command)
        elif isinstance(command, HarnessCallIoCompleted):
            self._on_call_completed(command)
        elif isinstance(command, HarnessStartTimerElapsedCommand):
            self._on_start_timeout(command)
        elif isinstance(command, RefreshHarnessProjectionsCommand):
            self._publish()
            self._emit_event(
                HarnessProjectionsRefreshed(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                )
            )
        elif isinstance(command, StageHarnessDesiredCommand):
            self._on_manage_desired(command)
        elif isinstance(command, SnapshotAdapterRegistrationCommand):
            self._on_manage_desired(command)
        elif isinstance(command, RemoveAdapterRegistrationCommand):
            self._on_manage_desired(command)
        elif isinstance(command, RestoreAdapterRegistrationCommand):
            self._on_manage_desired(command)
        elif isinstance(command, ReconcileHarnessSessionRefsCommand):
            self._on_reconcile_session_refs(command)
        else:
            self._reject(command, ipc_errors.INVALID_ARGUMENT, "unsupported harness command")
        self._publish()
        receipt = getattr(command, "processed_receipt", None)
        if isinstance(receipt, _CompletionReceipt):
            self._remember_settled_completion(
                str(getattr(command, "correlation_id", ""))
            )
            receipt.mark_processed()

    def _on_lifecycle(self, request: object) -> None:
        from .lifecycle_receipts import LifecycleMutationRequest

        assert isinstance(request, LifecycleMutationRequest)
        if self._desired_state is None:
            self._reject_correlation(
                request.correlation_id,
                "HARNESS_LIFECYCLE_STORE_MISSING",
                "Harness lifecycle authority requires DesiredStateStore",
            )
            return
        payload = request.payload
        if not isinstance(payload, (EnsureHarnessCommand, RemoveHarnessCommand)):
            self._reject_correlation(
                request.correlation_id,
                ipc_errors.INVALID_ARGUMENT,
                f"unsupported Harness lifecycle payload: {type(payload).__name__}",
            )
            return
        self._persist_desired(
            DesiredStateOperation.APPLY_HARNESS_LIFECYCLE,
            request,
            (request,),
            kwargs=(
                ("generation", self._handler_generation),
                ("version", self._version),
            ),
            context=("lifecycle_apply", request),
        )

    def _continue_lifecycle_apply(
        self, request: object, result: object
    ) -> None:
        from .lifecycle_receipts import (
            LifecycleMutationRequest,
            MutationProvenance,
        )

        assert isinstance(request, LifecycleMutationRequest)
        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or not isinstance(result[0], MutationProvenance)
            or not isinstance(result[1], bool)
        ):
            raise TypeError("harness lifecycle persistence returned invalid result")
        provenance, _replayed = result
        payload = request.payload
        assert isinstance(payload, (EnsureHarnessCommand, RemoveHarnessCommand))
        harness_id = (
            f"{payload.spec.harness}:{payload.spec.name}"
            if isinstance(payload, EnsureHarnessCommand)
            else f"{payload.harness}:{payload.name}"
        )
        active = self._lifecycle_effect_requests.get(harness_id)
        pending_correlation = next(
            (
                correlation
                for correlation, (pending_request, _provenance)
                in self._lifecycle_pending.items()
                if pending_request.attempt_token == request.attempt_token
            ),
            None,
        )
        same_attempt = bool(
            active is not None
            and active[0].attempt_token == request.attempt_token
            and pending_correlation is not None
        )
        if (
            same_attempt
            and self._lifecycle_native_admitted.get(pending_correlation)
            == request.attempt_token
        ):
            # The original continuation already transferred exact start/stop
            # custody to ProcessIoPort.  Its completion will settle the
            # retained pending correlation; replaying the frozen persistence
            # result must not supersede that native generation.
            return
        record = self._records.get(harness_id)
        runtime_satisfied = bool(
            isinstance(payload, EnsureHarnessCommand)
            and record is not None
            and record.spec == _launch_spec(payload.spec)
            and record.process is not None
            and record.process.running
            and not record.starting
        )
        if not provenance.changed and (
            isinstance(payload, RemoveHarnessCommand) or runtime_satisfied
        ):
            base = HarnessMutationCompleted(
                request.correlation_id,
                self._handler_generation,
                self._version,
                harness_id,
                False,
            )
            if isinstance(payload, EnsureHarnessCommand):
                # U0b: even the no-op "already running" completion must own
                # the desired-state row (idempotently) -- under start-after-
                # success semantics the row's ONLY writers are this confirm,
                # the migration, and offline staging.
                operation = DesiredStateOperation.CONFIRM_HARNESS_LIFECYCLE
                args = (
                    request.attempt_token,
                    provenance.resource_token,
                    _launch_spec(payload.spec),
                )
            else:
                operation = DesiredStateOperation.COMPLETE_HARNESS_LIFECYCLE
                args = (request.attempt_token, provenance.resource_token)
            self._persist_desired(
                operation,
                request,
                args,
                kwargs=(
                    ("generation", self._handler_generation),
                    ("version", self._version),
                ),
                context=(
                    "lifecycle_noop",
                    request,
                    provenance,
                    base,
                ),
            )
            return
        internal_correlation = (
            pending_correlation
            if same_attempt
            else f"{request.correlation_id}:io:{uuid.uuid4().hex}"
        )
        assert internal_correlation is not None
        internal_payload = replace(payload, correlation_id=internal_correlation)
        resource_id = harness_id
        if not same_attempt:
            self._lifecycle_pending[internal_correlation] = (request, provenance)
            self._lifecycle_effect_resources.add(resource_id)
            self._lifecycle_effect_requests[resource_id] = (request, provenance)
        if isinstance(internal_payload, EnsureHarnessCommand):
            self._on_ensure(internal_payload, lifecycle=True)
            record = self._records.get(resource_id)
            if (
                record is not None
                and record.starting
                and record.attempt_correlation_id == internal_correlation
            ):
                self._lifecycle_ensure_io[internal_correlation] = (
                    request.attempt_token,
                    resource_id,
                    record.generation,
                )
        else:
            assert isinstance(internal_payload, RemoveHarnessCommand)
            self._on_lifecycle_remove(internal_payload)

    def _replay_lifecycle_completion(
        self, request: object, receipt: object
    ) -> None:
        from .lifecycle_receipts import (
            LifecycleMutationCompleted,
            LifecycleMutationRequest,
            StoredLifecycleReceipt,
        )

        assert isinstance(request, LifecycleMutationRequest)
        assert isinstance(receipt, StoredLifecycleReceipt)
        payload = request.payload
        if not isinstance(payload, (EnsureHarnessCommand, RemoveHarnessCommand)):
            self.release_lifecycle_replay(request.attempt_token)
            self._reject(
                request,
                ipc_errors.INVALID_ARGUMENT,
                f"unsupported Harness lifecycle payload: {type(payload).__name__}",
            )
            return
        generation = receipt.generation or self._handler_generation
        version = receipt.version if receipt.version is not None else self._version
        harness_id = (
            f"{payload.spec.harness}:{payload.spec.name}"
            if isinstance(payload, EnsureHarnessCommand)
            else f"{payload.harness}:{payload.name}"
        )
        correlation_id = receipt.correlation_id or request.correlation_id
        base = HarnessMutationCompleted(
            correlation_id,
            generation,
            version,
            harness_id,
            receipt.provenance.changed,
        )
        self._emit_event(
            LifecycleMutationCompleted(
                correlation_id,
                request.attempt_token,
                generation,
                version,
                "harness",
                receipt.provenance,
                base,
                replayed=True,
            )
        )

    def _on_lifecycle_remove(self, command: RemoveHarnessCommand) -> None:
        """Stop without dropping actor custody until the I/O effect succeeds."""

        self._restore_eligibility.pop(command.name, None)
        harness_id = f"{command.harness}:{command.name}"
        record = self._records.get(harness_id)
        if record is None:
            self._emit_event(
                HarnessMutationCompleted(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    harness_id,
                    True,
                )
            )
            return
        record.generation += 1
        process = record.process
        binding = record.liveness_binding
        correlation = self._register_call(
            command.correlation_id,
            record.generation,
            operation="lifecycle-remove",
            harness_id=harness_id,
            subject_id=harness_id,
        )

        def stop_all() -> bool:
            detail: list[str] = []
            if binding is not None:
                try:
                    cast(Callable[[], object], getattr(binding, "close"))()
                except BaseException as error:
                    detail.append(str(error))
            if process is not None:
                assert self._io is not None
                stopped, error = self._io._stop_checked(
                    process,
                    record.identity,
                    harness_id=harness_id,
                    interruption_reason=command.interruption_reason,
                )
                if not stopped:
                    detail.append(error or "harness did not stop")
            if detail:
                raise RuntimeError("; ".join(detail))
            return True

        assert self._io is not None
        self._io.call(correlation, record.generation, self._version, stop_all)
        self._mark_lifecycle_native_admitted(correlation)

    def close_runtime(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._generation_lock:
            self._closing = True
        for timer in tuple(self._lifecycle_retry_timers.values()):
            timer.cancel()
        self._lifecycle_retry_timers.clear()
        terminalized = self._close_lifecycle_in_actor(deadline)
        if self._persistence is not None:
            if not self._persistence.close(
                max(0.0, deadline - time.monotonic())
            ):
                return False
        if self._claimed_results:
            return False
        if self._result_effects is not None:
            if not self._result_effects.close(max(0.0, deadline - time.monotonic())):
                return False
        if self._claimed_results:
            return False
        drained = True
        if self._io is not None:
            drained = self._io.close(deadline)
        if drained and self._owns_orphan_processes:
            drained = self._orphan_processes.close(max(0.0, deadline - time.monotonic()))
        if not drained:
            # Process completions still need the actor to acknowledge their
            # exact receipt.  Keep it addressable so a later close retry can
            # join that custody after the native operation settles.
            return False
        handle = self._handle
        if handle is not None:
            stopped = self._runtime.stop(
                handle, timeout=max(0.0, deadline - time.monotonic())
            )
            if not stopped:
                # Actor/backend ownership is still live.  Retain the stable
                # handle and every dependent call so a later close can join
                # the same stop future instead of losing cleanup custody.
                return False
            self._handle = None
        # Edge correlation custody is independently synchronized and can be
        # cleared after the actor is no longer addressable.  Domain records
        # are never mutated here outside their actor.
        self._calls.clear()
        return drained and terminalized

    def _close_lifecycle_in_actor(self, deadline: float) -> bool:
        from .lifecycle_receipts import (
            TerminalizeHarnessLifecycleCommand, HarnessLifecycleTerminalized,
        )

        if self._handle is None:
            return True
        command = TerminalizeHarnessLifecycleCommand(
            f"harness:closing:{uuid.uuid4().hex}", "HARNESS_RUNTIME_CLOSING",
            "Harness runtime closed before its lifecycle process effect completed",
        )
        settled = threading.Event()

        def observe(event: object) -> None:
            if isinstance(event, HarnessLifecycleTerminalized) and event.correlation_id == command.correlation_id:
                settled.set()

        self.subscribe_events(observe)
        try:
            while time.monotonic() < deadline:
                admission = self._admit(command)
                if admission is AdmissionResult.ACCEPTED:
                    return settled.wait(max(0.0, deadline - time.monotonic()))
                if admission is not AdmissionResult.OVERLOADED:
                    return False
                time.sleep(min(0.002, max(0.0, deadline - time.monotonic())))
            return False
        finally:
            self._event_sinks.remove(observe)

    def _terminalize_incomplete_lifecycle(self, command: object) -> None:
        if self._desired_state is None:
            from .lifecycle_receipts import HarnessLifecycleTerminalized

            self._publish_event(HarnessLifecycleTerminalized(command.correlation_id))
            return
        state = self._desired_state.load()
        receipts = tuple(
            receipt
            for receipt in state.lifecycle_receipts
            if receipt.domain == "harness" and not receipt.completed
        )
        requests = tuple(
            (resource_id, entry[0], entry[1])
            for resource_id, entry in self._lifecycle_effect_requests.items()
        )
        self._lifecycle_pending.clear()
        self._lifecycle_native_admitted.clear()
        self._terminalize_next(command, receipts, requests)

    def _terminalize_next(
        self,
        command: object,
        receipts: tuple[object, ...],
        requests: tuple[tuple[str, object, object], ...],
    ) -> None:
        from .lifecycle_receipts import HarnessLifecycleTerminalized

        if not receipts:
            self._publish()
            self._publish_event(HarnessLifecycleTerminalized(command.correlation_id))
            return
        receipt, remaining = receipts[0], receipts[1:]
        resource_id = receipt.resource_key.removeprefix("harness:")
        request_entry = next(
            (entry for entry in requests if entry[0] == resource_id), None
        )
        request = None if request_entry is None else request_entry[1]
        if request is not None and isinstance(request.payload, RemoveHarnessCommand):
            operation = DesiredStateOperation.FAIL_HARNESS_REMOVAL
        else:
            operation = DesiredStateOperation.ROLLBACK_HARNESS_LIFECYCLE
        self._persist_desired(
            operation,
            command,
            (receipt.attempt_token, receipt.provenance.resource_token),
            context=(
                "terminalize",
                command,
                receipt,
                request,
                remaining,
                requests,
            ),
        )

    def _new_generation_handler(self) -> _HarnessActorGeneration:
        """Construct one fresh handler and fence every prior async callback.

        The stable coordinator retains desired/process authority and pending
        completion correlations.  I/O workers replay the exact same frozen
        completion until the replacement generation marks its processed
        receipt, so a mailbox admission lost with the old actor is not
        mistaken for completion.
        """

        with self._generation_lock:
            self._handler_generation += 1
            generation = self._handler_generation
        if generation > 1:
            self._version += 1
            self._publish()
        self._handler_token = uuid.uuid4().hex
        return _HarnessActorGeneration(self, generation, self._handler_token)

    def _on_restore(self, command: RestoreHarnessesCommand) -> None:
        self._closing = False
        desired_specs = tuple(_launch_spec(spec) for spec in command.specs)
        desired = {self._key(spec): spec for spec in desired_specs}
        self._records = {key: _Record(spec=spec) for key, spec in desired.items()}
        if not desired:
            self._emit_event(
                HarnessRestoreCompleted(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    HarnessRestoreProjection(0, 0, 0, 0),
                )
            )
            return
        batch_id = uuid.uuid4().hex
        self._restore_batches[batch_id] = _RestoreBatch(
            correlation_id=command.correlation_id,
            attempted=len(desired),
            pending=set(desired),
        )
        batch = self._restore_batches[batch_id]
        for key, record in self._records.items():
            if key in self._lifecycle_effect_resources:
                # Desired but not dispositionable this round: a lifecycle
                # effect owns the resource, so admission never opens for it.
                # The round still owes one report per desired target --
                # silence would read as "never desired" in the projection.
                # F1: the deferred report is also the round's disposition,
                # so it settles the batch's interest in the key right here;
                # leaving it in ``batch.pending`` waited forever on a start
                # this round never admits, and the gate never opened.
                self._settle_deferred_restore_target(key, batch_id)
                continue
            record.restore_batch = batch_id
            batch.queued.append(key)
        self._admit_restore_starts(batch_id)

    def _settle_deferred_restore_target(self, key: str, batch_id: str) -> None:
        """Disposition one restore target as deferred and settle its batch.

        Two doors reach here, and neither may leave the key in
        ``batch.pending``: ``_on_restore`` seeing a target a lifecycle
        effect already owns, and ``_begin_start`` hitting the same claim
        after admission queued the key.  In both cases no start attempt of
        this round will ever settle the key -- the claiming saga starts the
        connector through the lifecycle path with its own disposition --
        so the round settles it as a handoff: report ``deferred``, take it
        out of the batch, and complete the batch when nothing else is
        pending.  A batch whose every target is deferred still completes.
        """
        self._report_readiness(key, "deferred")
        batch = self._restore_batches.get(batch_id)
        if batch is None or key not in batch.pending:
            return
        batch.pending.remove(key)
        batch.deferred += 1
        if not batch.pending:
            self._restore_batches.pop(batch_id, None)
            if not batch.terminal_rejected:
                self._emit_event(
                    HarnessRestoreCompleted(
                        batch.correlation_id,
                        self._handler_generation,
                        self._version,
                        HarnessRestoreProjection(
                            batch.attempted,
                            batch.restored,
                            batch.failed,
                            batch.deferred,
                        ),
                    )
                )

    def _starts_in_flight(self) -> int:
        """Starts currently handed to the I/O port, derived not tracked.

        Counting `starting` records rather than keeping a counter means there
        is no increment/decrement pair to get wrong across the several places
        a start can settle.  It over-counts slightly -- a reconcile-kind start
        stays `starting` until the next tick harvests it -- and over-counting
        only ever admits fewer starts, which is the safe direction.
        """

        return sum(1 for record in self._records.values() if record.starting)

    def _admit_restore_starts(self, batch_id: str) -> None:
        """Hand queued restore starts to the I/O port, up to executor width.

        The bound is the executor width, not the port's semaphore (which is
        four times wider).  A start admitted past the width is accepted by
        the port but waits for a worker thread, and its start timer is
        already running — so a fleet that merely starts slowly reports
        timeouts for harnesses nothing has contacted yet.  Keeping in-flight
        starts at the width means every running timer measures a launcher
        call that is actually happening.
        """

        batch = self._restore_batches.get(batch_id)
        if batch is None:
            return
        while batch.queued and self._starts_in_flight() < self._start_admission_width:
            key = batch.queued.popleft()
            record = self._records.get(key)
            if record is None or record.restore_batch != batch_id:
                continue
            self._begin_start(
                key,
                record,
                "restore",
                correlation_id=f"{batch.correlation_id}:{key}",
            )

    def _on_ensure(
        self, command: EnsureHarnessCommand, *, lifecycle: bool = False
    ) -> None:
        spec = _launch_spec(command.spec)
        key = self._key(spec)
        if key in self._lifecycle_effect_resources and not lifecycle:
            self._reject(
                command,
                "HARNESS_LIFECYCLE_IN_PROGRESS",
                f"{key} is owned by an incomplete lifecycle effect",
            )
            return
        record = self._records.get(key)
        if (
            record is not None
            and record.spec == spec
            and record.process is not None
            and record.process.running
            and not record.starting
        ):
            # Already running IS the disposition for an ensure: the target
            # was examined and needed no action.  Report it ready.
            self._report_readiness(key, "ready")
            self._emit_event(
                HarnessMutationCompleted(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    key,
                    False,
                )
            )
            return
        if record is None:
            record = _Record(spec=spec)
            self._records[key] = record
        elif record.explicit_correlation_id is not None:
            same_lifecycle_retry = bool(
                lifecycle
                and record.explicit_correlation_id == command.correlation_id
            )
            if not same_lifecycle_retry:
                self._reject_correlation(
                    record.explicit_correlation_id,
                    "HARNESS_START_SUPERSEDED",
                    "harness start superseded by a newer command",
                )
                record.explicit_correlation_id = None
        incumbent = (
            (record.process, record.identity)
            if record.process is not None
            else None
        )
        old_binding = record.liveness_binding
        record.liveness_binding = None
        if old_binding is not None:
            assert self._io is not None
            self._io.call(
                uuid.uuid4().hex,
                record.generation,
                self._version,
                lambda binding=old_binding: binding.close(),
            )
        record.spec = spec
        record.failures = 0
        record.restart_after = None
        # An explicit start is the documented way out of both failed
        # states (budget-exhausted and the U0b restore verdict).
        record.restore_failed_terminal = False
        record.explicit_correlation_id = command.correlation_id
        self._begin_start(
            key,
            record,
            "explicit",
            correlation_id=command.correlation_id,
            replace=incumbent,
            allow_lifecycle=lifecycle,
        )

    def _on_remove(self, command: RemoveHarnessCommand) -> None:
        harness_id = f"{command.harness}:{command.name}"
        if harness_id in self._lifecycle_effect_resources:
            self._reject(
                command,
                "HARNESS_LIFECYCLE_IN_PROGRESS",
                f"{harness_id} is owned by an incomplete lifecycle effect",
            )
            return
        record = self._records.pop(harness_id, None)
        if record is None:
            self._emit_event(
                HarnessMutationCompleted(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    harness_id,
                    False,
                )
            )
            return
        record.generation += 1
        process = record.process
        binding = record.liveness_binding
        if process is None and binding is None:
            self._emit_event(
                HarnessMutationCompleted(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    harness_id,
                    True,
                )
            )
            return
        correlation = self._register_call(
            command.correlation_id,
            record.generation,
            operation="remove",
            harness_id=None,
            subject_id=harness_id,
        )

        def stop_all() -> bool:
            detail: list[str] = []
            if binding is not None:
                try:
                    cast(Callable[[], object], getattr(binding, "close"))()
                except BaseException as error:
                    detail.append(str(error))
            if process is not None:
                assert self._io is not None
                stopped, error = self._io._stop_checked(
                    process,
                    record.identity,
                    harness_id=harness_id,
                    interruption_reason=command.interruption_reason,
                )
                if not stopped:
                    detail.append(error or "harness did not stop")
            if detail:
                raise RuntimeError("; ".join(detail))
            return True

        assert self._io is not None
        self._io.call(correlation, record.generation, self._version, stop_all)

    def _on_process_facts(self, command) -> None:
        record = self._records.get(command.harness_id)
        if (
            record is not None
            and getattr(record.process, "process_token", None) == command.process_token
            and not self._closing
        ):
            if not command.running:
                self._on_reconcile(
                    HarnessTimerElapsedCommand(
                        f"process-facts:{command.process_token}",
                        self._handler_generation,
                        self._last_timer_sequence,
                        int(time.time() * 1000),
                    ),
                    observation=True,
                )

    def _restore_entity_current(
        self, eligibility: RestoreEligibilityProjection
    ) -> bool:
        identity = self._agent_identity
        if identity is None:
            return True
        try:
            current = identity.entity_token(eligibility.actor)
        except Exception:  # cache view failure must fail open for process restore
            return False
        return current is not None and current == eligibility.entity_token

    def _on_reconcile(
        self, command: HarnessTimerElapsedCommand, *, observation: bool = False
    ) -> None:
        from .process_owner import ProcessOwner

        if not observation and (
            command.generation != self._handler_generation
            or command.version <= self._last_timer_sequence
        ):
            self._reject(
                command,
                "STALE_HARNESS_TIMER",
                "timer generation/version no longer owns harness reconciliation",
            )
            return
        if not observation:
            self._last_timer_sequence = command.version
        if self._closing:
            self._emit_event(
                HarnessTimerCompleted(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    0,
                )
            )
            return
        now = self._clock()
        restarted = 0
        for key, record in self._records.items():
            if not observation and isinstance(record.process, ProcessOwner):
                record.process.refresh()
            if key in self._lifecycle_effect_resources:
                continue
            completed = record.completed_start
            if completed is not None:
                record.completed_start = None
                record.starting = False
                # A reconcile-kind start settles here, on the tick that
                # harvests it -- that harvest is the disposition, so this
                # is where its readiness report is written (reconcile-kind
                # starts never reach `_finish_start_attempt`).
                if completed.error is not None:
                    self._record_failure(
                        key,
                        record,
                        str(completed.error),
                        permanent=bool(
                            getattr(
                                completed.error,
                                "permanent_start_failure",
                                False,
                            )
                        ),
                    )
                    self._report_readiness(
                        key,
                        "failed" if self._is_failed(record) else "retrying",
                    )
                else:
                    assert completed.process is not None
                    record.process = cast(ManagedHarnessProcess, completed.process)
                    record.identity = ProcessIdentity(
                        completed.pid, completed.identity_marker
                    )
                    record.last_error = None
                    record.failures = 0
                    record.restart_after = None
                    record.restore_failed_terminal = False
                    self._report_readiness(key, "ready")
                    restarted += 1
            if self._is_failed(record):
                # Failed is terminal: nothing in reconcile ever puts the
                # harness back into the start queue.  The only ways out are
                # an explicit operator start and a start that actually
                # succeeds -- both clear the failure counter.  External
                # faults (quota exhausted, a dead peer) recover on a
                # hours-to-weeks scale, so automatic re-probing was noise,
                # and the alarm raised on entering failed ("explicit start
                # is required") now literally means it.
                continue
            if record.starting:
                continue
            process = record.process
            if process is not None and process.running:
                continue
            if process is not None:
                runtime_error = getattr(process, "last_error", None)
                if runtime_error:
                    record.last_error = str(runtime_error)
                old_identity = record.identity
                record.process = None
                record.identity = None
                record.generation += 1
                assert self._io is not None
                self._io.stop(
                    uuid.uuid4().hex,
                    key,
                    record.generation,
                    self._version,
                    process,
                    old_identity,
                )
                old_binding = record.liveness_binding
                record.liveness_binding = None
                if old_binding is not None:
                    self._io.call(
                        uuid.uuid4().hex,
                        record.generation,
                        self._version,
                        lambda binding=old_binding: binding.close(),
                    )
                if self._restart_backoff_seconds > 0:
                    record.restart_after = now + self._restart_backoff_seconds
                    continue
            if self._automatic_restore_allowed is not None:
                try:
                    allowed = self._automatic_restore_allowed(record.spec)
                except Exception:  # noqa: BLE001 - degraded policy restores as before
                    allowed = True
                if not allowed:
                    continue
            if record.restart_after is not None and now < record.restart_after:
                continue
            eligibility = self._restore_eligibility.get(record.spec.name)
            if eligibility is not None and not self._restore_entity_current(
                eligibility
            ):
                self._restore_eligibility.pop(record.spec.name, None)
                self._version += 1
                eligibility = None
            if (
                eligibility is not None
                and eligibility.suppressed
                and eligibility.desired_generation
                == harness_desired_generation(record.spec)
            ):
                continue
            if self._starts_in_flight() >= self._start_admission_width:
                # Same bound as restore, for the same reason: the process I/O
                # port rejects submissions past its capacity outright, and a
                # rejected start is charged a failure for a harness nothing
                # contacted.  Reconcile runs about once a second, so the
                # remainder simply comes up over the next few ticks.
                continue
            record.restart_after = None
            self._begin_start(
                key,
                record,
                "automatic",
                correlation_id=uuid.uuid4().hex,
            )
        self._emit_event(
            HarnessTimerCompleted(
                command.correlation_id,
                self._handler_generation,
                self._version,
                restarted,
            )
        )

    def _on_reconcile_session_refs(
        self, command: ReconcileHarnessSessionRefsCommand
    ) -> None:
        """Join native facts before persisting the current session refs."""

        from .process_owner import ProcessOwner

        captured = tuple(
            (
                key, record.incarnation, record.generation,
                getattr(record.process, "process_token", None),
                record.spec.harness, record.spec.name, record.process,
            )
            for key, record in sorted(self._records.items())
        )
        if not any(entry[6] is not None for entry in captured):
            self._publish()
            self._complete_session_ref_reconcile(command, ())
            return

        def observe() -> tuple[_SessionRefObservation, ...]:
            observed = []
            for key, incarnation, generation, token, harness, name, process in captured:
                if isinstance(process, ProcessOwner):
                    value = process.observe_facts().session_ref
                else:
                    # Compatibility processes still run on ProcessIoPort's
                    # external I/O worker, never on the Harness mailbox.
                    value = getattr(process, "session_ref", None) if process else None
                observed.append(
                    _SessionRefObservation(
                        key, incarnation, generation, token, harness, name,
                        value if isinstance(value, str) and value else None,
                    )
                )
            return tuple(observed)

        correlation = self._register_call(
            f"{command.correlation_id}:observe:{uuid.uuid4().hex}",
            self._handler_generation,
            operation="session_refs",
            reply_correlation_id=command.correlation_id,
        )
        assert self._io is not None
        self._io.call(correlation, self._handler_generation, self._version, observe)

    def _complete_session_ref_reconcile(
        self,
        command: ReconcileHarnessSessionRefsCommand,
        refs: tuple[HarnessSessionRefProjection, ...],
    ) -> None:
        if refs:
            if self._desired_state is None:
                self._emit_event(
                    HarnessSessionRefsReconciled(
                        command.correlation_id,
                        self._handler_generation,
                        self._version,
                        refs,
                        "Harness session-ref authority requires DesiredStateStore",
                    )
                )
            else:
                pairs = tuple(
                    ((item.harness, item.name), item.session_ref) for item in refs
                )
                self._persist_desired(
                    DesiredStateOperation.SYNC_HARNESS_SESSION_REFS,
                    command,
                    (pairs,),
                    context=("session_refs", command, refs),
                )
            return
        self._emit_event(
            HarnessSessionRefsReconciled(
                command.correlation_id,
                self._handler_generation,
                self._version,
                refs,
                None,
            )
        )

    def _on_manage_desired(
        self,
        command: (
            StageHarnessDesiredCommand
            | SnapshotAdapterRegistrationCommand
            | RemoveAdapterRegistrationCommand
            | RestoreAdapterRegistrationCommand
        ),
    ) -> None:
        """Run offline-management registry work on the Harness authority."""

        operation = type(command).__name__
        store = self._desired_state
        if store is None:
            self._emit_event(
                HarnessDesiredStateManaged(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    operation,
                    False,
                    None,
                    error_detail=(
                        "Harness desired-state management requires DesiredStateStore"
                    ),
                )
            )
            return
        if isinstance(command, StageHarnessDesiredCommand):
            spec = _launch_spec(command.spec)
            before = next(
                (
                    item
                    for item in store.load().harnesses
                    if (item.harness, item.name) == (spec.harness, spec.name)
                ),
                None,
            )
            self._persist_desired(
                DesiredStateOperation.UPSERT_HARNESS,
                command,
                (spec,),
                context=("manage", command, operation, before != spec, None),
            )
            return
        if isinstance(command, SnapshotAdapterRegistrationCommand):
            try:
                state = store.load()
                spec = next(
                    (
                        item
                        for item in state.harnesses
                        if (item.harness, item.name) == ("lark", command.name)
                    ),
                    None,
                )
                legacy_pin = dict(state.channel_pins).get(command.name)
                adapter = HarnessAdapterRegistrationProjection(
                    command.name,
                    None if spec is None else _launch_projection(spec),
                    legacy_pin,
                )
                error = None
            except Exception as caught:  # noqa: BLE001 - typed actor result
                adapter = None
                error = caught
            metadata = (
                {}
                if error is None
                else {
                    "error_code": type(error).__name__,
                    "error_detail": str(error),
                    "error_is_oserror": isinstance(error, OSError),
                    "error_errno": error.errno if isinstance(error, OSError) else None,
                    "error_strerror": error.strerror if isinstance(error, OSError) else None,
                    "error_filename": error.filename if isinstance(error, OSError) else None,
                    "error_filename2": error.filename2 if isinstance(error, OSError) else None,
                }
            )
            self._emit_event(
                HarnessDesiredStateManaged(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    operation,
                    False,
                    adapter,
                    **metadata,
                )
            )
            return
        if isinstance(command, RemoveAdapterRegistrationCommand):
            expected = (
                None
                if command.expected_spec is None
                else _launch_spec(command.expected_spec)
            )
            adapter = HarnessAdapterRegistrationProjection(
                command.name,
                command.expected_spec,
                command.expected_legacy_pin,
            )
            self._persist_desired(
                DesiredStateOperation.REMOVE_ADAPTER_REGISTRATION,
                command,
                (command.name,),
                kwargs=(
                    ("expected_spec", expected),
                    ("expected_legacy_pin", command.expected_legacy_pin),
                ),
                context=(
                    "manage",
                    command,
                    operation,
                    expected is not None or command.expected_legacy_pin is not None,
                    adapter,
                ),
            )
            return
        assert isinstance(command, RestoreAdapterRegistrationCommand)
        restored = None if command.spec is None else _launch_spec(command.spec)
        adapter = HarnessAdapterRegistrationProjection(
            command.name, command.spec, command.legacy_pin
        )
        self._persist_desired(
            DesiredStateOperation.RESTORE_ADAPTER_REGISTRATION,
            command,
            (command.name,),
            kwargs=(("spec", restored), ("legacy_pin", command.legacy_pin)),
            context=(
                "manage",
                command,
                operation,
                restored is not None or command.legacy_pin is not None,
                adapter,
            ),
        )

    def _on_wait_ready(self, command: WaitHarnessReadyCommand) -> None:
        harness_id = f"{command.harness}:{command.name}"
        record = self._records.get(harness_id)
        process = record.process if record else None
        if process is None:
            self._emit_event(
                HarnessReadyObserved(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    harness_id,
                    False,
                )
            )
            return
        correlation = self._register_call(
            command.correlation_id,
            record.generation,
            operation="ready",
            harness_id=harness_id,
        )

        def wait_ready() -> bool:
            wait = getattr(process, "wait_ready", None)
            if callable(wait):
                return bool(wait(timeout=command.timeout_seconds))
            return bool(process.running)

        assert self._io is not None
        self._io.call(correlation, record.generation, self._version, wait_ready)

    def _on_dispatch(self, command: DispatchHarnessDeliveryCommand) -> None:
        if command.delivery.delivery_id in self._delivery_reservations:
            self._emit_event(
                HarnessDeliveryAdmitted(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    "",
                    command.delivery.delivery_id,
                    False,
                )
            )
            return
        selected = next(
            (
                (key, record)
                for key, record in self._records.items()
                if record.spec.name == command.name
                and isinstance(record.process, StreamingHarnessProcess)
                and record.process.running
            ),
            None,
        )
        if selected is None or selected[1].process is None:
            self._emit_event(
                HarnessDeliveryAdmitted(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    "",
                    command.delivery.delivery_id,
                    False,
                )
            )
            return
        selected_key, selected_record = selected
        process = cast(StreamingHarnessProcess, selected_record.process)
        self._delivery_reservations[command.delivery.delivery_id] = (
            _DeliveryReservation(selected_key, selected_record.generation)
        )
        self._result_reservation_snapshot = frozenset(self._delivery_reservations)
        correlation = self._register_call(
            command.correlation_id,
            selected_record.generation,
            operation="dispatch",
            harness_id=selected_key,
            delivery_id=command.delivery.delivery_id,
        )
        assert self._io is not None
        self._io.call(
            correlation,
            selected_record.generation,
            self._version,
            lambda: process.enqueue(command.delivery),
        )

    def _on_drain_results(self, command: DrainHarnessResultsCommand) -> None:
        processes = tuple(
            cast(StreamingHarnessProcess, record.process)
            for record in self._records.values()
            if isinstance(record.process, StreamingHarnessProcess)
        )
        correlation = self._register_call(
            command.correlation_id,
            self._handler_generation,
            operation="results",
        )

        def drain() -> tuple[HarnessResult, ...]:
            results: list[HarnessResult] = []
            for process in processes:
                results.extend(process.drain_results())
            return tuple(results)

        assert self._io is not None
        self._io.call(
            correlation,
            self._handler_generation,
            self._version,
            drain,
        )

    def _scan_results(self, scan: _ClaimScan) -> _ClaimScanOutcome:
        rows: list[tuple[str, int, HarnessResult]] = []
        errors: list[str] = []
        for harness_id, generation, process in scan.processes:
            remaining = scan.limit - len(rows)
            if remaining <= 0:
                break
            try:
                results = process.drain_results(remaining)
            except Exception as error:
                errors.append(f"{harness_id}:{type(error).__name__}")
                continue
            rows.extend((harness_id, generation, result) for result in results)
        return _ClaimScanOutcome(tuple(rows), tuple(errors))

    def _on_claim_results(self, command: ClaimHarnessResultsCommand) -> None:
        if command.limit < 1 or command.limit > self._result_claim_capacity:
            self._reject(
                command, ipc_errors.INVALID_ARGUMENT, "result claim limit is out of range"
            )
            return
        if self._claim_scan_id is not None:
            if len(self._claim_waiters) >= self._result_claim_capacity:
                self._reject(
                    command, "HARNESS_RESULT_CLAIM_OVERLOADED",
                    "result claim waiters are full",
                )
                return
            self._claim_waiters.append((command.correlation_id, command.limit))
            return
        available = min(
            command.limit,
            self._result_claim_capacity - len(self._claimed_results),
        )
        processes = tuple(
            (harness_id, record.generation, cast(StreamingHarnessProcess, record.process))
            for harness_id, record in self._records.items()
            if isinstance(record.process, StreamingHarnessProcess)
        )
        if available <= 0 or not processes or self._closing:
            self._emit_event(HarnessResultsClaimed(
                command.correlation_id, self._handler_generation, self._version,
                tuple(self._claimed_results.values())[:command.limit],
            ))
            return
        operation_id = uuid.uuid4().hex
        scan = _ClaimScan(operation_id, available, processes)
        self._claim_scan_id = operation_id
        self._claim_scan_generations = frozenset(
            (harness_id, generation) for harness_id, generation, _ in processes
        )
        self._claim_waiters = [(command.correlation_id, command.limit)]
        if self._result_effects is None:
            self._result_effects = EffectLane(
                name="harness-result-claim-io",
                execute=self._scan_results,
                complete=lambda event: self._runtime.tell(self._handle, event),
                capacity=1,
            )
        admitted = self._result_effects.submit(
            EffectRequest(operation_id, self._handler_generation, scan)
        )
        if admitted is not AdmissionResult.ACCEPTED:
            self._claim_scan_id = None
            self._claim_scan_generations = frozenset()
            self._claim_waiters.clear()
            self._reject(
                command, "HARNESS_RESULT_CLAIM_OVERLOADED",
                f"result claim admission {admitted.value}",
            )

    def _on_claim_scan_completed(self, event: EffectCompleted[_ClaimScanOutcome]) -> None:
        assert self._result_effects is not None
        if event.operation_id != self._claim_scan_id:
            self._result_effects.acknowledge(event.operation_id, event.generation)
            return
        outcome = event.result or _ClaimScanOutcome((), (event.error or "claim-io-failed",))
        for harness_id, generation, result in outcome.rows:
            reservation = self._delivery_reservations.get(result.delivery_id)
            if (
                reservation is not None
                and reservation.claim_token is not None
                and reservation.process_generation == generation
            ):
                continue
            token = uuid.uuid4().hex
            claim = ClaimedHarnessResult(token, harness_id, generation, result)
            self._claimed_results[token] = claim
            # A confirmed stop can release an unclaimed old reservation while
            # this native drain is still in flight.  If the same delivery was
            # offered to a replacement, retain the old result without taking
            # ownership of the replacement's reservation.
            if reservation is None or reservation.process_generation == generation:
                self._delivery_reservations[result.delivery_id] = (
                    _DeliveryReservation(harness_id, generation, token)
                )
        self._result_reservation_snapshot = frozenset(self._delivery_reservations)
        claims = tuple(self._claimed_results.values())
        for correlation_id, limit in self._claim_waiters:
            self._emit_event(HarnessResultsClaimed(
                correlation_id, self._handler_generation, self._version,
                claims[:limit], outcome.errors,
            ))
        self._claim_waiters.clear()
        self._claim_scan_id = None
        self._claim_scan_generations = frozenset()
        for harness_id, generation in self._retire_after_claim_scan:
            for delivery_id, reservation in tuple(self._delivery_reservations.items()):
                if (
                    reservation.harness_id == harness_id
                    and reservation.process_generation == generation
                ):
                    self._release_unclaimed_delivery(delivery_id)
        self._retire_after_claim_scan.clear()
        self._result_effects.acknowledge(event.operation_id, event.generation)

    def _on_settle_result(self, command: SettleHarnessResultCommand) -> None:
        claim = self._claimed_results.get(command.claim_token)
        reservation = self._delivery_reservations.get(command.delivery_id)
        settled = bool(
            claim is not None
            and claim.result.delivery_id == command.delivery_id
        )
        if settled:
            self._claimed_results.pop(command.claim_token)
            if (
                reservation is not None
                and reservation.claim_token == command.claim_token
                and reservation.process_generation == claim.process_generation
            ):
                self._delivery_reservations.pop(command.delivery_id, None)
                self._result_reservation_snapshot = frozenset(
                    self._delivery_reservations
                )
        self._emit_event(HarnessResultSettled(
            command.correlation_id, self._handler_generation, self._version,
            command.claim_token, command.delivery_id, settled,
        ))

    def _release_unclaimed_delivery(
        self, delivery_id: str, *, harness_id: str | None = None,
        generation: int | None = None,
    ) -> None:
        reservation = self._delivery_reservations.get(delivery_id)
        if reservation is None or reservation.claim_token is not None:
            return
        if harness_id is not None and reservation.harness_id != harness_id:
            return
        if generation is not None and reservation.process_generation != generation:
            return
        self._delivery_reservations.pop(delivery_id, None)
        self._result_reservation_snapshot = frozenset(self._delivery_reservations)

    def _release_unclaimed_for(
        self, harness_id: str, *, before_generation: int | None = None,
    ) -> None:
        for delivery_id, reservation in tuple(self._delivery_reservations.items()):
            if reservation.harness_id != harness_id:
                continue
            if (
                before_generation is not None
                and reservation.process_generation >= before_generation
            ):
                continue
            scan_key = (reservation.harness_id, reservation.process_generation)
            if scan_key in self._claim_scan_generations:
                # A native drain may already have removed the only result.
                # Keep its reservation until the claim completion enters this
                # mailbox; stopping the old process cannot reopen the offer.
                self._retire_after_claim_scan.add(scan_key)
                continue
            self._release_unclaimed_delivery(delivery_id)

    def _on_drain_progress(self, command: DrainHarnessProgressCommand) -> None:
        processes = tuple(
            cast(StreamingHarnessProcess, record.process)
            for record in self._records.values()
            if isinstance(record.process, StreamingHarnessProcess)
        )
        correlation = self._register_call(
            command.correlation_id,
            self._handler_generation,
            operation="progress",
        )

        def drain() -> tuple[object, ...]:
            events: list[object] = []
            for process in processes:
                method = getattr(process, "drain_progress", None)
                if callable(method):
                    events.extend(method())
            return tuple(events)

        assert self._io is not None
        self._io.call(
            correlation,
            self._handler_generation,
            self._version,
            drain,
        )

    def _on_bind_liveness(self, command: BindHarnessLivenessCommand) -> None:
        harness_id = f"{command.harness}:{command.name}"
        record = self._records.get(harness_id)
        if record is None:
            self._emit_event(
                HarnessLivenessBound(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    harness_id,
                    False,
                )
            )
            return
        old = record.liveness_binding
        record.liveness_binding = command.binding
        self._emit_event(
            HarnessLivenessBound(
                command.correlation_id,
                self._handler_generation,
                self._version,
                harness_id,
                True,
            )
        )
        if old is not None and old is not command.binding:
            assert self._io is not None
            self._io.call(
                uuid.uuid4().hex,
                record.generation,
                self._version,
                lambda: old.close(),
            )

    def _on_stop_all(self, command: StopHarnessesCommand) -> None:
        self._closing = True
        self._terminate_pending(
            "HARNESS_RUNTIME_STOPPED", "harness runtime stopped"
        )
        deadline = command.deadline_ms / 1000.0
        for harness_id, record in self._records.items():
            record.generation += 1
            record.starting = False
            if record.explicit_correlation_id is not None:
                self._reject_correlation(
                    record.explicit_correlation_id,
                    "HARNESS_RUNTIME_STOPPED",
                    "harness runtime stopped",
                )
                record.explicit_correlation_id = None
            if record.process is not None:
                self._stop_retry_processes.setdefault(
                    harness_id, (record.process, record.identity)
                )
                record.process = None
                record.identity = None
            if record.liveness_binding is not None:
                self._stop_retry_bindings.setdefault(
                    harness_id, record.liveness_binding
                )
                record.liveness_binding = None
        processes = tuple(self._stop_retry_processes.items())
        bindings = tuple(self._stop_retry_bindings.items())
        correlation = self._register_call(
            command.correlation_id,
            self._handler_generation,
            operation="stop_all",
        )

        def stop_all() -> _StopAllOutcome:
            errors: list[str] = []
            stopped_ids: list[str] = []
            bindings_closed: list[str] = []
            assert self._io is not None
            for harness_id, (process, identity) in processes:
                if time.monotonic() >= deadline:
                    errors.append(f"{harness_id}: harness stop deadline elapsed")
                    continue
                stopped, detail = self._io._stop_checked(
                    process, identity, harness_id=harness_id
                )
                if stopped:
                    stopped_ids.append(harness_id)
                else:
                    errors.append(detail or "harness did not stop")
            for harness_id, binding in bindings:
                if time.monotonic() >= deadline:
                    errors.append(f"{harness_id}: binding stop deadline elapsed")
                    continue
                try:
                    binding.close()
                except BaseException as error:
                    errors.append(str(error))
                else:
                    bindings_closed.append(harness_id)
            return _StopAllOutcome(
                tuple(stopped_ids), tuple(bindings_closed), tuple(errors)
            )

        assert self._io is not None
        self._io.call(
            correlation,
            self._handler_generation,
            self._version,
            stop_all,
        )

    def _on_start_completed(self, event: HarnessProcessStarted) -> None:
        tracked = self._lifecycle_ensure_io.pop(event.correlation_id, None)
        if (
            tracked is not None
            and tracked[0] in self._lifecycle_ensure_failure_fences
        ):
            attempt_token, harness_id, generation = tracked
            self._cancel_start_timer(harness_id, generation)
            record = self._records.get(harness_id)
            if (
                record is not None
                and record.generation == event.generation
                and record.attempt_correlation_id == event.correlation_id
            ):
                record.starting = False
                record.explicit_correlation_id = None
                record.attempt_correlation_id = None
            if event.process is not None:
                self._schedule_ensure_failure_stop(
                    attempt_token,
                    harness_id,
                    event.generation,
                    cast(ManagedHarnessProcess, event.process),
                    ProcessIdentity(event.pid, event.identity_marker),
                )
            self._publish()
            self._maybe_finish_ensure_failure_fence(attempt_token)
            return
        self._cancel_start_timer(event.harness_id, event.generation)
        record = self._records.get(event.harness_id)
        if (
            record is None
            or event.generation != record.generation
            or event.correlation_id != record.attempt_correlation_id
            or not record.starting
        ):
            if event.process is not None:
                assert self._io is not None
                self._io.stop(
                    uuid.uuid4().hex,
                    event.harness_id,
                    event.generation,
                    self._version,
                    cast(ManagedHarnessProcess, event.process),
                    ProcessIdentity(event.pid, event.identity_marker),
                )
            return
        record.starting = False
        if record.attempt_kind == "automatic":
            record.starting = True
            record.completed_start = event
            return
        if event.error is not None:
            self._record_failure(
                event.harness_id,
                record,
                str(event.error),
                permanent=bool(
                    getattr(event.error, "permanent_start_failure", False)
                ),
            )
            self._finish_start_attempt(event.harness_id, record, False, event.error)
            return
        assert event.process is not None
        record.process = cast(ManagedHarnessProcess, event.process)
        record.identity = ProcessIdentity(event.pid, event.identity_marker)
        record.last_error = None
        record.failures = 0
        record.restart_after = None
        record.restore_failed_terminal = False
        self._finish_start_attempt(event.harness_id, record, True, None)

    def _on_start_timeout(self, event: HarnessStartTimerElapsedCommand) -> None:
        self._cancel_start_timer(event.harness_id, event.generation)
        record = self._records.get(event.harness_id)
        if (
            record is None
            or record.generation != event.generation
            or record.attempt_correlation_id != event.correlation_id
            or not record.starting
        ):
            return
        record.starting = False
        record.generation += 1
        error = TimeoutError(
            "harness start did not complete within "
            f"{self._start_timeout_seconds:g}s"
        )
        self._record_failure(event.harness_id, record, str(error))
        self._finish_start_attempt(event.harness_id, record, False, error)

    def _on_stop_completed(self, event: HarnessStopIoCompleted) -> None:
        fenced = self._lifecycle_ensure_failure_stops.pop(
            event.correlation_id, None
        )
        if fenced is not None:
            attempt_token, harness_id, generation, process, identity = fenced
            fence = self._lifecycle_ensure_failure_fences.get(attempt_token)
            if fence is None:
                return
            if not event.stopped:
                fence.unstopped.append(
                    (harness_id, generation, process, identity)
                )
            self._maybe_finish_ensure_failure_fence(attempt_token)
            return
        record = self._records.get(event.harness_id)
        if record is None or record.generation != event.generation:
            return
        if not event.stopped and event.detail:
            record.last_error = event.detail
        if event.stopped:
            self._release_unclaimed_for(
                event.harness_id, before_generation=event.generation
            )

    def _on_call_completed(self, event: HarnessCallIoCompleted) -> None:
        pending = self._calls.pop(event.correlation_id)
        if pending is None:
            if event.error is not None:
                self._reject_correlation(
                    event.correlation_id,
                    "HARNESS_IO_CANCELLED",
                    str(event.error),
                )
            return
        if pending.reply_correlation_id is not None:
            # Each observation has its own I/O settlement identity. A fleet
            # change may require another observation for the same caller;
            # only the reply/retry uses that caller's public correlation.
            event = replace(event, correlation_id=pending.reply_correlation_id)
        if event.generation != pending.expected_generation:
            if pending.operation == "dispatch" and pending.delivery_id is not None:
                self._release_unclaimed_delivery(pending.delivery_id)
            self._reject_correlation(
                event.correlation_id,
                "STALE_HARNESS_IO_COMPLETION",
                "stale process I/O completion",
            )
            return
        if pending.harness_id is not None:
            record = self._records.get(pending.harness_id)
            if record is None or record.generation != pending.expected_generation:
                if pending.operation == "dispatch" and pending.delivery_id is not None:
                    self._release_unclaimed_delivery(pending.delivery_id)
                self._reject_correlation(
                    event.correlation_id,
                    "STALE_HARNESS_IO_COMPLETION",
                    "stale process I/O completion",
                )
                return
        if event.error is not None:
            if pending.operation == "dispatch" and pending.delivery_id is not None:
                self._release_unclaimed_delivery(pending.delivery_id)
            self._reject_correlation(
                event.correlation_id,
                "HARNESS_IO_FAILED",
                str(event.error),
            )
            return
        if pending.operation == "session_refs":
            from .process_owner import ProcessOwner

            observed = event.value
            if (
                not isinstance(observed, tuple)
                or not all(isinstance(item, _SessionRefObservation) for item in observed)
            ):
                self._reject_correlation(
                    event.correlation_id,
                    "STALE_HARNESS_IO_COMPLETION",
                    "session-ref facts no longer cover the current harness fleet",
                )
                return
            if len(observed) != len(self._records):
                self._on_reconcile_session_refs(
                    ReconcileHarnessSessionRefsCommand(event.correlation_id)
                )
                return
            for item in observed:
                record = self._records.get(item.key)
                if (
                    record is None
                    or record.incarnation != item.incarnation
                    or record.generation != item.generation
                    or getattr(record.process, "process_token", None)
                    != item.process_token
                    or (
                        isinstance(record.process, ProcessOwner)
                        and record.process.facts().session_ref != item.session_ref
                    )
                ):
                    # A concurrent lifecycle remove/restart changed the fleet
                    # while native facts were read. Join the new owner (or
                    # settle empty after removal) under the same correlation;
                    # never write the old process's ref into a new row.
                    self._on_reconcile_session_refs(
                        ReconcileHarnessSessionRefsCommand(event.correlation_id)
                    )
                    return
            self._publish()
            refs = tuple(
                HarnessSessionRefProjection(item.harness, item.name, item.session_ref)
                for item in observed
                if item.session_ref is not None
            )
            self._complete_session_ref_reconcile(
                ReconcileHarnessSessionRefsCommand(event.correlation_id), refs
            )
        elif pending.operation == "remove":
            if bool(event.value) and pending.subject_id is not None:
                self._release_unclaimed_for(pending.subject_id)
            self._emit_event(
                HarnessMutationCompleted(
                    event.correlation_id,
                    self._handler_generation,
                    self._version,
                    pending.subject_id or "",
                    bool(event.value),
                )
            )
        elif pending.operation == "lifecycle-remove":
            if pending.subject_id is not None:
                if bool(event.value):
                    self._release_unclaimed_for(pending.subject_id)
                self._records.pop(pending.subject_id, None)
            self._emit_event(
                HarnessMutationCompleted(
                    event.correlation_id,
                    self._handler_generation,
                    self._version,
                    pending.subject_id or "",
                    bool(event.value),
                )
            )
        elif pending.operation == "ready":
            self._emit_event(
                HarnessReadyObserved(
                    event.correlation_id,
                    self._handler_generation,
                    self._version,
                    pending.harness_id or "",
                    bool(event.value),
                )
            )
        elif pending.operation == "dispatch":
            if not bool(event.value) and pending.delivery_id is not None:
                self._release_unclaimed_delivery(pending.delivery_id)
            self._emit_event(
                HarnessDeliveryAdmitted(
                    event.correlation_id,
                    self._handler_generation,
                    self._version,
                    pending.harness_id or "",
                    pending.delivery_id or "",
                    bool(event.value),
                )
            )
        elif pending.operation == "results":
            for result in cast(tuple[HarnessResult, ...], event.value):
                self._release_unclaimed_delivery(result.delivery_id)
            self._emit_event(
                HarnessResultObserved(
                    event.correlation_id,
                    self._handler_generation,
                    self._version,
                    cast(tuple[HarnessResult, ...], event.value),
                )
            )
        elif pending.operation == "progress":
            self._emit_event(
                HarnessProgressObserved(
                    event.correlation_id,
                    self._handler_generation,
                    self._version,
                    cast(tuple[object, ...], event.value),
                )
            )
        elif pending.operation == "stop_all":
            outcome = cast(_StopAllOutcome, event.value)
            for harness_id in outcome.stopped:
                self._stop_retry_processes.pop(harness_id, None)
                self._release_unclaimed_for(harness_id)
            for harness_id in outcome.bindings_closed:
                self._stop_retry_bindings.pop(harness_id, None)
            remaining = tuple(
                sorted({
                    *self._stop_retry_processes,
                    *self._stop_retry_bindings,
                    *(
                        key
                        for key, record in self._records.items()
                        if record.process is not None
                        or record.liveness_binding is not None
                    ),
                })
            )
            for detail in outcome.errors:
                self._failed_events.append(detail)
            self._emit_event(
                HarnessesStopped(
                    event.correlation_id,
                    self._handler_generation,
                    self._version,
                    remaining,
                    drain_complete=(
                        not outcome.errors
                        and not remaining
                        and (self._io is None or self._io.in_flight <= 1)
                    ),
                )
            )

    def _begin_start(
        self,
        key: str,
        record: _Record,
        kind: str,
        *,
        correlation_id: str,
        replace: tuple[ManagedHarnessProcess, ProcessIdentity | None] | None = None,
        allow_lifecycle: bool = False,
    ) -> None:
        if key in self._lifecycle_effect_resources and not allow_lifecycle:
            # A lifecycle effect claimed the resource while this key sat in
            # the batch queue (admission drains one settlement at a time),
            # or between the restore command and this call.  F1 door 2: a
            # silent return here used to strand the key in ``batch.pending``
            # forever -- no start, no report, no batch completion, gate
            # never opening.  The claim's own saga starts the connector
            # through the lifecycle path, so this round hands the key over:
            # settle it as deferred.
            batch_id = record.restore_batch
            if batch_id is not None:
                record.restore_batch = None
                self._settle_deferred_restore_target(key, batch_id)
            return
        record.generation += 1
        record.starting = True
        record.attempt_kind = kind
        record.process = None
        record.identity = None
        record.attempt_correlation_id = correlation_id
        generation = record.generation
        scheduled_version = self._version
        # Publish ownership/starting before a fast or blocked I/O worker can
        # report completion; projection readers never observe a ghost gap.
        self._publish()
        assert self._io is not None
        self._io.start(
            correlation_id,
            key,
            generation,
            self._version,
            record.spec,
            replace=replace,
        )
        if allow_lifecycle:
            self._mark_lifecycle_native_admitted(correlation_id)
        if self._start_timeout_seconds > 0:
            timer = threading.Timer(
                self._start_timeout_seconds,
                lambda: self._emit_completion(
                    HarnessStartTimerElapsedCommand(
                        correlation_id,
                        generation,
                        scheduled_version,
                        key,
                    )
                ),
            )
            timer.daemon = True
            self._timers[(key, generation)] = timer
            timer.start()

    def _is_failed(self, record: _Record) -> bool:
        """Derived, never stored: the failure budget is spent, retries stop.

        Budget 0 disables failure termination entirely (retries continue
        forever under backoff), which is why the check is not just a
        comparison.  Unlike the quarantine this replaced, failed does NOT
        lift itself: there is no cooldown and no probe start, so this state
        is where the harness stays until an explicit start.  The one
        exception to "budget decides" is ``restore_failed_terminal``: a
        restore-kind start gets a single attempt by ruling, even at
        budget 0.
        """

        return record.restore_failed_terminal or (
            self._failure_budget > 0
            and record.failures >= self._failure_budget
        )

    def _report_readiness(self, key: str, verdict: str) -> None:
        """Record one disposition report for the daemon's phase-③ drain.

        Emitted exactly where a disposition happens, never on a schedule:
        the readiness boundary is "the connector actor was established and
        handed back its first report", so each settled start attempt (or an
        ensure that found the connector already running, or a restore target
        admission could not take this round) appends exactly one report.
        """

        self._readiness_reports.append(
            ReadinessReport(phase="connector-up", verdict=verdict, source=key)
        )

    def _record_failure(
        self,
        key: str,
        record: _Record,
        detail: str,
        *,
        permanent: bool = False,
    ) -> None:
        record.last_error = detail
        was_failed = self._is_failed(record)
        record.failures += 1
        if permanent:
            record.restore_failed_terminal = True
        if self._is_failed(record):
            record.restart_after = None
            if not was_failed and (
                record.restore_batch is None or self._desired_state is None
            ):
                self._failed_events.append(key)
                # U0b: the failure budget just ran out.  If this harness
                # previously came up (its desired-state row exists), the row
                # must now read "ran before, did not come back" so a daemon
                # restart displays it and refuses to auto-retry it.  No row
                # (an explicit first start that never succeeded) leaves no
                # trace -- mark_harness_failed is a no-op then.  Persistence
                # is best-effort for reconcile: the budget verdict itself
                # must not be hostage to a status write. Restore keeps its
                # own exact write and batch correlation below.
                if self._desired_state is not None and record.restore_batch is None:
                    harness, name = key.split(":", 1)
                    self._persist_desired(
                        DesiredStateOperation.MARK_HARNESS_FAILED,
                        (harness, name),
                        (harness, name),
                        context=("mark_failed", harness, name),
                    )
            return
        base = self._restart_backoff_seconds
        delay = (
            min(
                capped_exponential(
                    base,
                    self._start_backoff_max_seconds,
                    max(0, record.failures - 1),
                ),
                self._start_backoff_max_seconds,
            )
            if base > 0
            else 0.0
        )
        record.restart_after = self._clock() + delay

    def _finish_start_attempt(
        self,
        key: str,
        record: _Record,
        success: bool,
        error: BaseException | None,
    ) -> None:
        # One settled start attempt = one disposition = one report. A failed
        # restore delays that report until its status write commits. On the
        # failure path `_record_failure` has already run, so the verdict
        # reads the post-settlement budget state.
        restore_single_attempt_failed = (
            not success and record.restore_batch is not None
        )
        if restore_single_attempt_failed:
            # U0b (Allen 2026-09-03): a restore-kind start gets exactly one
            # attempt, so its failure verdict is terminal BEFORE the report
            # reads it.  The row being restored had status "running" -- it
            # ran before -- so a failed bring-back is "previously up, did
            # not come back": persist failed (displayed, human-owned) and
            # keep the reconcile loop from retrying it.  Budget 0 is the
            # documented infinite-retry opt-in for in-generation reconcile;
            # it does not buy back this ruling, so the record is made
            # terminal regardless.
            was_failed = self._is_failed(record)
            record.restart_after = None
            record.failures = max(
                record.failures,
                self._failure_budget if self._failure_budget > 0 else 1,
            )
            if self._failure_budget <= 0:
                # _is_failed refuses to terminate at budget 0 by contract;
                # pin the verdict through the dedicated flag instead.
                record.restore_failed_terminal = True
            if (
                not was_failed
                and self._is_failed(record)
                and (record.restore_batch is None or self._desired_state is None)
            ):
                # Same alarm as the budget trip: entering failed must be
                # observable (watchdog/readiness drain these events).
                self._failed_events.append(key)
        batch_id = record.restore_batch
        if restore_single_attempt_failed and self._desired_state is not None:
            # The failed restore disposition is not durable until the exact
            # accepted desired-state operation reports its committed snapshot.
            # Keep the batch key pending; process start admission is separate.
            record.restore_batch = None
            assert batch_id is not None
            self._persist_restore_failure(
                batch_id, key, record.incarnation, record.generation
            )
            return
        self._report_readiness(
            key,
            "ready"
            if success
            else ("failed" if self._is_failed(record) else "retrying"),
        )
        if record.explicit_correlation_id is not None:
            correlation_id = record.explicit_correlation_id
            record.explicit_correlation_id = None
            if success:
                self._emit_event(
                    HarnessMutationCompleted(
                        correlation_id,
                        self._handler_generation,
                        self._version,
                        key,
                        True,
                    )
                )
            else:
                failure = error or RuntimeError(record.last_error or "start failed")
                failure_code = getattr(failure, "code", None)
                self._reject_correlation(
                    correlation_id,
                    (
                        "HARNESS_START_TIMEOUT"
                        if isinstance(failure, TimeoutError)
                        else (
                            failure_code
                            if isinstance(failure_code, str) and failure_code
                            else "HARNESS_START_FAILED"
                        )
                    ),
                    str(failure),
                )
        record.restore_batch = None
        if batch_id is None:
            return
        batch = self._restore_batches.get(batch_id)
        if batch is None or key not in batch.pending:
            return
        self._settle_restore_key(batch_id, key, success=success)

    def _settle_restore_key(
        self, batch_id: str, key: str, *, success: bool | None
    ) -> None:
        batch = self._restore_batches.get(batch_id)
        if batch is None or key not in batch.pending:
            return
        batch.pending.remove(key)
        if success:
            batch.restored += 1
        elif success is False:
            batch.failed += 1
        # One start settled, so one admission slot is free.  Restore now
        # drains at the pace starts actually complete instead of dumping the
        # whole fleet at a port that can only hold part of it.
        self._admit_restore_starts(batch_id)
        # Admission can synchronously defer the final queued target and
        # consume this batch. Only the still-owning path publishes its result.
        if not batch.pending and self._restore_batches.get(batch_id) is batch:
            self._restore_batches.pop(batch_id, None)
            if not batch.terminal_rejected:
                self._emit_event(
                    HarnessRestoreCompleted(
                        batch.correlation_id,
                        self._handler_generation,
                        self._version,
                        HarnessRestoreProjection(
                            batch.attempted,
                            batch.restored,
                            batch.failed,
                            batch.deferred,
                        ),
                    )
                )

    def _persist_restore_failure(
        self, batch_id: str, key: str, record_incarnation: str,
        record_generation: int,
    ) -> None:
        batch = self._restore_batches.get(batch_id)
        if batch is None or key not in batch.pending:
            return
        persistence = self._persistence
        if persistence is None:
            self._fail_restore_persistence(
                batch_id, key, "HARNESS_PERSISTENCE_UNAVAILABLE",
                "Harness desired-state persistence is unavailable",
            )
            return
        harness, name = key.split(":", 1)
        request = DesiredStateIoRequest(
            operation_id=f"harness-state-{uuid.uuid4().hex}",
            owner_generation=self._handler_generation,
            owner_version=self._version,
            operation=DesiredStateOperation.MARK_HARNESS_FAILED,
            args=(harness, name),
            context=(
                "restore_mark_failed", batch_id, key,
                record_incarnation, record_generation,
            ),
        )
        admission = persistence.submit(request)
        if admission is not AdmissionResult.ACCEPTED:
            self._fail_restore_persistence(
                batch_id, key, "HARNESS_PERSISTENCE_OVERLOADED",
                f"Harness persistence admission is {admission.value}",
            )

    def _fail_restore_persistence(
        self, batch_id: str, key: str, code: str, detail: str
    ) -> None:
        batch = self._restore_batches.get(batch_id)
        if batch is None or key not in batch.pending:
            return
        if not batch.terminal_rejected:
            batch.terminal_rejected = True
            self._reject_correlation(batch.correlation_id, code, detail)
        self._settle_restore_key(batch_id, key, success=None)

    def _register_call(
        self,
        correlation: str,
        generation: int,
        *,
        operation: str,
        harness_id: str | None = None,
        delivery_id: str | None = None,
        subject_id: str | None = None,
        reply_correlation_id: str | None = None,
    ) -> str:
        self._calls.register(
            correlation,
            _PendingCall(
                expected_generation=generation,
                operation=operation,
                harness_id=harness_id,
                delivery_id=delivery_id,
                subject_id=subject_id,
                reply_correlation_id=reply_correlation_id,
            ),
        )
        return correlation

    def _mark_lifecycle_native_admitted(self, correlation_id: str) -> None:
        pending = self._lifecycle_pending.get(correlation_id)
        if pending is None:
            return
        attempt_token = getattr(pending[0], "attempt_token", None)
        if isinstance(attempt_token, str) and attempt_token:
            self._lifecycle_native_admitted[correlation_id] = attempt_token

    def _pop_lifecycle_pending(self, correlation_id: str) -> tuple[object, object] | None:
        self._lifecycle_native_admitted.pop(correlation_id, None)
        return self._lifecycle_pending.pop(correlation_id, None)

    def _cancel_start_timer(self, key: str, generation: int) -> None:
        timer = self._timers.pop((key, generation), None)
        if timer is not None:
            timer.cancel()

    def _emit_completion(self, completion: object) -> tuple[AdmissionResult, int]:
        from .lifecycle_receipts import LifecycleMutationRequest

        with self._generation_lock:
            generation = self._handler_generation
            admission = (
                AdmissionResult.CLOSED
                if self._closing
                and isinstance(completion, LifecycleMutationRequest)
                else self._admit(completion)
            )
        return admission, generation

    def _fail_completion_delivery(
        self, completion: object, error: BaseException
    ) -> None:
        correlation_id = str(getattr(completion, "correlation_id", ""))
        if correlation_id:
            pending = self._calls.pop(correlation_id)
            if pending is not None and pending.reply_correlation_id is not None:
                correlation_id = pending.reply_correlation_id
            self._reject_correlation(
                correlation_id,
                "HARNESS_COMPLETION_HANDOFF_FAILED",
                str(error),
            )

    def _terminate_pending(self, code: str, detail: str) -> None:
        for correlation_id, pending in self._calls.clear():
            self._reject_correlation(
                pending.reply_correlation_id or correlation_id, code, detail
            )
        for batch in tuple(self._restore_batches.values()):
            if not batch.terminal_rejected:
                self._reject_correlation(batch.correlation_id, code, detail)
        self._restore_batches.clear()
        for record in self._records.values():
            if record.explicit_correlation_id is not None:
                self._reject_correlation(
                    record.explicit_correlation_id, code, detail
                )
                record.explicit_correlation_id = None
            record.restore_batch = None

    def _publish(self) -> None:
        self._projection.publish(
            self._records,
            self._version,
            failure_budget=self._failure_budget,
        )

    def _remember_settled_completion(self, correlation_id: str) -> None:
        if not correlation_id or correlation_id in self._settled_completion_ids:
            return
        if len(self._settled_completion_order) >= 1024:
            expired = self._settled_completion_order.popleft()
            self._settled_completion_ids.discard(expired)
        self._settled_completion_order.append(correlation_id)
        self._settled_completion_ids.add(correlation_id)

    def _durable_lifecycle_receipt(self, attempt_token: str) -> object | None:
        if self._desired_state is None:
            return None
        return next(
            (
                receipt
                for receipt in self._desired_state.load().lifecycle_receipts
                if receipt.domain == "harness"
                and receipt.attempt_token == attempt_token
            ),
            None,
        )

    def _settled_lifecycle_failure(self, attempt_token: str) -> object | None:
        return self._lifecycle_failures.get(attempt_token)

    def _remember_settled_lifecycle(self, attempt_token: str) -> None:
        """Keep only the short duplicate window; durable state owns recovery."""

        for correlation_id, owner in tuple(
            self._lifecycle_native_admitted.items()
        ):
            if owner == attempt_token:
                self._lifecycle_native_admitted.pop(correlation_id, None)
        if not attempt_token or attempt_token in self._settled_lifecycle_attempts:
            return
        if len(self._settled_lifecycle_order) >= _LIFECYCLE_SETTLED_CAPACITY:
            expired = self._settled_lifecycle_order.popleft()
            self._settled_lifecycle_attempts.discard(expired)
            self._lifecycle_failures.pop(expired, None)
        self._settled_lifecycle_order.append(attempt_token)
        self._settled_lifecycle_attempts.add(attempt_token)

    def _claim_lifecycle_replay(self, attempt_token: str) -> str:
        if attempt_token in self._lifecycle_replay_claims:
            return "wait"
        if len(self._lifecycle_replay_claims) >= self._lifecycle_replay_capacity:
            return "full"
        self._lifecycle_replay_claims.add(attempt_token)
        return "new"

    def release_lifecycle_replay(self, attempt_token: str) -> None:
        self._lifecycle_replay_claims.discard(attempt_token)

    def _reject(self, command: object, code: str, detail: str) -> None:
        self._reject_correlation(
            str(getattr(command, "correlation_id", "")), code, detail
        )

    def _reject_correlation(self, correlation_id: str, code: str, detail: str) -> None:
        self._emit_event(
            PortCommandRejected(
                correlation_id=correlation_id,
                domain="harness",
                generation=self._handler_generation,
                version=self._version,
                code=code,
                detail=detail,
            )
        )

    def _finish_failed_removal(
        self,
        request: object,
        provenance: object,
        code: str,
        detail: str,
        owns_resource: bool,
    ) -> object:
        from .lifecycle_receipts import LifecycleMutationFailed, LifecycleMutationRequest, MutationProvenance

        assert isinstance(request, LifecycleMutationRequest)
        assert isinstance(provenance, MutationProvenance)
        assert isinstance(request.payload, RemoveHarnessCommand)
        key = f"{request.payload.harness}:{request.payload.name}"
        active = self._lifecycle_effect_requests.get(key)
        owns_custody = active is None or active[0].attempt_token == request.attempt_token
        # Durable intent first, then memory, with no actor handoff in between.
        # Neither decision depends on a pid or on whether stop returned.
        if owns_resource and owns_custody:
            self._records.pop(key, None)
        if owns_custody:
            self._lifecycle_effect_resources.discard(key)
            self._lifecycle_effect_requests.pop(key, None)
        for correlation, (pending, _) in tuple(self._lifecycle_pending.items()):
            if pending.attempt_token == request.attempt_token:
                self._pop_lifecycle_pending(correlation)
                self._calls.pop(correlation)
        for correlation, timer in tuple(self._lifecycle_retry_timers.items()):
            if correlation.startswith(f"{request.correlation_id}:io:"):
                timer.cancel()
                self._lifecycle_retry_timers.pop(correlation)
        self._remember_settled_lifecycle(request.attempt_token)
        failure = LifecycleMutationFailed(
            request.correlation_id, request.attempt_token,
            self._handler_generation, self._version, "harness", code, detail, False,
        )
        self._lifecycle_failures[request.attempt_token] = failure
        self._publish()
        return failure

    def _begin_failed_removal(
        self,
        request: object,
        provenance: object,
        code: str,
        detail: str,
        *,
        control_correlation: str | None = None,
    ) -> None:
        from .lifecycle_receipts import LifecycleMutationRequest, MutationProvenance

        assert isinstance(request, LifecycleMutationRequest)
        assert isinstance(provenance, MutationProvenance)
        self._persist_desired(
            DesiredStateOperation.FAIL_HARNESS_REMOVAL,
            request,
            (request.attempt_token, provenance.resource_token),
            context=(
                "failed_removal",
                request,
                provenance,
                code,
                detail,
                control_correlation,
            ),
        )

    def _finish_failed_ensure(
        self,
        request: object,
        provenance: object,
        code: str,
        detail: str,
        rolled_back: bool,
    ) -> object:
        from .lifecycle_receipts import (
            LifecycleMutationFailed,
            LifecycleMutationRequest,
            MutationProvenance,
        )

        assert isinstance(request, LifecycleMutationRequest)
        assert isinstance(provenance, MutationProvenance)
        assert isinstance(request.payload, EnsureHarnessCommand)
        key = f"{request.payload.spec.harness}:{request.payload.spec.name}"
        active = self._lifecycle_effect_requests.get(key)
        owns_custody = (
            active is None or active[0].attempt_token == request.attempt_token
        )
        if owns_custody:
            self._records.pop(key, None)
            self._lifecycle_effect_resources.discard(key)
            self._lifecycle_effect_requests.pop(key, None)
        for correlation, (pending, _) in tuple(self._lifecycle_pending.items()):
            if pending.attempt_token == request.attempt_token:
                self._pop_lifecycle_pending(correlation)
                self._calls.pop(correlation)
        for correlation, timer in tuple(self._lifecycle_retry_timers.items()):
            if correlation.startswith(f"{request.correlation_id}:io:"):
                timer.cancel()
                self._lifecycle_retry_timers.pop(correlation)
        self._remember_settled_lifecycle(request.attempt_token)
        failure = LifecycleMutationFailed(
            request.correlation_id,
            request.attempt_token,
            self._handler_generation,
            self._version,
            "harness",
            code,
            detail,
            rolled_back,
        )
        self._lifecycle_failures[request.attempt_token] = failure
        self._publish()
        return failure

    def _schedule_ensure_failure_stop(
        self,
        attempt_token: str,
        harness_id: str,
        generation: int,
        process: ManagedHarnessProcess,
        identity: ProcessIdentity | None,
    ) -> None:
        correlation = f"harness:fail-stop:{uuid.uuid4().hex}"
        self._lifecycle_ensure_failure_stops[correlation] = (
            attempt_token,
            harness_id,
            generation,
            process,
            identity,
        )
        assert self._io is not None
        self._io.stop(
            correlation,
            harness_id,
            generation,
            self._version,
            process,
            identity,
        )

    def _drive_ensure_failure_fence(self, attempt_token: str) -> None:
        fence = self._lifecycle_ensure_failure_fences.get(attempt_token)
        if fence is None:
            return
        from .lifecycle_receipts import LifecycleMutationRequest

        request = fence.request
        assert isinstance(request, LifecycleMutationRequest)
        for correlation, timer in tuple(self._lifecycle_retry_timers.items()):
            if correlation.startswith(f"{request.correlation_id}:io:"):
                timer.cancel()
                self._lifecycle_retry_timers.pop(correlation)
        for _correlation, (attempt, harness_id, generation) in tuple(
            self._lifecycle_ensure_io.items()
        ):
            if attempt == attempt_token:
                self._cancel_start_timer(harness_id, generation)
        if not any(
            pending[0] == attempt_token
            for pending in self._lifecycle_ensure_failure_stops.values()
        ):
            unstopped = tuple(fence.unstopped)
            fence.unstopped.clear()
            for harness_id, generation, process, identity in unstopped:
                self._schedule_ensure_failure_stop(
                    attempt_token,
                    harness_id,
                    generation,
                    process,
                    identity,
                )
        self._maybe_finish_ensure_failure_fence(attempt_token)

    def _maybe_finish_ensure_failure_fence(self, attempt_token: str) -> None:
        fence = self._lifecycle_ensure_failure_fences.get(attempt_token)
        if fence is None or fence.unstopped:
            return
        if any(
            tracked[0] == attempt_token
            for tracked in self._lifecycle_ensure_io.values()
        ):
            return
        if any(
            pending[0] == attempt_token
            for pending in self._lifecycle_ensure_failure_stops.values()
        ):
            return
        if attempt_token in self._lifecycle_ensure_settling:
            return
        self._lifecycle_ensure_settling.add(attempt_token)
        accepted = self._persist_desired(
            DesiredStateOperation.ROLLBACK_HARNESS_LIFECYCLE,
            fence.request,
            (attempt_token, fence.provenance.resource_token),
            context=("failed_ensure", attempt_token),
        )
        if not accepted:
            self._lifecycle_ensure_settling.discard(attempt_token)

    def _fence_failed_ensure(
        self,
        command: object,
        request: object,
        provenance: object,
        code: str,
        detail: str,
    ) -> None:
        from .lifecycle_receipts import (
            FailHarnessLifecycleCommand,
            LifecycleMutationRequest,
        )

        assert isinstance(request, LifecycleMutationRequest)
        fence = self._lifecycle_ensure_failure_fences.get(request.attempt_token)
        if fence is None:
            fence = _EnsureFailureFence(request, provenance, code, detail)
            self._lifecycle_ensure_failure_fences[request.attempt_token] = fence
        if isinstance(command, FailHarnessLifecycleCommand):
            fence.control_correlations.append(command.correlation_id)
        self._drive_ensure_failure_fence(request.attempt_token)

    def _on_fail_lifecycle(self, command: object) -> None:
        from .lifecycle_receipts import (
            FailHarnessLifecycleCommand, HarnessLifecycleFailureSettled,
            LifecycleMutationCompleted,
        )

        assert isinstance(command, FailHarnessLifecycleCommand)
        request = command.request
        if not isinstance(
            request.payload, (EnsureHarnessCommand, RemoveHarnessCommand)
        ):
            self._reject(
                command,
                ipc_errors.INVALID_ARGUMENT,
                "only a harness ensure or removal may be failed by deadline",
            )
            return
        assert self._desired_state is not None
        receipt = self._durable_lifecycle_receipt(request.attempt_token)
        if receipt is not None and receipt.completed:
            # The process completion won the mailbox race. Do not turn success
            # into deletion of a newer incarnation or lie to the journal.
            result = LifecycleMutationCompleted(
                request.correlation_id, request.attempt_token,
                self._handler_generation, self._version, "harness", receipt.provenance,
                HarnessMutationCompleted(
                    request.correlation_id, self._handler_generation, self._version,
                    (
                        f"{request.payload.spec.harness}:{request.payload.spec.name}"
                        if isinstance(request.payload, EnsureHarnessCommand)
                        else f"{request.payload.harness}:{request.payload.name}"
                    ),
                    receipt.provenance.changed,
                ),
            )
        elif request.attempt_token in self._lifecycle_failures:
            result = self._lifecycle_failures[request.attempt_token]
        elif request.attempt_token in self._settled_lifecycle_attempts:
            self._reject(command, "LIFECYCLE_ATTEMPT_SETTLED", "the successful receipt was already retired")
            return
        else:
            if isinstance(request.payload, EnsureHarnessCommand):
                key = f"{request.payload.spec.harness}:{request.payload.spec.name}"
                active = self._lifecycle_effect_requests.get(key)
                if (
                    active is not None
                    and active[0].attempt_token == request.attempt_token
                ):
                    provenance = active[1]
                else:
                    # The control can overtake a queued (not yet admitted)
                    # mutation. Apply the genuine request before settling it.
                    self._persist_desired(
                        DesiredStateOperation.APPLY_HARNESS_LIFECYCLE,
                        command,
                        (request,),
                        kwargs=(
                            ("generation", self._handler_generation),
                            ("version", self._version),
                        ),
                        context=("fail_apply", command),
                    )
                    return
                self._fence_failed_ensure(
                    command,
                    request,
                    provenance,
                    command.code,
                    command.detail,
                )
                return
            # The control can overtake a queued (not yet admitted) mutation.
            # Apply its genuine request through the same domain transaction;
            # never guess absence from a journal's 'dispatched' label.
            self._persist_desired(
                DesiredStateOperation.APPLY_HARNESS_LIFECYCLE,
                command,
                (request,),
                kwargs=(
                    ("generation", self._handler_generation),
                    ("version", self._version),
                ),
                context=("fail_apply", command),
            )
            return
        self._emit_event(result)
        self._emit_event(HarnessLifecycleFailureSettled(command.correlation_id, result))

    def _persist_desired(
        self,
        operation: DesiredStateOperation,
        command: object,
        args: tuple[object, ...],
        *,
        kwargs: tuple[tuple[str, object], ...] = (),
        context: object,
    ) -> bool:
        persistence = self._persistence
        if persistence is None:
            self._reject_correlation(
                str(getattr(command, "correlation_id", "")),
                "HARNESS_PERSISTENCE_UNAVAILABLE",
                "Harness desired-state persistence is unavailable",
            )
            return False
        request = DesiredStateIoRequest(
            operation_id=f"harness-state-{uuid.uuid4().hex}",
            owner_generation=self._handler_generation,
            owner_version=self._version,
            operation=operation,
            args=args,
            kwargs=kwargs,
            context=context,
        )
        admission = persistence.submit(request)
        if admission is AdmissionResult.ACCEPTED:
            return True
        self._reject_correlation(
            str(getattr(command, "correlation_id", "")),
            "HARNESS_PERSISTENCE_OVERLOADED",
            f"Harness persistence admission is {admission.value}",
        )
        return False

    def _on_desired_completed(self, completion: DesiredStateIoCompleted) -> None:
        from .lifecycle_receipts import (
            LifecycleMutationCompleted,
            LifecycleMutationRequest,
            LifecycleReceiptCompleted,
            MutationProvenance,
        )

        request = completion.request
        context = request.context
        if not isinstance(context, tuple) or len(context) < 2:
            raise TypeError("Harness persistence completion has no typed context")
        kind = context[0]
        command = context[1]
        if (
            request.owner_generation > self._handler_generation
            or request.owner_version > self._version
        ):
            self._reject_correlation(
                str(getattr(command, "correlation_id", "")),
                "HARNESS_PERSISTENCE_FENCE_INVALID",
                "Harness persistence completion is from a future owner fence",
            )
            return
        if completion.error_code is not None:
            if kind == "failed_ensure":
                self._lifecycle_ensure_settling.discard(context[1])
            if kind == "restore_mark_failed":
                self._fail_restore_persistence(
                    context[1],
                    context[2],
                    "HARNESS_PERSISTENCE_FAILED",
                    f"{completion.error_code}: {completion.error_detail}",
                )
                return
            if kind == "mark_failed":
                return
            if kind == "session_refs":
                self._publish_event(
                    HarnessSessionRefsReconciled(
                        command.correlation_id,
                        self._handler_generation,
                        self._version,
                        context[2],
                        f"{completion.error_code}: {completion.error_detail}",
                    )
                )
                return
            if kind == "manage":
                self._publish_event(
                    HarnessDesiredStateManaged(
                        command.correlation_id,
                        self._handler_generation,
                        self._version,
                        context[2],
                        False,
                        context[4],
                        error_code=completion.error_code,
                        error_detail=completion.error_detail,
                        error_is_oserror=completion.error_is_oserror,
                        error_errno=completion.error_errno,
                        error_strerror=completion.error_strerror,
                        error_filename=completion.error_filename,
                        error_filename2=completion.error_filename2,
                    )
                )
                return
            self._reject_correlation(
                str(getattr(command, "correlation_id", "")),
                "HARNESS_PERSISTENCE_FAILED",
                f"{completion.error_code}: {completion.error_detail}",
            )
            return
        if kind == "receipt":
            self._publish_event(
                LifecycleReceiptCompleted(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    "harness",
                    command.attempt_token,
                    command.resource_token,
                    context[2],
                    bool(completion.result),
                )
            )
            return
        if kind == "mark_failed":
            return
        if kind == "restore_mark_failed":
            batch_id, key, record_incarnation, record_generation = context[1:5]
            batch = self._restore_batches.get(batch_id)
            if batch is None or key not in batch.pending:
                return
            harness, name = key.split(":", 1)
            committed = (
                completion.result is True
                and any(
                    spec.harness == harness
                    and spec.name == name
                    and spec.status == "failed"
                    for spec in completion.snapshot.harnesses
                )
            )
            if not committed:
                self._fail_restore_persistence(
                    batch_id,
                    key,
                    "HARNESS_PERSISTENCE_FAILED",
                    f"Failed restore status was not committed for {key}",
                )
                return
            record = self._records.get(key)
            if (
                record is not None
                and record.incarnation == record_incarnation
                and record.generation == record_generation
                and self._is_failed(record)
            ):
                self._failed_events.append(key)
                self._report_readiness(key, "failed")
            self._settle_restore_key(batch_id, key, success=False)
            return
        if kind == "lifecycle_rejection_count":
            self._continue_lifecycle_rejection(
                context[1], context[2], context[3], completion.result
            )
            return
        if kind == "failed_removal":
            failure = self._finish_failed_removal(
                context[1],
                context[2],
                context[3],
                context[4],
                bool(completion.result),
            )
            self._publish_event(failure)
            control_correlation = context[5]
            if isinstance(control_correlation, str):
                from .lifecycle_receipts import HarnessLifecycleFailureSettled

                self._publish_event(
                    HarnessLifecycleFailureSettled(control_correlation, failure)
                )
            return
        if kind == "failed_ensure":
            attempt_token = context[1]
            self._lifecycle_ensure_settling.discard(attempt_token)
            fence = self._lifecycle_ensure_failure_fences.pop(
                attempt_token, None
            )
            if fence is None:
                return
            failure = self._finish_failed_ensure(
                fence.request,
                fence.provenance,
                fence.code,
                fence.detail,
                bool(completion.result),
            )
            self._publish_event(failure)
            from .lifecycle_receipts import HarnessLifecycleFailureSettled

            for correlation in fence.control_correlations:
                self._publish_event(
                    HarnessLifecycleFailureSettled(correlation, failure)
                )
            return
        if kind == "terminalize":
            from .lifecycle_receipts import (
                LifecycleMutationFailed,
                TerminalizeHarnessLifecycleCommand,
            )

            terminalize, receipt, lifecycle_request, remaining, requests = context[1:6]
            assert isinstance(terminalize, TerminalizeHarnessLifecycleCommand)
            resource_id = receipt.resource_key.removeprefix("harness:")
            correlation_id = receipt.correlation_id
            if correlation_id is None and lifecycle_request is not None:
                raw = getattr(lifecycle_request, "correlation_id", None)
                correlation_id = raw if isinstance(raw, str) else None
            if lifecycle_request is not None and isinstance(
                lifecycle_request.payload, RemoveHarnessCommand
            ):
                self._publish_event(
                    self._finish_failed_removal(
                        lifecycle_request,
                        receipt.provenance,
                        terminalize.code,
                        terminalize.detail,
                        bool(completion.result),
                    )
                )
            else:
                self._remember_settled_lifecycle(receipt.attempt_token)
                if correlation_id is not None:
                    self._publish_event(
                        LifecycleMutationFailed(
                            correlation_id,
                            receipt.attempt_token,
                            self._handler_generation,
                            self._version,
                            "harness",
                            terminalize.code,
                            terminalize.detail,
                            bool(completion.result),
                        )
                    )
                self._lifecycle_effect_resources.discard(resource_id)
                self._lifecycle_effect_requests.pop(resource_id, None)
            self._terminalize_next(terminalize, remaining, requests)
            return
        if kind == "session_refs":
            self._publish_event(
                HarnessSessionRefsReconciled(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    context[2],
                    None,
                )
            )
            return
        if kind == "manage":
            self._publish_event(
                HarnessDesiredStateManaged(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    context[2],
                    bool(context[3]),
                    context[4],
                    None,
                )
            )
            return
        if kind == "lifecycle_apply":
            self._continue_lifecycle_apply(command, completion.result)
            return
        if kind == "fail_apply":
            from .lifecycle_receipts import FailHarnessLifecycleCommand, MutationProvenance

            assert isinstance(command, FailHarnessLifecycleCommand)
            if (
                not isinstance(completion.result, tuple)
                or len(completion.result) != 2
                or not isinstance(completion.result[0], MutationProvenance)
            ):
                raise TypeError("Harness failure persistence returned invalid result")
            provenance = completion.result[0]
            lifecycle_request = command.request
            if isinstance(lifecycle_request.payload, EnsureHarnessCommand):
                self._fence_failed_ensure(
                    command,
                    lifecycle_request,
                    provenance,
                    command.code,
                    command.detail,
                )
            else:
                self._begin_failed_removal(
                    lifecycle_request,
                    provenance,
                    command.code,
                    command.detail,
                    control_correlation=command.correlation_id,
                )
            return
        if kind == "lifecycle_noop":
            lifecycle_request, provenance, base = context[1:4]
            assert isinstance(lifecycle_request, LifecycleMutationRequest)
            assert isinstance(provenance, MutationProvenance)
            if completion.result is not True:
                self._reject_correlation(
                    lifecycle_request.correlation_id,
                    "HARNESS_LIFECYCLE_RECEIPT_MISMATCH",
                    "Harness lifecycle receipt could not settle",
                )
                return
            self._remember_settled_lifecycle(lifecycle_request.attempt_token)
            self._publish_event(
                LifecycleMutationCompleted(
                    lifecycle_request.correlation_id,
                    lifecycle_request.attempt_token,
                    self._handler_generation,
                    self._version,
                    "harness",
                    provenance,
                    base,
                )
            )
            return
        if kind == "lifecycle_finalize":
            lifecycle_request, provenance, event = context[1:4]
            assert isinstance(lifecycle_request, LifecycleMutationRequest)
            assert isinstance(provenance, MutationProvenance)
            assert isinstance(event, HarnessMutationCompleted)
            if completion.result is not True:
                self._reject_correlation(
                    lifecycle_request.correlation_id,
                    "HARNESS_LIFECYCLE_RECEIPT_MISMATCH",
                    "Harness lifecycle receipt could not settle",
                )
                return
            self._pop_lifecycle_pending(event.correlation_id)
            self._remember_settled_lifecycle(lifecycle_request.attempt_token)
            retry = self._lifecycle_retry_timers.pop(event.correlation_id, None)
            if retry is not None:
                retry.cancel()
            self._lifecycle_effect_resources.discard(event.harness_id)
            self._lifecycle_effect_requests.pop(event.harness_id, None)
            base = HarnessMutationCompleted(
                event.correlation_id,
                event.generation,
                event.version,
                event.harness_id,
                provenance.changed,
            )
            self._publish_event(
                LifecycleMutationCompleted(
                    lifecycle_request.correlation_id,
                    lifecycle_request.attempt_token,
                    event.generation,
                    event.version,
                    "harness",
                    provenance,
                    base,
                )
            )
            return
        raise TypeError(f"unsupported Harness persistence context: {kind}")

    def _continue_lifecycle_rejection(
        self,
        request: object,
        provenance: object,
        event: object,
        result: object,
    ) -> None:
        from .lifecycle_receipts import LifecycleMutationRequest, MutationProvenance

        assert isinstance(request, LifecycleMutationRequest)
        assert isinstance(provenance, MutationProvenance)
        assert isinstance(event, PortCommandRejected)
        if not isinstance(result, int):
            raise TypeError("Harness lifecycle failure count must be an integer")
        correlation_id = event.correlation_id
        retry_budget = self._failure_budget or 3
        if result < retry_budget and not self._closing:
            self._pop_lifecycle_pending(correlation_id)
            delay = min(
                capped_exponential(
                    self._lifecycle_retry_base_seconds,
                    1.0,
                    max(0, result - 1),
                ),
                1.0,
            )
            prior = self._lifecycle_retry_timers.pop(correlation_id, None)
            if prior is not None:
                prior.cancel()

            def retry() -> None:
                self._lifecycle_retry_timers.pop(correlation_id, None)
                self._emit_completion(request)

            timer = threading.Timer(delay, retry)
            timer.daemon = True
            self._lifecycle_retry_timers[correlation_id] = timer
            timer.start()
            return
        if isinstance(request.payload, RemoveHarnessCommand):
            self._pop_lifecycle_pending(correlation_id)
            self._remember_settled_lifecycle(request.attempt_token)
            self._begin_failed_removal(
                request, provenance, event.code, event.detail
            )
            return
        self._fence_failed_ensure(
            None,
            request,
            provenance,
            event.code,
            event.detail,
        )

    def _emit_event(self, event: object) -> None:
        from .lifecycle_receipts import (
            LifecycleMutationRequest,
            MutationProvenance,
        )

        correlation_id = str(getattr(event, "correlation_id", ""))
        pending = self._lifecycle_pending.get(correlation_id)
        if pending is not None and isinstance(event, PortCommandRejected):
            request, provenance = pending
            assert isinstance(request, LifecycleMutationRequest)
            assert isinstance(provenance, MutationProvenance)
            self._persist_desired(
                DesiredStateOperation.RECORD_HARNESS_LIFECYCLE_FAILURE,
                request,
                (request.attempt_token, provenance.resource_token),
                context=(
                    "lifecycle_rejection_count",
                    request,
                    provenance,
                    event,
                ),
            )
            return
        elif pending is not None and isinstance(event, HarnessMutationCompleted):
            request, provenance = pending
            assert isinstance(request, LifecycleMutationRequest)
            assert isinstance(provenance, MutationProvenance)
            if isinstance(request.payload, EnsureHarnessCommand):
                # U0b: the process is up (this event IS the start-success
                # settlement), so THIS is where the desired-state row is
                # earned.  The spec that actually started is the actor's
                # record; the payload spec is the fallback when the record
                # was superseded mid-flight.
                record = self._records.get(event.harness_id)
                confirmed_spec = (
                    record.spec
                    if record is not None
                    else _launch_spec(request.payload.spec)
                )
                operation = DesiredStateOperation.CONFIRM_HARNESS_LIFECYCLE
                args = (
                    request.attempt_token,
                    provenance.resource_token,
                    confirmed_spec,
                )
            else:
                operation = DesiredStateOperation.COMPLETE_HARNESS_LIFECYCLE
                args = (request.attempt_token, provenance.resource_token)
            self._persist_desired(
                operation,
                request,
                args,
                kwargs=(
                    ("generation", event.generation),
                    ("version", event.version),
                ),
                context=(
                    "lifecycle_finalize",
                    request,
                    provenance,
                    event,
                ),
            )
            return
        self._publish_event(event)

    def _publish_event(self, event: object) -> None:
        for sink in tuple(self._event_sinks):
            publish = getattr(sink, "publish", None)
            if callable(publish):
                publish(event)
            elif callable(sink):
                sink(event)
            else:
                raise TypeError("event sink must be callable or expose publish(event)")

    @staticmethod
    def _key(spec: HarnessLaunchSpec) -> str:
        return f"{spec.harness}:{spec.name}"


class _AwaitSettlement:
    """``_request`` timeout marker: wait for the event itself, no deadline.

    Restore is the only user.  Its wait used to carry a fleet-scaled
    wall-clock budget (rounds x start timeout, capped by a ceiling held under
    the CLI's 90s) because restore ran on the startup path, ahead of
    ``daemon.json``.  Both halves of that coupling are gone: restore runs on
    its own thread behind the serving boundary, and each target's disposition
    is a readiness report, so the batch's only legitimate bound is the
    per-start settlement timer every admitted start carries -- a reconcile
    strategy parameter, not a startup clock.  F1 completed that argument: a
    target a lifecycle effect owns settles immediately as deferred, so every
    key has a disposition owner and the structural bound is total.  The
    facade derives its wall-clock backstop from exactly that structure (see
    ``restore_settlement_budget``); this marker remains only for the
    timer-disabled opt-out, where no deadline exists to derive.  A runtime
    stop still ends the wait: ``_fail_pending`` raises
    ``HarnessRuntimeClosed`` here.
    """


_AWAIT_SETTLEMENT = _AwaitSettlement()


class HarnessRuntimeFacade:
    """Compatibility facade preserving ``ManagedHarnessRuntime`` behavior."""

    def __init__(
        self,
        actor: HarnessRuntimeActor,
        *,
        reply_timeout: float = 65.0,
    ) -> None:
        self._actor = actor
        self._reply_timeout = reply_timeout
        self._pending_lock = threading.Lock()
        self._pending: dict[str, tuple[type[object], _Reply[object]]] = {}
        self._timer_sequence = 0
        self._last_drain_complete = True
        self._actor.subscribe_events(self._on_event)

    @property
    def projection(self) -> HarnessProjection:
        return self._actor.projection

    def restore(self, desired: tuple[HarnessLaunchSpec, ...]) -> HarnessRestoreSummary:
        """Restore the declared fleet, returning when the batch has settled.

        No partial summary on a timeout: the batch completes when every
        admitted start has settled, each settlement bounded by the per-start
        timer the actor arms at admission.  Progress observation is the
        readiness reports (phase ③), not this summary, so a caller giving
        up early had nothing left to report that the reports do not already
        carry.

        The wait itself carries a derived wall-clock backstop (F1): the
        settlement bound the batch is structurally owed -- admission rounds
        times the per-start timeout, plus a round of margin -- so a lost
        settlement raises ``TimeoutError`` into the daemon's degraded-start
        path (gate opens with ``harness.recovery.failed``) instead of
        hanging the gate closed forever.  With start timers disabled the
        wait stays unlimited (``_AWAIT_SETTLEMENT``): the operator opted
        out of settlement deadlines.  A runtime stop still ends the wait:
        ``_fail_pending`` raises ``HarnessRuntimeClosed`` here.
        """

        targets = tuple(desired)
        budget = self._actor.restore_settlement_budget(len(targets))
        event = self._request(
            RestoreHarnessesCommand(
                uuid.uuid4().hex, tuple(_launch_projection(spec) for spec in targets)
            ),
            HarnessRestoreCompleted,
            timeout=_AWAIT_SETTLEMENT if budget is None else budget,
        )
        return cast(HarnessRestoreCompleted, event).result

    def reconcile(self) -> int:
        with self._pending_lock:
            self._timer_sequence += 1
            timer_sequence = self._timer_sequence
        event = self._request(
            HarnessTimerElapsedCommand(
                uuid.uuid4().hex,
                self._actor.generation,
                timer_sequence,
                int(time.time() * 1000),
            ),
            HarnessTimerCompleted,
        )
        return cast(HarnessTimerCompleted, event).restarted

    def start(self, spec: HarnessLaunchSpec) -> bool:
        event = self._request(
            EnsureHarnessCommand(uuid.uuid4().hex, _launch_projection(spec)),
            HarnessMutationCompleted,
        )
        return cast(HarnessMutationCompleted, event).changed

    def remove(self, harness: str, name: str) -> bool:
        event = self._request(
            RemoveHarnessCommand(uuid.uuid4().hex, harness, name),
            HarnessMutationCompleted,
        )
        return cast(HarnessMutationCompleted, event).changed

    def drain_failed_events(self) -> tuple[str, ...]:
        event = self._request(
            DrainHarnessFailedCommand(uuid.uuid4().hex),
            HarnessFailedDrained,
        )
        return cast(HarnessFailedDrained, event).harness_ids

    def drain_readiness_reports(self) -> tuple[ReadinessReport, ...]:
        event = self._request(
            DrainHarnessReadinessCommand(uuid.uuid4().hex),
            HarnessReadinessDrained,
        )
        return cast(HarnessReadinessDrained, event).reports

    def status(self) -> tuple[dict[str, object], ...]:
        return self.projection.status()

    def streaming_actors(self) -> tuple[str, ...]:
        return self.projection.streaming_actors()

    def session_refs(self) -> dict[tuple[str, str], str]:
        self._request(
            RefreshHarnessProjectionsCommand(uuid.uuid4().hex),
            HarnessProjectionsRefreshed,
        )
        return self.projection.session_refs()

    def reconcile_session_refs(self) -> dict[tuple[str, str], str]:
        event = cast(
            HarnessSessionRefsReconciled,
            self._request(
                ReconcileHarnessSessionRefsCommand(uuid.uuid4().hex),
                HarnessSessionRefsReconciled,
            ),
        )
        if event.error is not None:
            raise DesiredStateError(event.error)
        return {
            (item.harness, item.name): item.session_ref for item in event.refs
        }

    def stage_harness_desired(self, spec: HarnessLaunchSpec) -> bool:
        event = cast(
            HarnessDesiredStateManaged,
            self._request(
                StageHarnessDesiredCommand(uuid.uuid4().hex, _launch_projection(spec)),
                HarnessDesiredStateManaged,
            ),
        )
        self._raise_managed_error(event)
        return event.changed

    @staticmethod
    def _raise_managed_error(event: HarnessDesiredStateManaged) -> None:
        """Rebuild public errors only after immutable I/O completion settles."""

        if event.error is not None:
            raise event.error
        if event.error_code is None:
            if event.error_detail is not None:
                raise RuntimeError(event.error_detail)
            return
        if event.error_is_oserror:
            subclasses: dict[str, type[OSError]] = {
                "OSError": OSError,
                "BlockingIOError": BlockingIOError,
                "BrokenPipeError": BrokenPipeError,
                "ChildProcessError": ChildProcessError,
                "ConnectionError": ConnectionError,
                "ConnectionAbortedError": ConnectionAbortedError,
                "ConnectionRefusedError": ConnectionRefusedError,
                "ConnectionResetError": ConnectionResetError,
                "FileExistsError": FileExistsError,
                "FileNotFoundError": FileNotFoundError,
                "InterruptedError": InterruptedError,
                "IsADirectoryError": IsADirectoryError,
                "NotADirectoryError": NotADirectoryError,
                "PermissionError": PermissionError,
                "ProcessLookupError": ProcessLookupError,
                "TimeoutError": TimeoutError,
            }
            kind = subclasses.get(event.error_code or "")
            if kind is None:
                # Calling OSError(errno, ...) can auto-select a *different*
                # builtin subclass. Unknown custom names intentionally fall
                # back to plain OSError with the same frozen public facts.
                fallback = OSError(event.error_detail or "")
                fallback.errno = event.error_errno
                fallback.strerror = event.error_strerror
                fallback.filename = event.error_filename
                fallback.filename2 = event.error_filename2
                raise fallback
            if (
                event.error_errno is not None
                or event.error_strerror is not None
                or event.error_filename is not None
                or event.error_filename2 is not None
            ):
                args: list[object] = [
                    event.error_errno,
                    event.error_strerror or event.error_detail or "",
                ]
                if event.error_filename is not None or event.error_filename2 is not None:
                    args.append(event.error_filename)
                if event.error_filename2 is not None:
                    args.extend((None, event.error_filename2))
                raise kind(*args)
            raise kind(event.error_detail or "")
        if event.operation == "SnapshotAdapterRegistrationCommand":
            if event.error_code == "DesiredStateError":
                raise DesiredStateError(event.error_detail or "")
            if event.error_code == "RuntimeError":
                raise RuntimeError(event.error_detail or "")
        raise RuntimeError(f"{event.error_code}: {event.error_detail}")

    def snapshot_adapter_registration(
        self, name: str
    ) -> HarnessAdapterRegistrationProjection:
        event = cast(
            HarnessDesiredStateManaged,
            self._request(
                SnapshotAdapterRegistrationCommand(uuid.uuid4().hex, name),
                HarnessDesiredStateManaged,
            ),
        )
        self._raise_managed_error(event)
        assert event.adapter is not None
        return event.adapter

    def remove_adapter_registration(
        self, snapshot: HarnessAdapterRegistrationProjection
    ) -> bool:
        event = cast(
            HarnessDesiredStateManaged,
            self._request(
                RemoveAdapterRegistrationCommand(
                    uuid.uuid4().hex,
                    snapshot.name,
                    snapshot.spec,
                    snapshot.legacy_pin,
                ),
                HarnessDesiredStateManaged,
            ),
        )
        self._raise_managed_error(event)
        return event.changed

    def restore_adapter_registration(
        self, snapshot: HarnessAdapterRegistrationProjection
    ) -> bool:
        event = cast(
            HarnessDesiredStateManaged,
            self._request(
                RestoreAdapterRegistrationCommand(
                    uuid.uuid4().hex,
                    snapshot.name,
                    snapshot.spec,
                    snapshot.legacy_pin,
                ),
                HarnessDesiredStateManaged,
            ),
        )
        self._raise_managed_error(event)
        return event.changed

    def wait_ready(self, harness: str, name: str, timeout: float) -> bool:
        event = self._request(
            WaitHarnessReadyCommand(uuid.uuid4().hex, harness, name, timeout),
            HarnessReadyObserved,
            timeout=max(timeout + 1.0, self._reply_timeout),
        )
        return cast(HarnessReadyObserved, event).ready

    def dispatch(self, name: str, delivery: HarnessDelivery) -> bool:
        event = self._request(
            DispatchHarnessDeliveryCommand(uuid.uuid4().hex, name, delivery),
            HarnessDeliveryAdmitted,
        )
        return cast(HarnessDeliveryAdmitted, event).accepted

    def drain_results(self) -> tuple[HarnessResult, ...]:
        event = self._request(
            DrainHarnessResultsCommand(uuid.uuid4().hex), HarnessResultObserved
        )
        return cast(HarnessResultObserved, event).results

    def drain_progress(self) -> tuple[object, ...]:
        event = self._request(
            DrainHarnessProgressCommand(uuid.uuid4().hex), HarnessProgressObserved
        )
        return cast(HarnessProgressObserved, event).progress

    def bind_liveness(self, harness: str, name: str, binding: object) -> bool:
        event = self._request(
            BindHarnessLivenessCommand(
                uuid.uuid4().hex, harness, name, binding
            ),
            HarnessLivenessBound,
        )
        return cast(HarnessLivenessBound, event).changed

    def stop(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        command_drained = False
        try:
            event = self._request(
                StopHarnessesCommand(
                    uuid.uuid4().hex,
                    int(deadline * 1000),
                ),
                HarnessesStopped,
                timeout=timeout,
            )
            command_drained = cast(HarnessesStopped, event).drain_complete
        except TimeoutError:
            self._last_drain_complete = False
            return
        if not command_drained:
            self._last_drain_complete = False
            return
        self._fail_pending(HarnessRuntimeClosed("harness runtime stopped"))
        runtime_drained = self._actor.close_runtime(
            max(0.0, deadline - time.monotonic())
        )
        self._last_drain_complete = runtime_drained

    def _request(
        self,
        command: HarnessCommand,
        expected: type[object],
        *,
        timeout: float | _AwaitSettlement | None = None,
    ) -> object:
        reply: _Reply[object] = _Reply()
        with self._pending_lock:
            self._pending[command.correlation_id] = (expected, reply)
        reply.set_cancel(lambda: self._cancel(command.correlation_id))
        self._actor.submit(command)
        if isinstance(timeout, _AwaitSettlement):
            return reply.wait()
        return reply.wait(self._reply_timeout if timeout is None else timeout)

    def _cancel(self, correlation_id: str) -> None:
        with self._pending_lock:
            self._pending.pop(correlation_id, None)
        self._actor.cancel_correlation(correlation_id)

    def _fail_pending(self, error: BaseException) -> None:
        with self._pending_lock:
            pending = tuple(self._pending.values())
            self._pending.clear()
        for _expected, reply in pending:
            reply.fail(error)

    def _on_event(self, event: HarnessEvent) -> None:
        correlation_id = event.correlation_id
        with self._pending_lock:
            pending = self._pending.get(correlation_id)
            if pending is None:
                return
            expected, reply = pending
            if not isinstance(event, (expected, PortCommandRejected)):
                return
            self._pending.pop(correlation_id, None)
        if isinstance(event, PortCommandRejected):
            if event.code == "HARNESS_START_TIMEOUT":
                reply.fail(TimeoutError(event.detail))
            elif event.code in {
                "PORT_CLOSING",
                "PORT_OVERLOADED",
                "HARNESS_COMPLETION_HANDOFF_FAILED",
                "HARNESS_GENERATION_RESTARTED",
                "HARNESS_RUNTIME_STOPPED",
            }:
                reply.fail(HarnessRuntimeClosed(event.detail))
            else:
                failure = RuntimeError(event.detail)
                failure.code = event.code  # type: ignore[attr-defined]
                reply.fail(failure)
            return
        reply.complete(event)


def process_identities_match(
    expected: ProcessIdentity | None,
    current: ProcessIdentity | None,
) -> bool:
    """Pure comparison seam used by PID-reuse tests and stop fencing."""

    if expected is None or current is None:
        return expected == current
    return expected == current


def _launch_projection(spec: HarnessLaunchSpec) -> HarnessLaunchProjection:
    return HarnessLaunchProjection(
        harness=spec.harness,
        name=spec.name,
        headless=spec.headless,
        args=spec.args,
        ownership=spec.ownership,
        nickname=spec.nickname,
        cwd=spec.cwd,
        endpoint=spec.endpoint,
        session_ref=spec.session_ref,
        command=spec.command,
        turn_timeout_seconds=spec.turn_timeout_seconds,
        idle_timeout_seconds=spec.idle_timeout_seconds,
        containerized=spec.containerized,
        pinned_owner=spec.pinned_owner,
        container_image=spec.container_image,
        model_provider=spec.model_provider,
        model=spec.model,
    )


def _launch_spec(spec: HarnessLaunchProjection) -> HarnessLaunchSpec:
    return HarnessLaunchSpec(
        harness=spec.harness,
        name=spec.name,
        headless=spec.headless,
        args=spec.args,
        ownership=spec.ownership,
        nickname=spec.nickname,
        cwd=spec.cwd,
        endpoint=spec.endpoint,
        session_ref=spec.session_ref,
        command=spec.command,
        turn_timeout_seconds=spec.turn_timeout_seconds,
        idle_timeout_seconds=spec.idle_timeout_seconds,
        containerized=spec.containerized,
        pinned_owner=spec.pinned_owner,
        container_image=spec.container_image,
        model_provider=spec.model_provider,
        model=spec.model,
    )


__all__ = [
    "HarnessProjection",
    "HarnessRestoreSummary",
    "HarnessRuntimeActor",
    "HarnessRuntimeClosed",
    "HarnessRuntimeFacade",
    "ProcessIdentity",
    "ProcessIoPort",
    "process_identities_match",
]
