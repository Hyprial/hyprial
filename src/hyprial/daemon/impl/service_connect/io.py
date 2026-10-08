"""Separate bounded catalog, protected-record, and controller effect lanes."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Generic, TypeVar
from uuid import uuid4

from hyprial.kernel import AdmissionResult, EffectCompleted, EffectLane, EffectRequest

from .models import ServiceConnectError


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class CatalogRead:
    registry: object


@dataclass(frozen=True, slots=True)
class ProtectedRead:
    resolver: object


@dataclass(frozen=True, slots=True)
class ControllerCall:
    controller: object
    operation: str
    args: tuple[object, ...] = field(repr=False)


@dataclass(slots=True)
class _Waiter(Generic[T]):
    ready: threading.Event
    value: T | None = None
    error: BaseException | None = None
    detached: bool = False


@dataclass(frozen=True, slots=True)
class _Outcome(Generic[T]):
    value: T | None = None
    error_code: str | None = None
    error_kind: str | None = None


def _safe_execute(execute, request: object):
    try:
        return _Outcome(value=execute(request))
    except ServiceConnectError as error:
        return _Outcome(error_code=error.code, error_kind=str(error))
    except BaseException as error:
        return _Outcome(error_kind=type(error).__name__)


class _BoundedLane(Generic[T]):
    def __init__(self, name: str, execute, *, capacity: int = 16) -> None:
        self._lock = threading.Lock()
        self._waiters: dict[tuple[str, int], _Waiter[T]] = {}
        self._lane: EffectLane[object, _Outcome[T]] = EffectLane(
            name=name,
            execute=lambda request: _safe_execute(execute, request),
            complete=self._complete,
            capacity=capacity,
            workers=1,
            retry_seconds=0.01,
        )

    def call(self, request: object, timeout: float) -> T:
        operation_id = uuid4().hex
        generation = 1
        token = (operation_id, generation)
        waiter: _Waiter[T] = _Waiter(threading.Event())
        with self._lock:
            self._waiters[token] = waiter
        admission = self._lane.submit(
            EffectRequest(operation_id, generation, request)
        )
        if admission is not AdmissionResult.ACCEPTED:
            with self._lock:
                self._waiters.pop(token, None)
            raise RuntimeError(f"service effect lane refused work: {admission.value}")
        if not waiter.ready.wait(timeout):
            with self._lock:
                waiter.detached = True
                settled = waiter.ready.is_set()
            if settled:
                self._settle(token)
            raise TimeoutError("service effect remains unsettled")
        try:
            if waiter.error is not None:
                raise waiter.error
            return waiter.value  # type: ignore[return-value]
        finally:
            self._settle(token)

    def _complete(self, event: EffectCompleted[_Outcome[T]]) -> AdmissionResult:
        token = (event.operation_id, event.generation)
        with self._lock:
            waiter = self._waiters.get(token)
            if waiter is None:
                return AdmissionResult.ACCEPTED
            outcome = event.result
            if outcome is not None:
                waiter.value = outcome.value
                if outcome.error_code is not None:
                    waiter.error = ServiceConnectError(
                        outcome.error_code,
                        outcome.error_kind or "service effect failed",
                    )
                elif outcome.error_kind is not None:
                    waiter.error = RuntimeError(outcome.error_kind)
            if event.error is not None:
                waiter.error = RuntimeError(event.error)
            detached = waiter.detached
            waiter.ready.set()
        if detached:
            self._settle(token)
        return AdmissionResult.ACCEPTED

    def _settle(self, token: tuple[str, int]) -> None:
        with self._lock:
            self._waiters.pop(token, None)
        self._lane.acknowledge(*token)

    def close(self, timeout: float) -> bool:
        return self._lane.close(timeout)


def _read_catalog(request: object):
    assert isinstance(request, CatalogRead)
    return request.registry.load()


def _read_protected(request: object):
    assert isinstance(request, ProtectedRead)
    return request.resolver.load()


def _control(request: object):
    assert isinstance(request, ControllerCall)
    method = getattr(request.controller, request.operation)
    return method(*request.args)


class ServiceIo:
    """Own the three external-effect lanes used by one service manager."""

    def __init__(self, *, capacity: int = 16) -> None:
        self._catalog = _BoundedLane("service-catalog", _read_catalog, capacity=capacity)
        self._protected = _BoundedLane(
            "service-protected", _read_protected, capacity=capacity
        )
        self._control = _BoundedLane("service-control", _control, capacity=capacity)

    def read_catalog(self, registry: object, timeout: float):
        return self._catalog.call(CatalogRead(registry), timeout)

    def read_protected(self, resolver: object, timeout: float):
        return self._protected.call(ProtectedRead(resolver), timeout)

    def control(
        self,
        controller: object,
        operation: str,
        args: tuple[object, ...],
        timeout: float,
    ):
        try:
            return self._control.call(
                ControllerCall(controller, operation, args), timeout
            )
        except ServiceConnectError:
            raise

    def close(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        closed = True
        for lane in (self._catalog, self._protected, self._control):
            closed = lane.close(max(0.0, deadline - time.monotonic())) and closed
        return closed
