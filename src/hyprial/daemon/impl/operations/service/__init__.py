"""Packaged launchd/systemd templates used by the CLI service manager.

A supervisor status probe has one authoritative "not loaded" shape per manager.
``launchctl print gui/$UID/com.hyprial.hyprial.daemon`` returns 113 and includes
``Could not find service "com.hyprial.hyprial.daemon" in domain for user gui: $UID``.
``systemctl --user show hyprial.service`` returns 4 and exactly
``Unit hyprial.service could not be found.`` on stderr. Every other non-zero
result is a manager command failure, including launchctl's exit-5 I/O failure
and systemctl's exit-5 bus failure.
"""

from __future__ import annotations

from hyprial.daemon.impl.bootstrap.user_bus import (
    USER_BUS_ADDRESS_UNREACHABLE_REASON as USER_BUS_ADDRESS_UNREACHABLE_REASON,
    USER_BUS_UNAVAILABLE_REASON as USER_BUS_UNAVAILABLE_REASON,
    UserBusState as UserBusState,
    session_bus_environment as session_bus_environment,
    user_bus_state as user_bus_state,
)
from hyprial.kernel import current_uid as current_uid

from .manager import ServiceManager as ServiceManager
from .templates import ServiceTemplates as ServiceTemplates
from .types import (
    LAUNCHD_LABEL as LAUNCHD_LABEL,
    ServiceCommandError as ServiceCommandError,
    ServiceCommandRunner as ServiceCommandRunner,
    ServiceConfig as ServiceConfig,
    ServiceOwnershipError as ServiceOwnershipError,
    ServicePlatform as ServicePlatform,
    ServiceStatus as ServiceStatus,
    SupervisionState as SupervisionState,
)
