"""Cluster package for the delivery application surface."""

from hyprial.daemon.impl.application.messaging.delivery.lark import _LarkGatewayMixin
from hyprial.daemon.impl.application.messaging.delivery.sends import _UserDeliveryMixin
from hyprial.daemon.impl.application.messaging.delivery.status_queries import _DeliveryStatusMixin

class _LarkGatewayComposite(
    _LarkGatewayMixin,
    _UserDeliveryMixin,
    _DeliveryStatusMixin,
):
    """Composition of the cluster's parts."""

