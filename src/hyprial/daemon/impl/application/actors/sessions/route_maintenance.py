"""Small route-maintenance helpers used by the Session route mixin."""

from __future__ import annotations

from hyprial.daemon.impl.network.route_ports import RouteSpec
from hyprial.daemon.impl.transport import KeySpace


class _SessionRouteMaintenanceMixin:
    def _interactive_route_needs_ensure(
        self, actor: str, session_ref: str | None
    ) -> bool:
        routes = self._routes
        if routes is None or actor == self.node_id:
            return False
        lease = self._interactive_route_lease(actor, session_ref)
        return lease not in routes.owners(actor)

    def _close_other_interactive_routes_effect(
        self,
        actor: str,
        session_ref: str,
        *,
        operation_id: str,
        generation: int,
        version: int,
    ) -> None:
        routes = self._routes
        if routes is None:
            return
        current_owner = self._interactive_route_lease(actor, session_ref)
        for index, owner in enumerate(tuple(routes.owners(actor))):
            if owner.startswith("session:") and owner != current_owner:
                self._settled_route_mutation(
                    routes,
                    operation_id=f"{operation_id}:{index}",
                    generation=generation,
                    version=version,
                    actor=actor,
                    owner_lease=owner,
                )

    @staticmethod
    def _interactive_route_lease(actor: str, session_ref: str | None) -> str:
        return f"session:{session_ref or f'legacy:{actor}'}"

    @staticmethod
    def _actor_route_spec(actor: str, *, advertise: bool = True) -> RouteSpec:
        keys = KeySpace()
        return RouteSpec(
            route_id=actor,
            liveliness_key=keys.actor_liveliness(actor),
            inbox_key=keys.inbox_all(actor),
            advertise=advertise,
        )
