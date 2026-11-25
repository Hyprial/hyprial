# Decision record (file header; no inline comments by repository rule):
# - One adapter serves both ABI v1 modules: the packaged pure-Python
#   reference_abi module and the PyO3 hyprial._core.actors extension. It is
#   the only ActorEngine implementation after P8 removed the shipped Python
#   engine, so create_engine can choose a backend without changing the
#   business-facing contract.
# - The ABI module owns mailboxes, threads, generations, timers, pools and
#   supervision. This adapter owns only value translation and record lookup:
#   policies become five-tuples, native event tuples become ActorEvent,
#   admission integers become AdmissionResult, ABI exceptions become the port
#   exceptions, and native pool completions become the port EffectCompleted.
#   Dead-letter reasons pass through unchanged: the ABI's "stopped"/
#   "quarantined" names are the port names. It never holds a mailbox, timer
#   entry, owner table or lane.
# - Factory building is wrapped once per actor or pool worker for CPU
#   accounting and completion translation. Accounting samples thread CPU at
#   the handler choke point the EffectLane uses; a failure in accounting never
#   changes the outcome.
# - ABI v1 accepts a jitter_seed, not a per-sample random_source. The port
#   configuration has no seed field, so this backend passes None and the
#   engine draws its own entropy; jitter_ratio=0 remains exact. Extending the
#   configuration shape for a seed is a later decision, not part of P6.
# - Spawn refusal is the only lifecycle error translated to ActorSpawnRefused.
#   A factory exception keeps the ABI cause and is re-raised unchanged, which
#   is the port's guardian behavior. Ask admission and child failure map to
#   ActorAdmissionError and ActorFailedError; ExpectedActorError passes through
#   because it is installed as the ABI expected error type.
# - Engine-wide drain remaining names are converted back to handles from the
#   adapter's own spawn-order registry. Duplicate names are matched in spawn
#   order; a name with no surviving handle is still represented so the count
#   cannot silently shrink.
# - I2 keyed actors: spawn forwards the spec's optional (group, key) pair
#   only when set, so keyless spawn call shapes stay byte-identical for ABI
#   doubles. A keyed ABI spawn returns the same actor-id shape as a keyless
#   spawn. The adapter registers the caller's own spec name, while lookup is
#   the only path to an existing keyed handle. No factory or sink runs under
#   the registry lock. Keyed ABI event tuples carry (group, key) after
#   restart_delay, and the structural log line gains actorGroup/actorKey only
#   for keyed events.
"""ActorEngine port over an ABI v1 engine module (reference or native)."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from hyprial.kernel.impl.actor_runtime.contracts import (
    ActorAdmissionError,
    ActorEvent,
    ActorEventKind,
    ActorFailedError,
    ActorHandle,
    ActorSnapshot,
    ActorSpec,
    ActorState,
    AdmissionResult,
    CommandHandler,
    DrainReport,
    ExpectedActorError,
    trace_admission,
)
from hyprial.kernel.impl.actor_runtime.effects import EffectCompleted
from hyprial.kernel.impl.actor_runtime.policies import (
    SupervisionPolicy,
    freeze_policy_catalog,
)
from hyprial.kernel.impl.actor_runtime.port.config import ActorEngineConfig
from hyprial.kernel.impl.actor_runtime.port.contracts import (
    ActorPoolSpec,
    ActorSpawnRefused,
)

_ADMISSION = {
    0: AdmissionResult.ACCEPTED,
    1: AdmissionResult.OVERLOADED,
    2: AdmissionResult.CLOSED,
}

_DEFAULT_EVENT_LOGGER = logging.getLogger("hyprial.actor_runtime")


def _log_event(event: ActorEvent) -> None:
    level = (
        logging.ERROR
        if event.kind in {ActorEventKind.CHILD_FAILED, ActorEventKind.CHILD_QUARANTINED}
        else logging.INFO
    )
    payload = {
        "kind": event.kind.value,
        "actorId": event.handle.actor_id,
        "actorName": event.handle.name,
        "generation": event.generation,
        "commandType": event.command_type,
        "code": event.code,
        "restartDelay": event.restart_delay,
    }
    if event.handle.group is not None and event.handle.key is not None:
        payload["actorGroup"] = event.handle.group
        payload["actorKey"] = event.handle.key
    _DEFAULT_EVENT_LOGGER.log(
        level,
        "actor runtime event",
        extra={"actor_event": payload},
    )


@dataclass(frozen=True, slots=True)
class _Record:
    handle: ActorHandle
    kind: str


def _default_sink() -> Callable[[ActorEvent], None]:
    return _log_event


def _policy_tuples(
    policies: dict[str, SupervisionPolicy],
) -> dict[str, tuple[int, float, float, float, float]]:
    return {
        name: (
            policy.max_restarts,
            policy.restart_window,
            policy.base_backoff,
            policy.max_backoff,
            policy.jitter_ratio,
        )
        for name, policy in policies.items()
    }


def _completion(command: object, completed_type: object) -> object:
    try:
        if not isinstance(command, completed_type):
            return command
    except TypeError:
        return command
    return EffectCompleted(
        operation_id=getattr(command, "operation_id"),
        generation=getattr(command, "generation"),
        result=getattr(command, "result", None),
        error=getattr(command, "error", None),
        detail=None,
    )


class AbiEngine:
    """One ActorEngine port instance backed by an ABI v1 module."""

    def __init__(
        self,
        module: ModuleType,
        config: ActorEngineConfig | None = None,
    ) -> None:
        selected = ActorEngineConfig() if config is None else config
        self._module = module
        self._config = selected
        self._policies = freeze_policy_catalog(selected.policies)
        self._table = _policy_tuples(dict(self._policies))
        self._event_sink = selected.event_sink or _default_sink()
        self._cpu = selected.cpu_accounting
        self._lock = threading.RLock()
        self._records: dict[str, _Record] = {}
        self._native = module.Engine(
            policies=self._table,
            jitter_seed=None,
            event_sink=self._on_engine_event,
            expected_error_type=ExpectedActorError,
        )

    @property
    def cpu_accounting(self) -> Any:
        return self._config.cpu_accounting

    def spawn(self, spec: ActorSpec) -> ActorHandle:
        self._require_profile(spec.supervision_profile)
        keyed = spec.group is not None or spec.key is not None
        try:
            result = self._native.spawn(
                spec.name,
                self._factory(spec.name, spec.handler_factory),
                spec.mailbox_capacity,
                spec.supervision_profile,
                dead_letter_sink=self._undelivered(spec.undelivered_sink),
                event_sink=self._events(spec.event_sink),
                **({"group": spec.group, "key": spec.key} if keyed else {}),
            )
        except self._module.ActorSpawnError as exc:
            cause = exc.__cause__
            if cause is not None:
                raise cause
            if str(exc) == "key-in-use":
                raise ActorSpawnRefused("key-in-use") from None
            raise
        except RuntimeError as exc:
            raise ActorSpawnRefused(str(exc)) from exc
        actor_id = result
        return self._register(
            actor_id,
            spec.name,
            "serial",
            spec.group if keyed else None,
            spec.key if keyed else None,
        )

    def lookup(self, group: str, key: str) -> ActorHandle | None:
        try:
            actor_id, name = self._native.lookup(group, key)
        except KeyError:
            return None
        return self._register(actor_id, name, "serial", group, key)

    def spawn_pool(self, spec: ActorPoolSpec) -> ActorHandle:
        self._require_profile(spec.supervision_profile)
        try:
            actor_id = self._native.spawn_pool(
                spec.name,
                self._factory(spec.name, spec.handler_factory),
                spec.pool_size,
                spec.capacity,
                spec.supervision_profile,
                dead_letter_sink=self._undelivered(spec.undelivered_sink),
                event_sink=self._events(spec.event_sink),
                retry_interval=spec.retry_interval,
            )
        except self._module.ActorSpawnError as exc:
            cause = exc.__cause__
            if cause is not None:
                raise cause
            raise
        except RuntimeError as exc:
            raise ActorSpawnRefused(str(exc)) from exc
        return self._register(actor_id, spec.name, "pool")

    def tell(self, handle: ActorHandle, command: object) -> AdmissionResult:
        record = self._serial(handle)
        if record is None:
            return AdmissionResult.CLOSED
        try:
            code = self._native.tell(
                record.handle.actor_id, record.handle.name, command
            )
        except KeyError:
            return AdmissionResult.CLOSED
        admission = _ADMISSION.get(code, AdmissionResult.CLOSED)
        if admission is AdmissionResult.ACCEPTED:
            trace_admission(lambda: self._still_queued(record))
        return admission

    def _still_queued(self, record: _Record) -> bool:
        """Whether this actor still has an undelivered command in the engine.

        ABI v1 reports queue depth per actor, not per command, so a queued
        sibling keeps the probe true; the lane retries once the queue drains.
        """

        try:
            _, _, queued, _, _ = self._native.snapshot(
                record.handle.actor_id, record.handle.name
            )
        except Exception:
            return False
        return bool(queued)

    def ask(self, handle: ActorHandle, command: object, timeout: float) -> object:
        record = self._serial(handle)
        if record is None:
            raise ActorAdmissionError("closed")
        try:
            return self._native.ask(
                record.handle.actor_id, record.handle.name, command, timeout
            )
        except self._module.ActorAdmissionError as exc:
            raise ActorAdmissionError(self._reason(exc)) from None
        except self._module.ActorFailedError as exc:
            raise ActorFailedError(str(exc)) from exc
        except KeyError:
            raise ActorAdmissionError("closed") from None

    def snapshot(self, handle: ActorHandle) -> ActorSnapshot:
        record = self._find(handle)
        if record is None:
            raise KeyError(f"unknown actor handle: {_handle_id(handle)}")
        state, generation, queued, in_flight, failures = self._native.snapshot(
            record.handle.actor_id, record.handle.name
        )
        return ActorSnapshot(
            handle=record.handle,
            state=ActorState(state),
            generation=generation,
            queued=queued,
            in_flight=in_flight,
            failures_in_window=failures,
        )

    def stop(self, handle: ActorHandle, timeout: float) -> bool:
        record = self._find(handle)
        if record is None:
            return False
        try:
            return bool(
                self._native.stop(
                    record.handle.actor_id, record.handle.name, timeout
                )
            )
        except KeyError:
            return False

    def reset(self, handle: ActorHandle) -> bool:
        record = self._serial(handle)
        if record is None:
            return False
        try:
            return bool(
                self._native.reset(record.handle.actor_id, record.handle.name)
            )
        except KeyError:
            return False

    def drain(
        self, handles: Sequence[ActorHandle] | None, timeout: float
    ) -> DrainReport:
        targets: list[_Record] | None = None
        actor_ids: list[str] | None = None
        if handles is not None:
            targets = []
            actor_ids = []
            for handle in handles:
                record = self._find(handle)
                if record is None:
                    raise KeyError(f"unknown actor handle: {_handle_id(handle)}")
                targets.append(record)
                actor_ids.append(record.handle.actor_id)
        complete, elapsed, remaining = self._native.drain(actor_ids, timeout)
        return DrainReport(
            complete=bool(complete),
            elapsed=float(elapsed),
            remaining=self._remaining(targets, remaining),
        )

    def shutdown(self, timeout: float) -> bool:
        return bool(self._native.shutdown(timeout))

    def send_after(
        self, handle: ActorHandle, delay: float, command: object
    ) -> str:
        record = self._serial(handle)
        if record is None:
            if self._pool(handle) is not None:
                raise ValueError("send_after target is a pool")
            raise KeyError(_handle_id(handle))
        return self._native.send_after(
            record.handle.actor_id, record.handle.name, delay, command
        )

    def cancel_after(self, timer_id: str) -> bool:
        if not isinstance(timer_id, str):
            return False
        return bool(self._native.cancel_after(timer_id))

    def pool_reserve(
        self, handle: ActorHandle, operation_id: str, generation: int
    ) -> AdmissionResult:
        pool = self._pool(handle)
        if pool is None:
            return AdmissionResult.CLOSED
        code = self._native.pool_reserve(
            pool.handle.actor_id, pool.handle.name, operation_id, generation
        )
        return _ADMISSION.get(code, AdmissionResult.CLOSED)

    def pool_submit(
        self,
        handle: ActorHandle,
        owner: ActorHandle,
        operation_id: str,
        generation: int,
        payload: object,
    ) -> AdmissionResult:
        pool = self._pool(handle)
        owner_record = self._serial(owner)
        if pool is None or owner_record is None:
            return AdmissionResult.CLOSED
        code = self._native.pool_submit(
            pool.handle.actor_id,
            pool.handle.name,
            owner_record.handle.actor_id,
            owner_record.handle.name,
            operation_id,
            generation,
            payload,
        )
        return _ADMISSION.get(code, AdmissionResult.CLOSED)

    def pool_submit_reserved(
        self,
        handle: ActorHandle,
        owner: ActorHandle,
        operation_id: str,
        generation: int,
        payload: object,
    ) -> AdmissionResult:
        pool = self._pool(handle)
        owner_record = self._serial(owner)
        if pool is None or owner_record is None:
            return AdmissionResult.CLOSED
        code = self._native.pool_submit_reserved(
            pool.handle.actor_id,
            pool.handle.name,
            owner_record.handle.actor_id,
            owner_record.handle.name,
            operation_id,
            generation,
            payload,
        )
        return _ADMISSION.get(code, AdmissionResult.CLOSED)

    def pool_cancel_reservation(
        self, handle: ActorHandle, operation_id: str, generation: int
    ) -> bool:
        pool = self._pool(handle)
        if pool is None:
            return False
        return bool(
            self._native.pool_cancel_reservation(
                pool.handle.actor_id, pool.handle.name, operation_id, generation
            )
        )

    def pool_acknowledge(
        self, handle: ActorHandle, operation_id: str, generation: int
    ) -> bool:
        pool = self._pool(handle)
        if pool is None:
            return False
        return bool(
            self._native.acknowledge(
                pool.handle.actor_id, pool.handle.name, operation_id, generation
            )
        )

    def native_handler_factory(self, name: str) -> Any:
        return self._native.native_handler_factory(name)

    def _tell_internal(
        self, handle: ActorHandle, command: object
    ) -> AdmissionResult:
        return self.tell(handle, command)

    def _snapshot(self, handle: ActorHandle) -> ActorSnapshot:
        return self.snapshot(handle)

    def _on_engine_event(self, event: tuple) -> None:
        try:
            self._event_sink(self._event(event))
        except BaseException:
            pass

    def _events(
        self, sink: Callable[[ActorEvent], None] | None
    ) -> Callable[[tuple], None] | None:
        if sink is None:
            return None

        def deliver(event: tuple) -> None:
            try:
                sink(self._event(event))
            except BaseException:
                pass

        return deliver

    def _undelivered(
        self, sink: Callable[[object, str], None] | None
    ) -> Callable[[object, str], None] | None:
        if sink is None:
            return None

        def deliver(command: object, reason: str) -> None:
            try:
                sink(command, reason)
            except BaseException:
                pass

        return deliver

    def _event(self, event: tuple) -> ActorEvent:
        (
            kind,
            actor_id,
            name,
            generation,
            command_type,
            code,
            detail,
            restart_delay,
            *keyed,
        ) = event
        return ActorEvent(
            kind=ActorEventKind(kind),
            handle=ActorHandle(
                actor_id,
                name,
                keyed[0] if len(keyed) > 0 else None,
                keyed[1] if len(keyed) > 1 else None,
            ),
            generation=generation,
            command_type=command_type,
            code=code,
            detail=detail,
            restart_delay=restart_delay,
        )

    def _factory(
        self,
        name: str,
        factory: Callable[[], CommandHandler],
    ) -> Callable[[], CommandHandler]:
        owner = self._cpu.owner(name)
        account = owner is not None and self._cpu.enabled
        completed_type = self._module.EffectCompleted

        def build() -> CommandHandler:
            inner = factory()

            def handler(command: object) -> object:
                translated = _completion(command, completed_type)
                started = time.thread_time() if account else 0.0
                try:
                    return inner(translated)
                finally:
                    if account and owner is not None:
                        try:
                            self._cpu.record_normalized(
                                owner,
                                type(translated).__name__,
                                cpu_seconds=time.thread_time() - started,
                            )
                        except Exception:
                            pass

            return handler

        return build

    def _register(
        self,
        actor_id: str,
        name: str,
        kind: str,
        group: str | None = None,
        key: str | None = None,
    ) -> ActorHandle:
        with self._lock:
            existing = self._records.get(actor_id)
            if existing is not None:
                return existing.handle
            handle = ActorHandle(actor_id, name, group, key)
            self._records[actor_id] = _Record(handle, kind)
        return handle

    def _find(self, handle: object) -> _Record | None:
        if not isinstance(handle, ActorHandle):
            return None
        with self._lock:
            record = self._records.get(handle.actor_id)
        if record is None or record.handle != handle:
            return None
        return record

    def _serial(self, handle: object) -> _Record | None:
        record = self._find(handle)
        return record if record is not None and record.kind == "serial" else None

    def _pool(self, handle: object) -> _Record | None:
        record = self._find(handle)
        return record if record is not None and record.kind == "pool" else None

    def _remaining(
        self,
        targets: Sequence[_Record] | None,
        names: Sequence[str],
    ) -> tuple[ActorHandle, ...]:
        with self._lock:
            records = (
                list(self._records.values())
                if targets is None
                else list(targets)
            )
        by_name: dict[str, list[ActorHandle]] = {}
        for record in records:
            by_name.setdefault(record.handle.name, []).append(record.handle)
        remaining: list[ActorHandle] = []
        for name in names:
            candidates = by_name.get(name)
            if candidates:
                remaining.append(candidates.pop(0))
            else:
                remaining.append(ActorHandle(name, name))
        return tuple(remaining)

    def _require_profile(self, profile: str) -> None:
        if profile not in self._policies:
            raise ValueError(f"unknown supervision profile: {profile}")

    @staticmethod
    def _reason(error: BaseException) -> str:
        args = getattr(error, "args", ())
        return str(args[0]) if args else "closed"


def _handle_id(handle: object) -> object:
    return getattr(handle, "actor_id", handle)
