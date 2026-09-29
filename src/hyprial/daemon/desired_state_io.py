"""Bounded asynchronous access to the shared desired-state authority.

Business actors submit immutable operation values here instead of waiting on
StatePersistence from their mailbox thread.  One worker preserves mutation
ordering.  EffectLane retains every completion until the owning actor handles
and acknowledges it, including across actor generation replacement.
"""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum, StrEnum
from typing import Callable

from hyprial.actor_runtime import AdmissionResult
from hyprial.actor_runtime.effects import EffectCompleted, EffectLane, EffectRequest
from hyprial.cost_counters import CallCostCounters

from .desired_state import DesiredState
from .state_persistence import (
    StateCommandCompleted,
    StateCostOrigin,
    StatePersistenceTimeout,
)


class DesiredStateOperation(StrEnum):
    APPLY_SESSION_LIFECYCLE = "apply_session_lifecycle"
    RETIRE_LIFECYCLE_RECEIPT = "retire_lifecycle_receipt"
    CONFIRM_LIFECYCLE_RECEIPT_RETIRED = "confirm_lifecycle_receipt_retired"
    CLAIM_INTERACTIVE = "claim_interactive"
    CLAIM_INTERACTIVE_WITH_AGENT_EFFECTS = "claim_interactive_with_agent_effects"
    UNREGISTER_INTERACTIVE_IF_CURRENT = "unregister_interactive_if_current"
    RECORD_SESSION_AGENT_EFFECTS = "record_session_agent_effects"
    COMPLETE_SESSION_AGENT_EFFECT = "complete_session_agent_effect"
    APPLY_HARNESS_LIFECYCLE = "apply_harness_lifecycle"
    CONFIRM_HARNESS_LIFECYCLE = "confirm_harness_lifecycle"
    COMPLETE_HARNESS_LIFECYCLE = "complete_harness_lifecycle"
    RECORD_HARNESS_LIFECYCLE_FAILURE = "record_harness_lifecycle_failure"
    ROLLBACK_HARNESS_LIFECYCLE = "rollback_harness_lifecycle"
    FAIL_HARNESS_REMOVAL = "fail_harness_removal"
    MARK_HARNESS_FAILED = "mark_harness_failed"
    UPSERT_HARNESS = "upsert_harness"
    REMOVE_ADAPTER_REGISTRATION = "remove_adapter_registration"
    RESTORE_ADAPTER_REGISTRATION = "restore_adapter_registration"
    SYNC_HARNESS_SESSION_REFS = "sync_harness_session_refs"


_IO_COST_KEYS = frozenset(
    f"{operation.value}/{origin.value}"
    for operation in DesiredStateOperation
    for origin in StateCostOrigin
)


@dataclass(frozen=True, slots=True)
class DesiredStateIoRequest:
    operation_id: str
    owner_generation: int
    owner_version: int
    operation: DesiredStateOperation
    args: tuple[object, ...] = ()
    kwargs: tuple[tuple[str, object], ...] = ()
    context: object = None
    cost_origin: StateCostOrigin = StateCostOrigin.BACKGROUND

    def __post_init__(self) -> None:
        if not self.operation_id:
            raise ValueError("desired-state I/O operation_id must not be empty")
        if not isinstance(self.cost_origin, StateCostOrigin):
            raise TypeError("cost_origin must be a StateCostOrigin")
        _assert_immutable(self.args)
        _assert_immutable(self.kwargs)
        _assert_immutable(self.context)


@dataclass(frozen=True, slots=True)
class DesiredStateIoCompleted:
    request: DesiredStateIoRequest
    result: object | None
    snapshot: DesiredState
    error_code: str | None = None
    error_detail: str | None = None
    error_is_oserror: bool = False
    error_errno: int | None = None
    error_strerror: str | None = None
    error_filename: str | bytes | None = None
    error_filename2: str | bytes | None = None


class DesiredStateIoPort:
    """One ordered, bounded desired-state effect lane."""

    def __init__(
        self,
        store: object,
        *,
        complete: Callable[[EffectCompleted[DesiredStateIoCompleted]], AdmissionResult],
        late_result: Callable[[str], StateCommandCompleted | None] | None = None,
        capacity: int = 128,
        retry_seconds: float = 0.01,
    ) -> None:
        self._store = store
        self._late_result = late_result
        self._complete = complete
        self._command_costs = CallCostCounters(_IO_COST_KEYS, wall=False)
        self._handoff_lock = threading.Lock()
        self._delivered: set[tuple[str, int]] = set()
        self._effects: EffectLane[DesiredStateIoRequest, DesiredStateIoCompleted] = (
            EffectLane(
                name="desired-state-io",
                execute=self._execute,
                complete=self._deliver,
                capacity=capacity,
                workers=1,
                retry_seconds=retry_seconds,
            )
        )

    def submit(self, request: DesiredStateIoRequest) -> AdmissionResult:
        return self._effects.submit(
            EffectRequest(request.operation_id, request.owner_generation, request)
        )

    def acknowledge(self, request: DesiredStateIoRequest) -> bool:
        acknowledged = self._effects.acknowledge(
            request.operation_id, request.owner_generation
        )
        with self._handoff_lock:
            self._delivered.discard(
                (request.operation_id, request.owner_generation)
            )
        return acknowledged

    def redeliver(self, request: DesiredStateIoRequest) -> None:
        """Release one accepted mailbox handoff without releasing effect custody."""

        with self._handoff_lock:
            self._delivered.discard(
                (request.operation_id, request.owner_generation)
            )

    def close(self, timeout: float) -> bool:
        return self._effects.close(timeout)

    @property
    def command_costs(self) -> CallCostCounters:
        return self._command_costs

    def _deliver(
        self, event: EffectCompleted[DesiredStateIoCompleted]
    ) -> AdmissionResult:
        token = (event.operation_id, event.generation)
        with self._handoff_lock:
            if token in self._delivered:
                return AdmissionResult.ACCEPTED
            # Reserve before tell: a fast actor may acknowledge before the
            # admission call returns.
            self._delivered.add(token)
        try:
            admission = self._complete(event)
        except BaseException:
            with self._handoff_lock:
                self._delivered.discard(token)
            raise
        if admission is not AdmissionResult.ACCEPTED:
            with self._handoff_lock:
                self._delivered.discard(token)
        return admission

    def _execute(self, request: DesiredStateIoRequest) -> DesiredStateIoCompleted:
        costs = self._command_costs
        started_cpu = time.thread_time() if costs.enabled else 0.0
        completion = self._execute_owned(request)
        if costs.enabled:
            costs.record(
                f"{request.operation.value}/{request.cost_origin.value}",
                cpu_seconds=time.thread_time() - started_cpu,
                error=completion.error_code is not None,
            )
        return completion

    def _execute_owned(
        self, request: DesiredStateIoRequest
    ) -> DesiredStateIoCompleted:
        try:
            method = getattr(self._store, request.operation.value)
            try:
                args = request.args
                if request.operation is DesiredStateOperation.SYNC_HARNESS_SESSION_REFS:
                    args = (dict(args[0]),)
                kwargs = dict(request.kwargs)
                if (
                    getattr(self._store, "supports_state_cost_origin", False)
                    and request.operation in {
                        DesiredStateOperation.APPLY_SESSION_LIFECYCLE,
                        DesiredStateOperation.CLAIM_INTERACTIVE,
                        DesiredStateOperation.CLAIM_INTERACTIVE_WITH_AGENT_EFFECTS,
                        DesiredStateOperation.UNREGISTER_INTERACTIVE_IF_CURRENT,
                        DesiredStateOperation.RECORD_SESSION_AGENT_EFFECTS,
                        DesiredStateOperation.COMPLETE_SESSION_AGENT_EFFECT,
                    }
                ):
                    kwargs["_cost_origin"] = request.cost_origin
                result = method(*args, **kwargs)
            except StatePersistenceTimeout as error:
                result = self._join_late(error.correlation_id)
            snapshot = self._store.load()
            return DesiredStateIoCompleted(request, result, snapshot)
        except BaseException as error:
            # Exceptions and tracebacks are mutable thread-affine objects.
            # Completion messages carry only stable typed text.
            try:
                snapshot = self._store.load()
            except BaseException:
                snapshot = DesiredState()
            return DesiredStateIoCompleted(
                request,
                None,
                snapshot,
                type(error).__name__,
                str(error),
                isinstance(error, OSError),
                error.errno if isinstance(error, OSError) else None,
                error.strerror if isinstance(error, OSError) else None,
                error.filename if isinstance(error, OSError) else None,
                error.filename2 if isinstance(error, OSError) else None,
            )

    def _join_late(self, correlation_id: str) -> object:
        if self._late_result is None:
            raise StatePersistenceTimeout(correlation_id)
        while True:
            completion = self._late_result(correlation_id)
            if completion is None:
                time.sleep(0.01)
                continue
            if completion.error is not None:
                raise completion.error
            return completion.result


def _assert_immutable(value: object) -> None:
    if value is None or isinstance(value, str | bytes | bool | int | float | Enum):
        return
    if isinstance(value, tuple):
        for child in value:
            _assert_immutable(child)
        return
    if is_dataclass(value) and bool(
        getattr(type(value), "__dataclass_params__", None).frozen
    ):
        for field in fields(value):
            _assert_immutable(getattr(value, field.name))
        return
    raise TypeError(
        f"desired-state I/O requests require immutable values, got {type(value).__name__}"
    )
