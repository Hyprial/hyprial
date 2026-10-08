"""Cluster package for the wiring application surface."""

from hyprial.daemon.impl.application.wiring.transport import _WiringTransportMixin
from hyprial.daemon.impl.application.wiring.services import _WiringServicesMixin

class _WiringTransportComposite(
    _WiringTransportMixin,
    _WiringServicesMixin,
):
    """Composition of the cluster's parts."""

