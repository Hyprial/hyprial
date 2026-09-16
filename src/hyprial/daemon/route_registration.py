"""Bounded owner for blocking Zenoh route registration effects."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, replace
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


class RouteCommandError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(slots=True)
class _Custody:
    command: LifecycleMutationRequest
    event: RouteEvent | None = None


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
        completion_deadline: float = 1.0,
        completion_backoff: tuple[float, ...] = (0.005, 0.01, 0.02),
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        if completion_deadline <= 0:
            raise ValueError("completion_deadline must be positive")
        self._transport = transport
        self._handlers = handlers
        self._registration_factory = registration_factory
        self._completions = completions
        self._deadline = completion_deadline
        self._backoff = completion_backoff
        self._capacity = capacity
        self._queue: Queue[str | None] = Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._custody: dict[str, _Custody] = {}
        self._receipts: dict[str, tuple[LifecycleMutationRequest, RouteEvent]] = {}
        self._retire_requests: dict[str, str] = {}
        self._retired_receipts: dict[str, str] = {}
        self._confirm_requests: dict[str, str] = {}
        self._routes: dict[str, _RegistrationPair] = {}
        self._closed = False
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
                # Custody already contains the new receiver fence.  An
                # existing queued settlement token will observe it; queue
                # admission here is not the D22 completion receipt.
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

    def drain(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            self._closed = True
            while (
                self._custody
                or self._receipts
                or self._retired_receipts
                or any(row.cleanup_attempt is not None for row in self._routes.values())
                or self._queue.unfinished_tasks
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        try:
            self._queue.put_nowait(None)
        except Full:
            return False
        self._thread.join(max(0.0, deadline - time.monotonic()))
        return not self._thread.is_alive()

    def close_registrations(self) -> None:
        with self._lock:
            registrations = tuple(self._routes.items())
            for route_id, registration in registrations:
                if registration.cleanup_attempt is None:
                    registration.cleanup_attempt = f"shutdown:{route_id}"
        for route_id, registration in registrations:
            try:
                self._close_registration(route_id, registration)
            except RoutePartialCleanup:
                continue
            with self._condition:
                if self._routes.get(route_id) is registration:
                    del self._routes[route_id]
                self._condition.notify_all()

    def _run(self) -> None:
        while True:
            try:
                token = self._queue.get(timeout=0.1)
            except Empty:
                with self._condition:
                    if self._closed and not self._custody:
                        return
                continue
            if token is None:
                self._queue.task_done()
                return
            with self._lock:
                custody = self._custody.get(token)
            if custody is not None:
                if custody.event is None:
                    custody.event = self._execute(custody.command)
                self._settle(token, custody)
            self._queue.task_done()
            with self._condition:
                self._condition.notify_all()

    def _execute(self, request: LifecycleMutationRequest) -> RouteEvent:
        command = request.payload
        if not isinstance(command, (EnsureRouteCommand, DropRouteCommand)):
            raise TypeError("route request payload is not a route command")
        try:
            if isinstance(command, EnsureRouteCommand):
                changed, provenance = self._ensure(
                    command.spec,
                    command.attempt_token,
                    _owner_lease(command.owner_lease, command.spec.route_id),
                    request.expected_resource_token,
                )
                operation = "ensure"
                route_id = command.spec.route_id
            else:
                changed, provenance = self._drop(
                    command.route_id,
                    command.attempt_token,
                    _owner_lease(command.owner_lease, command.route_id),
                    request.expected_resource_token,
                )
                operation = "drop"
                route_id = command.route_id
            return RouteMutationCompleted(
                correlation_id=command.correlation_id,
                attempt_token=command.attempt_token,
                generation=command.generation,
                version=command.version,
                operation=operation,
                route_id=route_id,
                changed=changed,
                provenance=provenance,
            )
        except Exception as error:
            return RouteMutationFailed(
                correlation_id=command.correlation_id,
                attempt_token=command.attempt_token,
                generation=command.generation,
                version=command.version,
                operation=(
                    "ensure" if isinstance(command, EnsureRouteCommand) else "drop"
                ),
                route_id=(
                    command.spec.route_id
                    if isinstance(command, EnsureRouteCommand)
                    else command.route_id
                ),
                code=(
                    "ROUTE_PARTIAL_CLEANUP"
                    if isinstance(error, RoutePartialCleanup)
                    else "ROUTE_IO_FAILED"
                ),
                detail=f"{type(error).__name__}: {error}",
            )

    def _ensure(
        self,
        spec: RouteSpec,
        attempt_token: str,
        owner_lease: str,
        expected_resource_token: str | None,
    ) -> tuple[bool, MutationProvenance]:
        if not spec.route_id or not spec.liveliness_key or not spec.inbox_key:
            raise ValueError("route ids and keys must not be blank")
        with self._lock:
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
                    if (
                        expected_resource_token is not None
                        and incumbent_token != expected_resource_token
                    ):
                        return False, MutationProvenance(
                            False, False, incumbent_token
                        )
                    return False, MutationProvenance(
                        created_by_operation=False,
                        changed=False,
                        resource_token=incumbent_token,
                    )
                lease_token = expected_resource_token or uuid.uuid4().hex
                current.owner_leases[owner_lease] = lease_token
                return True, MutationProvenance(
                    created_by_operation=True,
                    changed=True,
                    resource_token=lease_token,
                )
        if self._registration_factory is not None:
            liveliness, queryable_registration = self._registration_factory(spec)
        else:
            liveliness = (
                self._transport.declare_liveliness(spec.liveliness_key)
                if spec.advertise
                else None
            )
            try:
                queryable_registration = self._transport.declare_queryable(
                    spec.inbox_key, self._handlers(spec.route_id)
                )
            except BaseException:
                if liveliness is not None:
                    liveliness.close()
                raise
        with self._lock:
            incumbent = self._routes.get(spec.route_id)
            if incumbent is not None:
                queryable_registration.close()
                if liveliness is not None:
                    liveliness.close()
                if incumbent.cleanup_attempt is not None:
                    raise RoutePartialCleanup(
                        f"route cleanup belongs to {incumbent.cleanup_attempt}"
                    )
                if incumbent.spec != spec:
                    raise ValueError("route raced with a different spec")
                incumbent_token = incumbent.owner_leases.get(owner_lease)
                if incumbent_token is not None:
                    return False, MutationProvenance(
                        False, False, incumbent_token
                    )
                lease_token = expected_resource_token or uuid.uuid4().hex
                incumbent.owner_leases[owner_lease] = lease_token
                return True, MutationProvenance(True, True, lease_token)
            lease_token = expected_resource_token or uuid.uuid4().hex
            self._routes[spec.route_id] = _RegistrationPair(
                spec,
                liveliness,
                queryable_registration,
                {owner_lease: lease_token},
            )
        return True, MutationProvenance(True, True, lease_token)

    def _drop(
        self,
        route_id: str,
        attempt_token: str,
        owner_lease: str,
        expected_resource_token: str | None,
    ) -> tuple[bool, MutationProvenance]:
        with self._lock:
            registration = self._routes.get(route_id)
            if registration is None:
                return False, MutationProvenance(False, False, f"absent:{route_id}")
            lease_token = registration.owner_leases.get(owner_lease)
            if lease_token is None:
                return False, MutationProvenance(
                    False, False, f"absent:{route_id}:{owner_lease}"
                )
            if (
                expected_resource_token is not None
                and lease_token != expected_resource_token
            ):
                return False, MutationProvenance(
                    False, False, lease_token
                )
            if len(registration.owner_leases) > 1:
                del registration.owner_leases[owner_lease]
                return True, MutationProvenance(True, True, lease_token)
            if (
                registration.cleanup_attempt is not None
                and registration.cleanup_attempt != attempt_token
            ):
                raise RoutePartialCleanup(
                    f"route cleanup belongs to {registration.cleanup_attempt}"
                )
            registration.cleanup_attempt = attempt_token
        self._close_registration(route_id, registration)
        with self._condition:
            if self._routes.get(route_id) is registration:
                del self._routes[route_id]
            self._condition.notify_all()
        return True, MutationProvenance(True, True, lease_token)

    def _close_registration(
        self, route_id: str, registration: _RegistrationPair
    ) -> None:
        errors: list[str] = []
        if not registration.inbox_closed:
            try:
                registration.queryable_registration.close()
            except BaseException as error:
                errors.append(f"inbox:{type(error).__name__}:{error}")
            else:
                with self._condition:
                    registration.inbox_closed = True
                    self._condition.notify_all()
        if registration.liveliness is None:
            registration.liveliness_closed = True
        elif not registration.liveliness_closed:
            try:
                registration.liveliness.close()
            except BaseException as error:
                errors.append(f"liveliness:{type(error).__name__}:{error}")
            else:
                with self._condition:
                    registration.liveliness_closed = True
                    self._condition.notify_all()
        if errors:
            raise RoutePartialCleanup(
                f"partial route cleanup for {route_id}: {'; '.join(errors)}"
            )

    def _settle(self, token: str, custody: _Custody) -> None:
        deadline = time.monotonic() + self._deadline
        attempt = 0
        while True:
            receipt = self._completions.publish(custody.event)  # type: ignore[arg-type]
            if receipt is CompletionReceipt.COMMITTED:
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
                # D22: preserve custody for explicit reassociation.
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            delay = self._backoff[min(attempt, len(self._backoff) - 1)]
            attempt += 1
            time.sleep(min(delay, remaining))


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
            EnsureRouteCommand(token, token, 0, 0, spec, owner_lease)
        )

    def drop(
        self, route_id: str, *, owner_lease: str | None = None
    ) -> RouteMutationCompleted:
        token = f"route-client:drop:{route_id}:{uuid.uuid4().hex}"
        return self._call(
            DropRouteCommand(token, token, 0, 0, route_id, owner_lease)
        )

    def registered(self) -> tuple[str, ...]:
        return self._authority.registered()

    def owners(self, route_id: str) -> tuple[str, ...]:
        return self._authority.owners(route_id)

    def _call(
        self, command: EnsureRouteCommand | DropRouteCommand
    ) -> RouteMutationCompleted:
        waiter = self._router.register(
            command.correlation_id,
            attempt_token=command.attempt_token,
            generation=command.generation,
            versions=frozenset({command.version}),
        )
        admission = self._authority.submit(command)
        if admission is not PortAdmission.ACCEPTED:
            waiter.cancel()
            raise RouteCommandError(
                f"PORT_{admission.value.upper()}",
                f"route command admission is {admission.value}",
            )
        event = waiter.wait(self._timeout)
        if isinstance(event, RouteMutationFailed):
            raise RouteCommandError(event.code, event.detail)
        if not isinstance(event, RouteMutationCompleted):
            raise TypeError("route authority returned an invalid completion")
        deadline = time.monotonic() + self._timeout
        while not self._authority.retire_receipt(
            event.attempt_token, event.provenance.resource_token
        ):
            if time.monotonic() >= deadline:
                raise RouteCommandError(
                    "ROUTE_RECEIPT_UNSETTLED",
                    f"route receipt did not settle: {event.route_id}",
                )
            time.sleep(0.005)
        self._authority.confirm_receipt_retired(
            event.attempt_token, event.provenance.resource_token
        )
        return event


__all__ = [
    "RouteCommandError",
    "RouteRegistrationClient",
    "RouteRegistrationIo",
    "RoutePartialCleanup",
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


def _owner_lease(value: str | None, route_id: str) -> str:
    owner = value.strip() if isinstance(value, str) else ""
    return owner or f"legacy:{route_id}"
