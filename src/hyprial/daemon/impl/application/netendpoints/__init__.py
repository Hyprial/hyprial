"""Cluster package for the netendpoints application surface."""

from hyprial.daemon.impl.application.netendpoints.endpoints import _EndpointResolutionMixin
from hyprial.daemon.impl.application.netendpoints.forwarding import _ForwardingOpsMixin

class _EndpointResolutionComposite(
    _EndpointResolutionMixin,
    _ForwardingOpsMixin,
):
    """Composition of the cluster's parts."""

