"""Actor-owned Lark adapter lifecycle with asynchronous process effects.

The official Lark SDK and websocket client deliberately remain in a dedicated
worker process.  This module owns only daemon-side decisions: configured and
desired adapters, process generations, retry budget, quarantine, and stable
status projections.  Every subprocess/control-socket operation runs on the
effect executor and returns a generation/version-fenced completion to the
actor; no actor handler waits for a process, socket, or network call.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable

from hyprial.kernel import LarkGatewayConfig

from hyprial.daemon.impl.adapters.lark.runtime.commands import (
    AdapterDesiredStatePort,
    _IoResult,
    _Launcher,
    _Observation,
    _WorkerProcess,
)
from hyprial.daemon.impl.adapters.lark.worker.process import (
    ERROR,
    ONLINE,
)
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
