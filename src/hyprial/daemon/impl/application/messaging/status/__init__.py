"""Cluster package for the status application surface."""

from hyprial.daemon.impl.application.messaging.status.views import _StatusViewsMixin
from hyprial.daemon.impl.application.messaging.status.snapshots import _WorkerSnapshotMixin

class _StatusViewsComposite(
    _StatusViewsMixin,
    _WorkerSnapshotMixin,
):
    """Composition of the cluster's parts."""

