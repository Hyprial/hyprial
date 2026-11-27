"""hc terminal backend internals."""

from .acquisition import HcAcquisitionError
from .acquisition import HcLock
from .acquisition import HcTargetSpec
from .acquisition import ensure_pinned_hc_installed
from .acquisition import installed_hc_executable
from .acquisition import load_bundled_hc_lock
from .acquisition import remove_hc_version_dir
from .client import CoreIdentity
from .client import CoreStatus
from .client import HcClient
from .client import HcEventSubscription
from .client import HcHeartbeatTimeout
from .client import HcRpcError
from .client import HcStreamClosed
from .client import HcStreamEvent
from .client import HcTransportError
from .client import HolderInfo
from .client import SubscribeSnapshot
from .client import TerminalRecord
from .client import TerminalStatus
from .client import probe_socket_alive
from .supervisor import HcGenerationIncompatibleError
from .supervisor import HcSocketPathTooLongError
from .supervisor import PrivateCoreSupervisor
from .supervisor import SupervisorSnapshot

__all__ = [
    "CoreIdentity",
    "CoreStatus",
    "HcAcquisitionError",
    "HcClient",
    "HcEventSubscription",
    "HcHeartbeatTimeout",
    "HcRpcError",
    "HcGenerationIncompatibleError",
    "HcLock",
    "HcSocketPathTooLongError",
    "HcStreamClosed",
    "HcStreamEvent",
    "HcTargetSpec",
    "HcTransportError",
    "HolderInfo",
    "PrivateCoreSupervisor",
    "SubscribeSnapshot",
    "SupervisorSnapshot",
    "TerminalRecord",
    "TerminalStatus",
    "ensure_pinned_hc_installed",
    "installed_hc_executable",
    "load_bundled_hc_lock",
    "probe_socket_alive",
    "remove_hc_version_dir",
]
