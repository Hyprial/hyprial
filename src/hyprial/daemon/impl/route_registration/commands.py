"""Route registration command vocabulary: protocols, cleanup errors and the control-plane message types."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
import threading
from dataclasses import dataclass, field
from typing import Callable, Protocol
from hyprial.daemon.impl.transport.api import Registration
from hyprial.daemon.impl.correlation.correlation  import CompletionReceipt
from hyprial.kernel import LifecycleMutationRequest
from hyprial.daemon.impl.network.route_ports  import (
    DropRouteCommand,
    EnsureRouteCommand,
    RouteEvent,
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
