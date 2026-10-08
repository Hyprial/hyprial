"""Daemon lifecycle, desired state, supervision, diagnostics and services."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hyprial.daemon.impl.application import DaemonApplication
    from hyprial.daemon.impl.runtime import DaemonRecoverySummary, DaemonEventBridge, ReconcileSummary
    from hyprial.daemon.impl.processes.supervisor import HarnessRestoreSummary, ManagedHarnessRuntime

from hyprial.kernel import (
    ManagedHarnessProcess,
    )
from hyprial.daemon.impl.api import (
    HarnessDelivery,
    HarnessLauncher,
    HarnessResult,
    HarnessResultStatus,
    StreamingHarnessProcess,
)
from hyprial.daemon.impl.desired_state import (
    DesiredState,
    DesiredStateStore,
    InteractiveSession,
    ZenohEndpoints,
)
from hyprial.kernel  import DesiredStateError
from hyprial.kernel  import HarnessLaunchSpec
from hyprial.daemon.impl.operations.service import (
    LAUNCHD_LABEL,
    ServiceConfig,
    ServiceManager,
    ServiceStatus,
    ServiceTemplates,
)

__all__ = [
    "LAUNCHD_LABEL",
    "DaemonApplication",
    "DaemonRecoverySummary",
    "DaemonEventBridge",
    "DesiredState",
    "DesiredStateError",
    "DesiredStateStore",
    "InteractiveSession",
    "ManagedHarnessProcess",
    "HarnessDelivery",
    "HarnessLaunchSpec",
    "HarnessLauncher",
    "HarnessRestoreSummary",
    "HarnessResult",
    "HarnessResultStatus",
    "ManagedHarnessRuntime",
    "ReconcileSummary",
    "ServiceConfig",
    "ServiceManager",
    "ServiceStatus",
    "ServiceTemplates",
    "StreamingHarnessProcess",
    "ZenohEndpoints",
]


def __getattr__(name: str):
    # Guest MCP clients read the state contracts but never instantiate the
    # daemon or its native mesh transport. Keep the public facade compatible.
    if name == "DaemonApplication":
        from .application import DaemonApplication as value
    elif name == "DaemonRecoverySummary":
        from .runtime import DaemonRecoverySummary as value
    elif name == "DaemonEventBridge":
        from .runtime import DaemonEventBridge as value
    elif name == "ReconcileSummary":
        from .runtime import ReconcileSummary as value
    elif name == "HarnessRestoreSummary":
        from .processes.supervisor import HarnessRestoreSummary as value
    elif name == "ManagedHarnessRuntime":
        from .processes.supervisor import ManagedHarnessRuntime as value
    else:
        raise AttributeError(name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
