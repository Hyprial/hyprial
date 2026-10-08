from __future__ import annotations

from hyprial.identity.impl.agents.home.provisioner import AgentHomeError
from typing import Any
from hyprial.identity.impl.agents.runtime.grants import CapabilityGrant
from hyprial.identity.impl.agents.runtime.grants import GrantJournalEntry
from hyprial.identity.impl.agents.home.provisioner import HomeReceipt
from collections.abc import Iterable
import json
from hyprial.identity.impl.agents.runtime.grants import principal
from dataclasses import replace
from hyprial.identity.impl.agents.runtime.grants import single_line
import sqlite3
import uuid

from hyprial.kernel import canonical_user_uri

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hyprial.identity.impl.agents.runtime.secrets import SecretGrant
    from hyprial.identity.impl.agents.runtime.secrets import SecretSource

from ._base import (
    AGENT_ACTIVITY_WRITE_INTERVAL_MS,
    AGENT_SESSION_REF_HISTORY_LIMIT,
    Agent,
    AgentBlock,
    AgentEntityConflict,
    AgentError,
    AgentNotFoundError,
    HandoverNotice,
    PinConflictError,
    RestoreDisposition,
)


class _RegistryRecordsMixin:
    def record_session_ref(
        self, actor: str, entity_token: str, session_ref: str
    ) -> None:
        """Remember one successful registration for this exact incarnation."""

        if not entity_token or not session_ref:
            raise AgentError("session history identity must not be empty")
        name = self.normalize_actor(actor)
        registered_at_ms = int(self._clock())
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT uri, entity_token FROM agents WHERE actor = ?", (name,)
            ).fetchone()
            if row is None or str(row["entity_token"]) != entity_token:
                raise AgentEntityConflict(
                    f"agent {actor!r} changed while its session was registered"
                )
            # Delete+insert deliberately advances history_id for a repeated ref:
            # the bound is on the newest distinct registrations, not lexical refs.
            self._db.execute(
                "DELETE FROM agent_session_ref_history "
                "WHERE actor = ? AND entity_token = ? AND session_ref = ?",
                (name, entity_token, session_ref),
            )
            self._db.execute(
                "INSERT INTO agent_session_ref_history"
                "(actor, entity_token, session_ref, registered_at_ms) "
                "VALUES(?, ?, ?, ?)",
                (name, entity_token, session_ref, registered_at_ms),
            )
            overflow_query = (
                "FROM agent_session_ref_history "
                "WHERE actor = ? AND entity_token = ? AND history_id NOT IN ("
                "SELECT history_id FROM agent_session_ref_history "
                "WHERE actor = ? AND entity_token = ? "
                "ORDER BY history_id DESC LIMIT ?)"
            )
            overflow_params = (
                name,
                entity_token,
                name,
                entity_token,
                AGENT_SESSION_REF_HISTORY_LIMIT,
            )
            evicted = [
                str(item["session_ref"])
                for item in self._db.execute(
                    "SELECT session_ref " + overflow_query, overflow_params
                )
            ]
            if evicted:
                # A ref dropped from the history would escape the next destroy
                # and could later resurrect the agent, so eviction retires it.
                # It is never the live session: every register re-inserts its
                # ref as the newest row, and any other register takes over.
                # Tagged with its own attempt so a destroy rollback (which
                # deletes by destroy attempt) never releases it.
                self._db.executemany(
                    "INSERT INTO retired_session_refs"
                    "(actor, entity_token, session_ref, retired_at_ms, reason, "
                    "destroy_attempt) VALUES(?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(actor, session_ref) DO NOTHING",
                    (
                        (
                            str(row["uri"]), entity_token, ref, registered_at_ms,
                            "history-evicted", "history-evicted",
                        )
                        for ref in evicted
                    ),
                )
                self._db.execute("DELETE " + overflow_query, overflow_params)
                self._refresh_retired_session_refs_locked()

    def session_ref_history(
        self, actor: str, entity_token: str
    ) -> tuple[str, ...]:
        name = self.normalize_actor(actor)
        with self._lock:
            rows = self._db.execute(
                "SELECT session_ref FROM agent_session_ref_history "
                "WHERE actor = ? AND entity_token = ? ORDER BY history_id",
                (name, entity_token),
            ).fetchall()
        return tuple(str(row["session_ref"]) for row in rows)

    def is_session_ref_retired(self, actor: str, session_ref: str) -> bool:
        candidates = {actor}
        local = self.local_actor(actor)
        if local is not None and actor == local:
            candidates.add(self.uri_for(local))
        with self._lock:
            return any(
                (candidate, session_ref) in self._retired_session_refs
                for candidate in candidates
            )

    def release_retired_session_ref(self, actor: str, session_ref: str) -> bool:
        """Release exactly one explicit-resume credential, never its peers."""

        candidates = {actor}
        local = self.local_actor(actor)
        if local is not None and actor == local:
            candidates.add(self.uri_for(local))
        with self._lock, self._db:
            before = self._db.total_changes
            self._db.executemany(
                "DELETE FROM retired_session_refs WHERE actor = ? AND session_ref = ?",
                ((candidate, session_ref) for candidate in sorted(candidates)),
            )
            changed = self._db.total_changes - before
            self._refresh_retired_session_refs_locked()
        return changed > 0

    def save(self, agent: Agent) -> Agent:
        """Write the record; the caller owns the merge.

        ``pinned_adapters`` is deliberately not written here — pins change
        only through :meth:`pin`/:meth:`unpin`, so a stale copy of the record
        can never clobber a binding.  The upsert updates in place (never
        DELETE+INSERT, which would fire the pin cascade).
        """

        self._validate_config_location(agent)
        with self._lock, self._db:
            existing = self._db.execute(
                "SELECT owner, machine, uri, hosted_by, entity_token, last_active_at_ms "
                "FROM agents WHERE actor = ?",
                (agent.actor,),
            ).fetchone()
            if agent.hosted_by is not None or (
                existing is not None and existing["hosted_by"] is not None
            ):
                identity = (agent.owner, agent.machine, agent.uri, agent.hosted_by)
                stored_identity = (
                    None
                    if existing is None
                    else (
                        str(existing["owner"]),
                        str(existing["machine"]),
                        str(existing["uri"]),
                        existing["hosted_by"],
                    )
                )
                if stored_identity != identity:
                    raise AgentError(
                        "save cannot authorize or change hosted identity; "
                        "hosting requires transfer receive"
                    )
            if existing is not None and str(existing["entity_token"]) != agent.entity_token:
                raise AgentError(
                    "save cannot change an agent incarnation; use the authority rotation API"
                )
            if existing is not None:
                agent = replace(
                    agent,
                    last_active_at_ms=(
                        None
                        if existing["last_active_at_ms"] is None
                        else int(existing["last_active_at_ms"])
                    ),
                )
            statement, values = self._insert_statement(agent)
            self._db.execute(
                statement
                + " ON CONFLICT(actor) DO UPDATE SET "
                + ", ".join(
                    f"{column} = excluded.{column}"
                    for column in (
                        "owner",
                        "machine",
                        "uri",
                        "cwd",
                        "config",
                        "provider",
                        "model",
                        "capabilities",
                        "harness_args",
                        "preferred_harness",
                        "last_harness",
                        "last_session_id",
                        "last_active_at_ms",
                        "created_at_ms",
                        "hosted_by",
                    )
                ),
                values,
            )
        return agent
    @staticmethod
    def _insert_statement(agent: Agent) -> tuple[str, tuple[Any, ...]]:
        return (
            "INSERT INTO agents (actor, owner, machine, uri, entity_token, cwd, config, provider, "
            "model, capabilities, harness_args, preferred_harness, "
            "last_harness, last_session_id, last_active_at_ms, created_at_ms, hosted_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                agent.actor,
                agent.owner,
                agent.machine,
                agent.uri,
                agent.entity_token,
                agent.cwd,
                None
                if agent.config is None
                else json.dumps(agent.config.to_json(), sort_keys=True),
                agent.provider,
                agent.model,
                json.dumps(dict(agent.capabilities), sort_keys=True),
                json.dumps(
                    {
                        harness: list(args)
                        for harness, args in agent.harness_args.items()
                    },
                    sort_keys=True,
                ),
                agent.preferred_harness,
                agent.last_harness,
                agent.last_session_id,
                agent.last_active_at_ms,
                agent.created_at_ms,
                agent.hosted_by,
            ),
        )
    def record_activity(self, actor: str) -> bool:
        """Best-effort durable activity mark, coalesced per agent per minute.

        The in-memory fence avoids even issuing SQL for repeated events in one
        daemon. The conditional update is the cross-process fence, so two
        registry instances racing inside the same window still change one row
        at most once. Unknown or foreign actors are ignored.
        """

        name = self.local_actor(actor)
        if name is None:
            return False
        now_ms = self.now_ms()
        with self._lock:
            previous = self._last_activity_write.get(name)
            if (
                previous is not None
                and now_ms - previous < AGENT_ACTIVITY_WRITE_INTERVAL_MS
            ):
                return False
            with self._db:
                cursor = self._db.execute(
                    "UPDATE agents SET last_active_at_ms = ? WHERE actor = ? "
                    "AND (last_active_at_ms IS NULL OR last_active_at_ms <= ?)",
                    (now_ms, name, now_ms - AGENT_ACTIVITY_WRITE_INTERVAL_MS),
                )
            if cursor.rowcount != 1:
                row = self._db.execute(
                    "SELECT last_active_at_ms FROM agents WHERE actor = ?", (name,)
                ).fetchone()
                if row is not None and row["last_active_at_ms"] is not None:
                    self._last_activity_write[name] = int(row["last_active_at_ms"])
                return False
            self._last_activity_write[name] = now_ms
            return True
    def restore_disposition(
        self, actor: str | Agent, *, desired_generation: str | None = None
    ) -> RestoreDisposition | None:
        agent = actor if isinstance(actor, Agent) else self.get(actor)
        if agent is None:
            return None
        with self._lock:
            disposition = self._restore_dispositions.get(agent.actor)
            if disposition is None:
                return None
            if (
                disposition.entity_token != agent.entity_token
                or (
                    desired_generation is not None
                    and disposition.desired_generation != desired_generation
                )
            ):
                with self._db:
                    self._db.execute(
                        "DELETE FROM agent_restore_dispositions WHERE actor = ?",
                        (agent.actor,),
                    )
                self._restore_dispositions.pop(agent.actor, None)
                return None
            return disposition
    def suppress_restore(
        self,
        actor: str,
        *,
        desired_generation: str,
        last_active_at_ms: int | None,
        idle_age_ms: int | None,
        restore_threshold_ms: int,
        restore_override: str = "none",
        activity_unknown: bool = False,
    ) -> RestoreDisposition:
        agent = self.require(actor)
        recorded_at_ms = self.now_ms()
        with self._lock, self._db:
            prior = self._restore_dispositions.get(agent.actor)
            disposition_token = (
                prior.disposition_token
                if prior is not None
                and prior.entity_token == agent.entity_token
                and prior.desired_generation == desired_generation
                else uuid.uuid4().hex
            )
            self._db.execute(
                "INSERT INTO agent_restore_dispositions "
                "(actor,entity_token,desired_generation,disposition_token,status,last_active_at_ms,"
                "idle_age_ms,restore_threshold_ms,restore_override,activity_unknown,"
                "recorded_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(actor) DO UPDATE SET "
                "entity_token=excluded.entity_token,"
                "desired_generation=excluded.desired_generation,"
                "disposition_token=excluded.disposition_token,"
                "status=excluded.status,"
                "last_active_at_ms=excluded.last_active_at_ms,"
                "idle_age_ms=excluded.idle_age_ms,"
                "restore_threshold_ms=excluded.restore_threshold_ms,"
                "restore_override=excluded.restore_override,"
                "activity_unknown=excluded.activity_unknown,"
                "recorded_at_ms=excluded.recorded_at_ms",
                (
                    agent.actor,
                    agent.entity_token,
                    desired_generation,
                    disposition_token,
                    "idle-suppressed",
                    last_active_at_ms,
                    idle_age_ms,
                    restore_threshold_ms,
                    restore_override,
                    int(activity_unknown),
                    recorded_at_ms,
                ),
            )
            disposition = RestoreDisposition(
                agent.actor,
                agent.entity_token,
                desired_generation,
                disposition_token,
                "idle-suppressed",
                last_active_at_ms,
                idle_age_ms,
                restore_threshold_ms,
                restore_override,
                activity_unknown,
                recorded_at_ms,
            )
            self._restore_dispositions[agent.actor] = disposition
        return disposition
    @staticmethod
    def _restore_disposition_row(row: sqlite3.Row) -> RestoreDisposition:
        return RestoreDisposition(
            actor=str(row["actor"]),
            entity_token=str(row["entity_token"]),
            desired_generation=str(row["desired_generation"]),
            disposition_token=str(row["disposition_token"]),
            status=str(row["status"]),
            last_active_at_ms=(
                None
                if row["last_active_at_ms"] is None
                else int(row["last_active_at_ms"])
            ),
            idle_age_ms=(
                None if row["idle_age_ms"] is None else int(row["idle_age_ms"])
            ),
            restore_threshold_ms=int(row["restore_threshold_ms"]),
            restore_override=str(row["restore_override"]),
            activity_unknown=bool(row["activity_unknown"]),
            recorded_at_ms=int(row["recorded_at_ms"]),
        )
    @staticmethod
    def _agent_block_row(row: sqlite3.Row) -> AgentBlock:
        return AgentBlock(
            actor=str(row["actor"]),
            entity_token=str(row["entity_token"]),
            reason=str(row["reason"]),
            blocked_at_ms=int(row["blocked_at_ms"]),
        )
    def clear_restore_disposition(
        self,
        actor: str,
        *,
        expected_entity_token: str | None = None,
        expected_desired_generation: str | None = None,
        expected_disposition_token: str | None = None,
    ) -> bool:
        name = self.local_actor(actor)
        if name is None:
            return False
        clauses = ["actor = ?"]
        values: list[object] = [name]
        if expected_entity_token is not None:
            clauses.append("entity_token = ?")
            values.append(expected_entity_token)
        if expected_desired_generation is not None:
            clauses.append("desired_generation = ?")
            values.append(expected_desired_generation)
        if expected_disposition_token is not None:
            clauses.append("disposition_token = ?")
            values.append(expected_disposition_token)
        with self._lock, self._db:
            changed = self._db.execute(
                "DELETE FROM agent_restore_dispositions WHERE "
                + " AND ".join(clauses),
                tuple(values),
            ).rowcount == 1
            if changed:
                self._restore_dispositions.pop(name, None)
            return changed
    def block_agent(
        self,
        actor: str,
        *,
        reason: str,
        expected_entity_token: str | None = None,
    ) -> tuple[AgentBlock | None, bool]:
        if reason not in {"provider-quota", "credential-invalid"}:
            raise ValueError(f"unsupported agent block reason: {reason}")
        agent = self.require(actor)
        if (
            expected_entity_token is not None
            and agent.entity_token != expected_entity_token
        ):
            return None, False
        now_ms = self.now_ms()
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT INTO agent_blocks(actor,entity_token,reason,blocked_at_ms) "
                "VALUES (?,?,?,?) ON CONFLICT(actor) DO NOTHING",
                (agent.actor, agent.entity_token, reason, now_ms),
            )
            cleared = self._db.execute(
                "DELETE FROM agent_restore_dispositions WHERE actor = ?",
                (agent.actor,),
            )
            self._restore_dispositions.pop(agent.actor, None)
            block = self._agent_blocks.get(agent.actor)
            if block is None:
                block = AgentBlock(
                    agent.actor, agent.entity_token, reason, now_ms
                )
                self._agent_blocks[agent.actor] = block
        return block, cursor.rowcount == 1 or cleared.rowcount == 1
    def agent_block(self, actor: str | Agent) -> AgentBlock | None:
        agent = actor if isinstance(actor, Agent) else self.get(actor)
        if agent is None:
            return None
        with self._lock:
            block = self._agent_blocks.get(agent.actor)
            if block is None:
                return None
            if block.entity_token != agent.entity_token:
                with self._db:
                    self._db.execute(
                        "DELETE FROM agent_blocks WHERE actor = ?", (agent.actor,)
                    )
                self._agent_blocks.pop(agent.actor, None)
                return None
            return block
    def is_blocked(self, actor: str) -> bool:
        return self.agent_block(actor) is not None
    def unblock_agent(self, actor: str) -> bool:
        agent = self.get(actor)
        if agent is None:
            return False
        with self._lock, self._db:
            changed = self._db.execute(
                "DELETE FROM agent_blocks WHERE actor = ? AND entity_token = ?",
                (agent.actor, agent.entity_token),
            ).rowcount == 1
            if changed:
                self._agent_blocks.pop(agent.actor, None)
            return changed
    def update(self, actor: str, **changes: Any) -> Agent:
        return self.save(replace(self.require(actor), **changes))
    def record_session(
        self, actor: str, *, harness: str, session_id: str | None
    ) -> HandoverNotice | None:
        """Pin the harness/session this agent is running under, for A9.

        Returns a notice exactly when the harness changed, so the caller can
        tell the agent — at the start of its very first turn — which harness it
        came from and which session id to resume by hand.  Context does not
        travel between harnesses; this makes that loss visible instead of
        silent (design §6.2).
        """

        agent = self.require(actor)
        notice: HandoverNotice | None = None
        if agent.last_harness is not None and agent.last_harness != harness:
            notice = HandoverNotice(
                actor=agent.actor,
                previous_harness=agent.last_harness,
                previous_session_id=agent.last_session_id,
                next_harness=harness,
            )
        self.save(
            replace(
                agent,
                last_harness=harness,
                last_session_id=session_id,
                preferred_harness=agent.preferred_harness or harness,
            )
        )
        return notice
    def destroy(self, actor: str) -> bool:
        """Delete the record outright.  No tombstone, no revival (A6).

        One transaction: ``ON DELETE CASCADE`` erases the agent's pins with
        the row, so a crash can never leave a pin aimed at a deleted agent.
        Accepted cost, recorded here so nobody rediscovers it as a bug: after
        a destroy, historical messages addressed to this agent can no longer
        resolve a recipient.  Recreating the same name by hand is the only
        recovery, and it does not restore the destroyed history.
        """

        name = self.local_actor(actor)
        if name is None:
            return False
        revoked: HomeReceipt | None = None
        with self._lock, self._db:
            previous = self._db.execute(
                "SELECT * FROM agents WHERE actor = ?", (name,)
            ).fetchone()
            previous_pins = self._pinned_adapters_locked(name)
            cursor = self._db.execute(
                "DELETE FROM agents WHERE actor = ?", (name,)
            )
            if previous is not None:
                prior_agent = self._row_agent(previous, previous_pins)
                revoked = self._revoke_home_locked(prior_agent)
                self._record_external_resource_locked(
                    f"agent-record:{name}", False, prior_agent.to_json()
                )
            removed = cursor.rowcount > 0
        if removed:
            with self._lock:
                self._restore_dispositions.pop(name, None)
                self._agent_blocks.pop(name, None)
        if revoked is not None:
            # The revoke/delete transaction is the crash fence.  Cleanup is
            # synchronous for the successful API contract, while a crash in
            # this gap remains resumable by cleanup_revoked_home().
            self.cleanup_home(name, expected_token=revoked.resource_token)
        return removed
    def record_external_binding(
        self,
        actor: str,
        *,
        harness: str | None,
        runtime: str | None,
        session_id: str | None,
    ) -> None:
        """Rotate the fence for a non-lifecycle binding mutation."""

        agent = self.require(actor)
        active = harness is not None and runtime is not None
        payload: dict[str, object] = (
            {
                "actor": agent.uri,
                "harness": harness,
                "runtime": runtime,
                "sessionId": session_id,
            }
            if active
            else {}
        )
        with self._lock, self._db:
            self._record_external_resource_locked(
                f"binding:{agent.uri}", active, payload
            )
    def _record_external_resource_locked(
        self, resource_key: str, active: bool, payload: dict[str, object]
    ) -> None:
        row = self._db.execute(
            "SELECT active, payload FROM lifecycle_resources WHERE resource_key = ?",
            (resource_key,),
        ).fetchone()
        encoded = json.dumps(payload, sort_keys=True)
        if row is not None and bool(row["active"]) == active and str(row["payload"]) == encoded:
            return
        self._db.execute(
            "INSERT INTO lifecycle_resources VALUES(?, ?, ?, ?) "
            "ON CONFLICT(resource_key) DO UPDATE SET "
            "resource_token=excluded.resource_token, active=excluded.active, "
            "payload=excluded.payload",
            (resource_key, uuid.uuid4().hex, int(active), encoded),
        )
    def _append_grant_journal_locked(
        self, agent: Agent, *, action: str, grant_id: str, by: str,
        capability: str | None = None, scope: str | None = None,
        revision: int | None = None, note: str | None = None,
    ) -> GrantJournalEntry:
        values = (int(self._clock()), agent.actor, agent.entity_token, action,
                  grant_id, capability, scope, by, revision, note)
        cursor = self._db.execute(
            'INSERT INTO agent_grant_journal '
            '(at_ms, actor, entity_token, action, grant_id, capability, scope, "by", revision, note) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', values,
        )
        return GrantJournalEntry(int(cursor.lastrowid), *values)
    def _record_host_invite_locked(self, agent: Agent) -> GrantJournalEntry:
        existing = self._db.execute(
            "SELECT * FROM agent_grant_journal WHERE actor=? AND entity_token=? "
            "AND action='host-invite' ORDER BY seq LIMIT 1",
            (agent.actor, agent.entity_token),
        ).fetchone()
        if existing is not None:
            return GrantJournalEntry(**dict(existing))
        return self._append_grant_journal_locked(
            agent, action="host-invite", grant_id="host-invite",
            by=canonical_user_uri(self.owner), note="ownerAsserted=host",
        )
    def record_host_invite(
        self, actor: str, *, by: str, note: str | None,
    ) -> GrantJournalEntry:
        """Idempotent invitation fact, always attributed to this host owner."""
        if by != canonical_user_uri(self.owner) or note != "ownerAsserted=host":
            raise ValueError("invitation attribution is fixed by the host")
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            agent = self.require(actor)
            if agent.hosted_by != "host-invite":
                raise AgentError("not a host-invited agent")
            return self._record_host_invite_locked(agent)
    def grant_capability(
        self, actor: str, *, grant_id: str, capability: str,
        scope: str, granted_by: str, revision: int,
        expected_entity_token: str | None = None,
    ) -> CapabilityGrant:
        """Record one current-incarnation grant and its audit in one transaction."""
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            agent = self.require(actor)
            if expected_entity_token is not None and agent.entity_token != expected_entity_token:
                raise AgentEntityConflict(f"agent {actor!r} changed before capability grant")
            grant = CapabilityGrant(agent.actor, agent.entity_token, grant_id,
                                    capability, scope, granted_by, revision)
            previous = self._db.execute(
                "SELECT revision FROM agent_capability_grants WHERE actor=? AND grant_id=?",
                (agent.actor, grant_id),
            ).fetchone()
            if previous is not None and revision <= int(previous["revision"]):
                raise AgentError("capability grant revision must increase")
            self._db.execute(
                "INSERT INTO agent_capability_grants VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(actor, grant_id) DO UPDATE SET "
                "entity_token=excluded.entity_token, capability=excluded.capability, "
                "scope=excluded.scope, granted_by=excluded.granted_by, revision=excluded.revision",
                (grant.actor, grant.entity_token, grant.grant_id, grant.capability,
                 grant.scope, grant.granted_by, grant.revision),
            )
            self._append_grant_journal_locked(
                agent, action="grant", grant_id=grant_id, by=granted_by,
                capability=capability, scope=scope, revision=revision,
            )
            return grant
    def capability_grants(self, actor: str | None = None) -> tuple[CapabilityGrant, ...]:
        # A stale/corrupt grant is never returned as an active grant. The join is
        # the incarnation fence, not a same-name or same-owner inference.
        with self._lock:
            name = None if actor is None else self.require(actor).actor
            rows = self._db.execute(
                "SELECT g.* FROM agent_capability_grants g JOIN agents a "
                "ON a.actor=g.actor AND a.entity_token=g.entity_token "
                "WHERE (? IS NULL OR g.actor=?) ORDER BY g.actor,g.grant_id", (name, name),
            ).fetchall()
            return tuple(CapabilityGrant(**dict(row)) for row in rows)
    def revoke_capability(
        self, actor: str, grant_id: str, *, revoked_by: str,
        expected_entity_token: str | None = None,
    ) -> bool:
        single_line(grant_id, "grant_id")
        principal(revoked_by)
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            agent = self.require(actor)
            if expected_entity_token is not None and agent.entity_token != expected_entity_token:
                raise AgentEntityConflict(f"agent {actor!r} changed before capability revoke")
            row = self._db.execute(
                "SELECT * FROM agent_capability_grants WHERE actor=? AND grant_id=? "
                "AND entity_token=?", (agent.actor, grant_id, agent.entity_token),
            ).fetchone()
            if row is None:
                return False
            grant = CapabilityGrant(**dict(row))
            if grant.capability == "agent-home":
                raise AgentError("agent-home is intrinsic; destroy the agent to retire it")
            self._db.execute(
                "DELETE FROM agent_capability_grants WHERE actor=? AND grant_id=?",
                (agent.actor, grant_id),
            )
            self._append_grant_journal_locked(
                agent, action="revoke", grant_id=grant_id, by=revoked_by,
                capability=grant.capability, scope=grant.scope, revision=grant.revision,
            )
            return True
    def grant_journal(self, actor: str) -> tuple[GrantJournalEntry, ...]:
        # No require(): operators can inspect a destroyed actor's history.
        name = self.normalize_actor(actor)
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM agent_grant_journal WHERE actor=? ORDER BY seq", (name,),
            ).fetchall()
            return tuple(GrantJournalEntry(**dict(row)) for row in rows)
    def grant_secret(
        self,
        actor: str,
        *,
        grant_id: str,
        source: "SecretSource",
        entry_id: str,
        field_name: str | None,
        environment_names: Iterable[str],
        revision: int,
        prevalidated_home_token: str | None = None,
    ) -> "SecretGrant":
        """Bind one exact entry to the current agent incarnation."""

        from hyprial.identity.impl.agents.runtime.secrets  import SecretGrant, SecretSource

        agent = self.require(actor)
        grant = SecretGrant(
            actor=agent.actor,
            entity_token=agent.entity_token,
            grant_id=grant_id,
            source=SecretSource(source),
            entry_id=entry_id,
            field_name=field_name,
            environment_names=tuple(environment_names),
            revision=revision,
        )
        validated_home_token = None
        if grant.source is SecretSource.AGENT_PRIVATE:
            validated_home_token = (
                self.home_receipt(agent.actor).entity_token
                if prevalidated_home_token is None
                else prevalidated_home_token
            )
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            incumbent = self._db.execute(
                "SELECT entity_token FROM agents WHERE actor=?", (agent.actor,)
            ).fetchone()
            if incumbent is None or str(incumbent["entity_token"]) != grant.entity_token:
                raise AgentError("agent incarnation changed during secret grant")
            if validated_home_token is not None:
                row = self._db.execute(
                    "SELECT active,payload FROM lifecycle_resources WHERE resource_key=?",
                    (f"agent-home:{agent.actor}",),
                ).fetchone()
                if row is None or not bool(row["active"]):
                    raise AgentHomeError("revoked", agent.actor, "grant-secret")
                receipt = HomeReceipt.from_json(json.loads(str(row["payload"])))
                if (
                    receipt.status != "ready"
                    or receipt.entity_token != validated_home_token
                    or receipt.entity_token != grant.entity_token
                ):
                    raise AgentHomeError("receipt-mismatch", agent.actor, "grant-secret")
            prior = self._db.execute(
                "SELECT revision FROM agent_secret_grants "
                "WHERE agent = ? AND grant_id = ?",
                (agent.actor, grant.grant_id),
            ).fetchone()
            if prior is not None and revision <= int(prior["revision"]):
                raise AgentError("secret grant revision must increase")
            self._db.execute(
                "INSERT INTO agent_secret_grants "
                "(agent, entity_token, grant_id, source, entry_id, field_name, "
                "environment_names, revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(agent, grant_id) DO UPDATE SET "
                "entity_token=excluded.entity_token, source=excluded.source, "
                "entry_id=excluded.entry_id, field_name=excluded.field_name, "
                "environment_names=excluded.environment_names, "
                "revision=excluded.revision",
                (
                    grant.actor,
                    grant.entity_token,
                    grant.grant_id,
                    grant.source.value,
                    grant.entry_id,
                    grant.field_name,
                    json.dumps(list(grant.environment_names)),
                    grant.revision,
                ),
            )
        return grant
    def secret_grant(self, actor: str, grant_id: str) -> "SecretGrant | None":
        """Read one named grant; this is the resolver's non-enumerating seam."""

        from hyprial.identity.impl.agents.runtime.secrets  import SecretGrant, SecretSource

        name = self.local_actor(actor)
        if name is None:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM agent_secret_grants "
                "WHERE agent = ? AND grant_id = ?",
                (name, grant_id),
            ).fetchone()
        if row is None:
            return None
        names = json.loads(str(row["environment_names"]))
        if not isinstance(names, list) or any(not isinstance(item, str) for item in names):
            raise AgentError("secret grant environment metadata is invalid")
        return SecretGrant(
            actor=str(row["agent"]),
            entity_token=str(row["entity_token"]),
            grant_id=str(row["grant_id"]),
            source=SecretSource(str(row["source"])),
            entry_id=str(row["entry_id"]),
            field_name=(
                None if row["field_name"] is None else str(row["field_name"])
            ),
            environment_names=tuple(names),
            revision=int(row["revision"]),
        )
    def secret_inventory(self, actor: str | None = None) -> tuple["SecretGrant", ...]:
        """Operator metadata inventory; values and filesystem discovery excluded."""

        names = (
            tuple(agent.actor for agent in self.list())
            if actor is None
            else (self.require(actor).actor,)
        )
        with self._lock:
            rows = self._db.execute(
                "SELECT agent, grant_id FROM agent_secret_grants "
                "ORDER BY agent, grant_id"
            ).fetchall()
        grants: list[SecretGrant] = []
        allowed = set(names)
        for row in rows:
            name = str(row["agent"])
            if name not in allowed:
                continue
            grant = self.secret_grant(name, str(row["grant_id"]))
            if grant is not None:
                grants.append(grant)
        return tuple(grants)
    def revoke_secret_grant(self, actor: str, grant_id: str) -> bool:
        agent = self.require(actor)
        with self._lock, self._db:
            cursor = self._db.execute(
                "DELETE FROM agent_secret_grants WHERE agent = ? AND grant_id = ?",
                (agent.actor, grant_id),
            )
            return cursor.rowcount > 0
    def rotate_secret_authority(self, actor: str) -> Agent:
        """Fence a real account/hosting transfer without treating it as an alias."""

        agent = self.require(actor)
        replacement = replace(agent, entity_token=uuid.uuid4().hex)
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            self._revoke_home_locked(agent)
            self._db.execute(
                "DELETE FROM agent_secret_grants WHERE agent = ?", (agent.actor,)
            )
            self._db.execute(
                "DELETE FROM agent_capability_grants WHERE actor = ?", (agent.actor,)
            )
            self._db.execute(
                "UPDATE agents SET entity_token = ? WHERE actor = ?",
                (replacement.entity_token, agent.actor),
            )
            self._record_external_resource_locked(
                f"agent-record:{agent.actor}", True, replacement.to_json()
            )
        return replacement
    def pin(self, adapter: str, actor: str) -> str | None:
        """Bind ``adapter`` to ``actor`` one-to-one; return the previous URI.

        Idempotent for the same pair; re-pinning the adapter to a different
        agent is a move (the previous binding is released in the same
        transaction).  The one-to-one rule is the schema's, not this
        method's: UNIQUE(agent) rejects a second adapter on the same agent
        (:class:`PinConflictError` names the holder and the exact unpin
        command), and the FOREIGN KEY rejects a pin to a nonexistent agent.
        """

        name = self.normalize_actor(actor)
        with self._lock:
            previous = self._pin_of_locked(adapter)
            if previous is not None and previous[0] == name:
                return previous[1]
            try:
                with self._db:
                    self._db.execute(
                        "DELETE FROM pins WHERE adapter = ?", (adapter,)
                    )
                    self._db.execute(
                        "INSERT INTO pins (adapter, agent) VALUES (?, ?)",
                        (adapter, name),
                    )
            except sqlite3.IntegrityError as error:
                message = str(error)
                if "FOREIGN KEY" in message.upper():
                    raise AgentNotFoundError(
                        f"no agent named {name!r} on this machine"
                    ) from error
                holder = self._db.execute(
                    "SELECT adapter FROM pins WHERE agent = ?", (name,)
                ).fetchone()
                raise PinConflictError(
                    self.uri_for(name),
                    adapter,
                    holder["adapter"] if holder is not None else "(unknown)",
                ) from error
            return None if previous is None else previous[1]
    def unpin(self, adapter: str) -> str | None:
        """Remove ``adapter``'s pin; return the URI it pointed at, if any."""

        with self._lock:
            previous = self._pin_of_locked(adapter)
            if previous is None:
                return None
            with self._db:
                self._db.execute(
                    "DELETE FROM pins WHERE adapter = ?", (adapter,)
                )
            return previous[1]
    def pins(self) -> dict[str, str]:
        """Every pin, as ``{adapter: canonical agent URI}``, adapter-sorted."""

        with self._lock:
            return {
                row["adapter"]: row["uri"]
                for row in self._db.execute(
                    "SELECT pins.adapter AS adapter, agents.uri AS uri "
                    "FROM pins JOIN agents ON agents.actor = pins.agent "
                    "ORDER BY pins.adapter"
                )
            }
    def pinned_adapters(self, actor: str) -> tuple[str, ...]:
        name = self.local_actor(actor)
        if name is None:
            return ()
        with self._lock:
            return self._pinned_adapters_locked(name)
