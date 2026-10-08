"""Cluster package for the sessions application surface."""

from hyprial.daemon.impl.application.actors.sessions.routes import _SessionRoutesMixin
from hyprial.daemon.impl.application.actors.sessions.registry import _SessionRegistryMixin

class _SessionRoutesComposite(
    _SessionRoutesMixin,
    _SessionRegistryMixin,
):
    """Composition of the cluster's parts."""

