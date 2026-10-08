"""Cluster package for the inbox surface application surface."""

from hyprial.daemon.impl.application.messaging.inbox_surface.send import _MessageSendMixin
from hyprial.daemon.impl.application.messaging.inbox_surface.queries import _MessageQueriesMixin

class _MessageSendComposite(
    _MessageSendMixin,
    _MessageQueriesMixin,
):
    """Composition of the cluster's parts."""

