"""Cluster package for the agents admin application surface."""

from hyprial.daemon.impl.application.actors.agents_admin.registry_ops import _AgentRegistryOpsMixin
from hyprial.daemon.impl.application.actors.agents_admin.adapters import _AdapterAdminMixin
from hyprial.daemon.impl.application.actors.agents_admin.migration import _AgentMigrationMixin
from hyprial.daemon.impl.application.actors.agents_admin.destroys import _AgentDestroyMixin
from hyprial.daemon.impl.application.actors.agents_admin.readiness import _AgentReadinessMixin

class _AgentRegistryOpsComposite(
    _AgentRegistryOpsMixin,
    _AdapterAdminMixin,
    _AgentMigrationMixin,
    _AgentDestroyMixin,
    _AgentReadinessMixin,
):
    """Composition of the cluster's parts."""

