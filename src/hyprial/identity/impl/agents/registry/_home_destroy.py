from __future__ import annotations

from hyprial.identity.impl.agents.home.provisioner import AgentHomeError
from hyprial.identity.impl.agents.home.effects import CleanupHome
from hyprial.identity.impl.agents.home.provisioner import HomePayloadFile
from hyprial.identity.impl.agents.home.provisioner import HomeProvisioningAttempt
from hyprial.identity.impl.agents.home.provisioner import HomeReceipt
from collections.abc import Iterable, Iterator
from pathlib import Path
from hyprial.identity.impl.agents.home.effects import ProvisionHome
from hyprial.identity.impl.agents.home.provisioner import WorkspaceSummary
from contextlib import contextmanager
import json
from dataclasses import replace

from ._base import (
    Agent,
    AgentDestroySettlementUnknown,
    AgentEntityConflict,
    AgentError,
    AgentHomeReservation,
    RETIRED_SESSION_REF_RETENTION_MS,
    _LOG,
)


class _RegistryHomeDestroyMixin:
    def retire_session_refs(
        self,
        actor: str,
        entity_token: str,
        session_refs: Iterable[str],
        *,
        reason: str,
        destroy_attempt: str,
    ) -> int:
        """Fence the exact incarnation's remembered and currently known refs."""

        if not actor or not entity_token or not reason or not destroy_attempt:
            raise AgentError("retired session identity and reason must not be empty")
        name = self.normalize_actor(actor)
        refs = set(session_refs)
        if any(not item for item in refs):
            raise AgentError("retired session refs must not be empty")
        retired_at_ms = int(self._clock())
        cutoff = retired_at_ms - RETIRED_SESSION_REF_RETENTION_MS
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT uri, entity_token FROM agents WHERE actor = ?", (name,)
            ).fetchone()
            if row is None or str(row["entity_token"]) != entity_token:
                raise AgentEntityConflict(
                    f"agent {actor!r} changed while its sessions were retired"
                )
            refs.update(
                str(item["session_ref"])
                for item in self._db.execute(
                    "SELECT session_ref FROM agent_session_ref_history "
                    "WHERE actor = ? AND entity_token = ?",
                    (name, entity_token),
                )
            )
            if not refs:
                return 0
            self._db.execute(
                "DELETE FROM retired_session_refs WHERE retired_at_ms < ?", (cutoff,)
            )
            before = self._db.total_changes
            self._db.executemany(
                "INSERT INTO retired_session_refs"
                "(actor, entity_token, session_ref, retired_at_ms, reason, destroy_attempt) "
                "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(actor, session_ref) DO NOTHING",
                (
                    (
                        str(row["uri"]), entity_token, item, retired_at_ms,
                        reason, destroy_attempt,
                    )
                    for item in sorted(refs)
                ),
            )
            changed = self._db.total_changes - before
            self._refresh_retired_session_refs_locked()
            return changed

    def rollback_retired_session_refs(self, destroy_attempt: str) -> int:
        if not destroy_attempt:
            raise AgentError("destroy attempt must not be empty")
        with self._lock, self._db:
            cursor = self._db.execute(
                "DELETE FROM retired_session_refs WHERE destroy_attempt = ?",
                (destroy_attempt,),
            )
            self._refresh_retired_session_refs_locked()
        return cursor.rowcount

    def prepare_destroy_record(
        self, actor: str, expected_entity_token: str | None = None
    ) -> tuple[bool, AgentHomeReservation | None]:
        name = self.local_actor(actor)
        if name is None:
            return False, None
        with self._lock, self._db:
            previous = self._db.execute(
                "SELECT * FROM agents WHERE actor = ?", (name,)
            ).fetchone()
            if previous is None:
                return False, None
            prior_agent = self._row_agent(
                previous, self._pinned_adapters_locked(name)
            )
            if (
                expected_entity_token is not None
                and prior_agent.entity_token != expected_entity_token
            ):
                raise AgentEntityConflict(
                    f"agent {actor!r} changed since destroy was requested"
                )
            self._db.execute("DELETE FROM agents WHERE actor = ?", (name,))
            revoked = self._revoke_home_locked(prior_agent)
            self._record_external_resource_locked(
                f"agent-record:{name}", False, prior_agent.to_json()
            )
        with self._lock:
            self._restore_dispositions.pop(name, None)
            self._agent_blocks.pop(name, None)
        if revoked is None:
            return True, None
        return True, AgentHomeReservation(
            "destroy", prior_agent, CleanupHome(revoked), changed=True
        )
    def prepare_destroy_settlement(
        self, actor: str, expected_entity_token: str
    ) -> tuple[str | None, AgentHomeReservation | None]:
        """Settle one exact destroy from live identity or durable revoke facts.

        ``None`` disposition means CleanupHome still owns settlement.  A bare
        missing Agent is never success: replay requires the inactive
        ``agent-record`` for the expected incarnation and, when a home existed,
        its exact revoked/cleaned receipt.
        """

        current = self.get(actor)
        if current is not None:
            if current.entity_token != expected_entity_token:
                return "stale-incarnation", None
            changed, reservation = self.prepare_destroy_record(
                actor, expected_entity_token
            )
            assert changed
            return ("destroyed", None) if reservation is None else (None, reservation)

        name = self.local_actor(actor)
        if name is None:
            raise AgentDestroySettlementUnknown(
                f"no local Agent settlement identity for {actor!r}"
            )
        claim_key = f"agent-home-effect:create:{name}"
        with self._lock:
            record = self._db.execute(
                "SELECT active,payload FROM lifecycle_resources "
                "WHERE resource_key=?",
                (f"agent-record:{name}",),
            ).fetchone()
            home = self._db.execute(
                "SELECT active,payload FROM lifecycle_resources "
                "WHERE resource_key=?",
                (f"agent-home:{name}",),
            ).fetchone()
            claim = self._db.execute(
                "SELECT payload FROM lifecycle_resources WHERE resource_key=?",
                (claim_key,),
            ).fetchone()
        if record is None or bool(record["active"]):
            raise AgentDestroySettlementUnknown(
                f"no durable destroy settlement for {name!r}"
            )
        try:
            prior_agent = Agent.from_json(
                json.loads(str(record["payload"])), "destroy settlement"
            )
        except (ValueError, json.JSONDecodeError) as error:
            raise AgentHomeError(
                "invalid-registry-receipt", name, "destroy-settlement"
            ) from error
        if prior_agent.entity_token != expected_entity_token:
            return "stale-incarnation", None
        if claim is not None and home is None and prior_agent.hosted_by == "host-invite":
            # A process can die after the filesystem effect but before the
            # owner promotes its create claim to a revoked home receipt. The
            # claim is the durable identity; cleanup handles either a still
            # matching mirror or an absent, never-created home.
            payload = json.loads(str(claim["payload"]))
            if not isinstance(payload, dict) or payload.get("priorHome") is not None:
                raise AgentDestroySettlementUnknown(
                    f"Agent {name!r} failed create claim is not a fresh invite"
                )
            reservation = self._create_reservation_from_payload(claim_key, payload)
            replacement = (
                reservation.plan.receipt
                if isinstance(reservation.plan, ProvisionHome)
                else None
            )
            if replacement is None or reservation.agent is None or (
                reservation.agent.entity_token != expected_entity_token
            ):
                raise AgentDestroySettlementUnknown(
                    f"Agent {name!r} failed create claim changed incarnation"
                )
            self.revoke_failed_create_record(reservation, replacement)
            return self.prepare_destroy_settlement(actor, expected_entity_token)
        if home is None:
            return "already-cleaned", None
        if bool(home["active"]):
            raise AgentDestroySettlementUnknown(
                f"Agent {name!r} has an active home without a live identity"
            )
        try:
            receipt = HomeReceipt.from_json(json.loads(str(home["payload"])))
        except (ValueError, json.JSONDecodeError) as error:
            raise AgentHomeError(
                "invalid-registry-receipt", name, "destroy-settlement"
            ) from error
        if receipt.entity_token != expected_entity_token:
            raise AgentHomeError(
                "cleanup-fenced", name, "destroy-settlement"
            )
        if receipt.status == "cleaned":
            return "already-cleaned", None
        if receipt.status != "revoked":
            raise AgentDestroySettlementUnknown(
                f"Agent {name!r} home is not revoked or cleaned"
            )
        claim_payload = None if claim is None else str(claim["payload"])
        if claim_payload is not None:
            payload = json.loads(claim_payload)
            if (
                not isinstance(payload, dict)
                or Agent.from_json(payload.get("agent"), "failed create claim").entity_token
                != expected_entity_token
                or HomeReceipt.from_json(payload.get("replacement")).resource_token
                != receipt.resource_token
            ):
                raise AgentDestroySettlementUnknown(
                    f"Agent {name!r} create claim does not own home cleanup"
                )
        return None, AgentHomeReservation(
            "settle-destroy", prior_agent, CleanupHome(receipt),
            claim_key if claim_payload is not None else None,
            claim_payload,
            changed=True,
        )
    def prepare_cleanup_revoked_home(
        self, actor: str
    ) -> AgentHomeReservation | None:
        if self._home is None:
            return None
        name = self.local_actor(actor)
        if name is None:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT active, payload FROM lifecycle_resources WHERE resource_key = ?",
                (f"agent-home:{name}",),
            ).fetchone()
            incumbent = self._db.execute(
                "SELECT 1 FROM agents WHERE actor = ?", (name,)
            ).fetchone()
        if row is None or bool(row["active"]) or incumbent is not None:
            return None
        try:
            receipt = HomeReceipt.from_json(json.loads(str(row["payload"])))
        except (ValueError, json.JSONDecodeError) as error:
            raise AgentHomeError(
                "invalid-registry-receipt", name, "cleanup-registry"
            ) from error
        if receipt.actor != name:
            raise AgentHomeError("receipt-mismatch", name, "cleanup-registry")
        if receipt.status != "revoked":
            return None
        return AgentHomeReservation(
            "cleanup-home", None, CleanupHome(receipt), changed=True
        )
    def commit_cleanup_home(
        self, reservation: AgentHomeReservation, cleaned: HomeReceipt
    ) -> None:
        plan = reservation.plan
        if not isinstance(plan, CleanupHome):
            raise TypeError("not an Agent cleanup reservation")
        prior = plan.receipt
        if (
            cleaned.actor != prior.actor or cleaned.status != "cleaned"
            or cleaned.entity_token != prior.entity_token
            or cleaned.path != prior.path or cleaned.owner_uid != prior.owner_uid
        ):
            raise AgentHomeError("cleanup-fenced", prior.actor, "cleanup-completion")
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT active, payload FROM lifecycle_resources WHERE resource_key = ?",
                (f"agent-home:{prior.actor}",),
            ).fetchone()
            incumbent = self._db.execute(
                "SELECT 1 FROM agents WHERE actor = ?", (prior.actor,)
            ).fetchone()
            if row is None or bool(row["active"]) or incumbent is not None:
                raise AgentHomeError("cleanup-fenced", prior.actor, "cleanup-commit")
            current = HomeReceipt.from_json(json.loads(str(row["payload"])))
            if current != prior:
                raise AgentHomeError("cleanup-fenced", prior.actor, "cleanup-commit")
            if reservation.claim_key is not None:
                claim = self._db.execute(
                    "SELECT payload FROM lifecycle_resources WHERE resource_key=?",
                    (reservation.claim_key,),
                ).fetchone()
                if (
                    claim is None
                    or str(claim["payload"]) != reservation.claim_payload
                ):
                    raise AgentHomeError(
                        "cleanup-fenced", prior.actor, "cleanup-commit"
                    )
            self._record_home_resource_locked(cleaned, False)
            if reservation.claim_key is not None:
                self._db.execute(
                    "DELETE FROM lifecycle_resources WHERE resource_key=?",
                    (reservation.claim_key,),
                )
    def ensure_home(self, actor: str) -> HomeReceipt:
        """Provision or validate the current entity's home in two phases."""

        agent = self.require(actor)
        if self._home is None:
            raise AgentHomeError("not-configured", agent.actor, "provision")
        with self._lock:
            row = self._db.execute(
                "SELECT active, payload FROM lifecycle_resources WHERE resource_key = ?",
                (f"agent-home:{agent.actor}",),
            ).fetchone()
        if row is not None:
            try:
                receipt = HomeReceipt.from_json(json.loads(str(row["payload"])))
            except (ValueError, json.JSONDecodeError) as error:
                raise AgentHomeError(
                    "invalid-registry-receipt", agent.actor, "ensure"
                ) from error
            if bool(row["active"]) and receipt.status == "ready":
                self._home.validate(receipt)
                return receipt
            raise AgentHomeError("operation-pending", agent.actor, "ensure")

        claim = self._home.claim_receipt(
            actor=agent.actor, entity_token=agent.entity_token
        )
        with self._lock, self._db:
            current = self.require(agent.actor)
            if current.entity_token != agent.entity_token:
                raise AgentHomeError("create-fenced", agent.actor, "ensure-claim")
            row = self._db.execute(
                "SELECT 1 FROM lifecycle_resources WHERE resource_key = ?",
                (f"agent-home:{agent.actor}",),
            ).fetchone()
            if row is not None:
                raise AgentHomeError("operation-pending", agent.actor, "ensure-claim")
            self._record_home_resource_locked(claim, False)
        attempt: HomeProvisioningAttempt | None = None
        try:
            attempt = self._home.provision_claimed(claim)
            with self._lock, self._db:
                current = self.require(agent.actor)
                row = self._db.execute(
                    "SELECT active, payload FROM lifecycle_resources "
                    "WHERE resource_key = ?",
                    (f"agent-home:{agent.actor}",),
                ).fetchone()
                if current.entity_token != agent.entity_token or row is None or bool(row["active"]):
                    raise AgentHomeError("create-fenced", agent.actor, "ensure-commit")
                stored = HomeReceipt.from_json(json.loads(str(row["payload"])))
                if stored != claim:
                    raise AgentHomeError("create-fenced", agent.actor, "ensure-commit")
                self._record_home_resource_locked(claim, True)
            return claim
        except BaseException:
            removed = False
            if attempt is not None:
                removed = self._home.compensate(attempt)
                if not removed:
                    _LOG.warning(
                        "agent-home compensation left a residue: actor=%s "
                        "resource_token=%s phase=ensure; it surfaces as "
                        "unowned-residue on the next create until cleaned",
                        attempt.receipt.actor,
                        attempt.receipt.resource_token,
                    )
            with self._lock, self._db:
                row = self._db.execute(
                    "SELECT active, payload FROM lifecycle_resources "
                    "WHERE resource_key = ?",
                    (f"agent-home:{agent.actor}",),
                ).fetchone()
                if row is not None and not bool(row["active"]):
                    try:
                        stored = HomeReceipt.from_json(json.loads(str(row["payload"])))
                    except (ValueError, json.JSONDecodeError):
                        stored = None
                    if stored == claim:
                        self._record_home_resource_locked(
                            replace(claim, status="cleaned" if removed else "revoked"),
                            False,
                        )
            raise
    def home_receipt(
        self,
        actor: str,
        *,
        require_ready: bool = True,
        validate_mirror: bool = True,
        expected_entity_token: str | None = None,
    ) -> HomeReceipt:
        """Read home authority from SQLite, optionally validating its mirror."""

        agent = self.require(actor)
        if expected_entity_token is not None and agent.entity_token != expected_entity_token:
            raise AgentEntityConflict(f"agent {actor!r} changed before home access")
        with self._lock:
            row = self._db.execute(
                "SELECT active, payload FROM lifecycle_resources WHERE resource_key = ?",
                (f"agent-home:{agent.actor}",),
            ).fetchone()
        if row is None:
            raise AgentHomeError("not-provisioned", agent.actor, "read-registry")
        try:
            receipt = HomeReceipt.from_json(json.loads(str(row["payload"])))
        except (ValueError, json.JSONDecodeError) as error:
            raise AgentHomeError("invalid-registry-receipt", agent.actor, "read-registry") from error
        if receipt.entity_token != agent.entity_token:
            raise AgentHomeError("receipt-mismatch", agent.actor, "read-registry")
        if require_ready and (not bool(row["active"]) or receipt.status != "ready"):
            raise AgentHomeError("revoked", agent.actor, "read-registry")
        if require_ready and validate_mirror:
            if self._home is None:
                # Not ``assert``: this is control flow (a registry opened
                # without a provisioner can still hold home rows), and an
                # assert vanishes under ``python -O`` — the typed error keeps
                # the fence loud in every build.
                raise AgentHomeError("not-configured", agent.actor, "validate")
            self._home.validate(receipt)
        return receipt
    def confirm_home_authority(self, receipt: HomeReceipt) -> None:
        """Fence publication against entity or resource-token replacement."""

        current = self.home_receipt(
            receipt.actor, require_ready=True, validate_mirror=False
        )
        if (
            current.entity_token != receipt.entity_token
            or current.resource_token != receipt.resource_token
            or current.path != receipt.path
        ):
            raise AgentHomeError(
                "receipt-mismatch", receipt.actor, "confirm-home-authority"
            )
    def destroy_landing_home(self, actor: str, *, expected_entity_token: str) -> bool:
        """Settle only the captured incarnation, including an already-revoked home."""
        with self._lock:
            disposition, reservation = self.prepare_destroy_settlement(actor, expected_entity_token)
            if disposition == "stale-incarnation":
                raise AgentEntityConflict(f"agent {actor!r} changed before landing rollback")
            if reservation is not None:
                receipt = reservation.plan.receipt
                cleaned = self.home_provisioner.cleanup(receipt, expected_token=receipt.resource_token)
                self.commit_cleanup_home(reservation, cleaned)
            return True
    def materialise_landing_home(
        self, actor: str, staged_root: str, *, expected_entity_token: str,
        expected_files: tuple[HomePayloadFile, ...],
    ) -> str:
        """Standalone registry path; daemon callers use its existing FS owner."""
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            receipt = self.home_receipt(actor, expected_entity_token=expected_entity_token)
            home = self.home_provisioner.materialise_payload(receipt, Path(staged_root), expected_files)
            self.confirm_home_authority(receipt)
            return str(home)
    def confirm_landing_home(self, actor: str, *, expected_entity_token: str) -> str:
        with self._lock:
            receipt = self.home_receipt(
                actor, expected_entity_token=expected_entity_token, validate_mirror=False,
            )
            return receipt.path
    def workspace_path(self, actor: str) -> Path:
        """Return the private default workspace path without creating it."""

        if self._home is None:
            raise AgentHomeError("not-configured", actor, "workspace")
        name = self.native_actor(actor)
        return self._home.agents_root / name / "workspace"
    def ensure_workspace(self, actor: str) -> Path:
        """Create the current incarnation's workspace under its home receipt."""

        if self._home is None:
            raise AgentHomeError("not-configured", actor, "workspace")
        receipt = self.home_receipt(actor)
        return self._home.ensure_workspace(receipt)
    def workspace_summary(self, actor: str) -> WorkspaceSummary:
        """Inventory the current incarnation's workspace without following links."""

        if self._home is None:
            raise AgentHomeError("not-configured", actor, "workspace-summary")
        receipt = self.home_receipt(actor)
        return self._home.workspace_summary(receipt)
    def cleanup_home(self, actor: str, *, expected_token: str) -> HomeReceipt:
        """Clean a destroyed/revoked home only under its durable token fence."""

        if self._home is None:
            raise AgentHomeError("not-configured", actor, "cleanup")
        name = self.local_actor(actor)
        if name is None:
            raise AgentHomeError("cleanup-fenced", actor, "cleanup-registry")
        # Phase 1 is a read-only durable claim check.  Filesystem traversal is
        # deliberately outside both the registry lock and SQLite transaction;
        # phase 2 below revalidates the exact token and absent incarnation
        # before publishing the cleaned receipt.
        with self._lock:
            row = self._db.execute(
                "SELECT active, payload FROM lifecycle_resources WHERE resource_key = ?",
                (f"agent-home:{name}",),
            ).fetchone()
            incumbent = self._db.execute(
                "SELECT 1 FROM agents WHERE actor = ?", (name,)
            ).fetchone()
            if row is None or bool(row["active"]) or incumbent is not None:
                raise AgentHomeError("cleanup-fenced", name, "cleanup-registry")
            try:
                receipt = HomeReceipt.from_json(json.loads(str(row["payload"])))
            except (ValueError, json.JSONDecodeError) as error:
                raise AgentHomeError("invalid-registry-receipt", name, "cleanup-registry") from error
            if receipt.actor != name:
                raise AgentHomeError("receipt-mismatch", name, "cleanup-registry")
        cleaned = self._home.cleanup(receipt, expected_token=expected_token)
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT active, payload FROM lifecycle_resources WHERE resource_key = ?",
                (f"agent-home:{name}",),
            ).fetchone()
            incumbent = self._db.execute(
                "SELECT 1 FROM agents WHERE actor = ?", (name,)
            ).fetchone()
            if row is None or bool(row["active"]) or incumbent is not None:
                raise AgentHomeError("cleanup-fenced", name, "cleanup-commit")
            try:
                current = HomeReceipt.from_json(json.loads(str(row["payload"])))
            except (ValueError, json.JSONDecodeError) as error:
                raise AgentHomeError(
                    "invalid-registry-receipt", name, "cleanup-commit"
                ) from error
            if current != receipt or current.resource_token != expected_token:
                raise AgentHomeError("cleanup-fenced", name, "cleanup-commit")
            self._record_home_resource_locked(cleaned, False)
            return cleaned
    def cleanup_revoked_home(self, actor: str) -> HomeReceipt | None:
        """Resume destroy cleanup only when a durable revoked receipt exists.

        This is the crash-convergence entry point used by both a subsequent
        create and a repeated destroy.  Merely knowing the actor name never
        authorizes deletion: a missing, active, cleaned, malformed, or
        still-owned lifecycle row is either a no-op or a loud refusal.
        """

        if self._home is None:
            return None
        name = self.local_actor(actor)
        if name is None:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT active, payload FROM lifecycle_resources WHERE resource_key = ?",
                (f"agent-home:{name}",),
            ).fetchone()
            if row is None or bool(row["active"]):
                return None
            try:
                receipt = HomeReceipt.from_json(json.loads(str(row["payload"])))
            except (ValueError, json.JSONDecodeError) as error:
                raise AgentHomeError(
                    "invalid-registry-receipt", name, "cleanup-registry"
                ) from error
            if receipt.actor != name:
                raise AgentHomeError("receipt-mismatch", name, "cleanup-registry")
            if receipt.status != "revoked":
                return None
        return self.cleanup_home(name, expected_token=receipt.resource_token)
    @contextmanager
    def _home_transaction(self) -> Iterator[list[HomeProvisioningAttempt]]:
        """Serialize DB/FS reservations and compensate caught SQL failures."""

        attempts: list[HomeProvisioningAttempt] = []
        with self._lock:
            try:
                with self._db:
                    self._db.execute("BEGIN IMMEDIATE")
                    yield attempts
            except BaseException:
                if self._home is not None:
                    for attempt in reversed(attempts):
                        self._compensate_home_fs(attempt, "saga-rollback")
                raise
    def _provision_home_locked(
        self, agent: Agent, *, allow_revoked: bool = False
    ) -> HomeProvisioningAttempt | None:
        if self._home is None:
            return None
        row = self._db.execute(
            "SELECT payload FROM lifecycle_resources WHERE resource_key = ?",
            (f"agent-home:{agent.actor}",),
        ).fetchone()
        incumbent = None
        if row is not None:
            try:
                incumbent = HomeReceipt.from_json(json.loads(str(row["payload"])))
            except (ValueError, json.JSONDecodeError) as error:
                raise AgentHomeError(
                    "invalid-registry-receipt", agent.actor, "reserve"
                ) from error
        return self._home.provision(
            actor=agent.actor,
            entity_token=agent.entity_token,
            incumbent=incumbent,
            allow_revoked=allow_revoked,
        )
    def _record_home_resource_locked(self, receipt: HomeReceipt, active: bool) -> None:
        self._db.execute(
            "INSERT INTO lifecycle_resources VALUES(?, ?, ?, ?) "
            "ON CONFLICT(resource_key) DO UPDATE SET "
            "resource_token=excluded.resource_token, active=excluded.active, "
            "payload=excluded.payload",
            (
                f"agent-home:{receipt.actor}",
                receipt.resource_token,
                int(active),
                json.dumps(receipt.to_json(), sort_keys=True),
            ),
        )
    def _revoke_home_locked(self, agent: Agent) -> HomeReceipt | None:
        row = self._db.execute(
            "SELECT payload FROM lifecycle_resources WHERE resource_key = ?",
            (f"agent-home:{agent.actor}",),
        ).fetchone()
        if row is None:
            return None
        try:
            receipt = HomeReceipt.from_json(json.loads(str(row["payload"])))
        except (ValueError, json.JSONDecodeError) as error:
            raise AgentHomeError(
                "invalid-registry-receipt", agent.actor, "revoke"
            ) from error
        if receipt.entity_token != agent.entity_token:
            raise AgentHomeError("receipt-mismatch", agent.actor, "revoke")
        revoked = replace(receipt, status="revoked")
        self._record_home_resource_locked(revoked, False)
        return revoked
    def _compensate_home_locked(self, agent: Agent) -> None:
        """Remove a half-built home when its create saga rolls back."""

        if self._home is None:
            return
        row = self._db.execute(
            "SELECT payload FROM lifecycle_resources WHERE resource_key = ?",
            (f"agent-home:{agent.actor}",),
        ).fetchone()
        if row is None:
            return
        try:
            receipt = HomeReceipt.from_json(json.loads(str(row["payload"])))
        except (ValueError, json.JSONDecodeError) as error:
            raise AgentHomeError(
                "invalid-registry-receipt", agent.actor, "compensate"
            ) from error
        if receipt.entity_token != agent.entity_token:
            raise AgentHomeError("receipt-mismatch", agent.actor, "compensate")
        removed = self._home.compensate(HomeProvisioningAttempt(receipt, True))
        # A non-empty home means files this rollback never placed still exist;
        # keep a diagnosable revoked residue rather than dropping authority for
        # on-disk state.  Either way the resource goes inactive so a fresh
        # create re-provisions from zero.
        status = "cleaned" if removed else "revoked"
        self._record_home_resource_locked(replace(receipt, status=status), False)
