"""Fixed read-effect queries; mutations remain in actor command dispatch."""
from __future__ import annotations

from hyprial.identity.impl.agents.runtime.grants import CapabilityGrant, GrantJournalEntry
from hyprial.identity.impl.agents.runtime.secrets import SecretGrant
from hyprial.identity.impl.agents.home.provisioner import HomeReceipt, WorkspaceSummary


class _AgentReadQueriesMixin:
    def read_capability_grants(
        self, actor: str | None = None
    ) -> tuple[CapabilityGrant, ...]:
        return self._read_effects.capability_grants(actor)

    def read_secret_inventory(
        self, actor: str | None = None, *, expected_uri: str | None = None,
        expected_created_at_ms: int | None = None,
    ) -> tuple[SecretGrant, ...]:
        return self._read_effects.secret_inventory(actor, expected_uri=expected_uri,
                                                   expected_created_at_ms=expected_created_at_ms)

    def prevalidate_home(self, actor: str) -> HomeReceipt:
        return self._read_effects.validate_home(actor)

    def read_grant_journal(self, actor: str) -> tuple[GrantJournalEntry, ...]:
        return self._read_effects.grant_journal(actor)

    def read_workspace_summary(self, actor: str) -> WorkspaceSummary:
        return self._read_effects.workspace_summary(actor)

    def resolve_workspace_path(self, actor: str) -> str:
        return self._read_effects.workspace_path(actor)
