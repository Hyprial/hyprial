# Decision record (provider-key contract freeze r3, docs/design/provider-key-contract-freeze-2026-11-03.md)
# - This module is the single seam between the provider-key slices: IPC, CLI-facing
#   handlers, launch injection and the legacy agent.secret adapter all depend on
#   these names only, never on service internals.
# - Views are plain JSON dicts whose keys are frozen by contract §5.1; they never
#   contain secret material, DEKs or private keys.
# - Agent identity is the incarnation (F-3): AgentRef.agent is the canonical UUID of
#   the registry entity_token; a rebuilt agent is a different AgentRef.
"""Shared provider-key service seam: method names, agent reference and service protocol."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

USER_PROVIDER_LOGIN = "user.provider.login"
USER_PROVIDER_LIST = "user.provider.list"
USER_PROVIDER_LOGOUT = "user.provider.logout"
USER_PROVIDER_ROTATE = "user.provider.rotate"
AGENT_PROVIDER_ALLOW = "agent.provider.allow"
AGENT_PROVIDER_REVOKE = "agent.provider.revoke"
AGENT_PROVIDER_STATUS = "agent.provider.status"

PROVIDER_KEY_METHODS = frozenset({
    USER_PROVIDER_LOGIN, USER_PROVIDER_LIST, USER_PROVIDER_LOGOUT, USER_PROVIDER_ROTATE,
    AGENT_PROVIDER_ALLOW, AGENT_PROVIDER_REVOKE, AGENT_PROVIDER_STATUS,
})

GRANT_TTL_MS = 24 * 60 * 60 * 1000
CAS_ATTEMPTS = 3

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class AgentRef:
    """One registry snapshot of a local agent incarnation (contract §8.3 step 2)."""

    actor: str
    uri: str
    created_at_ms: int
    entity_token: str
    agent: str
    agent_id: str


@dataclass(frozen=True)
class LaunchSecret:
    """One unsealed grant for a single launch; value never leaves the daemon process."""

    grant_id: str
    provider: str
    seq: int
    environment_names: tuple[str, ...]
    value: str = ""

    def __repr__(self) -> str:
        return (f"LaunchSecret(grant_id={self.grant_id!r}, provider={self.provider!r}, "
                f"seq={self.seq!r}, environment_names={self.environment_names!r})")

    def environment(self) -> dict[str, str]:
        return {name: self.value for name in self.environment_names}


class ProviderKeyService(Protocol):
    """Owner-local operations over the single owner-scope store (contract §4, §5)."""

    def login(self, *, provider: str, kind: str, secret: str, label: str | None = None,
              environment_names: Sequence[str] | None = None,
              legacy_ref: Mapping[str, str] | None = None) -> JsonObject:
        """Create one key; return {"key": ProviderKeyView}. Completes matching awaiting-key grants."""
        ...

    def list_keys(self, *, provider: str | None = None, include_deleted: bool = False) -> JsonObject:
        """Return {"keys": [ProviderKeyView], "invalid": [{"keyId", "code"}]}."""
        ...

    def logout(self, *, key_id: str) -> JsonObject:
        """Revoke every grant on the key, tombstone it; return the §5.2 logout result."""
        ...

    def rotate(self, *, key_id: str, replace_secret: str | None = None) -> JsonObject:
        """Reseal (DEK, optionally plaintext) and reissue active grants; return {"key", "reissuedGrants"}."""
        ...

    def allow(self, *, agent: AgentRef, provider: str, key_id: str | None = None,
              environment_names: Sequence[str] | None = None, replace: bool = False,
              grant_id: str | None = None, legacy_ref: Mapping[str, Any] | None = None) -> JsonObject:
        """Write slot grant_id (default provider) active; return {"grant": GrantView, "changed": bool}."""
        ...

    def put_awaiting(self, *, agent: AgentRef, grant_id: str, provider: str,
                     environment_names: Sequence[str], legacy_ref: Mapping[str, Any]) -> JsonObject:
        """Legacy grant before the entry exists: write slot as awaiting-key; return {"grant", "changed"}."""
        ...

    def revoke(self, *, agent: AgentRef, grant_id: str) -> JsonObject:
        """Revoke slot grant_id; return {"grant": GrantView, "changed": bool}."""
        ...

    def status(self, *, agent: AgentRef, provider: str | None = None) -> JsonObject:
        """Return {"agent", "agentId", "actor", "grants": [GrantStatus]} without decrypting payloads."""
        ...

    def grants_for(self, agent: AgentRef) -> list[JsonObject]:
        """Verified grant-state payloads stored for this incarnation; invalid records are omitted."""
        ...

    def find_key_by_legacy_ref(self, *, entry_id: str, field_name: str) -> JsonObject | None:
        """The active ProviderKeyView whose legacyRef matches, or None."""
        ...

    def launch_secrets(self, agent: AgentRef, *, now_ms: int) -> tuple[frozenset[str], tuple[LaunchSecret, ...]]:
        """Slots held by the new authority and their unsealed secrets; raises on any refusal (§7.3)."""
        ...
