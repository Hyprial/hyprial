"""Bounded owner for blocking Zenoh route registration effects."""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from queue import Empty, Full, Queue
from typing import Callable, Protocol

from hyprial.contracts.ports import PortAdmission
from hyprial.transport.api import Registration, TransportSession

from .correlation import CompletionReceipt, CorrelationEventRouter
from .lifecycle_receipts import LifecycleMutationRequest, MutationProvenance
from .route_ports import (
    DropRouteCommand,
    EnsureRouteCommand,
    RouteEvent,
    RouteMutationCompleted,
    RouteMutationFailed,
    RouteSpec,
)


class RouteCompletionSink(Protocol):
    def publish(self, event: RouteEvent) -> CompletionReceipt: ...


class RouteHandlerFactory(Protocol):
    def __call__(self, route_id: str) -> Callable[[str], bytes | None]: ...


@dataclass(slots=True)
class _RegistrationPair:
    spec: RouteSpec
    liveliness: Registration | None
    queryable_registration: Registration
    owner_leases: dict[str, str]
    cleanup_attempt: str | None = None
    inbox_closed: bool = False
    liveliness_closed: bool = False


class RoutePartialCleanup(RuntimeError):
    pass


class RoutePreparationFailed(RuntimeError):
    """A route factory failed after acquiring handles that still need custody."""

    def __init__(
        self,
        detail: str,
        *,
        liveliness: Registration | None = None,
        queryable_registration: Registration | None = None,
        liveliness_closed: bool = False,
        inbox_closed: bool = False,
    ) -> None:
        super().__init__(detail)
        self.liveliness = liveliness
        self.queryable_registration = queryable_registration
        self.liveliness_closed = liveliness_closed
        self.inbox_closed = inbox_closed


class RouteCommandError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(slots=True)
class _Custody:
    command: LifecycleMutationRequest
    event: RouteEvent | None = None


@dataclass(slots=True)
class _CloseRegistrations:
    completed: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None
    pending: int = 0


@dataclass(frozen=True, slots=True)
class _NativeEnsure:
    token: str
    route_id: str
    spec: RouteSpec
    owner_lease: str
    lease_token: str


@dataclass(frozen=True, slots=True)
class _NativeClose:
    token: str
    route_id: str
    queryable_registration: Registration | None
    liveliness: Registration | None
    inbox_closed: bool
    liveliness_closed: bool
    lease_token: str
    retry: int = 0


NativeRouteEffect = _NativeEnsure | _NativeClose


@dataclass(frozen=True, slots=True)
class _NativeRouteCompleted:
    token: str
    route_id: str
    liveliness: Registration | None = None
    queryable_registration: Registration | None = None
    inbox_closed: bool = False
    liveliness_closed: bool = False
    error: str | None = None


@dataclass(slots=True)
class _SettlementRetry:
    deadline: float
    attempt: int
    ready_at: float


@dataclass(frozen=True, slots=True)
class _ReassociateReceipt:
    operation_id: str
    attempt_token: str
    correlation_id: str
    generation: int
    version: int


@dataclass(frozen=True, slots=True)
class _RetireReceipt:
    operation_id: str
    attempt_token: str
    resource_token: str


@dataclass(frozen=True, slots=True)
class _ConfirmReceipt:
    operation_id: str
    attempt_token: str
    resource_token: str


RouteControl = _ReassociateReceipt | _RetireReceipt | _ConfirmReceipt


class _ControlWaiter:
    def __init__(self) -> None:
        self.completed = threading.Event()
        self.result: bool | None = None
        self.error: BaseException | None = None


class RouteRegistrationIo:
    """Executes each route effect once and retains completion custody.

    The worker and its live registration handles survive state-actor
    generations.  A generation change uses :meth:`reassociate`; it never
    repeats a successful Zenoh declaration.
    """

    domain = "route"

    def __init__(
        self,
        transport: TransportSession,
        handlers: RouteHandlerFactory,
        completions: RouteCompletionSink,
        *,
        registration_factory: Callable[
            [RouteSpec], tuple[Registration | None, Registration]
        ]
        | None = None,
        capacity: int = 32,
        native_workers: int = 8,
        completion_deadline: float = 1.0,
        completion_backoff: tuple[float, ...] = (0.005, 0.01, 0.02),
    ) -> None:
        if capacity < 1 or native_workers < 1:
            raise ValueError("capacity and native_workers must be at least 1")
        if completion_deadline <= 0:
            raise ValueError("completion_deadline must be positive")
        self._transport = transport
        self._handlers = handlers
        self._registration_factory = registration_factory
        self._completions = completions
        self._deadline = completion_deadline
        self._backoff = completion_backoff
        self._capacity = capacity
        self._queue: Queue[
            str | RouteControl | _CloseRegistrations | _NativeRouteCompleted | None
        ] = Queue(maxsize=capacity)
        self._native_queue: Queue[NativeRouteEffect | None] = Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._custody: dict[str, _Custody] = {}
        self._receipts: dict[str, tuple[LifecycleMutationRequest, RouteEvent]] = {}
        self._retire_requests: dict[str, str] = {}
        self._retired_receipts: dict[str, str] = {}
        self._confirm_requests: dict[str, str] = {}
        self._control_waiters: dict[str, _ControlWaiter] = {}
        self._resettle_pending: set[str] = set()
        self._settlement_retries: dict[str, _SettlementRetry] = {}
        self._routes: dict[str, _RegistrationPair] = {}
        self._active_routes: dict[str, str] = {}
        self._route_waiting: dict[str, deque[str]] = {}
        self._shutdown_tokens: dict[
            str, tuple[_CloseRegistrations, str]
        ] = {}
        self._close_command: _CloseRegistrations | None = None
        self._native_work: dict[str, NativeRouteEffect] = {}
        self._closing_registrations: dict[str, _RegistrationPair] = {}
        self._prepare_cleanup_errors: dict[str, str] = {}
        self._native_pending: deque[NativeRouteEffect] = deque()
        self._closed = False
        self._drain_lock = threading.Lock()
        self._native_stops_sent = 0
        self._owner_stop_sent = False
        self._drained = False
        self._native_threads = tuple(
            threading.Thread(
                target=self._run_native,
                name=f"hyprial-route-native-{index}",
                daemon=True,
            )
            for index in range(min(capacity, native_workers))
        )
        for thread in self._native_threads:
            thread.start()
        self._thread = threading.Thread(
            target=self._run, name="hyprial-route-registration-io", daemon=True
        )
        self._thread.start()

    @property
    def generation(self) -> int:
        # Route completion fences are carried by each command and can be
        # reassociated; this property exists only for the common port shape.
        return 0

    @property
    def version(self) -> int:
        return 0

    def submit(self, command: object) -> PortAdmission:
        request = _route_request(command)
        token = request.attempt_token.strip()
        if not token:
            raise ValueError("attempt_token must not be blank")
        with self._condition:
            if self._closed:
                return PortAdmission.CLOSING
            incumbent = self._custody.get(token)
            if incumbent is not None:
                if incumbent.command != request:
                    return PortAdmission.OVERLOADED
                if incumbent.event is not None:
                    try:
                        self._queue.put_nowait(token)
                    except Full:
                        return PortAdmission.OVERLOADED
                    self._condition.notify_all()
                return PortAdmission.ACCEPTED
            receipt = self._receipts.get(token)
            if receipt is not None:
                prior_command, event = receipt
                if prior_command != request:
                    return PortAdmission.OVERLOADED
                self._custody[token] = _Custody(request, event)
                try:
                    self._queue.put_nowait(token)
                except Full:
                    del self._custody[token]
                    return PortAdmission.OVERLOADED
                self._condition.notify_all()
                return PortAdmission.ACCEPTED
            if (
                len(self._custody) + len(self._receipts) + len(self._retired_receipts)
                >= self._capacity
            ):
                return PortAdmission.OVERLOADED
            self._custody[token] = _Custody(request)
            try:
                self._queue.put_nowait(token)
            except Full:
                del self._custody[token]
                return PortAdmission.OVERLOADED
            self._condition.notify_all()
            return PortAdmission.ACCEPTED

    def reassociate(
        self,
        attempt_token: str,
        *,
        correlation_id: str,
        generation: int,
        version: int,
    ) -> bool:
        """Fence an executed completion into a new receiver generation."""

        return bool(self._call_control(_ReassociateReceipt(
            uuid.uuid4().hex, attempt_token, correlation_id, generation, version
        )))

    def _reassociate_owned(
        self, attempt_token: str, correlation_id: str,
        generation: int, version: int,
    ) -> bool:

        with self._condition:
            custody = self._custody.get(attempt_token)
            if custody is None or custody.event is None:
                return False
            custody.event = replace(
                custody.event,
                correlation_id=correlation_id,
                generation=generation,
                version=version,
            )
            try:
                self._queue.put_nowait(attempt_token)
            except Full:
                # Queue fullness can be another route's command. The owner
                # retains this token for a later settlement pass.
                self._resettle_pending.add(attempt_token)
                return True
            self._condition.notify_all()
            return True

    def registered(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._routes))

    def owners(self, route_id: str) -> tuple[str, ...]:
        """Project logical owners without exposing physical handles."""

        with self._lock:
            route = self._routes.get(route_id)
            return () if route is None else tuple(sorted(route.owner_leases))

    def receipt_pending(self, attempt_token: str) -> bool:
        with self._lock:
            return attempt_token in self._receipts

    def retire_receipt(self, attempt_token: str, resource_token: str) -> bool:
        return bool(self._call_control(_RetireReceipt(
            uuid.uuid4().hex, attempt_token, resource_token
        )))

    def _retire_receipt_owned(self, attempt_token: str, resource_token: str) -> bool:
        with self._condition:
            retired = self._retired_receipts.get(attempt_token)
            if retired is not None:
                if retired != resource_token:
                    raise ValueError("retired route receipt token mismatch")
                return True
            receipt = self._receipts.get(attempt_token)
            if receipt is None:
                custody = self._custody.get(attempt_token)
                provenance = (
                    None
                    if custody is None or custody.event is None
                    else getattr(custody.event, "provenance", None)
                )
                if isinstance(provenance, MutationProvenance):
                    if provenance.resource_token != resource_token:
                        raise ValueError("route custody resource token mismatch")
                    self._retire_requests[attempt_token] = resource_token
                    return True
                return False
            _command, event = receipt
            provenance = getattr(event, "provenance", None)
            if (
                not isinstance(provenance, MutationProvenance)
                or provenance.resource_token != resource_token
            ):
                raise ValueError("route receipt resource token mismatch")
            del self._receipts[attempt_token]
            self._retired_receipts[attempt_token] = resource_token
            self._condition.notify_all()
            return True

    def confirm_receipt_retired(self, attempt_token: str, resource_token: str) -> None:
        self._call_control(_ConfirmReceipt(
            uuid.uuid4().hex, attempt_token, resource_token
        ))

    def _confirm_receipt_owned(self, attempt_token: str, resource_token: str) -> None:
        with self._condition:
            retired = self._retired_receipts.get(attempt_token)
            if retired is None:
                pending = self._retire_requests.get(attempt_token)
                if pending is not None:
                    if pending != resource_token:
                        raise ValueError(
                            "route pending retirement confirmation token mismatch"
                        )
                    self._confirm_requests[attempt_token] = resource_token
                return
            if retired != resource_token:
                raise ValueError("route retirement confirmation token mismatch")
            del self._retired_receipts[attempt_token]
            self._condition.notify_all()

    def _call_control(self, command: RouteControl, timeout: float = 5.0) -> bool | None:
        if threading.current_thread() is self._thread:
            return self._apply_control(command)
        if not self._thread.is_alive():
            raise RuntimeError("route registration owner has stopped")
        deadline = time.monotonic() + max(0.0, timeout)
        waiter = _ControlWaiter()
        with self._condition:
            self._control_waiters[command.operation_id] = waiter
        try:
            self._queue.put(command, timeout=max(0.0, deadline - time.monotonic()))
        except Full as error:
            with self._condition:
                self._control_waiters.pop(command.operation_id, None)
            raise TimeoutError("route control admission timed out") from error
        if not waiter.completed.wait(max(0.0, deadline - time.monotonic())):
            raise TimeoutError(
                f"route control {command.operation_id} remains accepted"
            )
        if waiter.error is not None:
            raise waiter.error
        return waiter.result

    def _apply_control(self, command: RouteControl) -> bool | None:
        if isinstance(command, _ReassociateReceipt):
            return self._reassociate_owned(
                command.attempt_token, command.correlation_id,
                command.generation, command.version,
            )
        if isinstance(command, _RetireReceipt):
            return self._retire_receipt_owned(
                command.attempt_token, command.resource_token
            )
        self._confirm_receipt_owned(command.attempt_token, command.resource_token)
        return None

    def drain(self, timeout: float) -> bool:
        with self._drain_lock:
            if self._drained:
                return True
            deadline = time.monotonic() + max(0.0, timeout)
            with self._condition:
                self._closed = True
                while (
                    self._custody
                    or self._receipts
                    or self._retired_receipts
                    or any(
                        row.cleanup_attempt is not None
                        for row in self._routes.values()
                    )
                    or self._active_routes
                    or self._shutdown_tokens
                    or self._native_pending
                    or self._queue.unfinished_tasks
                    or self._native_queue.unfinished_tasks
                ):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._condition.wait(remaining)
            while self._native_stops_sent < len(self._native_threads):
                try:
                    self._native_queue.put_nowait(None)
                except Full:
                    return False
                self._native_stops_sent += 1
            for thread in self._native_threads:
                thread.join(max(0.0, deadline - time.monotonic()))
            if any(thread.is_alive() for thread in self._native_threads):
                return False
            if not self._owner_stop_sent:
                try:
                    self._queue.put_nowait(None)
                except Full:
                    return False
                self._owner_stop_sent = True
            self._thread.join(max(0.0, deadline - time.monotonic()))
            self._drained = not self._thread.is_alive()
            return self._drained

    def close_registrations(self, timeout: float = 5.0) -> None:
        """Fence physical teardown behind all admitted route effects.

        The route worker alone closes handles.  A timed-out caller must not
        take over teardown while an earlier declaration can still complete.
        """

        if not self._thread.is_alive():
            if self._routes:
                raise RuntimeError("route owner stopped with live registrations")
            return
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            command = self._close_command
            enqueue = command is None or command.completed.is_set()
            if enqueue:
                command = _CloseRegistrations()
                self._close_command = command
        assert command is not None
        if enqueue:
            try:
                self._queue.put(
                    command, timeout=max(0.0, deadline - time.monotonic())
                )
            except Full as error:
                failure = TimeoutError("route close admission timed out")
                command.error = failure
                command.completed.set()
                with self._condition:
                    if self._close_command is command:
                        self._close_command = None
                    self._condition.notify_all()
                raise failure from error
        if not command.completed.wait(max(0.0, deadline - time.monotonic())):
            raise TimeoutError("route close did not finish before deadline")
        if command.error is not None:
            raise command.error

    def _close_registrations_owned(self, command: _CloseRegistrations) -> None:
        with self._condition:
            route_ids = tuple(sorted({*self._routes, *self._active_routes}))
        if not route_ids:
            command.completed.set()
            return
        command.pending = len(route_ids)
        for route_id in route_ids:
            token = f"shutdown:{id(command)}:{route_id}"
            self._shutdown_tokens[token] = (command, route_id)
            self._queue_route_token(route_id, token)

    def _run(self) -> None:
        while True:
            timeout = 0.1
            if self._settlement_retries:
                ready_at = min(
                    retry.ready_at for retry in self._settlement_retries.values()
                )
                timeout = min(timeout, max(0.0, ready_at - time.monotonic()))
            try:
                token = self._queue.get(timeout=timeout)
            except Empty:
                self._service_native_pending()
                self._service_resettlements()
                continue
            if token is None:
                self._queue.task_done()
                with self._condition:
                    self._condition.notify_all()
                return
            if isinstance(token, _CloseRegistrations):
                try:
                    self._close_registrations_owned(token)
                except BaseException as error:
                    token.error = error
                    token.completed.set()
                self._queue.task_done()
                with self._condition:
                    self._condition.notify_all()
                continue
            if isinstance(token, (_ReassociateReceipt, _RetireReceipt, _ConfirmReceipt)):
                try:
                    result = self._apply_control(token)
                    error: BaseException | None = None
                except BaseException as caught:
                    result = None
                    error = caught
                with self._condition:
                    waiter = self._control_waiters.pop(token.operation_id, None)
                    if waiter is not None:
                        waiter.result = result
                        waiter.error = error
                        waiter.completed.set()
                    self._condition.notify_all()
                self._queue.task_done()
                continue
            if isinstance(token, _NativeRouteCompleted):
                self._complete_native(token)
                self._service_native_pending()
                self._queue.task_done()
                with self._condition:
                    self._condition.notify_all()
                continue
            custody = self._custody.get(token)
            if custody is not None and custody.event is not None:
                self._settle(token, custody)
            elif custody is not None:
                self._queue_route_token(_request_route_id(custody.command), token)
            self._queue.task_done()
            with self._condition:
                self._condition.notify_all()

    def _queue_route_token(self, route_id: str, token: str) -> None:
        if route_id in self._active_routes:
            self._route_waiting.setdefault(route_id, deque()).append(token)
            return
        self._active_routes[route_id] = token
        self._begin_route_token(route_id, token)

    def _begin_route_token(self, route_id: str, token: str) -> None:
        shutdown = self._shutdown_tokens.get(token)
        if shutdown is not None:
            with self._condition:
                registration = self._routes.get(route_id)
                if registration is not None and registration.cleanup_attempt is None:
                    registration.cleanup_attempt = token
            if registration is None:
                self._finish_route_token(route_id, token)
                return
            self._submit_native_close(
                token,
                route_id,
                registration,
                f"shutdown:{route_id}",
            )
            return
        custody = self._custody.get(token)
        if custody is None:
            self._finish_route_token(route_id, token)
            return
        command = custody.command.payload
        if isinstance(command, EnsureRouteCommand):
            self._begin_ensure(custody.command, command)
        elif isinstance(command, DropRouteCommand):
            self._begin_drop(custody.command, command)
        else:
            raise TypeError("route request payload is not a route command")

    def _begin_ensure(
        self,
        request: LifecycleMutationRequest,
        command: EnsureRouteCommand,
    ) -> None:
        spec = command.spec
        token = request.attempt_token
        owner_lease = _owner_lease(command.owner_lease, spec.route_id)
        try:
            if not spec.route_id or not spec.liveliness_key or not spec.inbox_key:
                raise ValueError("route ids and keys must not be blank")
            outcome: tuple[bool, MutationProvenance] | None = None
            with self._condition:
                current = self._routes.get(spec.route_id)
                if current is not None:
                    if current.cleanup_attempt is not None:
                        raise RoutePartialCleanup(
                            f"route cleanup belongs to {current.cleanup_attempt}"
                        )
                    if current.spec != spec:
                        raise ValueError(
                            "route id already registered with a different spec"
                        )
                    incumbent_token = current.owner_leases.get(owner_lease)
                    if incumbent_token is not None:
                        outcome = (
                            False,
                            MutationProvenance(False, False, incumbent_token),
                        )
                    else:
                        lease_token = (
                            request.expected_resource_token or uuid.uuid4().hex
                        )
                        current.owner_leases[owner_lease] = lease_token
                        outcome = (
                            True,
                            MutationProvenance(True, True, lease_token),
                        )
            if outcome is not None:
                self._complete_owned(
                    token, "ensure", spec.route_id, outcome[0], outcome[1]
                )
                return
            lease_token = request.expected_resource_token or uuid.uuid4().hex
            self._submit_native(
                _NativeEnsure(token, spec.route_id, spec, owner_lease, lease_token)
            )
        except Exception as error:
            self._fail_owned(token, "ensure", spec.route_id, error)

    def _begin_drop(
        self,
        request: LifecycleMutationRequest,
        command: DropRouteCommand,
    ) -> None:
        route_id = command.route_id
        token = request.attempt_token
        owner_lease = _owner_lease(command.owner_lease, route_id)
        try:
            outcome: tuple[bool, MutationProvenance] | None = None
            with self._condition:
                registration = self._routes.get(route_id)
                if registration is None:
                    outcome = (
                        False,
                        MutationProvenance(False, False, f"absent:{route_id}"),
                    )
                    lease_token = f"absent:{route_id}"
                else:
                    lease_token = registration.owner_leases.get(owner_lease) or ""
                    if not lease_token:
                        outcome = (
                            False,
                            MutationProvenance(
                                False, False, f"absent:{route_id}:{owner_lease}"
                            ),
                        )
                    elif (
                        request.expected_resource_token is not None
                        and lease_token != request.expected_resource_token
                    ):
                        outcome = (
                            False,
                            MutationProvenance(False, False, lease_token),
                        )
                    elif len(registration.owner_leases) > 1:
                        del registration.owner_leases[owner_lease]
                        outcome = (
                            True,
                            MutationProvenance(True, True, lease_token),
                        )
                    else:
                        if (
                            registration.cleanup_attempt is not None
                            and registration.cleanup_attempt != token
                        ):
                            raise RoutePartialCleanup(
                                "route cleanup belongs to "
                                f"{registration.cleanup_attempt}"
                            )
                        registration.cleanup_attempt = token
            if outcome is not None:
                self._complete_owned(
                    token, "drop", route_id, outcome[0], outcome[1]
                )
                return
            assert registration is not None
            self._submit_native_close(
                token, route_id, registration, lease_token
            )
        except Exception as error:
            self._fail_owned(token, "drop", route_id, error)

    def _submit_native(self, work: NativeRouteEffect) -> None:
        self._native_work[work.token] = work
        try:
            self._native_queue.put_nowait(work)
        except Full:
            self._native_pending.append(work)

    def _submit_native_close(
        self,
        token: str,
        route_id: str,
        registration: _RegistrationPair,
        lease_token: str,
    ) -> None:
        self._closing_registrations[token] = registration
        self._submit_native(
            _NativeClose(
                token,
                route_id,
                registration.queryable_registration,
                registration.liveliness,
                registration.inbox_closed,
                registration.liveliness_closed,
                lease_token,
            )
        )

    def _service_native_pending(self) -> None:
        while self._native_pending:
            try:
                self._native_queue.put_nowait(self._native_pending[0])
            except Full:
                return
            self._native_pending.popleft()

    def _run_native(self) -> None:
        while True:
            work = self._native_queue.get()
            if work is None:
                self._before_native_stop()
                self._native_queue.task_done()
                with self._condition:
                    self._condition.notify_all()
                return
            try:
                completion = self._execute_native(work)
                self._queue.put(completion)
            finally:
                self._native_queue.task_done()
                with self._condition:
                    self._condition.notify_all()

    def _before_native_stop(self) -> None:
        """Test seam before a native worker consumes its one stop token."""

    def _execute_native(self, work: NativeRouteEffect) -> _NativeRouteCompleted:
        if isinstance(work, _NativeEnsure):
            liveliness: Registration | None = None
            try:
                if self._registration_factory is not None:
                    liveliness, queryable = self._registration_factory(work.spec)
                else:
                    liveliness = (
                        self._transport.declare_liveliness(work.spec.liveliness_key)
                        if work.spec.advertise
                        else None
                    )
                    try:
                        queryable = self._transport.declare_queryable(
                            work.spec.inbox_key, self._handlers(work.route_id)
                        )
                    except BaseException as prepare_error:
                        liveliness_closed = liveliness is None
                        cleanup_error: BaseException | None = None
                        if liveliness is not None:
                            try:
                                liveliness.close()
                            except BaseException as error:
                                cleanup_error = error
                            else:
                                liveliness_closed = True
                        detail = f"{type(prepare_error).__name__}: {prepare_error}"
                        if cleanup_error is not None:
                            detail += (
                                "; cleanup:"
                                f"{type(cleanup_error).__name__}: {cleanup_error}"
                            )
                        return _NativeRouteCompleted(
                            work.token,
                            work.route_id,
                            liveliness=liveliness,
                            inbox_closed=True,
                            liveliness_closed=liveliness_closed,
                            error=detail,
                        )
                return _NativeRouteCompleted(
                    work.token,
                    work.route_id,
                    liveliness=liveliness,
                    queryable_registration=queryable,
                )
            except RoutePreparationFailed as error:
                return _NativeRouteCompleted(
                    work.token,
                    work.route_id,
                    liveliness=error.liveliness,
                    queryable_registration=error.queryable_registration,
                    inbox_closed=error.inbox_closed,
                    liveliness_closed=error.liveliness_closed,
                    error=str(error),
                )
            except BaseException as error:
                return _NativeRouteCompleted(
                    work.token,
                    work.route_id,
                    error=f"{type(error).__name__}: {error}",
                )
        errors: list[str] = []
        if work.retry:
            time.sleep(min(0.005 * (2 ** min(work.retry, 7)), 0.5))
        inbox_closed = work.inbox_closed
        liveliness_closed = work.liveliness_closed
        if work.queryable_registration is None:
            inbox_closed = True
        elif not inbox_closed:
            try:
                work.queryable_registration.close()
            except BaseException as error:
                errors.append(f"inbox:{type(error).__name__}:{error}")
            else:
                inbox_closed = True
        if work.liveliness is None:
            liveliness_closed = True
        elif not liveliness_closed:
            try:
                work.liveliness.close()
            except BaseException as error:
                errors.append(f"liveliness:{type(error).__name__}:{error}")
            else:
                liveliness_closed = True
        return _NativeRouteCompleted(
            work.token,
            work.route_id,
            inbox_closed=inbox_closed,
            liveliness_closed=liveliness_closed,
            error="; ".join(errors) or None,
        )

    def _complete_native(self, completion: _NativeRouteCompleted) -> None:
        work = self._native_work.get(completion.token)
        if work is None:
            return
        if isinstance(work, _NativeEnsure):
            if completion.error is not None:
                needs_cleanup = bool(
                    (
                        completion.queryable_registration is not None
                        and not completion.inbox_closed
                    )
                    or (
                        completion.liveliness is not None
                        and not completion.liveliness_closed
                    )
                )
                if needs_cleanup:
                    self._prepare_cleanup_errors[work.token] = completion.error
                    self._submit_native(
                        _NativeClose(
                            work.token,
                            work.route_id,
                            completion.queryable_registration,
                            completion.liveliness,
                            completion.inbox_closed,
                            completion.liveliness_closed,
                            work.lease_token,
                        )
                    )
                    return
                self._fail_owned(
                    work.token,
                    "ensure",
                    work.route_id,
                    RuntimeError(completion.error),
                )
                return
            assert completion.queryable_registration is not None
            with self._condition:
                self._routes[work.route_id] = _RegistrationPair(
                    work.spec,
                    completion.liveliness,
                    completion.queryable_registration,
                    {work.owner_lease: work.lease_token},
                )
            self._complete_owned(
                work.token,
                "ensure",
                work.route_id,
                True,
                MutationProvenance(True, True, work.lease_token),
            )
            return
        prepare_error = self._prepare_cleanup_errors.get(work.token)
        if prepare_error is not None:
            if completion.error is not None:
                self._submit_native(
                    replace(
                        work,
                        inbox_closed=completion.inbox_closed,
                        liveliness_closed=completion.liveliness_closed,
                        retry=work.retry + 1,
                    )
                )
                return
            del self._prepare_cleanup_errors[work.token]
            self._fail_owned(
                work.token,
                "ensure",
                work.route_id,
                RuntimeError(prepare_error),
            )
            return
        registration = self._closing_registrations.pop(work.token, None)
        if registration is None:
            return
        with self._condition:
            registration.inbox_closed = completion.inbox_closed
            registration.liveliness_closed = completion.liveliness_closed
        if completion.error is not None:
            if work.token in self._shutdown_tokens:
                self._finish_route_token(work.route_id, work.token)
            else:
                self._fail_owned(
                    work.token,
                    "drop",
                    work.route_id,
                    RoutePartialCleanup(completion.error),
                )
            return
        with self._condition:
            if self._routes.get(work.route_id) is registration:
                del self._routes[work.route_id]
        if work.token in self._shutdown_tokens:
            self._invalidate_route_completions(work.route_id)
            self._finish_route_token(work.route_id, work.token)
            return
        self._complete_owned(
            work.token,
            "drop",
            work.route_id,
            True,
            MutationProvenance(True, True, work.lease_token),
        )

    def _complete_owned(
        self,
        token: str,
        operation: str,
        route_id: str,
        changed: bool,
        provenance: MutationProvenance,
    ) -> None:
        custody = self._custody.get(token)
        if custody is not None:
            command = custody.command.payload
            assert isinstance(command, (EnsureRouteCommand, DropRouteCommand))
            custody.event = RouteMutationCompleted(
                command.correlation_id,
                command.attempt_token,
                command.generation,
                command.version,
                operation,
                route_id,
                changed,
                provenance,
            )
        self._finish_route_token(route_id, token)

    def _fail_owned(
        self, token: str, operation: str, route_id: str, error: Exception
    ) -> None:
        custody = self._custody.get(token)
        if custody is not None:
            command = custody.command.payload
            assert isinstance(command, (EnsureRouteCommand, DropRouteCommand))
            custody.event = RouteMutationFailed(
                command.correlation_id,
                command.attempt_token,
                command.generation,
                command.version,
                operation,
                route_id,
                (
                    "ROUTE_PARTIAL_CLEANUP"
                    if isinstance(error, RoutePartialCleanup)
                    else "ROUTE_IO_FAILED"
                ),
                f"{type(error).__name__}: {error}",
            )
        self._finish_route_token(route_id, token)

    def _finish_route_token(self, route_id: str, token: str) -> None:
        self._native_work.pop(token, None)
        self._closing_registrations.pop(token, None)
        self._prepare_cleanup_errors.pop(token, None)
        if self._active_routes.get(route_id) == token:
            del self._active_routes[route_id]
        custody = self._custody.get(token)
        if custody is not None and custody.event is not None:
            self._settle(token, custody)
        shutdown = self._shutdown_tokens.pop(token, None)
        if shutdown is not None:
            command, _route_id = shutdown
            command.pending -= 1
            if command.pending == 0:
                command.completed.set()
        waiting = self._route_waiting.get(route_id)
        while waiting:
            next_token = waiting.popleft()
            if next_token in self._custody or next_token in self._shutdown_tokens:
                self._active_routes[route_id] = next_token
                self._begin_route_token(route_id, next_token)
                break
        if waiting is not None and not waiting:
            self._route_waiting.pop(route_id, None)
        with self._condition:
            self._condition.notify_all()

    def _invalidate_route_completions(self, route_id: str) -> None:
        for token, custody in self._custody.items():
            event = custody.event
            if isinstance(event, RouteMutationCompleted) and event.route_id == route_id:
                custody.event = RouteMutationFailed(
                    event.correlation_id,
                    event.attempt_token,
                    event.generation,
                    event.version,
                    event.operation,
                    event.route_id,
                    "ROUTE_CLOSED_BEFORE_COMPLETION",
                    "physical route closed before completion receipt settled",
                )
                self._resettle_pending.add(token)

    def _settle(self, token: str, custody: _Custody) -> None:
        receipt = self._completions.publish(custody.event)  # type: ignore[arg-type]
        if receipt is CompletionReceipt.COMMITTED:
            self._settlement_retries.pop(token, None)
            with self._condition:
                if self._custody.get(token) is custody:
                    if isinstance(custody.event, RouteMutationCompleted):
                        provenance = custody.event.provenance
                        retired = self._retire_requests.pop(token, None)
                        if retired is None:
                            self._receipts[token] = (
                                custody.command,
                                custody.event,
                            )
                        elif retired != provenance.resource_token:
                            raise ValueError(
                                "route retirement resource token mismatch"
                            )
                        else:
                            confirmed = self._confirm_requests.pop(token, None)
                            if confirmed is None:
                                self._retired_receipts[token] = retired
                            elif confirmed != retired:
                                raise ValueError(
                                    "route retirement confirmation token mismatch"
                                )
                    del self._custody[token]
                self._condition.notify_all()
            return
        if receipt in {CompletionReceipt.STALE, CompletionReceipt.CLOSING}:
            self._settlement_retries.pop(token, None)
            return
        now = time.monotonic()
        retry = self._settlement_retries.get(token)
        if retry is None:
            retry = _SettlementRetry(now + self._deadline, 0, now)
            self._settlement_retries[token] = retry
        if now >= retry.deadline:
            self._settlement_retries.pop(token, None)
            return
        delay = self._backoff[min(retry.attempt, len(self._backoff) - 1)]
        retry.attempt += 1
        retry.ready_at = min(now + delay, retry.deadline)

    def _service_resettlements(self) -> None:
        with self._condition:
            immediate = tuple(self._resettle_pending)
            self._resettle_pending.clear()
        now = time.monotonic()
        due = {
            token
            for token, retry in self._settlement_retries.items()
            if retry.ready_at <= now
        }
        for token in {*immediate, *due}:
            custody = self._custody.get(token)
            if custody is not None and custody.event is not None:
                self._settle(token, custody)


class RouteRegistrationClient:
    """Bounded system-edge waits over the one RouteRegistrationIo mailbox."""

    def __init__(
        self,
        authority: RouteRegistrationIo,
        router: CorrelationEventRouter,
        *,
        timeout: float = 2.0,
    ) -> None:
        self._authority = authority
        self._router = router
        self._timeout = timeout

    def ensure(
        self, spec: RouteSpec, *, owner_lease: str | None = None
    ) -> RouteMutationCompleted:
        token = f"route-client:ensure:{spec.route_id}:{uuid.uuid4().hex}"
        return self._call(
            _route_request(
                EnsureRouteCommand(token, token, 0, 0, spec, owner_lease)
            )
        )

    def drop(
        self, route_id: str, *, owner_lease: str | None = None
    ) -> RouteMutationCompleted:
        token = f"route-client:drop:{route_id}:{uuid.uuid4().hex}"
        return self._call(
            _route_request(
                DropRouteCommand(token, token, 0, 0, route_id, owner_lease)
            )
        )

    def call_settled(
        self, request: LifecycleMutationRequest
    ) -> RouteMutationCompleted:
        """Join one exact route attempt through completion and receipt retirement.

        This is an internal bounded-I/O edge. A local observation timeout does
        not replace an accepted route mutation: the same immutable request is
        resubmitted so ``RouteRegistrationIo`` replays its retained completion.
        """

        if not isinstance(request.payload, (EnsureRouteCommand, DropRouteCommand)):
            raise TypeError("settled route request has an invalid payload")
        if request.payload.attempt_token != request.attempt_token:
            raise ValueError("route request attempt token mismatch")
        return self._call(request, wait_for_settlement=True)

    def registered(self) -> tuple[str, ...]:
        return self._authority.registered()

    def owners(self, route_id: str) -> tuple[str, ...]:
        return self._authority.owners(route_id)

    def _call(
        self,
        request: LifecycleMutationRequest,
        *,
        wait_for_settlement: bool = False,
    ) -> RouteMutationCompleted:
        command = request.payload
        if not isinstance(command, (EnsureRouteCommand, DropRouteCommand)):
            raise TypeError("route request payload is invalid")
        while True:
            waiter = self._router.register(
                command.correlation_id,
                attempt_token=command.attempt_token,
                generation=command.generation,
                versions=frozenset({command.version}),
            )
            admission = self._authority.submit(request)
            if admission is PortAdmission.ACCEPTED:
                try:
                    event = waiter.wait(self._timeout)
                except TimeoutError:
                    if wait_for_settlement:
                        continue
                    raise
                break
            waiter.cancel()
            if wait_for_settlement and admission is PortAdmission.OVERLOADED:
                time.sleep(0.005)
                continue
            raise RouteCommandError(
                f"PORT_{admission.value.upper()}",
                f"route command admission is {admission.value}",
            )
        if isinstance(event, RouteMutationFailed):
            raise RouteCommandError(event.code, event.detail)
        if not isinstance(event, RouteMutationCompleted):
            raise TypeError("route authority returned an invalid completion")
        deadline = None if wait_for_settlement else time.monotonic() + self._timeout
        while not self._authority.retire_receipt(
            event.attempt_token, event.provenance.resource_token
        ):
            if deadline is not None and time.monotonic() >= deadline:
                raise RouteCommandError(
                    "ROUTE_RECEIPT_UNSETTLED",
                    f"route receipt did not settle: {event.route_id}",
                )
            time.sleep(0.005)
        while True:
            try:
                self._authority.confirm_receipt_retired(
                    event.attempt_token, event.provenance.resource_token
                )
                break
            except TimeoutError:
                if not wait_for_settlement:
                    raise
        return event


__all__ = [
    "RouteCommandError",
    "RouteRegistrationClient",
    "RouteRegistrationIo",
    "RoutePartialCleanup",
    "RoutePreparationFailed",
]


def _route_request(command: object) -> LifecycleMutationRequest:
    if isinstance(command, LifecycleMutationRequest):
        if not isinstance(command.payload, (EnsureRouteCommand, DropRouteCommand)):
            raise TypeError("lifecycle route request has an invalid payload")
        return command
    if isinstance(command, (EnsureRouteCommand, DropRouteCommand)):
        return LifecycleMutationRequest(
            command.correlation_id,
            command.attempt_token,
            command.attempt_token.split(":", 1)[0],
            None,
            command,
        )
    raise TypeError(f"unsupported route command: {type(command).__name__}")


def _request_route_id(request: LifecycleMutationRequest) -> str:
    command = request.payload
    if isinstance(command, EnsureRouteCommand):
        return command.spec.route_id
    if isinstance(command, DropRouteCommand):
        return command.route_id
    raise TypeError("route request payload is not a route command")


def _owner_lease(value: str | None, route_id: str) -> str:
    owner = value.strip() if isinstance(value, str) else ""
    return owner or f"legacy:{route_id}"
