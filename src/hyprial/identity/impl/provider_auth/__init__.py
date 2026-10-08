"""Provider OAuth failure handling: relogin flow, owner alert, dispatch mark."""

from hyprial.identity.impl.provider_auth.classification import (
    DEVICE_CODE_PROVIDERS,
    ProviderFailureClass,
    classify_provider_failure,
)
from hyprial.identity.impl.provider_auth.coordinator._base import (
    MAX_ROUNDS,
    REASON_PROVIDER_AUTH_ACCOUNT,
    REASON_PROVIDER_AUTH_INVALID,
    RELOGIN_COOLDOWN_SECONDS,
    )
from hyprial.identity.impl.provider_auth.coordinator._coordinator import (
    ProviderAuthCoordinator,
)
from hyprial.identity.impl.provider_auth.helper import (
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
