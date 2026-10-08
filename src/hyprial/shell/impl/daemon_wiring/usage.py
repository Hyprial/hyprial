"""Compose the optional business usage collector above the daemon."""

from hyprial.biz import UsageAuthority, usage_collection_disabled
from hyprial.daemon import network_isolated_from_environment


def usage_factory():
    """Do not allocate collectors for disabled or network-isolated startup."""
    if network_isolated_from_environment() or usage_collection_disabled():
        return None
    return UsageAuthority()
