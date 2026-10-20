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
    CLEANUP_RETRIES_PER_SWEEP,
    CLEANUP_RETRY_BASE_MS,
    CLEANUP_RETRY_CAP_MS,
    cleanup_retry_delay_ms,
    FileLaunchResolver,
    LaunchResolver,
    LaunchSpec,
    ResolvedLaunch,
    RuntimeObservation,
    WORKER_IN_USE,
    cleanup_operation_base,
    cleanup_stop_operation,
    now_ms,
    request_actor_stop,
    record_cleanup_reconcile_failure,
    request_actor_wake,
    throttle_cleanup_retries,
    worker_in_use,
)

__all__ = [
    "ActorCoordinator",
    "CLEANUP_ATTEMPT_LIMIT",
    "CLEANUP_RETRIES_PER_SWEEP",
    "CLEANUP_RETRY_BASE_MS",
    "CLEANUP_RETRY_CAP_MS",
    "cleanup_retry_delay_ms",
    "LaunchSpec",
    "ResolvedLaunch",
    "RuntimeObservation",
    "FileLaunchResolver",
    "ActorRuntime",
    "LaunchResolver",
    "WORKER_IN_USE",
    "worker_in_use",
    "throttle_cleanup_retries",
    "record_cleanup_reconcile_failure",
    "cleanup_operation_base",
    "cleanup_stop_operation",
    "now_ms",
    "request_actor_stop",
    "request_actor_wake",
]


class ActorCoordinator(_CoordinatorCore, _CoordinatorUp, _CoordinatorDown):
    """Reconcile current activation direction against one observed connector."""
