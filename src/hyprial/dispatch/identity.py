"""Stable daemon identity used for durable dispatch and PAC notifications."""

from __future__ import annotations

from uuid import UUID, uuid5

from hyprial.uri import canonical_agent_uri

# This value predates PAC v2 and is already present in durable sender fields.
# Moving its ownership must not rewrite that history or silently create a new
# sender, so the wire value remains unchanged while its definition becomes
# independent of the legacy workflow/agent.task package.
DISPATCH_SERVICE_ACTOR_NAME = "mfu-coordinator"


def dispatch_service_actor_uri(owner: str, node_id: str) -> str:
    """Return the canonical identity shared by daemon-owned dispatch IO."""

    return canonical_agent_uri(owner, node_id, DISPATCH_SERVICE_ACTOR_NAME)


__all__ = ["DISPATCH_SERVICE_ACTOR_NAME", "dispatch_service_actor_uri"]


MESSAGE_NAMESPACE = UUID("1329686b-1adf-5e69-b835-4e05b214cdd6")


def dispatch_message_id(effect_id: str) -> str:
    """Stable outbound identity; shared without importing an inbox read plane."""
    if not effect_id:
        raise ValueError("effect_id must not be empty")
    return f"workflow-{uuid5(MESSAGE_NAMESPACE, effect_id)}"
