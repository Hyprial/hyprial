"""RouteRegistrationClient and the lifecycle request helpers it submits."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
import time
import uuid
from hyprial.kernel import PortAdmission
from hyprial.daemon.impl.correlation.correlation  import CorrelationEventRouter
from hyprial.kernel import LifecycleMutationRequest
from hyprial.daemon.impl.network.route_ports  import (
    DropRouteCommand,
    EnsureRouteCommand,
    RouteMutationCompleted,
    RouteMutationFailed,
    RouteSpec,
)

from .commands import (
    RouteCommandError,
    _route_request,
)
from .io import (
    RouteRegistrationIo,
)


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
]
