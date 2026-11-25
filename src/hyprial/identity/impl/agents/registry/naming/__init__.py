# Decision record (UUID addressing §2.2,
# docs/design/uuid-addressing-implementation-2026-11-03.md):
# - agents.actor stays the immutable internal key (grants, blocks, home
#   and desired-state keep keying on it), so rename writes one column and no
#   foreign key moves.  display_name NULL means the display is the actor.
# - Names are unique across the live agents' actor and display_name union.
#   The display column carries a unique index; actor/display collisions are
#   checked under the registry's BEGIN IMMEDIATE.
# - Re-creation (owner decision 2026-11-19, replacing the ruling-X name
#   ledger): a new agent with a previously used name, by create or rename, is
#   a new agent with a new id.  Nothing of the old one is inherited: its id,
#   entity token, grants, home receipt, session refs, pending rows,
#   routines and default conversations are keyed by the incarnation and end
#   with it.  The registry keeps no record of used names; rows an older build
#   wrote to agent_name_ledger are never read.
# - Retired session refs are fenced by incarnation, never by name (H1 of the
#   same decision).  Session refs are chosen by the carrier and are not
#   unique across agents: one harness session moves to another name by
#   re-registering (supersession handoff).  A call presenting a ref is
#   refused when the ref is retired for the presenting live incarnation (its
#   token, or a row stored at its URI after rotation), or retired for any
#   other incarnation unless the presenting live incarnation's own session
#   history holds the ref.  So a destroyed agent's carrier is refused by
#   name, at its dead id and after a same-name agent was created, and never
#   auto-creates the agent; the handoff target keeps its session when the
#   old name is destroyed later.  Accepted consequence: a destroyed agent's
#   harness session cannot be resumed onto an agent that never registered it.
# - Rotation with the switch on re-mints the URI from the new token: the
#   binding fence and retired refs move to the new URI.
# - Rename does not refresh the agent-record lifecycle payload: like update it
#   is an external mutation, and the saga fence is meant to see that drift.
# - Payloads of agent-record resources written before Agent.to_json gained
#   agentId/displayName are upgraded in place on open; otherwise every stored
#   fence would read as drift after the upgrade and rotate its token.
"""Display names, rename and the retired-session fence of the agent registry."""

from __future__ import annotations

import json
import sqlite3
from hyprial.kernel import member_actor_registry_key

from .._base import (
    Agent,
    AgentError,
    AgentExistsError,
    AgentNotFoundError,
    InvalidAgentNameError,
    agent_id_for_entity_token,
)


def ensure_naming_schema(connection: sqlite3.Connection) -> None:
    """Add the display column and upgrade stored record payloads."""

    columns = {row[1] for row in connection.execute("PRAGMA table_info(agents)")}
    if "display_name" not in columns:
        connection.execute("ALTER TABLE agents ADD COLUMN display_name TEXT DEFAULT NULL")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS agents_display_name ON agents(display_name)"
    )
    upgrades: list[tuple[str, str]] = []
    for row in connection.execute(
        "SELECT resource_key, payload FROM lifecycle_resources "
        "WHERE resource_key LIKE 'agent-record:%'"
    ).fetchall():
        try:
            payload = json.loads(str(row[1]))
            if not isinstance(payload, dict) or "agentId" in payload:
                continue
            payload["agentId"] = agent_id_for_entity_token(str(payload["entityToken"]))
        except (ValueError, KeyError, AgentError):
            continue
        payload.setdefault("displayName", None)
        upgrades.append((json.dumps(payload, sort_keys=True), str(row[0])))
    connection.executemany(
        "UPDATE lifecycle_resources SET payload = ? WHERE resource_key = ?", upgrades
    )


class _RegistryNamingMixin:
    def _local_name(self, value: str) -> str:
        """Normalize a bare name, mapping a live display name to its actor."""

        name = self.normalize_actor(member_actor_registry_key(value))
        with self._lock:
            row = self._db.execute(
                "SELECT actor FROM agents WHERE actor = ? OR display_name = ? "
                "ORDER BY actor = ? DESC LIMIT 1",
                (name, name, name),
            ).fetchone()
        return name if row is None else str(row["actor"])

    def _refuse_taken_name_locked(self, agent: Agent) -> None:
        """Creation gate: the name is a live agent's display name."""

        holder = self._db.execute(
            "SELECT actor FROM agents WHERE display_name = ? AND actor != ?",
            (agent.actor, agent.actor),
        ).fetchone()
        if holder is not None:
            raise AgentExistsError(
                f"the name {agent.actor!r} is the display name of agent "
                f"{holder['actor']!r} on this node ({self.owner}@{self.machine})"
            )

    def rename(self, actor: str, display_name: str) -> Agent:
        """U2: change only the display name; actor, URI, home and grants stay."""

        if not isinstance(display_name, str) or ":" in display_name:
            raise InvalidAgentNameError(
                f"display name {display_name!r} must be a bare agent name"
            )
        new = self.native_actor(display_name)
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            name = self.local_actor(actor)
            row = None if name is None else self._db.execute(
                "SELECT * FROM agents WHERE actor = ?", (name,)
            ).fetchone()
            if row is None:
                raise AgentNotFoundError(f"no agent named {actor!r} on this machine")
            agent = self._row_agent(row)
            if new == agent.display:
                return agent
            holder = self._db.execute(
                "SELECT actor FROM agents WHERE (actor = ? OR display_name = ?) "
                "AND actor != ?",
                (new, new, agent.actor),
            ).fetchone()
            if holder is not None:
                raise AgentExistsError(
                    f"the name {new!r} is already taken by agent "
                    f"{holder['actor']!r} on this node ({self.owner}@{self.machine})"
                )
            self._db.execute(
                "UPDATE agents SET display_name = ? WHERE actor = ?",
                (None if new == agent.actor else new, agent.actor),
            )
        return self.require(agent.actor)

    def _carry_rotated_uri_locked(self, agent: Agent, new_uri: str) -> None:
        """Move what this database keys by a rotated agent's URI to ``new_uri``."""

        from .id_form import move_binding_fence

        move_binding_fence(self._db, agent.uri, new_uri)
        self._db.execute(
            "UPDATE OR IGNORE retired_session_refs SET actor = ? WHERE actor = ?",
            (new_uri, agent.uri),
        )

    def is_session_ref_retired(self, actor: str, session_ref: str) -> bool:
        """Whether ``session_ref`` is retired for the incarnation ``actor`` names now."""

        with self._lock:
            retired = self._retired_session_refs.get(session_ref)
            if not retired:
                return False
            local = self.local_actor(actor)
            row = None if local is None else self._db.execute(
                "SELECT uri, entity_token FROM agents WHERE actor = ?", (local,)
            ).fetchone()
            if row is None:
                return True
            uri, token = str(row["uri"]), str(row["entity_token"])
            if any(stored == uri or retired_token == token for stored, retired_token in retired):
                return True
            held = self._db.execute(
                "SELECT 1 FROM agent_session_ref_history "
                "WHERE actor = ? AND entity_token = ? AND session_ref = ?",
                (local, token, session_ref),
            ).fetchone()
            return held is None

    def release_retired_session_ref(self, actor: str, session_ref: str) -> bool:
        """Release exactly one explicit-resume credential, never its peers.

        The ref is released where it is stored: at ``actor`` as given, or at
        the URI of the live agent ``actor`` names.
        """

        live = self.get(actor)
        candidates = sorted({actor} | ({live.uri} if live is not None else set()))
        with self._lock, self._db:
            before = self._db.total_changes
            self._db.executemany(
                "DELETE FROM retired_session_refs WHERE actor = ? AND session_ref = ?",
                ((candidate, session_ref) for candidate in candidates),
            )
            changed = self._db.total_changes - before
            self._refresh_retired_session_refs_locked()
        return changed > 0


__all__ = ["ensure_naming_schema"]
