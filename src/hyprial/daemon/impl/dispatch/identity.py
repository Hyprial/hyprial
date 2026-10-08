"""Stable daemon identity used for durable dispatch and PAC notifications."""

from __future__ import annotations

from uuid import UUID, uuid5

from hyprial.kernel import canonical_agent_uri

# Renamed from the retired ``mfu-coordinator`` without an alias: durable
# records and peers still carrying the old sender are not recognised.
DISPATCH_SERVICE_ACTOR_NAME = "dispatch-coordinator"


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
