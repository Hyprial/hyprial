"""Provider OAuth failure handling: relogin flow, owner alert, dispatch mark."""

from .classification import (
    DEVICE_CODE_PROVIDERS,
    ProviderFailureClass,
    classify_provider_failure,
)
from .coordinator import (
    MAX_ROUNDS,
    REASON_PROVIDER_AUTH_ACCOUNT,
    REASON_PROVIDER_AUTH_INVALID,
    RELOGIN_COOLDOWN_SECONDS,
    ProviderAuthCoordinator,
)
from .helper import (
    DeviceCodeAnnouncement,
    DeviceLoginRunner,
    HelperOutcome,
    announcement_from,
    find_pi_package_root,
    parse_helper_line,
)

__all__ = [
    "DEVICE_CODE_PROVIDERS",
    "MAX_ROUNDS",
    "REASON_PROVIDER_AUTH_ACCOUNT",
    "REASON_PROVIDER_AUTH_INVALID",
    "RELOGIN_COOLDOWN_SECONDS",
    "DeviceCodeAnnouncement",
    "DeviceLoginRunner",
    "HelperOutcome",
    "ProviderAuthCoordinator",
    "ProviderFailureClass",
    "announcement_from",
    "classify_provider_failure",
    "find_pi_package_root",
    "parse_helper_line",
]
