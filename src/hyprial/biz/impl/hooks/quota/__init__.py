"""biz/hooks/quota (impl): the quota policy as a unified-hooks consumer (§3, H4)."""

from hyprial.biz.impl.hooks.quota.consumer import (
    QUOTA_HOOK_EVENTS,
    USAGE_LIMIT_FAILURE_CODE,
    QuotaHookConsumer,
    QuotaWatchdogAuthority,
)
from hyprial.biz.impl.hooks.quota.policy import (
    FAILURE_HOLD_MS,
    FAILURE_REPORTED_TTL_MS,
    LEVEL_EXHAUSTED,
    LEVEL_NORMAL,
    LEVEL_WARNING,
    KIND_EXHAUSTED,
    KIND_RECOVERED,
    KIND_WARNING,
    QuotaAlert,
    QuotaWatchdog,
    STATE_FILE_NAME,
    STATE_SCHEMA_VERSION,
    WARN_RATIO,
)

__all__ = [
    "FAILURE_HOLD_MS",
    "FAILURE_REPORTED_TTL_MS",
    "KIND_EXHAUSTED",
    "KIND_RECOVERED",
    "KIND_WARNING",
    "LEVEL_EXHAUSTED",
    "LEVEL_NORMAL",
    "LEVEL_WARNING",
    "QUOTA_HOOK_EVENTS",
    "QuotaAlert",
    "QuotaHookConsumer",
    "QuotaWatchdog",
    "QuotaWatchdogAuthority",
    "STATE_FILE_NAME",
    "STATE_SCHEMA_VERSION",
    "USAGE_LIMIT_FAILURE_CODE",
    "WARN_RATIO",
]
