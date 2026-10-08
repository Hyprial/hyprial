"""Additive public status contract for Lark inbound recovery coverage."""

from __future__ import annotations


def lark_recovery_coverage() -> dict[str, str]:
    """Return the honest recovery boundary exposed by status and Doctor.

    Reconciliation currently scans only chats already present in local adapter
    state. Chat enumeration is intentionally not wired in, so arbitrary
    unknown first-chat messages cannot be promised.
    """

    return {
        "knownChatsRecovery": "supported",
        "unknownFirstChatRecovery": "unsupported",
        "chatEnumeration": "not-implemented",
    }
