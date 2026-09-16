"""Stable daemon identity used for durable dispatch and PAC notifications."""

from __future__ import annotations

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
