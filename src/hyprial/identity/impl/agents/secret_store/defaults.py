"""Non-secret default references and agent-owned consumer grant metadata."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hyprial.identity.impl.agents.runtime.secrets import (
    SecretGrant, SecretResolutionError, SecretSource,
)

CLAUDE_AUTH_NAMES = frozenset({
    "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
})
OAUTH_FIELD = "CLAUDE_CODE_OAUTH_TOKEN"


def initialize_shared_grants(connection: sqlite3.Connection) -> None:
    """Upgrade the source CHECK without dropping existing grant metadata."""
    metadata = tuple(connection.execute("PRAGMA table_info(agent_secret_grants)"))
    columns = {row[1] for row in metadata}
    if "owner_actor" not in columns:
        # Preserve historical NULL fences as stale grants, never rebind them to
        # the healed agent incarnation. Normal registries retain NOT NULL.
        token_constraint = "NOT NULL" if next(row[3] for row in metadata if row[1] == "entity_token") else ""
        connection.execute("ALTER TABLE agent_secret_grants RENAME TO old_secret_grants")
        connection.execute(
            "CREATE TABLE agent_secret_grants ("
            f"agent TEXT NOT NULL, entity_token TEXT {token_constraint}, grant_id TEXT NOT NULL, "
            "source TEXT NOT NULL CHECK (source IN ('user-provider','agent-private','agent-shared')), "
            "entry_id TEXT NOT NULL, field_name TEXT, environment_names TEXT NOT NULL, "
            "revision INTEGER NOT NULL CHECK (revision > 0), "
            "owner_actor TEXT, owner_entity_token TEXT, "
            "CHECK ((source='agent-shared' AND owner_actor IS NOT NULL AND owner_entity_token IS NOT NULL) "
            "OR (source!='agent-shared' AND owner_actor IS NULL AND owner_entity_token IS NULL)), "
            "PRIMARY KEY (agent, grant_id), "
            "FOREIGN KEY (agent, entity_token) REFERENCES agents(actor, entity_token) ON DELETE CASCADE)"
        )
        connection.execute(
            "INSERT INTO agent_secret_grants "
            "(agent,entity_token,grant_id,source,entry_id,field_name,environment_names,revision) "
            "SELECT agent,entity_token,grant_id,source,entry_id,field_name,environment_names,revision "
            "FROM old_secret_grants"
        )
        connection.execute("DROP TABLE old_secret_grants")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS agent_secret_defaults ("
        "harness TEXT PRIMARY KEY CHECK(harness='claude'), owner_actor TEXT NOT NULL, "
        "owner_entity_token TEXT NOT NULL, entry_id TEXT NOT NULL, field_name TEXT NOT NULL)"
    )
    if "kind" not in {row[1] for row in connection.execute("PRAGMA table_info(agent_secret_defaults)")}:
        connection.execute("ALTER TABLE agent_secret_defaults ADD COLUMN kind TEXT NOT NULL DEFAULT 'setup-token'")


def read_shared_value(registry, grant: SecretGrant) -> str:
    """Read only the explicitly named owner's current SQLite value; no migration."""
    from hyprial.identity.impl.agents.secret_store.store import AgentSecretStore
    from hyprial.identity.impl.agents.registry._base import AgentError
    from hyprial.identity.impl.agents.home.provisioner import AgentHomeError

    try:
        owner = registry.require(grant.owner_actor)
        if owner.entity_token != grant.owner_entity_token:
            raise SecretResolutionError("owner-stale", grant.entry_id, "authorize")
        receipt = registry.home_receipt(owner.actor)
        if receipt.entity_token != owner.entity_token:
            raise SecretResolutionError("owner-stale", grant.entry_id, "authorize")
        store = AgentSecretStore(Path(receipt.path), read_only=True)
        secret = store.get(grant.entry_id, grant.field_name or "VALUE")
    except SecretResolutionError:
        raise
    except (ValueError, OSError, sqlite3.Error, AgentError, AgentHomeError):
        raise SecretResolutionError("store-unavailable", grant.entry_id, "shared-read") from None
    if secret is None or not secret.value:
        raise SecretResolutionError("missing", grant.entry_id, "shared-read")
    return secret.value


class DefaultCredentialMixin:
    """Runs on the registry owner, using its transaction and incarnation fences."""

    def default_claude_credential(self) -> dict[str, str] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM agent_secret_defaults WHERE harness='claude'").fetchone()
        if row is None:
            return None
        return {"ownerActor": row["owner_actor"], "ownerEntityToken": row["owner_entity_token"],
                "entryId": row["entry_id"], "fieldName": row["field_name"], "kind": row["kind"]}

    def clear_default_claude_credential(self) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM agent_secret_defaults WHERE harness='claude'")

    def validate_default_claude_credential(self, owner_actor: str, entry_id: str) -> str:
        owner = self.require(owner_actor)
        grant = SecretGrant(owner.actor, owner.entity_token, "default-claude", SecretSource.AGENT_SHARED,
                            entry_id, OAUTH_FIELD, (OAUTH_FIELD,), 1, owner.actor, owner.entity_token)
        read_shared_value(self, grant)
        return owner.entity_token

    def set_default_claude_credential(
        self, owner_actor: str, entry_id: str, *, prevalidated_owner_token: str | None = None,
        kind: str = "setup-token",
    ) -> dict[str, str]:
        if kind not in {"setup-token", "native"}:
            raise ValueError("unsupported Claude credential source")
        owner = self.require(owner_actor)
        if owner.hosted_by is not None or owner.owner != self.owner:
            raise ValueError("default credential must belong to a local owned agent")
        grant = SecretGrant(owner.actor, owner.entity_token, "default-claude", SecretSource.AGENT_SHARED,
                            entry_id, OAUTH_FIELD, (OAUTH_FIELD,), 1, owner.actor, owner.entity_token)
        # Verify the exact authorized source; never discover other stores/files.
        if prevalidated_owner_token is None:
            if kind == "native":
                from hyprial.identity.impl.agents.home.native_credential import read_native_credential_set
                read_native_credential_set(Path(self.home_receipt(owner.actor).path), "claude")
            else:
                read_shared_value(self, grant)
        elif prevalidated_owner_token != owner.entity_token:
            raise ValueError("default credential owner incarnation changed")
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            if self.require(owner.actor).entity_token != owner.entity_token:
                raise ValueError("default credential owner incarnation changed")
            self._db.execute(
                "INSERT INTO agent_secret_defaults VALUES ('claude',?,?,?,?,?) "
                "ON CONFLICT(harness) DO UPDATE SET owner_actor=excluded.owner_actor, "
                "owner_entity_token=excluded.owner_entity_token, entry_id=excluded.entry_id, "
                "field_name=excluded.field_name, kind=excluded.kind",
                (owner.actor, owner.entity_token, entry_id, OAUTH_FIELD, kind),
            )
        return {"ownerActor": owner.actor, "ownerEntityToken": owner.entity_token,
                "entryId": entry_id, "fieldName": OAUTH_FIELD, "kind": kind}

    def _grant_default_claude_locked(self, agent) -> None:
        if (agent.preferred_harness != "claude" or agent.hosted_by is not None
                or agent.owner != self.owner):
            return
        default = self.default_claude_credential()
        if default is None:
            return
        for row in self._db.execute(
            "SELECT environment_names FROM agent_secret_grants WHERE agent=?", (agent.actor,)
        ):
            if CLAUDE_AUTH_NAMES.intersection(json.loads(row[0])):
                return
        owner = self.require(default["ownerActor"])
        if owner.entity_token != default["ownerEntityToken"]:
            raise ValueError("default Claude credential owner incarnation changed; designate it again")
        if default["kind"] == "native":
            from hyprial.identity.impl.agents.home.native_credential import (
                read_native_credential_set, install_native_credential_set,
            )
            documents = read_native_credential_set(Path(self.home_receipt(owner.actor).path), "claude")
            install_native_credential_set(Path(self.home_receipt(agent.actor).path), "claude", documents)
            return
        self._db.execute(
            "INSERT INTO agent_secret_grants "
            "(agent,entity_token,grant_id,source,entry_id,field_name,environment_names,revision,"
            "owner_actor,owner_entity_token) VALUES (?,?,'default-claude','agent-shared',?,?,?,1,?,?)",
            (agent.actor, agent.entity_token, default["entryId"], OAUTH_FIELD,
             json.dumps([OAUTH_FIELD]), owner.actor, owner.entity_token),
        )
