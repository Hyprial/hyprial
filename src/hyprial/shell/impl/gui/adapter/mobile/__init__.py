"""Authoritative mobile device access for the App Core host.

The pairing lifecycle, per-device revocation state, explicit per-session
grants and the persistent replay window all live in one SQLite authority
(:mod:`store`).  Nothing in this package treats a tailnet address or a
display name as authentication: the host operator approves a device public
key, and every later call is checked against that durable record.
"""

from hyprial.shell.impl.gui.adapter.mobile.models import (
    MobileAccessError,
    SUPPORTED_PERMISSIONS,
)
from hyprial.shell.impl.gui.adapter.mobile.store import MobileAccessStore

__all__ = ["MobileAccessError", "MobileAccessStore", "SUPPORTED_PERMISSIONS"]
