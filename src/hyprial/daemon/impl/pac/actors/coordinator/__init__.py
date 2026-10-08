"""Run-owned actor reconciliation over the existing hyprial lifecycle path.

PAC persists direction and operation identity before calling the runtime port.
The port is deliberately the same start/down control plane used by the daemon;
this module is a reconciler, not another process launcher.

``ActorCoordinator`` keeps its state here; the mixins in ``_coordinator_core``,
``_coordinator_up`` and ``_coordinator_down`` only contribute methods.
"""

from __future__ import annotations

from hyprial.daemon.impl.pac.actors.coordinator.core import _CoordinatorCore
from hyprial.daemon.impl.pac.actors.coordinator.stop import _CoordinatorDown
from hyprial.daemon.impl.pac.actors.coordinator.start import _CoordinatorUp
from hyprial.daemon.impl.pac.actors.coordinator.types import (
    ActorRuntime,
    CLEANUP_ATTEMPT_LIMIT,
    FileLaunchResolver,
    LaunchResolver,
    LaunchSpec,
    ResolvedLaunch,
    RuntimeObservation,
    now_ms,
    request_actor_stop,
    request_actor_wake,
)

__all__ = [
    "ActorCoordinator",
    "CLEANUP_ATTEMPT_LIMIT",
    "LaunchSpec",
    "ResolvedLaunch",
    "RuntimeObservation",
    "FileLaunchResolver",
    "ActorRuntime",
    "LaunchResolver",
    "now_ms",
    "request_actor_stop",
    "request_actor_wake",
]


class ActorCoordinator(_CoordinatorCore, _CoordinatorUp, _CoordinatorDown):
    """Reconcile current activation direction against one observed connector."""
