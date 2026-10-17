"""Stable daemon identity used for durable dispatch and PAC notifications."""

from __future__ import annotations

from uuid import UUID, uuid5

from hyprial.kernel import DISPATCH_SERVICE_ACTOR_NAME, canonical_agent_uri

# Re-exported from the kernel contract so the identity registration guard can
# refuse the same reserved name without importing daemon (README ruling 63).
__all__ = ["DISPATCH_SERVICE_ACTOR_NAME", "dispatch_service_actor_uri"]


def dispatch_service_actor_uri(owner: str, node_id: str) -> str:
    """Return the canonical identity shared by daemon-owned dispatch IO."""

    return canonical_agent_uri(owner, node_id, DISPATCH_SERVICE_ACTOR_NAME)


MESSAGE_NAMESPACE = UUID("1329686b-1adf-5e69-b835-4e05b214cdd6")


def dispatch_message_id(effect_id: str) -> str:
    """Stable outbound identity; shared without importing an inbox read plane."""
    if not effect_id:
        raise ValueError("effect_id must not be empty")
    return f"workflow-{uuid5(MESSAGE_NAMESPACE, effect_id)}"
