"""Adapter-facing identity projections; these values do not own persistence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


OrganizationStatus = Literal["active", "legacy-unresolved", "revoked"]
AuthorizationStatus = Literal["fresh", "stale", "revoked", "unavailable"]


@dataclass(frozen=True, slots=True)
class OrgRef:
    """Stable affiliation plus the expiring authorization used to act on it."""

    organization_id: str | None = None
    status: OrganizationStatus = "legacy-unresolved"
    generation: int = 0
    evidence_ref: str | None = None
    evidence_digest: str | None = None
    membership_revision: str | None = None
    policy_revision: str | None = None
    authorization_status: AuthorizationStatus = "unavailable"
    authorization_lease_generation: int = 0
    authorization_issued_at_ms: int | None = None
    authorization_expires_at_ms: int | None = None
    authorization_max_staleness_ms: int | None = None
    authorization_scope_digest: str | None = None
    source_frontier_digest: str | None = None

    def __post_init__(self) -> None:
        if self.generation < 0 or self.authorization_lease_generation < 0:
            raise ValueError("organization generations must not be negative")
        if self.status == "active":
            required = (
                self.organization_id,
                self.evidence_ref,
                self.evidence_digest,
                self.membership_revision,
                self.policy_revision,
                self.authorization_scope_digest,
                self.source_frontier_digest,
            )
            if any(not value for value in required):
                raise ValueError("active organization association is incomplete")
            if self.generation <= 0 or self.authorization_lease_generation <= 0:
                raise ValueError("active organization generations must be positive")
            if self.authorization_status not in {"fresh", "stale", "revoked"}:
                raise ValueError("active organization authorization status is invalid")
            issued = self.authorization_issued_at_ms
            expires = self.authorization_expires_at_ms
            maximum = self.authorization_max_staleness_ms
            if issued is None or expires is None or maximum is None:
                raise ValueError("active organization lease timestamps are required")
            if not 1_000 <= maximum <= 300_000:
                raise ValueError("organization lease max staleness is invalid")
            if expires <= issued or expires - issued > maximum:
                raise ValueError("organization lease lifetime is invalid")
        elif self.organization_id is not None and self.status == "legacy-unresolved":
            raise ValueError(
                "legacy-unresolved association cannot name an organization"
            )

    @classmethod
    def legacy(cls) -> OrgRef:
        return cls()

    @property
    def is_active(self) -> bool:
        return self.status == "active" and self.organization_id is not None


@dataclass(frozen=True, slots=True)
class AgentIdentitySnapshot:
    actor: str
    owner: str
    machine: str
    entity_token: str
    organization: OrgRef = OrgRef()

    def __post_init__(self) -> None:
        if not all((self.actor, self.owner, self.machine, self.entity_token)):
            raise ValueError("agent identity fields must not be blank")

    def public_payload(self, *, agent_uri: str) -> dict[str, object]:
        return {
            "agentUri": agent_uri,
            "orgId": self.organization.organization_id,
            "associationGeneration": self.organization.generation,
        }
