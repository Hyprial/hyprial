"""Actor-owned Lark adapter lifecycle with asynchronous process effects.

The official Lark SDK and websocket client deliberately remain in a dedicated
worker process.  This module owns only daemon-side decisions: configured and
desired adapters, process generations, retry budget, quarantine, and stable
status projections.  Every subprocess/control-socket operation runs on the
effect executor and returns a generation/version-fenced completion to the
actor; no actor handler waits for a process, socket, or network call.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Protocol

from hyprial.actor_runtime import (
    PROCESS_LIFECYCLE,
    ActorHandle,
    ActorRuntime,
    ActorSpec,
    AdmissionResult,
)
from hyprial.actor_runtime.policies import DEFAULT_POLICIES, SupervisionPolicy
from hyprial.contracts.lark import lark_recovery_coverage
from hyprial.contracts.ports import PortAdmission, PortCommandRejected
from hyprial.persistent_config import ChannelConfiguration, LarkGatewayConfig

from .api import ActorTarget, HarnessDelivery
from .errors import AdapterStartError
from .lifecycle import ADAPTER_START_TIMEOUT, START_DEADLINE_SECONDS
from .ports import (
    AdapterIoCompleted,
    AdapterHealthEventsCompleted,
    AdapterMutationCompleted,
    AdapterProjection,
    AdapterReloadProjection,
    AdapterTimerElapsedCommand,
    DeliverLarkMessageCommand,
    DeliverLarkAlarmCommand,
    DrainAdapterHealthCommand,
    LarkCommand,
    LarkEvent,
    LarkEventSink,
    PinAdapterCommand,
    ReloadAdaptersCommand,
    StartAdapterCommand,
    StopAdapterCommand,
    UnpinAdapterCommand,
)

STARTING = "starting"
ONLINE = "online"
ERROR = "error"


class _WorkerProcess(Protocol):
    @property
    def running(self) -> bool: ...

    @property
    def pid(self) -> int: ...

    @property
    def readiness(self) -> str: ...

    @property
    def last_sdk_output(self) -> str | None: ...

    @property
    def error(self) -> str | None: ...

    @property
    def health(self) -> dict[str, object]: ...

    def wait_ready(self, timeout: float | None = None) -> str: ...

    def drain_health_events(self) -> tuple[dict[str, object], ...]: ...

    def deliver_reply(self, delivery: HarnessDelivery) -> bool: ...

    def deliver_alarm(
        self, correlation_id: str, text: str, *, idempotency_key: str
    ) -> bool: ...

    def stop(self, timeout: float = 5.0) -> None: ...


class _Launcher(Protocol):
    startup_timeout: float

    def spawn(self, gateway: LarkGatewayConfig) -> _WorkerProcess: ...


class AdapterDesiredStatePort(Protocol):
    """Blocking persistence effect owned by the Adapter actor operation."""

    def activate(self, name: str) -> bool: ...

    def deactivate(self, name: str) -> bool: ...


class _NullDesiredStatePort:
    """Compatibility boundary for isolated AdapterRuntime unit tests."""

    def activate(self, name: str) -> bool:
        del name
        return False

    def deactivate(self, name: str) -> bool:
        del name
        return False


@dataclass(frozen=True, slots=True)
class AdapterRestoreSummary:
    attempted: int
    restored: int
    failed: int


@dataclass(frozen=True, slots=True)
class _Observation:
    running: bool
    readiness: str | None
    pid: int | None
    error: str | None
    attempt_token: str | None = None
    health: Mapping[str, object] = field(default_factory=dict)
    events: tuple[dict[str, object], ...] = ()
    last_sdk_output: str | None = None


@dataclass(frozen=True, slots=True)
class _IoResult:
    correlation_id: str
    generation: int
    version: int
    name: str
    operation: str
    succeeded: bool
    attempt_token: str | None = None
    observation: _Observation | None = None
    value: object = None
    code: str | None = None
    detail: str | None = None


#: Failure codes meaning the resource is still held: the rollback itself did
#: not succeed, so the worker is alive even though the operation failed.
_UNSETTLED_CODES = frozenset({"WORKER_STOP_FAILED"})


def _released(result: _IoResult) -> bool:
    """Whether the worker this result concerns is actually gone.

    The effect side already keeps a worker that refused to stop both
    registered and running -- see the compensating-stop path.  Completion then
    has to honour that: discarding the desired entry for a worker that is
    still alive makes the projection report "should not be running" while the
    process runs, and reconcile finds nothing to adopt because desired is
    empty.  Cleanup must not delete the record before the resource is really
    released; a failed rollback degrades the entry to unsettled, never to
    absent.
    """

    return result.code not in _UNSETTLED_CODES


@dataclass(frozen=True, slots=True)
class _Start:
    correlation_id: str
    name: str
    result: Future[bool]
    explicit: bool = True


@dataclass(frozen=True, slots=True)
class _Remove:
    correlation_id: str
    name: str
    result: Future[bool]


@dataclass(frozen=True, slots=True)
class _Reload:
    channels: ChannelConfiguration
    result: Future[dict[str, list[str]]]


@dataclass(frozen=True, slots=True)
class _Restore:
    names: tuple[str, ...]
    result: Future[AdapterRestoreSummary]


@dataclass(frozen=True, slots=True)
class _Reconcile:
    result: Future[int]


@dataclass(frozen=True, slots=True)
class _Refresh:
    result: Future[None]


@dataclass(frozen=True, slots=True)
class _DrainEvents:
    result: Future[tuple[dict[str, object], ...]]


@dataclass(frozen=True, slots=True)
class _DeliverReply:
    correlation_id: str
    name: str
    delivery: HarnessDelivery
    result: Future[bool]


@dataclass(frozen=True, slots=True)
class _DeliverAlarm:
    correlation_id: str
    name: str
    alarm_correlation_id: str
    text: str
    idempotency_key: str
    result: Future[bool]


@dataclass(frozen=True, slots=True)
class _Shutdown:
    correlation_id: str
    result: Future[None]
    deadline: float


@dataclass(slots=True)
class _Aggregate:
    kind: str
    future: Future[Any]
    remaining: set[str]
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    restarted: int = 0


def _is_online(authority: "_Authority", name: str) -> bool:
    observation = authority.observations.get(name)
    return bool(
        name in authority.desired
        and observation
        and observation.running
        and observation.readiness == ONLINE
        and observation.health.get("streamHealth") not in {"stale", "checking"}
    )


def _build_statuses(
    authority: "_Authority", generation: int
) -> dict[str, dict[str, object]]:
    """The one status builder.

    Both the live actor and the post-stop settlement path publish through
    this. A hand-written minimal dict in the second path dropped nine schema
    fields and hardcoded processRunning=False, which projected a worker that
    was still held and still running as stopped -- the exact confusion this
    module exists to prevent, reintroduced by the code meant to clear it.
    """

    known = set(authority.gateways) | set(authority.observations)
    statuses: dict[str, dict[str, object]] = {}
    for name in known:
        observation = authority.observations.get(name)
        configured = name in authority.gateways
        running = bool(observation and observation.running)
        readiness = observation.readiness if observation else None
        health = dict(observation.health) if observation else {}
        stream_stale = health.get("streamHealth") == "stale"
        stream_checking = health.get("streamHealth") == "checking"
        online = _is_online(authority, name)
        if name in authority.quarantined:
            status = "quarantined"
        elif not configured:
            status = "detached" if running else "stopped"
        elif stream_stale and name in authority.desired and running:
            status = "stale"
        elif stream_checking and name in authority.desired and running:
            status = "checking"
        elif online:
            status = ONLINE
        elif name in authority.desired and running and readiness == STARTING:
            status = STARTING
        elif name in authority.errors:
            status = ERROR
        else:
            status = "stopped"
        statuses[name] = {
            "id": f"lark:{name}",
            "provider": "lark",
            "name": name,
            "status": status,
            "online": online,
            "configured": configured,
            "desired": name in authority.desired,
            "processRunning": running,
            # Passed in, never read off the authority: a handler holds the
            # generation it was built with, and a stale handler publishing the
            # authority's live value would stamp somebody else's generation on
            # its own projection -- which is exactly what generation fencing
            # exists to detect.
            "generation": generation,
            "version": authority.versions.get(name, 0),
            **lark_recovery_coverage(),
            **health,
            **(
                {"pid": observation.pid}
                if running and observation is not None and observation.pid is not None
                else {}
            ),
            **({"error": authority.errors[name]} if name in authority.errors else {}),
            **(
                {"unsettled": authority.unsettled[name]}
                if name in authority.unsettled
                else {}
            ),
        }
        # G3 (v2): while a lifecycle transition is in flight, say who holds
        # the lock and for how long -- "谁锁的、锁了多久" -- so a wedged
        # adapter is visible in `hyprial adapter status` instead of only as
        # "already in progress" refusals.
        held = authority.transitions.get(name)
        if held is not None:
            statuses[name]["lifecycleCorrelationId"] = held
            written = authority.transition_since.get(name)
            if written is not None:
                statuses[name]["lifecycleAgeSeconds"] = max(
                    0, int(time.monotonic() - written)
                )
    return statuses


class _ProjectionStore:
    """Immutable copies written only by the actor and read by IPC threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._gateways: tuple[str, ...] = ()
        self._desired: frozenset[str] = frozenset()
        self._statuses: dict[str, dict[str, object]] = {}
        self._errors: dict[str, str] = {}

    def publish(
        self,
        *,
        gateways: Mapping[str, LarkGatewayConfig],
        desired: set[str],
        statuses: Mapping[str, Mapping[str, object]],
        errors: Mapping[str, str],
    ) -> None:
        with self._lock:
            self._gateways = tuple(sorted(gateways))
            self._desired = frozenset(desired)
            self._statuses = {name: dict(value) for name, value in statuses.items()}
            self._errors = dict(errors)

    @property
    def gateway_names(self) -> tuple[str, ...]:
        with self._lock:
            return self._gateways

    @property
    def desired(self) -> frozenset[str]:
        with self._lock:
            return self._desired

    @property
    def errors(self) -> dict[str, str]:
        with self._lock:
            return dict(self._errors)

    def status(self, name: str | None = None) -> tuple[dict[str, object], ...]:
        with self._lock:
            names = [name] if name is not None else sorted(self._statuses)
            return tuple(
                dict(self._statuses[item]) for item in names if item in self._statuses
            )


class _Authority:
    """Generation-stable actor state.

    Only the current actor handler mutates these containers.  The factory reads
    them after the guardian has stopped the failed generation, so reload and
    desired-state decisions survive an actor restart without a second owner.
    """

    def __init__(self, gateways: Mapping[str, LarkGatewayConfig]) -> None:
        self._custody_lock = threading.Lock()
        self.gateways = dict(gateways)
        self.desired: set[str] = set()
        self.observations: dict[str, _Observation] = {}
        self.errors: dict[str, str] = {}
        #: name -> failure code while the worker is still held. Survives a
        #: healthy probe, unlike ``errors``; cleared only by a real stop.
        self.unsettled: dict[str, str] = {}
        self.versions: dict[str, int] = {}
        self.retry_after: dict[str, float] = {}
        self.failures: dict[str, deque[float]] = {}
        self.failed_attempts: set[tuple[str, str]] = set()
        self.quarantined: set[str] = set()
        self.transitions: dict[str, str] = {}
        #: name -> monotonic timestamp of the currently held lifecycle
        #: transition (G2: the deadline object is this write-to-pop age).
        self.transition_since: dict[str, float] = {}
        self.health_events: list[dict[str, object]] = []
        self.pending: dict[str, Future[Any]] = {}
        self.aggregates: dict[str, _Aggregate] = {}
        self.stopping = False
        self.generation = 0

    def begin_generation(self) -> int:
        failure = RuntimeError("Lark adapter actor generation restarted")
        with self._custody_lock:
            pending = tuple(self.pending.values())
            aggregates = tuple(self.aggregates.values())
            self.pending.clear()
            self.aggregates.clear()
        for future in pending:
            if not future.done():
                future.set_exception(failure)
        for aggregate in aggregates:
            if not aggregate.future.done():
                aggregate.future.set_exception(failure)
        self.generation += 1
        return self.generation

    def fail_correlation(self, correlation_id: str, error: BaseException) -> None:
        """Last-resort custody cleanup after bounded completion handoff fails."""

        with self._custody_lock:
            future = self.pending.pop(correlation_id, None)
            aggregate = self.aggregates.pop(correlation_id, None)
        if future is not None and not future.done():
            future.set_exception(error)
        if aggregate is not None and not aggregate.future.done():
            aggregate.future.set_exception(error)

    def fail_all_custody(self, error: BaseException) -> None:
        """Terminate every admitted caller when the runtime loses completion custody."""

        with self._custody_lock:
            correlations = tuple(set(self.pending) | set(self.aggregates))
        for correlation_id in correlations:
            self.fail_correlation(correlation_id, error)

    def put_pending(self, correlation_id: str, future: Future[Any]) -> None:
        with self._custody_lock:
            self.pending[correlation_id] = future

    def pop_pending(self, correlation_id: str) -> Future[Any] | None:
        with self._custody_lock:
            return self.pending.pop(correlation_id, None)

    def put_aggregate(self, correlation_id: str, aggregate: _Aggregate) -> None:
        with self._custody_lock:
            self.aggregates[correlation_id] = aggregate

    def get_aggregate(self, correlation_id: str) -> _Aggregate | None:
        with self._custody_lock:
            return self.aggregates.get(correlation_id)

    def pop_aggregate(self, correlation_id: str) -> _Aggregate | None:
        with self._custody_lock:
            return self.aggregates.pop(correlation_id, None)


class _ProcessEffects:
    """The sole owner of live process handles and blocking process I/O."""

    def __init__(
        self,
        launcher: _Launcher,
        completion: Callable[[_IoResult], None],
        desired_state: AdapterDesiredStatePort,
        *,
        start_confirm_timeout: float,
        max_workers: int = 8,
        capacity: int = 64,
    ) -> None:
        self._launcher = launcher
        self._completion = completion
        self._desired_state = desired_state
        self._start_confirm_timeout = start_confirm_timeout
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._pending = 0
        self._processes: dict[str, tuple[str, _WorkerProcess]] = {}
        self._latest_attempt: dict[str, str] = {}
        self._slots = threading.BoundedSemaphore(capacity)
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="hyprial-lark-effect",
        )
        self._closing = False

    def processes(self) -> dict[str, _WorkerProcess]:
        with self._lock:
            return {name: item[1] for name, item in self._processes.items()}

    def delivery_ready(self, name: str) -> bool:
        """Read the effect owner's live process, never an actor observation."""

        with self._lock:
            item = self._processes.get(name)
            process = item[1] if item is not None else None
        return bool(
            process is not None
            and process.running
            and process.readiness == ONLINE
        )

    def liveness(self, name: str) -> dict[str, object] | None:
        """The live process owner's view of one adapter (G1/G2 readings).

        The actor's observation is a snapshot that a racing command may have
        stale-minted (fd73140a §1: an online worker under a placeholder
        observation); guards that decide "already running" or "timed out"
        read the process registry here instead.
        """

        with self._lock:
            item = self._processes.get(name)
        if item is None:
            return None
        attempt_token, process = item
        running = process.running
        return {
            "running": running,
            "readiness": process.readiness,
            "pid": process.pid if running else None,
            "lastSdkOutput": getattr(process, "last_sdk_output", None),
            "attemptToken": attempt_token,
        }

    def begin_shutdown(self) -> None:
        """Close effect admission and invalidate every current spawn token."""

        with self._lock:
            self._closing = True
            for adapter in tuple(self._latest_attempt):
                self._latest_attempt[adapter] = f"shutdown:{uuid.uuid4().hex}"

    def submit(
        self,
        *,
        correlation_id: str,
        generation: int,
        version: int,
        name: str,
        operation: str,
        gateway: LarkGatewayConfig | None = None,
        payload: object = None,
        attempt_token: str | None = None,
    ) -> bool:
        attempt_token = attempt_token or uuid.uuid4().hex
        if operation == "shutdown":
            # This is the shutdown/spawn linearization point.  A launcher may
            # already be blocked outside our lock; replacing every token makes
            # its eventual process stale before shutdown snapshots the live
            # registry.  The late spawn then stops its exact local process and
            # can never repopulate the registry after drain.
            self.begin_shutdown()

            with self._condition:
                self._pending += 1

            def control() -> None:
                try:
                    self._completion(
                        self._run(
                            correlation_id,
                            generation,
                            version,
                            name,
                            operation,
                            gateway,
                            payload,
                            attempt_token,
                        )
                    )
                finally:
                    with self._condition:
                        self._pending -= 1
                        self._condition.notify_all()

            threading.Thread(
                target=control,
                name="hyprial-lark-effect-shutdown",
                daemon=True,
            ).start()
            return True
        if self._closing and operation != "shutdown":
            self._completion(
                _IoResult(
                    correlation_id,
                    generation,
                    version,
                    name,
                    operation,
                    False,
                    attempt_token=attempt_token,
                    code="CLOSING",
                    detail="adapter runtime is closing",
                )
            )
            return False
        if not self._slots.acquire(blocking=False):
            self._completion(
                _IoResult(
                    correlation_id,
                    generation,
                    version,
                    name,
                    operation,
                    False,
                    attempt_token=attempt_token,
                    code="OVERLOADED",
                    detail="Lark effect executor is at capacity",
                )
            )
            return False
        if operation in {"spawn", "activate-spawn", "stop", "deactivate-stop"}:
            with self._lock:
                self._latest_attempt[name] = attempt_token
        with self._condition:
            self._pending += 1
        try:
            future = self._pool.submit(
                self._run,
                correlation_id,
                generation,
                version,
                name,
                operation,
                gateway,
                payload,
                attempt_token,
            )
        except BaseException:
            with self._condition:
                self._pending -= 1
                self._condition.notify_all()
            self._slots.release()
            raise

        def completed(done: Future[_IoResult]) -> None:
            try:
                result = done.result()
            except BaseException as error:  # defensive completion boundary
                result = _IoResult(
                    correlation_id,
                    generation,
                    version,
                    name,
                    operation,
                    False,
                    attempt_token=attempt_token,
                    detail=type(error).__name__,
                )
            try:
                self._completion(result)
            finally:
                self._slots.release()
                with self._condition:
                    self._pending -= 1
                    self._condition.notify_all()

        future.add_done_callback(completed)
        return True

    def _run(
        self,
        correlation_id: str,
        generation: int,
        version: int,
        name: str,
        operation: str,
        gateway: LarkGatewayConfig | None,
        payload: object,
        attempt_token: str,
    ) -> _IoResult:
        activated = False
        try:
            if operation in {"spawn", "activate-spawn"}:
                assert gateway is not None
                process = self._launcher.spawn(gateway)
                with self._lock:
                    current = self._latest_attempt.get(name)
                    previous_item = self._processes.get(name)
                    if current == attempt_token:
                        self._processes[name] = (attempt_token, process)
                if current != attempt_token:
                    process.stop()
                    if activated:
                        self._desired_state.deactivate(name)
                    return _IoResult(
                        correlation_id,
                        generation,
                        version,
                        name,
                        operation,
                        False,
                        attempt_token=attempt_token,
                        code="STALE_COMPLETION",
                        detail="spawn attempt was superseded",
                    )
                previous = previous_item[1] if previous_item is not None else None
                if previous is not None and previous is not process:
                    previous.stop()
                process.wait_ready(self._start_confirm_timeout)
                with self._lock:
                    still_current = self._latest_attempt.get(name) == attempt_token
                if not still_current:
                    process.stop()
                    if activated:
                        self._desired_state.deactivate(name)
                    return _IoResult(
                        correlation_id,
                        generation,
                        version,
                        name,
                        operation,
                        False,
                        attempt_token=attempt_token,
                        code="STALE_COMPLETION",
                        detail="spawn attempt was superseded while becoming ready",
                    )
                observation = self._observe(name)
                succeeded = observation.readiness != ERROR
                if succeeded and operation == "activate-spawn":
                    activated = self._desired_state.activate(name)
                    with self._lock:
                        still_current = self._latest_attempt.get(name) == attempt_token
                    if not still_current:
                        process.stop()
                        if activated:
                            self._desired_state.deactivate(name)
                        return _IoResult(
                            correlation_id,
                            generation,
                            version,
                            name,
                            operation,
                            False,
                            attempt_token=attempt_token,
                            code="STALE_COMPLETION",
                            detail="spawn attempt was superseded while persisting intent",
                        )
                if not succeeded:
                    with self._lock:
                        current_item = self._processes.get(name)
                        if current_item is not None and current_item[0] == attempt_token:
                            self._processes.pop(name, None)
                    process.stop()
                    if activated:
                        self._desired_state.deactivate(name)
                    observation = _Observation(False, None, None, observation.error)
                return _IoResult(
                    correlation_id,
                    generation,
                    version,
                    name,
                    operation,
                    succeeded,
                    attempt_token=attempt_token,
                    observation=observation,
                    detail=observation.error,
                )
            if operation == "probe":
                return _IoResult(
                    correlation_id,
                    generation,
                    version,
                    name,
                    operation,
                    True,
                    attempt_token=attempt_token,
                    observation=self._observe(name),
                )
            if operation in {"stop", "deactivate-stop"}:
                with self._lock:
                    item = self._processes.get(name)
                process = item[1] if item is not None else None
                if process is not None:
                    process.stop()
                if operation == "deactivate-stop":
                    self._desired_state.deactivate(name)
                with self._lock:
                    current = self._processes.get(name)
                    if current is item:
                        self._processes.pop(name, None)
                return _IoResult(
                    correlation_id,
                    generation,
                    version,
                    name,
                    operation,
                    True,
                    attempt_token=attempt_token,
                    observation=_Observation(False, None, None, None),
                    value=process is not None,
                )
            if operation == "discard":
                with self._lock:
                    item = self._processes.get(name)
                    process = (
                        item[1]
                        if item is not None and item[0] == str(payload)
                        else None
                    )
                if process is not None:
                    # Stop first, pop only once it really stopped. Popping
                    # first and then letting the stop fail left a live worker
                    # with no registry entry: nothing would ever try to stop
                    # it again, and reconcile had nothing to adopt.
                    try:
                        process.stop()
                    except Exception as stop_error:  # noqa: BLE001 - reported
                        return _IoResult(
                            correlation_id,
                            generation,
                            version,
                            name,
                            operation,
                            False,
                            attempt_token=attempt_token,
                            code="WORKER_STOP_FAILED",
                            detail=f"discard stop failed: {stop_error}",
                        )
                    with self._lock:
                        current = self._processes.get(name)
                        if current is not None and current[0] == str(payload):
                            self._processes.pop(name, None)
                return _IoResult(
                    correlation_id,
                    generation,
                    version,
                    name,
                    operation,
                    True,
                    attempt_token=attempt_token,
                )
            if operation == "deliver-reply":
                process = self._process(name)
                value = bool(process and process.deliver_reply(payload))
                return _IoResult(
                    correlation_id,
                    generation,
                    version,
                    name,
                    operation,
                    True,
                    attempt_token=attempt_token,
                    value=value,
                )
            if operation == "deliver-alarm":
                process = self._process(name)
                correlation, text, key = payload  # type: ignore[misc]
                value = bool(
                    process
                    and process.deliver_alarm(
                        correlation,
                        text,
                        idempotency_key=key,
                    )
                )
                return _IoResult(
                    correlation_id,
                    generation,
                    version,
                    name,
                    operation,
                    True,
                    attempt_token=attempt_token,
                    value=value,
                )
            if operation == "shutdown":
                self._closing = True
                with self._lock:
                    # Snapshot without clearing: custody stays with the
                    # registry until each worker really stops. Clearing first
                    # meant a stop that failed or timed out left a live worker
                    # nobody owned -- shutdown could not retry it and reconcile
                    # could not see it.
                    owned = tuple(
                        (entry, item[0], item[1])
                        for entry, item in self._processes.items()
                    )
                processes = tuple(process for _, _, process in owned)
                with self._condition:
                    self._pending += len(processes)

                def stop_owned(
                    entry: str, token: str, process: _WorkerProcess
                ) -> None:
                    try:
                        process.stop()
                    except Exception:  # noqa: BLE001 - custody stays put
                        return
                    else:
                        with self._lock:
                            current = self._processes.get(entry)
                            if current is not None and current[0] == token:
                                self._processes.pop(entry, None)
                    finally:
                        with self._condition:
                            self._pending -= 1
                            self._condition.notify_all()

                threads = tuple(
                    threading.Thread(
                        target=stop_owned, args=(entry, token, process), daemon=True
                    )
                    for entry, token, process in owned
                )
                for thread in threads:
                    thread.start()
                deadline = float(payload)
                for thread in threads:
                    thread.join(max(0.0, deadline - time.monotonic()))
                # Workers whose stop raised are still held. Reporting success
                # here told the caller shutdown was done while adapters were
                # still running, and hid which ones.
                stranded = self.retained()
                return _IoResult(
                    correlation_id,
                    generation,
                    version,
                    name,
                    operation,
                    not stranded,
                    value=stranded,
                    code="WORKER_STOP_FAILED" if stranded else None,
                    attempt_token=attempt_token,
                )
            raise ValueError(f"unknown Lark process effect: {operation}")
        except (NameError, ImportError):
            raise
        except Exception as error:  # process/control-socket boundary
            if operation == "activate-spawn":
                with self._lock:
                    current = self._processes.get(name)
                    process = (
                        current[1]
                        if current is not None and current[0] == attempt_token
                        else None
                    )
                stop_error: BaseException | None = None
                if process is not None:
                    # Deregister only once the process is actually stopped.
                    # Dropping the entry first and then swallowing a stop
                    # failure strands a live worker that neither reconcile nor
                    # shutdown can reach again: it is no longer registered, so
                    # nothing will ever try to stop it a second time.
                    try:
                        process.stop()
                    except Exception as caught:  # noqa: BLE001 - reported below
                        stop_error = caught
                    else:
                        with self._lock:
                            current = self._processes.get(name)
                            if current is not None and current[0] == attempt_token:
                                self._processes.pop(name, None)
                if stop_error is not None:
                    # Keep the desired state as well: a worker that is still
                    # running must stay both registered and desired, or the
                    # next reconcile sees an orphan it has no reason to adopt.
                    return _IoResult(
                        correlation_id,
                        generation,
                        version,
                        name,
                        operation,
                        False,
                        attempt_token=attempt_token,
                        code="WORKER_STOP_FAILED",
                        detail=f"{error}; stop failed: {stop_error}",
                    )
                if activated:
                    try:
                        self._desired_state.deactivate(name)
                    except Exception as rollback_error:
                        return _IoResult(
                            correlation_id,
                            generation,
                            version,
                            name,
                            operation,
                            False,
                            attempt_token=attempt_token,
                            code="DESIRED_ROLLBACK_FAILED",
                            detail=f"{error}; rollback failed: {rollback_error}",
                        )
            return _IoResult(
                correlation_id,
                generation,
                version,
                name,
                operation,
                False,
                attempt_token=attempt_token,
                detail=str(error),
            )

    def _process(self, name: str) -> _WorkerProcess | None:
        with self._lock:
            item = self._processes.get(name)
            return item[1] if item is not None else None

    def _observe(self, name: str) -> _Observation:
        with self._lock:
            item = self._processes.get(name)
        if item is None:
            return _Observation(False, None, None, None)
        attempt_token, process = item
        running = process.running
        return _Observation(
            running=running,
            readiness=process.readiness,
            pid=process.pid if running else None,
            error=process.error,
            attempt_token=attempt_token,
            health=process.health,
            events=process.drain_health_events(),
            last_sdk_output=getattr(process, "last_sdk_output", None),
        )

    def retained(self) -> tuple[str, ...]:
        """Adapters whose worker is still held because its stop did not succeed."""

        with self._lock:
            return tuple(sorted(self._processes))

    def stop_retained(self, timeout: float) -> tuple[str, ...]:
        """Retry every retained worker inside ``timeout``; return those still held.

        Retention is only worth anything if something later uses it: a second
        public stop that merely waited on finished pending work left a worker
        which refused once held forever with no path back.

        The budget is shared, not per worker. Calling ``process.stop()`` with
        its default timeout for each of N retained workers would serialise N
        full timeouts past the caller's own deadline.

        Callers must drain in-flight stops first. Retrying a worker whose
        earlier stop thread is still running puts two ``process.stop()`` calls
        on the same worker concurrently, which is a different bug from the one
        this exists to fix.
        """

        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            owned = tuple(
                (name, item[0], item[1]) for name, item in self._processes.items()
            )
        for name, token, process in owned:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                process.stop(timeout=remaining)
            except Exception:  # noqa: BLE001 - custody stays put, reported below
                continue
            with self._lock:
                current = self._processes.get(name)
                if current is not None and current[0] == token:
                    self._processes.pop(name, None)
        return self.retained()

    def pending(self) -> int:
        with self._condition:
            return self._pending

    def close(self, timeout: float) -> bool:
        self._closing = True
        self._pool.shutdown(wait=False, cancel_futures=True)
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True


class _AdapterHandler:
    def __init__(
        self,
        *,
        generation: int,
        authority: _Authority,
        projections: _ProjectionStore,
        effects: _ProcessEffects,
        policy: SupervisionPolicy,
        retry_delay_override: float | None,
        start_deadline: float = START_DEADLINE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.generation = generation
        # The restart back-off is the one decision here a test must be able to
        # drive: it asks "has the delay elapsed", and a test that answers by
        # sleeping is asserting how fast this machine is.  Injected so the
        # window can be crossed deliberately instead of waited for.
        self._clock = clock
        self._authority = authority
        self._gateways = authority.gateways
        self._desired = authority.desired
        self._observations = authority.observations
        self._errors = authority.errors
        self._unsettled = authority.unsettled
        self._versions = authority.versions
        self._retry_after = authority.retry_after
        self._failures = authority.failures
        self._failed_attempts = authority.failed_attempts
        self._quarantined = authority.quarantined
        self._transitions = authority.transitions
        self._transition_since = authority.transition_since
        self._start_deadline = start_deadline
        self._health_events = authority.health_events
        self._projections = projections
        self._effects = effects
        self._policy = policy
        self._retry_delay_override = retry_delay_override
        self._publish()
        retained = (set(self._observations) | set(self._desired)) - set(
            self._transitions
        )
        if retained and generation > 1:
            correlation = f"generation-recovery:{uuid.uuid4().hex}"
            recovery: Future[None] = Future()
            self._authority.put_aggregate(correlation, _Aggregate(
                "refresh",
                recovery,
                set(retained),
            ))
            for name in sorted(retained):
                self._effects.submit(
                    correlation_id=correlation,
                    generation=self.generation,
                    version=self._versions.get(name, 0),
                    name=name,
                    operation="probe",
                )

    def __call__(self, command: object) -> None:
        if isinstance(command, _Start):
            self._start(command)
        elif isinstance(command, _Remove):
            self._remove(command)
        elif isinstance(command, _Reload):
            self._reload(command)
        elif isinstance(command, _Restore):
            self._restore(command)
        elif isinstance(command, _Reconcile):
            self._reconcile(command)
        elif isinstance(command, _Refresh):
            self._refresh(command)
        elif isinstance(command, _DrainEvents):
            events = tuple(self._health_events)
            self._health_events.clear()
            command.result.set_result(events)
        elif isinstance(command, _DeliverReply):
            self._deliver_reply(command)
        elif isinstance(command, _DeliverAlarm):
            self._deliver_alarm(command)
        elif isinstance(command, _Shutdown):
            self._shutdown(command)
        elif isinstance(command, _IoResult):
            self._complete(command)
        else:
            raise TypeError(f"unsupported Lark adapter command: {type(command).__name__}")

    def _next_version(self, name: str) -> int:
        version = self._versions.get(name, 0) + 1
        self._versions[name] = version
        return version

    def _start(self, command: _Start) -> None:
        if self._authority.stopping:
            command.result.set_exception(RuntimeError("adapter runtime is closing"))
            return
        if command.name in self._transitions:
            command.result.set_exception(
                RuntimeError(f"adapter lifecycle is already in progress: {command.name}")
            )
            return
        gateway = self._gateways.get(command.name)
        if gateway is None:
            command.result.set_exception(
                AdapterStartError(f"Lark adapter is not configured: {command.name}")
            )
            return
        observation = self._observations.get(command.name)
        if (
            command.name in self._desired
            and observation is not None
            and observation.running
            and observation.readiness in {STARTING, ONLINE}
        ):
            command.result.set_result(False)
            return
        already_desired = command.name in self._desired
        self._desired.add(command.name)
        self._transitions[command.name] = command.correlation_id
        self._transition_since[command.name] = time.monotonic()
        if command.explicit:
            self._quarantined.discard(command.name)
            self._failures.pop(command.name, None)
            self._retry_after.pop(command.name, None)
        version = self._next_version(command.name)
        self._authority.put_pending(command.correlation_id, command.result)
        self._observations[command.name] = _Observation(
            False,
            STARTING,
            None,
            None,
        )
        self._publish()
        self._effects.submit(
            correlation_id=command.correlation_id,
            generation=self.generation,
            version=version,
            name=command.name,
            operation="spawn" if already_desired else "activate-spawn",
            gateway=gateway,
        )

    def _remove(self, command: _Remove) -> None:
        if command.name in self._transitions:
            command.result.set_exception(
                RuntimeError(f"adapter lifecycle is already in progress: {command.name}")
            )
            return
        existed = command.name in self._desired or command.name in self._observations
        self._transitions[command.name] = command.correlation_id
        self._transition_since[command.name] = time.monotonic()
        self._retry_after.pop(command.name, None)
        self._failures.pop(command.name, None)
        self._quarantined.discard(command.name)
        self._errors.pop(command.name, None)
        version = self._next_version(command.name)
        self._authority.put_pending(command.correlation_id, command.result)
        self._publish()
        self._effects.submit(
            correlation_id=command.correlation_id,
            generation=self.generation,
            version=version,
            name=command.name,
            operation="deactivate-stop",
            payload=existed,
        )

    def _reload(self, command: _Reload) -> None:
        if self._transitions:
            command.result.set_exception(
                RuntimeError("adapter lifecycle is in progress during reload")
            )
            return
        fresh = {item.name: item for item in command.channels.gateways}
        previous = self._gateways
        added = sorted(name for name in fresh if name not in previous)
        updated = sorted(
            name for name in fresh if name in previous and fresh[name] != previous[name]
        )
        removed = sorted(name for name in previous if name not in fresh)
        removed_running = sorted(
            name
            for name in removed
            if (observation := self._observations.get(name)) is not None
            and observation.running
        )
        for name in removed:
            self._desired.discard(name)
            self._transitions.pop(name, None)
            self._transition_since.pop(name, None)
            self._retry_after.pop(name, None)
            self._next_version(name)
        self._gateways.clear()
        self._gateways.update(fresh)
        self._publish()
        command.result.set_result(
            {
                "added": added,
                "updated": updated,
                "removed": removed,
                "removedRunning": removed_running,
            }
        )

    def _restore(self, command: _Restore) -> None:
        self._authority.stopping = False
        self._desired.clear()
        self._desired.update(command.names)
        self._transitions.clear()
        self._retry_after.clear()
        correlation = f"restore:{uuid.uuid4().hex}"
        aggregate = _Aggregate(
            "restore",
            command.result,
            set(command.names),
            attempted=len(command.names),
        )
        self._authority.put_aggregate(correlation, aggregate)
        if not command.names:
            self._finish_aggregate(correlation)
            return
        for name in command.names:
            gateway = self._gateways.get(name)
            if gateway is None:
                self._errors[name] = f"Lark adapter is not configured: {name}"
                aggregate.failed += 1
                aggregate.remaining.discard(name)
                continue
            version = self._next_version(name)
            self._observations[name] = _Observation(False, STARTING, None, None)
            self._effects.submit(
                correlation_id=correlation,
                generation=self.generation,
                version=version,
                name=name,
                operation="spawn",
                gateway=gateway,
            )
        self._publish()
        if not aggregate.remaining:
            self._finish_aggregate(correlation)

    def _reconcile(self, command: _Reconcile) -> None:
        self._expire_transition_deadlines()
        if self._authority.stopping or not self._desired:
            command.result.set_result(0)
            return
        correlation = f"reconcile:{uuid.uuid4().hex}"
        names = set(self._desired) - set(self._transitions)
        self._authority.put_aggregate(correlation, _Aggregate(
            "reconcile-probe",
            command.result,
            set(names),
        ))
        if not names:
            self._finish_aggregate(correlation)
            return
        for name in sorted(names):
            self._effects.submit(
                correlation_id=correlation,
                generation=self.generation,
                version=self._versions.get(name, 0),
                name=name,
                operation="probe",
            )

    def _refresh(self, command: _Refresh) -> None:
        names = set(self._gateways) | set(self._observations)
        if not names:
            command.result.set_result(None)
            return
        correlation = f"refresh:{uuid.uuid4().hex}"
        self._authority.put_aggregate(correlation, _Aggregate(
            "refresh",
            command.result,
            set(names),
        ))
        for name in sorted(names):
            self._effects.submit(
                correlation_id=correlation,
                generation=self.generation,
                version=self._versions.get(name, 0),
                name=name,
                operation="probe",
            )

    def _deliver_reply(self, command: _DeliverReply) -> None:
        if not self._effects.delivery_ready(command.name):
            command.result.set_result(False)
            return
        self._authority.put_pending(command.correlation_id, command.result)
        self._effects.submit(
            correlation_id=command.correlation_id,
            generation=self.generation,
            version=self._versions.get(command.name, 0),
            name=command.name,
            operation="deliver-reply",
            payload=command.delivery,
        )

    def _deliver_alarm(self, command: _DeliverAlarm) -> None:
        if not self._effects.delivery_ready(command.name):
            command.result.set_result(False)
            return
        self._authority.put_pending(command.correlation_id, command.result)
        self._effects.submit(
            correlation_id=command.correlation_id,
            generation=self.generation,
            version=self._versions.get(command.name, 0),
            name=command.name,
            operation="deliver-alarm",
            payload=(
                command.alarm_correlation_id,
                command.text,
                command.idempotency_key,
            ),
        )

    def _shutdown(self, command: _Shutdown) -> None:
        self._authority.stopping = True
        self._desired.clear()
        self._transitions.clear()
        self._transition_since.clear()
        for name in tuple(self._versions):
            self._next_version(name)
        self._authority.put_pending(command.correlation_id, command.result)
        self._publish()
        self._effects.submit(
            correlation_id=command.correlation_id,
            generation=self.generation,
            version=0,
            name="*",
            operation="shutdown",
            payload=command.deadline,
        )

    def _expire_transition_deadlines(self) -> None:
        """G2: bound how long one lifecycle transition may stay un-popped.

        The deadline object is the transition's write-to-pop age (fd73140a
        v2): a held ``_transitions[name]`` blocks every further lifecycle
        command for that adapter at admission, so an operation whose
        completion never arrives (or arrives only as a stale failure) locks
        the adapter until a daemon restart -- production held it for >20
        minutes on 2026-09-04.  The tick fires one fail-loud event
        (``lark.adapter.lifecycle.timeout``) and releases the lock: the
        pending caller is failed and the name becomes stoppable/startable
        again.  N is measured, not guessed -- see
        :data:`hyprial.adapters.lark.lifecycle.START_DEADLINE_SECONDS`.
        """

        now = time.monotonic()
        for name in tuple(self._transitions):
            correlation = self._transitions.get(name)
            if correlation is None:
                continue
            written = self._transition_since.get(name)
            if written is None:
                self._transition_since[name] = now
                continue
            age = now - written
            if age <= self._start_deadline:
                continue
            observation = self._observations.get(name)
            live = self._effects.liveness(name)
            pid: int | None = None
            last_output: str | None = None
            if live is not None:
                if live["pid"] is not None:
                    pid = int(live["pid"])  # type: ignore[arg-type]
                last_output = live["lastSdkOutput"]  # type: ignore[assignment]
            if observation is not None:
                if pid is None and observation.pid is not None:
                    pid = observation.pid
                last_output = observation.last_sdk_output or last_output
            detail = (
                f"Lark adapter {name} lifecycle transition {correlation} did "
                f"not complete within {self._start_deadline:g}s "
                f"(held {age:.0f}s)"
            )
            self._health_events.append(
                {
                    "event": "lark.adapter.lifecycle.timeout",
                    "adapter": name,
                    "correlationId": correlation,
                    "ageSeconds": round(age, 1),
                    "timeoutSeconds": self._start_deadline,
                    **({"pid": pid} if pid is not None else {}),
                    **(
                        {"lastSdkOutput": last_output[-2000:]}
                        if last_output
                        else {}
                    ),
                }
            )
            future = self._authority.pop_pending(correlation)
            if future is not None and not future.done():
                future.set_exception(
                    AdapterStartError(detail, code=ADAPTER_START_TIMEOUT)
                )
            self._transitions.pop(name, None)
            self._transition_since.pop(name, None)
            self._errors[name] = detail
            self._publish()

    def _complete(self, result: _IoResult) -> None:
        # G0 (fd73140a v2): a completion that settles as failure -- stale
        # generation below, stale version after it -- still releases the
        # lifecycle transition its correlation owns.  Both early returns used
        # to fail the caller's custody and return without popping, so the
        # first stale completion locked the adapter forever: production
        # (daemon.jsonl 2026-09-04 09:21Z) held `_transitions` from the
        # user's `lark:start:` write until a daemon restart 22 minutes later.
        if self._transitions.get(result.name) == result.correlation_id:
            self._transitions.pop(result.name, None)
            self._transition_since.pop(result.name, None)
        if result.generation != self.generation:
            self._authority.fail_correlation(
                result.correlation_id,
                RuntimeError("stale Lark completion generation"),
            )
            return
        if result.name != "*" and result.version != self._versions.get(result.name, 0):
            if result.operation == "spawn":
                self._effects.submit(
                    correlation_id=f"discard:{uuid.uuid4().hex}",
                    generation=self.generation,
                    version=self._versions.get(result.name, 0),
                    name=result.name,
                    operation="discard",
                    payload=result.attempt_token,
                )
            self._authority.fail_correlation(
                result.correlation_id,
                RuntimeError("Lark operation was superseded"),
            )
            return
        # The original guarded pop (dev :1405): for normal completions this
        # releases the transition the hoisted pop above already released --
        # kept as its own site so the early returns having their own release
        # is an additive guarantee, not a moved one.
        if self._transitions.get(result.name) == result.correlation_id:
            self._transitions.pop(result.name, None)
            self._transition_since.pop(result.name, None)
        if not _released(result) and result.name != "*":
            # "*" is the shutdown aggregate, not an adapter: recording debt
            # under it created an entry no adapter-scoped settlement could
            # ever clear. The per-adapter debt is written in the shutdown
            # branch below, from the stranded list.
            #
            # Any operation whose release failed, not only activate-spawn:
            # discard and shutdown reach here with the same code.
            # Unsettled custody outlives the probe that happens to succeed
            # next. Reporting this only through _errors let one healthy
            # refresh clear it, so the single structured signal that a live
            # worker is stranded vanished while the worker kept running.
            self._unsettled[result.name] = result.code or "UNSETTLED"
        if result.observation is not None:
            self._observations[result.name] = result.observation
            self._health_events.extend(result.observation.events)
            if result.observation.error:
                self._errors[result.name] = result.observation.error
            elif result.succeeded and result.name not in self._unsettled:
                self._errors.pop(result.name, None)
        aggregate = self._authority.get_aggregate(result.correlation_id)
        if aggregate is not None:
            self._complete_aggregate(result, aggregate)
            self._publish()
            return
        future = self._authority.pop_pending(result.correlation_id)
        if result.operation in {"spawn", "activate-spawn"}:
            if result.succeeded:
                if future is not None and not future.done():
                    future.set_result(True)
            else:
                if result.operation == "activate-spawn" and _released(result):
                    self._desired.discard(result.name)
                    self._observations.pop(result.name, None)
                self._record_failure(
                    result.name,
                    result.detail or "adapter start failed",
                    attempt_token=result.attempt_token,
                )
                if future is not None and not future.done():
                    future.set_exception(
                        AdapterStartError(result.detail or "adapter start failed")
                    )
        elif result.operation in {"stop", "deactivate-stop"}:
            if result.succeeded:
                # The worker is genuinely gone, so the custody debt recorded by
                # an earlier failed rollback is now settled and the record may
                # be dropped.
                self._unsettled.pop(result.name, None)
                if result.operation == "deactivate-stop":
                    self._desired.discard(result.name)
                self._observations.pop(result.name, None)
                if future is not None and not future.done():
                    future.set_result(bool(result.value))
            else:
                self._errors[result.name] = result.detail or "adapter stop failed"
                if future is not None and not future.done():
                    future.set_exception(
                        RuntimeError(result.detail or "adapter stop failed")
                    )
        elif result.operation in {"deliver-reply", "deliver-alarm"}:
            if future is not None and not future.done():
                future.set_result(bool(result.value) if result.succeeded else False)
        elif result.operation == "shutdown":
            stranded = result.value if isinstance(result.value, tuple) else ()
            for adapter in stranded:
                # Per adapter, so the caller can see *which* one is unsettled;
                # the shutdown result carries one name and cannot say that.
                self._unsettled[str(adapter)] = "WORKER_STOP_FAILED"
                self._errors[str(adapter)] = "adapter stop failed during shutdown"
            for adapter in tuple(self._observations):
                if adapter not in stranded:
                    self._observations.pop(adapter, None)
            if future is not None and not future.done():
                future.set_result(None)
        self._publish()

    def _complete_aggregate(self, result: _IoResult, aggregate: _Aggregate) -> None:
        aggregate.remaining.discard(result.name)
        if aggregate.kind == "restore":
            if result.succeeded and result.observation is not None:
                if result.observation.readiness == ONLINE:
                    aggregate.succeeded += 1
            else:
                aggregate.failed += 1
                self._record_failure(
                    result.name,
                    result.detail or "restore failed",
                    attempt_token=result.attempt_token,
                )
        elif aggregate.kind == "refresh":
            pass
        elif aggregate.kind == "reconcile-probe":
            observation = result.observation
            healthy = bool(
                observation
                and observation.running
                and observation.readiness in {ONLINE, STARTING}
            )
            if not healthy:
                self._record_failure(
                    result.name,
                    (observation.error if observation else None)
                    or "adapter process is not running",
                    attempt_token=(observation.attempt_token if observation else None),
                )
        elif aggregate.kind == "reconcile-spawn":
            if result.succeeded:
                aggregate.restarted += 1
            else:
                self._record_failure(
                    result.name,
                    result.detail or "restart failed",
                    attempt_token=result.attempt_token,
                )
        if aggregate.remaining:
            return
        if aggregate.kind == "reconcile-probe":
            self._begin_reconcile_spawns(result.correlation_id, aggregate)
            return
        self._finish_aggregate(result.correlation_id)

    def _begin_reconcile_spawns(
        self, correlation_id: str, aggregate: _Aggregate
    ) -> None:
        now = self._clock()
        restart: set[str] = set()
        for name in sorted(self._desired):
            observation = self._observations.get(name)
            healthy = bool(
                observation
                and observation.running
                and observation.readiness in {ONLINE, STARTING}
            )
            if healthy or name in self._quarantined:
                continue
            # Review round 4 (fd73140a): possession alone holds here.  The
            # tick-start expiry owns ALL age arithmetic -- by the time this
            # spawn phase walks the desired names it has already released
            # every over-age transition, so anything still held below is in
            # flight by construction.  Re-deriving age with this function's
            # own now₂ opened a window the width of the probe phase (and an
            # operator mismatch at age == deadline): the expiry kept the
            # transition while this gate released it -- supersede without
            # the G2 fail-loud promise.  A leaked transition can no longer
            # interlock with this skip: G0 releases it on the stale early
            # return, and G2's deadline releases the never-completing one.
            if name in self._transitions:
                continue
            if now < self._retry_after.get(name, 0.0):
                continue
            if (gateway := self._gateways.get(name)) is None:
                continue
            restart.add(name)
            attempt = len(self._failures.get(name, ())) + 1
            delay = (
                self._retry_delay_override
                if self._retry_delay_override is not None
                else self._policy.delay(attempt, 0.5)
            )
            self._retry_after[name] = now + delay
            version = self._next_version(name)
            self._effects.submit(
                correlation_id=correlation_id,
                generation=self.generation,
                version=version,
                name=name,
                operation="spawn",
                gateway=gateway,
            )
        aggregate.kind = "reconcile-spawn"
        aggregate.remaining = restart
        if not restart:
            self._finish_aggregate(correlation_id)

    def _finish_aggregate(self, correlation_id: str) -> None:
        aggregate = self._authority.pop_aggregate(correlation_id)
        if aggregate is None:
            return
        if aggregate.future.done():
            return
        if aggregate.kind == "restore":
            aggregate.future.set_result(
                AdapterRestoreSummary(
                    aggregate.attempted,
                    aggregate.succeeded,
                    aggregate.failed,
                )
            )
        elif aggregate.kind in {"reconcile-probe", "reconcile-spawn"}:
            aggregate.future.set_result(aggregate.restarted)
        else:
            aggregate.future.set_result(None)

    def _record_failure(
        self,
        name: str,
        detail: str,
        *,
        attempt_token: str | None = None,
    ) -> None:
        if attempt_token is not None:
            key = (name, attempt_token)
            if key in self._failed_attempts:
                self._errors[name] = detail
                return
            self._failed_attempts.add(key)
        now = time.monotonic()
        failures = self._failures.setdefault(name, deque())
        while failures and now - failures[0] > self._policy.restart_window:
            failures.popleft()
        failures.append(now)
        self._errors[name] = detail
        if len(failures) > self._policy.max_restarts:
            self._quarantined.add(name)

    def _is_online(self, name: str) -> bool:
        observation = self._observations.get(name)
        return bool(
            name in self._desired
            and observation
            and observation.running
            and observation.readiness == ONLINE
            and observation.health.get("streamHealth") not in {"stale", "checking"}
        )

    def _publish(self) -> None:
        statuses = _build_statuses(self._authority, self.generation)
        self._projections.publish(
            gateways=self._gateways,
            desired=self._desired,
            statuses=statuses,
            errors=self._errors,
        )


class _NullEventSink:
    def publish(self, event: LarkEvent) -> None:
        del event


class _PortEventDispatcher:
    """Bounded nonblocking handoff from actor completion to event ports."""

    def __init__(self, sink: LarkEventSink, capacity: int = 128) -> None:
        self._sink = sink
        self._slots = threading.BoundedSemaphore(capacity)
        self._queue: queue.Queue[LarkEvent | None] = queue.Queue(maxsize=capacity)
        self._thread = threading.Thread(
            target=self._run,
            name="hyprial-lark-port-events",
            daemon=True,
        )
        self._thread.start()

    def reserve(self) -> bool:
        return self._slots.acquire(blocking=False)

    def release(self) -> None:
        self._slots.release()

    def publish_reserved(self, event: LarkEvent) -> None:
        # A slot was reserved before command admission, so put_nowait cannot
        # overflow unless the invariant in this class is broken.
        self._queue.put_nowait(event)

    def close(self, timeout: float = 1.0) -> bool:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            return False
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        while True:
            event = self._queue.get()
            if event is None:
                return
            try:
                self._sink.publish(event)
            except Exception:
                pass
            finally:
                self._slots.release()


class AdapterRuntime:
    """Pykka-backed public facade; business callers never receive a raw ref."""

    def __init__(
        self,
        launcher: _Launcher,
        channels: ChannelConfiguration,
        *,
        start_confirm_timeout: float = 2.0,
        restore_confirm_timeout: float | None = None,
        retry_interval: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        mailbox_capacity: int = 128,
        actor_runtime: ActorRuntime | None = None,
        effect_capacity: int = 64,
        event_sink: LarkEventSink | None = None,
        configuration_source: Callable[[], ChannelConfiguration] | None = None,
        desired_state: AdapterDesiredStatePort | None = None,
        start_deadline_seconds: float = START_DEADLINE_SECONDS,
    ) -> None:
        # Legacy knobs remain accepted while composition migrates.  Restart
        # budget/window/backoff come from the one central named profile.
        del restore_confirm_timeout
        self._runtime = actor_runtime or ActorRuntime()
        self._owns_runtime = actor_runtime is None
        self._projections = _ProjectionStore()
        self._events = _PortEventDispatcher(event_sink or _NullEventSink())
        self._configuration_source = configuration_source
        self._reload_slot = threading.Lock()
        self._handle: ActorHandle | None = None
        gateways = {item.name: item for item in channels.gateways}
        self._authority = _Authority(gateways)
        self._policy = DEFAULT_POLICIES[PROCESS_LIFECYCLE]

        def completion(result: _IoResult) -> None:
            handle = self._handle
            if handle is None:
                self._authority.fail_correlation(
                    result.correlation_id,
                    RuntimeError(
                        "Lark completion arrived after adapter runtime shutdown"
                    ),
                )
                return
            result = replace(result, generation=self._authority.generation)
            deadline = time.monotonic() + 5.0
            while True:
                admission = self._runtime.tell(handle, result)
                if admission is AdmissionResult.ACCEPTED:
                    return
                if time.monotonic() >= deadline:
                    self._authority.fail_correlation(
                        result.correlation_id,
                        RuntimeError(
                            "Lark completion custody could not enter the actor mailbox"
                        ),
                    )
                    return
                time.sleep(0.01)

        self._effects = _ProcessEffects(
            launcher,
            completion,
            desired_state or _NullDesiredStatePort(),
            start_confirm_timeout=start_confirm_timeout,
            capacity=effect_capacity,
        )

        def handler_factory() -> _AdapterHandler:
            generation = self._authority.begin_generation()
            return _AdapterHandler(
                generation=generation,
                authority=self._authority,
                projections=self._projections,
                effects=self._effects,
                policy=self._policy,
                retry_delay_override=retry_interval,
                start_deadline=start_deadline_seconds,
                clock=clock,
            )

        self._handle = self._runtime.start(
            ActorSpec(
                name="lark-adapter-runtime",
                handler_factory=handler_factory,
                mailbox_capacity=mailbox_capacity,
                supervision_profile="process_lifecycle",
            )
        )

    @property
    def gateway_names(self) -> tuple[str, ...]:
        return self._projections.gateway_names

    @property
    def generation(self) -> int:
        return self._authority.generation

    @property
    def version(self) -> int:
        return max(self._authority.versions.values(), default=0)

    @property
    def last_errors(self) -> dict[str, str]:
        return self._projections.errors

    @property
    def _desired(self) -> frozenset[str]:
        """Read-only compatibility projection; it is not mutable authority."""

        return self._projections.desired

    @property
    def _processes(self) -> dict[str, _WorkerProcess]:
        """Read-only compatibility view of the effect-owned process registry."""

        return self._effects.processes()

    def _submit(self, command: object, future: Future[Any], timeout: float) -> Any:
        handle = self._handle
        if handle is None:
            raise RuntimeError("adapter runtime is stopped")
        admission = self._runtime.tell(handle, command)
        if admission is AdmissionResult.OVERLOADED:
            raise RuntimeError("adapter runtime overloaded")
        if admission is AdmissionResult.CLOSED:
            raise RuntimeError("adapter runtime is closing")
        try:
            return future.result(timeout=timeout)
        except FutureTimeout as error:
            correlation_id = getattr(command, "correlation_id", None)
            if isinstance(correlation_id, str):
                self._authority.fail_correlation(
                    correlation_id,
                    RuntimeError("Lark adapter operation timed out"),
                )
            future.cancel()
            raise RuntimeError("adapter runtime operation timed out") from error

    @staticmethod
    def _port_admission(admission: AdmissionResult) -> PortAdmission:
        if admission is AdmissionResult.ACCEPTED:
            return PortAdmission.ACCEPTED
        if admission is AdmissionResult.OVERLOADED:
            return PortAdmission.OVERLOADED
        return PortAdmission.CLOSING

    def _port_rejection(
        self,
        correlation_id: str,
        code: str,
        detail: str,
        *,
        admission: PortAdmission | None = None,
    ) -> PortCommandRejected:
        return PortCommandRejected(
            correlation_id=correlation_id,
            domain="lark-adapter",
            generation=self._authority.generation,
            version=max(self._authority.versions.values(), default=0),
            code=code,
            detail=detail,
            admission=admission,
        )

    def _admit_port_future(
        self,
        internal: object,
        future: Future[Any],
        correlation_id: str,
        completed: Callable[[Any], LarkEvent],
    ) -> PortAdmission:
        handle = self._handle
        if handle is None:
            return PortAdmission.CLOSING
        if not self._events.reserve():
            return PortAdmission.OVERLOADED
        admission = self._runtime.tell(handle, internal)
        port_admission = self._port_admission(admission)
        if port_admission is not PortAdmission.ACCEPTED:
            self._events.release()
            return port_admission

        def settled(done: Future[Any]) -> None:
            try:
                event = completed(done.result())
            except BaseException as error:
                event = self._port_rejection(
                    correlation_id,
                    # A typed domain verdict (ADAPTER_ALREADY_RUNNING /
                    # ADAPTER_START_TIMEOUT) keeps its stable code; anything
                    # else degrades to the exception type name as before.
                    getattr(error, "code", None) or type(error).__name__,
                    str(error),
                )
            self._events.publish_reserved(event)

        future.add_done_callback(settled)
        return PortAdmission.ACCEPTED

    def submit(self, command: LarkCommand) -> PortAdmission:
        """Nonblocking frozen-port command admission.

        Pin commands intentionally terminate here with a typed rejection: pin
        authority is the Agent domain and integration's composite sink routes
        those commands there.  This adapter actor never mutates pin state.
        """

        if isinstance(command, (PinAdapterCommand, UnpinAdapterCommand)):
            if not self._events.reserve():
                return PortAdmission.OVERLOADED
            self._events.publish_reserved(
                self._port_rejection(
                    command.correlation_id,
                    "PIN_OWNED_BY_AGENT_DOMAIN",
                    "adapter pin mutations must be routed to the Agent actor",
                )
            )
            return PortAdmission.ACCEPTED
        if isinstance(command, StartAdapterCommand):
            result: Future[bool] = Future()
            return self._admit_port_future(
                _Start(command.correlation_id, command.name, result),
                result,
                command.correlation_id,
                lambda changed: AdapterMutationCompleted(
                    correlation_id=command.correlation_id,
                    generation=self._authority.generation,
                    version=(projection.version if (projection := self.read_adapter(command.name)) else 0),
                    changed=bool(changed),
                    adapter=projection,
                ),
            )
        if isinstance(command, StopAdapterCommand):
            result = Future()
            return self._admit_port_future(
                _Remove(command.correlation_id, command.name, result),
                result,
                command.correlation_id,
                lambda changed: AdapterMutationCompleted(
                    correlation_id=command.correlation_id,
                    generation=self._authority.generation,
                    version=(projection.version if (projection := self.read_adapter(command.name)) else 0),
                    changed=bool(changed),
                    adapter=projection,
                ),
            )
        if isinstance(command, ReloadAdaptersCommand):
            return self._submit_reload_port(command)
        if isinstance(command, AdapterTimerElapsedCommand):
            current_version = max(self._authority.versions.values(), default=0)
            if (
                command.generation != self._authority.generation
                or command.version != current_version
            ):
                if not self._events.reserve():
                    return PortAdmission.OVERLOADED
                self._events.publish_reserved(
                    self._port_rejection(
                        command.correlation_id,
                        "STALE_TIMER",
                        "adapter timer generation/version is stale",
                    )
                )
                return PortAdmission.ACCEPTED
            result = Future()
            return self._admit_port_future(
                _Reconcile(result),
                result,
                command.correlation_id,
                lambda restarted: AdapterMutationCompleted(
                    correlation_id=command.correlation_id,
                    generation=self._authority.generation,
                    version=max(self._authority.versions.values(), default=0),
                    changed=bool(restarted),
                ),
            )
        if isinstance(command, DrainAdapterHealthCommand):
            result = Future()
            return self._admit_port_future(
                _DrainEvents(result),
                result,
                command.correlation_id,
                lambda events: AdapterHealthEventsCompleted(
                    correlation_id=command.correlation_id,
                    generation=self._authority.generation,
                    version=max(self._authority.versions.values(), default=0),
                    events=tuple(
                        tuple(sorted(dict(event).items())) for event in events
                    ),
                ),
            )
        if isinstance(command, DeliverLarkMessageCommand):
            try:
                delivery = self._decode_delivery(command.message_json)
            except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as error:
                if not self._events.reserve():
                    return PortAdmission.OVERLOADED
                self._events.publish_reserved(
                    self._port_rejection(
                        command.correlation_id,
                        "INVALID_LARK_DELIVERY",
                        str(error),
                    )
                )
                return PortAdmission.ACCEPTED
            result = Future()
            return self._admit_port_future(
                _DeliverReply(
                    command.correlation_id,
                    command.adapter,
                    delivery,
                    result,
                ),
                result,
                command.correlation_id,
                lambda succeeded: AdapterIoCompleted(
                    correlation_id=command.correlation_id,
                    generation=self._authority.generation,
                    version=(projection.version if (projection := self.read_adapter(command.adapter)) else 0),
                    adapter=command.adapter,
                    operation="deliver",
                    succeeded=bool(succeeded),
                    code=None if succeeded else "DELIVERY_REJECTED",
                ),
            )
        if isinstance(command, DeliverLarkAlarmCommand):
            result = Future()
            return self._admit_port_future(
                _DeliverAlarm(
                    command.correlation_id,
                    command.adapter,
                    command.message_id,
                    command.text,
                    command.idempotency_key,
                    result,
                ),
                result,
                command.correlation_id,
                lambda succeeded: AdapterIoCompleted(
                    correlation_id=command.correlation_id,
                    generation=self._authority.generation,
                    version=(
                        projection.version
                        if (projection := self.read_adapter(command.adapter))
                        else 0
                    ),
                    adapter=command.adapter,
                    operation="alarm",
                    succeeded=bool(succeeded),
                    code=None if succeeded else "DELIVERY_REJECTED",
                ),
            )
        raise TypeError(f"unsupported Lark port command: {type(command).__name__}")

    def _submit_reload_port(self, command: ReloadAdaptersCommand) -> PortAdmission:
        if not self._events.reserve():
            return PortAdmission.OVERLOADED
        source = self._configuration_source
        if source is None:
            self._events.publish_reserved(
                self._port_rejection(
                    command.correlation_id,
                    "RELOAD_REQUIRES_CONFIGURATION_PORT",
                    "composition must provide the Lark configuration I/O port",
                )
            )
            return PortAdmission.ACCEPTED
        if not self._reload_slot.acquire(blocking=False):
            self._events.release()
            return PortAdmission.OVERLOADED

        def reload_effect() -> None:
            try:
                summary = self.reload_gateways(source())
                event: LarkEvent = AdapterMutationCompleted(
                    correlation_id=command.correlation_id,
                    generation=self._authority.generation,
                    version=max(self._authority.versions.values(), default=0),
                    changed=any(summary.values()),
                    reload=AdapterReloadProjection(
                        added=tuple(summary["added"]),
                        updated=tuple(summary["updated"]),
                        removed=tuple(summary["removed"]),
                        removed_running=tuple(summary["removedRunning"]),
                    ),
                )
            except BaseException as error:
                event = self._port_rejection(
                    command.correlation_id,
                    type(error).__name__,
                    str(error),
                )
            finally:
                self._reload_slot.release()
            self._events.publish_reserved(event)

        threading.Thread(
            target=reload_effect,
            name="hyprial-lark-config-reload",
            daemon=True,
        ).start()
        return PortAdmission.ACCEPTED

    @staticmethod
    def _decode_delivery(payload: bytes) -> HarnessDelivery:
        if len(payload) > 1024 * 1024:
            raise ValueError("Lark delivery JSON exceeds 1 MiB")
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Lark delivery JSON must be an object")
        actor = value.get("fromActor")
        if not isinstance(actor, dict):
            raise ValueError("Lark delivery fromActor must be an object")

        def text(record: Mapping[str, object], key: str) -> str:
            item = record.get(key)
            if not isinstance(item, str) or not item:
                raise ValueError(f"Lark delivery {key} must be a non-empty string")
            return item

        return HarnessDelivery(
            delivery_id=text(value, "deliveryId"),
            message_id=text(value, "messageId"),
            reply_to=text(value, "replyTo"),
            from_actor=ActorTarget(
                actor_id=text(actor, "actorId"),
                actor_key=text(actor, "actorKey"),
                display_name=text(actor, "displayName"),
            ),
            text=text(value, "text"),
        )

    def read_adapter(self, name: str) -> AdapterProjection | None:
        statuses = self._projections.status(name)
        if not statuses:
            return None
        value = statuses[0]
        return AdapterProjection(
            version=int(value.get("version", 0)),
            adapter_id=str(value["id"]),
            name=str(value["name"]),
            status=str(value["status"]),
            online=bool(value["online"]),
            configured=bool(value["configured"]),
            desired=bool(value["desired"]),
            process_running=bool(value["processRunning"]),
            pid=(int(value["pid"]) if "pid" in value else None),
            error=(str(value["error"]) if "error" in value else None),
            lifecycle_correlation_id=(
                str(value["lifecycleCorrelationId"])
                if "lifecycleCorrelationId" in value
                else None
            ),
            lifecycle_age_seconds=(
                int(value["lifecycleAgeSeconds"])
                if "lifecycleAgeSeconds" in value
                else None
            ),
        )

    def read_adapters(self) -> tuple[AdapterProjection, ...]:
        return tuple(
            projection
            for value in self._projections.status()
            if (projection := self.read_adapter(str(value["name"]))) is not None
        )

    def reload_gateways(self, channels: ChannelConfiguration) -> dict[str, list[str]]:
        result: Future[dict[str, list[str]]] = Future()
        return self._submit(_Reload(channels, result), result, 5.0)

    def restore(self, specs: tuple[Any, ...]) -> AdapterRestoreSummary:
        names = tuple(sorted(spec.name for spec in specs if spec.harness == "lark"))
        result: Future[AdapterRestoreSummary] = Future()
        timeout = max(5.0, len(names) * 0.5 + 5.0)
        return self._submit(_Restore(names, result), result, timeout)

    def start(self, name: str) -> bool:
        result: Future[bool] = Future()
        correlation = uuid.uuid4().hex
        return bool(self._submit(_Start(correlation, name, result), result, 10.0))

    def remove(self, name: str) -> bool:
        result: Future[bool] = Future()
        correlation = uuid.uuid4().hex
        return bool(self._submit(_Remove(correlation, name, result), result, 10.0))

    def reconcile(self) -> int:
        result: Future[int] = Future()
        return int(self._submit(_Reconcile(result), result, 10.0))

    def _refresh(self) -> None:
        result: Future[None] = Future()
        self._submit(_Refresh(result), result, 5.0)

    def status(self, name: str | None = None) -> tuple[dict[str, object], ...]:
        if self._handle is not None:
            self._refresh()
        return self._projections.status(name)

    def drain_health_events(self) -> tuple[dict[str, object], ...]:
        self._refresh()
        result: Future[tuple[dict[str, object], ...]] = Future()
        return self._submit(_DrainEvents(result), result, 5.0)

    def reply_online(self, adapter: str) -> bool:
        # Reply delivery immediately follows this read.  Refreshing the
        # external worker here turns one reply into two serialized actor
        # operations (refresh, then deliver) and can consume the inbox
        # system-edge completion budget while an inbound event is still being
        # processed.  This precheck means only "the bridge is configured and
        # routable"; ``deliver_reply`` performs the authoritative live and
        # generation-fenced check inside the actor.  A just-started runtime
        # may not have published a status row yet, so stale/empty status must
        # never reject a valid configured bridge before that final check.
        return adapter in self._projections.gateway_names

    def deliver_reply(self, adapter: str, delivery: HarnessDelivery) -> bool:
        result: Future[bool] = Future()
        correlation = uuid.uuid4().hex
        return bool(
            self._submit(
                _DeliverReply(correlation, adapter, delivery, result),
                result,
                15.0,
            )
        )

    def deliver_alarm(
        self,
        adapter: str,
        correlation_id: str,
        text: str,
        *,
        idempotency_key: str,
    ) -> bool:
        result: Future[bool] = Future()
        correlation = uuid.uuid4().hex
        return bool(
            self._submit(
                _DeliverAlarm(
                    correlation,
                    adapter,
                    correlation_id,
                    text,
                    idempotency_key,
                    result,
                ),
                result,
                15.0,
            )
        )

    def _settle_released_custody(self) -> None:
        """Settle exactly the adapters this retry released -- no others.

        The actor is stopped, so no completion clears debt or republishes.
        The first version cleared every gateway and hardcoded
        processRunning=False, which reported a worker that was still held and
        still running as stopped: a mixed result (one released, one refusing)
        came out as if both had stopped.

        Publishing goes through the shared builder so the schema and the
        status derivation stay identical to the live actor's.
        """

        held = set(self._effects.retained())
        authority = self._authority
        for adapter in tuple(authority.unsettled):
            # "*" is the shutdown aggregate, not an adapter; it can never be
            # settled by name, so it is dropped here as well.
            if adapter == "*" or adapter not in held:
                authority.unsettled.pop(adapter, None)
                authority.errors.pop(adapter, None)
                authority.observations.pop(adapter, None)
        for adapter in tuple(authority.observations):
            if adapter not in held:
                authority.observations.pop(adapter, None)
        self._projections.publish(
            gateways=authority.gateways,
            desired=authority.desired,
            statuses=_build_statuses(authority, authority.generation),
            errors=authority.errors,
        )

    def stop(self, timeout: float = 5.0) -> bool:
        handle = self._handle
        if handle is None:
            deadline = time.monotonic() + max(0.0, timeout)
            # Drain first. An earlier stop may still be running in its own
            # thread, and retrying then would call process.stop() on the same
            # worker concurrently -- a different bug from the one the retry
            # exists to fix. Only custody that is still held after every
            # in-flight stop finished has genuinely failed.
            effects_complete = self._effects.close(
                max(0.0, deadline - time.monotonic())
            )
            stranded = self._effects.stop_retained(
                max(0.0, deadline - time.monotonic())
            )
            self._settle_released_custody()
            events_complete = self._events.close(
                max(0.0, deadline - time.monotonic())
            )
            if stranded:
                raise RuntimeError(
                    "Lark adapter runtime still holds "
                    f"{', '.join(stranded)}: worker stop did not succeed"
                )
            if not effects_complete or not events_complete:
                raise RuntimeError(
                    "Lark adapter runtime still has unresolved effect custody"
                )
            return True
        deadline = time.monotonic() + max(0.0, timeout)
        result: Future[None] = Future()
        correlation = uuid.uuid4().hex
        self._effects.begin_shutdown()
        command_complete = False
        runtime_complete = False
        effects_complete = False
        events_complete = False
        try:
            self._submit(
                _Shutdown(correlation, result, deadline),
                result,
                max(0.0, deadline - time.monotonic()),
            )
            command_complete = True
        finally:
            try:
                effects_complete = self._effects.close(
                    max(0.0, deadline - time.monotonic())
                )
            finally:
                try:
                    runtime_complete = self._runtime.stop(
                        handle,
                        timeout=max(0.0, deadline - time.monotonic()),
                    )
                finally:
                    self._authority.fail_all_custody(
                        RuntimeError("Lark adapter runtime stopped before completion")
                    )
                    self._handle = None
                    events_complete = self._events.close(
                        max(0.0, deadline - time.monotonic())
                    )
        complete = (
            command_complete
            and runtime_complete
            and effects_complete
            and events_complete
        )
        # Ordered after the drain check on purpose: a stop still in progress
        # must keep reporting the drain timeout, which is a different
        # condition with its own contract. This one means everything finished
        # and custody is *still* held -- the stop genuinely failed.
        if complete:
            stranded = self._effects.retained()
            if stranded:
                raise RuntimeError(
                    "Lark adapter runtime still holds "
                    f"{', '.join(stranded)}: worker stop did not succeed"
                )
        if not complete:
            raise RuntimeError(
                "Lark adapter runtime did not drain before deadline "
                f"(command={command_complete}, actor={runtime_complete}, "
                f"effects={effects_complete}, events={events_complete})"
            )
        return True


__all__ = [
    "AdapterRestoreSummary",
    "AdapterRuntime",
    "AdapterStartError",
]
