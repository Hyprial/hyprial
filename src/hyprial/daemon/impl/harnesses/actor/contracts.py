"""Value, record, projection and generation vocabulary for the harness actor.

Pure data carriers, the completion-receipt/generation records, the status
projection, and the goal/spec conversions.  No actor state lives here.
"""

from __future__ import annotations

import threading
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Generic, TypeVar, cast
from hyprial.daemon.impl.api  import (
    HarnessResult,
    StreamingHarnessProcess,
)
from hyprial.kernel  import (
    ManagedHarnessProcess,
    )
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.runtime.ports  import (
    HarnessCallIoCompleted,
    HarnessLaunchProjection,
    HarnessProcessStarted,
    HarnessRestoreProjection,
    HarnessSessionRefProjection,
    HarnessSessionRefsProjection,
    HarnessStatusProjection,
    HarnessStopIoCompleted,
    HarnessStreamingProjection,
)

if TYPE_CHECKING:  # pragma: no cover - annotation-only, same package
    from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor


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
                    identity_marker=(
                        record.identity.marker
                        if running and record.identity is not None
                        else None
                    ),
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
    from hyprial.daemon.impl.mcp.channel.ownership import _read_process_identity

    return _read_process_identity(pid)


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
        execution_runtime=spec.execution_runtime,
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
        execution_runtime=spec.execution_runtime,
        model_provider=spec.model_provider,
        model=spec.model,
    )
