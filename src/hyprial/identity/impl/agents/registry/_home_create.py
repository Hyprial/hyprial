from __future__ import annotations

from hyprial.identity.impl.agents.home.provisioner import AgentHomeError
from typing import Any
from hyprial.identity.impl.agents.home.effects import HomeFilesystemPlan
from hyprial.identity.impl.agents.home.provisioner import HomeProvisioningAttempt
from hyprial.identity.impl.agents.home.provisioner import HomeReceipt
from collections.abc import Iterable
from collections.abc import Mapping
from hyprial.identity.impl.agents.home.effects import ProvisionHome
from hyprial.identity.impl.agents.home.effects import ReplaceHome
import json
from hyprial.identity.impl.agents.home.config import normalize_agent_config
from dataclasses import replace
import sqlite3
import uuid
from hyprial.identity.impl.agents.home.config import validate_agent_config_location

from ._base import (
    Agent,
    AgentError,
    AgentExistsError,
    AgentHomeReservation,
    _LOG,
    _uri,
    normalize_capabilities,
    normalize_harness_args,
)


class _RegistryHomeCreateMixin:
    def create(
        self,
        actor: str,
        *,
        cwd: str | None = None,
        config: object = None,
        provider: str | None = None,
        model: str | None = None,
        capabilities: Mapping[str, Any] | None = None,
        harness_args: Mapping[str, Iterable[str]] | None = None,
        preferred_harness: str | None = None,
    ) -> Agent:
        """Register a new agent, or fail loudly when the name is taken (A1).

        The failure is the point: two agents sharing one actor name mint one
        four-segment URI, and dispatch then hands every message to whichever
        connector sorts first while the other reports online forever.
        """

        name = self.native_actor(actor)
        agent = Agent(
            uri=self.uri_for(name),
            actor=name,
            owner=self.owner,
            machine=self.machine,
            cwd=cwd,
            config=normalize_agent_config(config),
            provider=provider,
            model=model,
            capabilities=normalize_capabilities(capabilities),
            harness_args=normalize_harness_args(harness_args),
            preferred_harness=preferred_harness,
            created_at_ms=int(self._clock()),
        )
        return self._create_record(agent)
    def create_transfer_hosted(
        self, actor: str, *, pinned_owner: str, cwd: str | None = None,
        harness_args: Mapping[str, Iterable[str]] | None = None,
        preferred_harness: str | None = None,
    ) -> Agent:
        """Receive-only insertion; never adopt or overwrite a same-name entity."""

        name = self.native_actor(actor)
        return self._create_record(Agent(
            uri=_uri().canonical_agent_uri(pinned_owner, self.machine, name),
            actor=name,
            owner=pinned_owner,
            machine=self.machine,
            hosted_by="transfer-receive",
            cwd=cwd,
            harness_args=normalize_harness_args(harness_args),
            preferred_harness=preferred_harness,
            created_at_ms=int(self._clock()),
        ))
    def create_host_invited(
        self, actor: str, *, pinned_owner: str, cwd: str | None = None,
        harness_args: Mapping[str, Iterable[str]] | None = None,
        preferred_harness: str | None = None,
    ) -> Agent:
        """Explicit host invitation; never adopt or overwrite an existing agent.

        The host asserts the visitor's owner string. This is not proof of a
        login, an OS isolation boundary, or permission to start a worker.
        """

        if (
            not isinstance(pinned_owner, str) or not pinned_owner
            or pinned_owner != pinned_owner.strip() or ":" in pinned_owner
        ):
            raise ValueError("owner must be non-empty, unpadded and contain no ':'")
        if pinned_owner == self.owner:
            raise ValueError("use agent create for the host's own agents")
        name = self.native_actor(actor)
        return self._create_record(Agent(
            uri=_uri().canonical_agent_uri(pinned_owner, self.machine, name),
            actor=name,
            owner=pinned_owner,
            machine=self.machine,
            hosted_by="host-invite",
            cwd=cwd,
            harness_args=normalize_harness_args(harness_args),
            preferred_harness=preferred_harness,
            created_at_ms=int(self._clock()),
        ))
    def _compensate_home_fs(self, attempt: HomeProvisioningAttempt, phase: str) -> None:
        """Remove a half-built home and record the failure when it stays.

        ``AgentHomeProvisioner.compensate`` answers False for a residue it
        must not touch (foreign token, non-empty tree, OSError).  That answer
        used to be dropped on the floor at every call site, leaving the
        residue discoverable only when the *next* create failed as
        ``unowned-residue``.  The warning is the observable surface; the
        durable revoked-resource row (written by the saga paths that have one)
        remains the authority.
        """

        if self._home is None:
            return
        if not self._home.compensate(attempt):
            _LOG.warning(
                "agent-home compensation left a residue: actor=%s "
                "resource_token=%s phase=%s; it surfaces as unowned-residue "
                "on the next create until cleaned",
                attempt.receipt.actor,
                attempt.receipt.resource_token,
                phase,
            )
    def prepare_create_record(self, agent: Agent) -> AgentHomeReservation:
        """Commit a durable create claim and return a filesystem-only plan."""

        self._validate_config_location(agent)
        home = self.home_provisioner
        claim_key = f"agent-home-effect:create:{agent.actor}"
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            if self._db.execute(
                "SELECT 1 FROM agents WHERE actor = ?", (agent.actor,)
            ).fetchone() is not None:
                if agent.hosted_by == "host-invite":
                    # The public invitation path historically refuses an
                    # already-owned home with AGENT_ERROR. Actor preflight
                    # must preserve that code while rejecting before effects.
                    raise AgentError(
                        f"host invitation cannot adopt existing agent {agent.actor!r}"
                    )
                self._raise_agent_exists(agent)
            pending = self._db.execute(
                "SELECT payload FROM lifecycle_resources WHERE resource_key = ?",
                (claim_key,),
            ).fetchone()
            if pending is not None:
                payload = json.loads(str(pending["payload"]))
                if not isinstance(payload, dict):
                    raise AgentHomeError(
                        "invalid-registry-receipt", agent.actor, "create-replay"
                    )
                return self._create_reservation_from_payload(claim_key, payload)

            home_row = self._db.execute(
                "SELECT active, payload FROM lifecycle_resources WHERE resource_key = ?",
                (f"agent-home:{agent.actor}",),
            ).fetchone()
            revoked: HomeReceipt | None = None
            prior_home: HomeReceipt | None = None
            prior_home_active: bool | None = None
            replacement: HomeReceipt
            record_row = self._db.execute(
                "SELECT active, payload FROM lifecycle_resources WHERE resource_key = ?",
                (f"agent-record:{agent.actor}",),
            ).fetchone()
            if home_row is not None:
                try:
                    prior = HomeReceipt.from_json(json.loads(str(home_row["payload"])))
                except (ValueError, json.JSONDecodeError) as error:
                    raise AgentHomeError(
                        "invalid-registry-receipt", agent.actor, "create-claim"
                    ) from error
                prior_home = prior
                prior_home_active = bool(home_row["active"])
                if bool(home_row["active"]):
                    raise AgentHomeError(
                        "operation-pending", agent.actor, "create-claim"
                    )
                if prior.status == "revoked":
                    revoked = prior
                    replacement = home.claim_receipt(
                        actor=agent.actor, entity_token=agent.entity_token
                    )
                elif prior.status == "ready" and record_row is not None:
                    pending_agent = Agent.from_json(
                        json.loads(str(record_row["payload"])), "agent create replay"
                    )
                    if prior.entity_token != pending_agent.entity_token:
                        raise AgentHomeError(
                            "receipt-mismatch", agent.actor, "create-replay"
                        )
                    agent = pending_agent
                    replacement = prior
                elif prior.status == "cleaned":
                    replacement = home.claim_receipt(
                        actor=agent.actor, entity_token=agent.entity_token
                    )
                else:
                    raise AgentHomeError(
                        "operation-pending", agent.actor, "create-claim"
                    )
            else:
                replacement = home.claim_receipt(
                    actor=agent.actor, entity_token=agent.entity_token
                )
            payload = {
                "operation": "create",
                "agent": agent.to_json(),
                "replacement": replacement.to_json(),
                "revoked": None if revoked is None else revoked.to_json(),
                "priorHome": (
                    None if prior_home is None else prior_home.to_json()
                ),
                "priorHomeActive": prior_home_active,
            }
            encoded = json.dumps(payload, sort_keys=True)
            self._record_external_resource_locked(
                f"agent-record:{agent.actor}", False, agent.to_json()
            )
            self._db.execute(
                "INSERT INTO lifecycle_resources VALUES(?, ?, 1, ?)",
                (claim_key, uuid.uuid4().hex, encoded),
            )
        plan: HomeFilesystemPlan = (
            ProvisionHome(replacement)
            if revoked is None
            else ReplaceHome(revoked, replacement)
        )
        return AgentHomeReservation(
            "create", agent, plan, claim_key, encoded, changed=True
        )
    def prepare_create_command(self, command: object) -> AgentHomeReservation:
        from hyprial.identity.impl.agents.actor.ports  import (
            CreateAgentCommand,
            CreateHostInvitedAgentCommand,
            CreateTransferHostedAgentCommand,
        )

        if isinstance(command, CreateAgentCommand):
            name = self.native_actor(command.name)
            existing = self.get(name)
            if existing is not None:
                if not command.reuse_existing:
                    self._raise_agent_exists(existing)
                return self.prepare_existing_home(existing)
            agent = Agent(
                uri=self.uri_for(name),
                actor=name,
                owner=self.owner,
                machine=self.machine,
                cwd=command.cwd,
                config=command.config_payload(),
                provider=command.provider,
                model=command.model,
                capabilities=command.capabilities_payload(),
                harness_args=dict(command.harness_args),
                preferred_harness=(
                    command.preferred_harness or command.launch_harness
                ),
                created_at_ms=int(self._clock()),
            )
        elif isinstance(command, CreateTransferHostedAgentCommand):
            name = self.native_actor(command.name)
            agent = Agent(
                uri=_uri().canonical_agent_uri(
                    command.pinned_owner, self.machine, name
                ),
                actor=name,
                owner=command.pinned_owner,
                machine=self.machine,
                hosted_by="transfer-receive",
                cwd=command.cwd,
                harness_args=normalize_harness_args(dict(command.harness_args)),
                preferred_harness=command.preferred_harness,
                created_at_ms=int(self._clock()),
            )
        elif isinstance(command, CreateHostInvitedAgentCommand):
            if (
                not command.pinned_owner
                or command.pinned_owner != command.pinned_owner.strip()
                or ":" in command.pinned_owner
                or command.pinned_owner == self.owner
                or not command.entity_token
            ):
                raise ValueError("invalid host-invited owner")
            name = self.native_actor(command.name)
            agent = Agent(
                uri=_uri().canonical_agent_uri(
                    command.pinned_owner, self.machine, name
                ),
                actor=name,
                owner=command.pinned_owner,
                machine=self.machine,
                entity_token=command.entity_token,
                hosted_by="host-invite",
                cwd=command.cwd,
                harness_args=normalize_harness_args(dict(command.harness_args)),
                preferred_harness=command.preferred_harness,
                created_at_ms=int(self._clock()),
            )
        else:
            raise TypeError("unsupported Agent create command")
        return self.prepare_create_record(agent)
    def prepare_existing_home(self, agent: Agent) -> AgentHomeReservation:
        home = self.home_provisioner
        claim_key = f"agent-home-effect:ensure:{agent.actor}"
        with self._lock, self._db:
            current = self.require(agent.actor)
            if current.entity_token != agent.entity_token:
                raise AgentHomeError("create-fenced", agent.actor, "ensure-claim")
            row = self._db.execute(
                "SELECT active,payload FROM lifecycle_resources WHERE resource_key=?",
                (f"agent-home:{agent.actor}",),
            ).fetchone()
            if row is not None and bool(row["active"]):
                receipt = HomeReceipt.from_json(json.loads(str(row["payload"])))
                if receipt.status != "ready" or receipt.entity_token != agent.entity_token:
                    raise AgentHomeError(
                        "receipt-mismatch", agent.actor, "ensure-claim"
                    )
                return AgentHomeReservation(
                    "create", agent, ProvisionHome(receipt), changed=False
                )
            pending = self._db.execute(
                "SELECT payload FROM lifecycle_resources WHERE resource_key=?",
                (claim_key,),
            ).fetchone()
            if pending is not None:
                payload = json.loads(str(pending["payload"]))
                if not isinstance(payload, dict):
                    raise AgentHomeError(
                        "invalid-registry-receipt", agent.actor, "ensure-replay"
                    )
                receipt = HomeReceipt.from_json(payload.get("replacement"))
                encoded = json.dumps(payload, sort_keys=True)
                return AgentHomeReservation(
                    "create", agent, ProvisionHome(receipt), claim_key, encoded
                )
            if row is not None:
                prior = HomeReceipt.from_json(json.loads(str(row["payload"])))
                if prior.status != "cleaned":
                    raise AgentHomeError(
                        "operation-pending", agent.actor, "ensure-claim"
                    )
            receipt = home.claim_receipt(
                actor=agent.actor, entity_token=agent.entity_token
            )
            payload = {
                "operation": "ensure",
                "actor": agent.actor,
                "entityToken": agent.entity_token,
                "replacement": receipt.to_json(),
                "priorHome": (
                    None
                    if row is None
                    else json.loads(str(row["payload"]))
                ),
                "priorHomeActive": (
                    None if row is None else bool(row["active"])
                ),
            }
            encoded = json.dumps(payload, sort_keys=True)
            self._db.execute(
                "INSERT INTO lifecycle_resources VALUES(?, ?, 1, ?)",
                (claim_key, uuid.uuid4().hex, encoded),
            )
        return AgentHomeReservation(
            "create", agent, ProvisionHome(receipt), claim_key, encoded
        )
    def commit_existing_home(
        self, reservation: AgentHomeReservation, receipt: HomeReceipt
    ) -> Agent:
        agent = reservation.agent
        if agent is None or not isinstance(reservation.plan, ProvisionHome):
            raise TypeError("not an Agent ensure-home reservation")
        if receipt != reservation.plan.receipt:
            raise AgentHomeError("create-fenced", agent.actor, "ensure-completion")
        with self._lock, self._db:
            current = self.require(agent.actor)
            if current.entity_token != agent.entity_token:
                raise AgentHomeError("create-fenced", agent.actor, "ensure-commit")
            row = self._db.execute(
                "SELECT active,payload FROM lifecycle_resources WHERE resource_key=?",
                (f"agent-home:{agent.actor}",),
            ).fetchone()
            if reservation.claim_key is None:
                if (
                    row is None
                    or not bool(row["active"])
                    or HomeReceipt.from_json(json.loads(str(row["payload"])))
                    != receipt
                ):
                    raise AgentHomeError(
                        "create-fenced", agent.actor, "ensure-commit"
                    )
                return current
            pending = self._db.execute(
                "SELECT payload FROM lifecycle_resources WHERE resource_key=?",
                (reservation.claim_key,),
            ).fetchone()
            if pending is None or str(pending["payload"]) != reservation.claim_payload:
                raise AgentHomeError("create-fenced", agent.actor, "ensure-commit")
            claim_payload = json.loads(reservation.claim_payload)
            expected_home = claim_payload.get("priorHome")
            expected_active = claim_payload.get("priorHomeActive")
            if expected_home is None:
                if row is not None:
                    raise AgentHomeError(
                        "create-fenced", agent.actor, "ensure-commit"
                    )
            elif (
                row is None
                or bool(row["active"]) != bool(expected_active)
                or json.loads(str(row["payload"])) != expected_home
            ):
                raise AgentHomeError("create-fenced", agent.actor, "ensure-commit")
            self._record_home_resource_locked(receipt, True)
            self._db.execute(
                "DELETE FROM lifecycle_resources WHERE resource_key=?",
                (reservation.claim_key,),
            )
            return current
    @staticmethod
    def _create_reservation_from_payload(
        claim_key: str, payload: Mapping[str, object]
    ) -> AgentHomeReservation:
        agent = Agent.from_json(payload.get("agent"), "agent create claim")
        replacement = HomeReceipt.from_json(payload.get("replacement"))
        raw_revoked = payload.get("revoked")
        revoked = None if raw_revoked is None else HomeReceipt.from_json(raw_revoked)
        plan: HomeFilesystemPlan = (
            ProvisionHome(replacement)
            if revoked is None
            else ReplaceHome(revoked, replacement)
        )
        return AgentHomeReservation(
            "create",
            agent,
            plan,
            claim_key,
            json.dumps(dict(payload), sort_keys=True),
            changed=True,
        )
    def commit_create_record(
        self, reservation: AgentHomeReservation, receipt: HomeReceipt
    ) -> Agent:
        if reservation.operation != "create" or reservation.agent is None:
            raise TypeError("not an Agent create reservation")
        if reservation.claim_key is None or reservation.claim_payload is None:
            raise TypeError("Agent create reservation is not durable")
        agent = reservation.agent
        replacement = (
            reservation.plan.receipt
            if isinstance(reservation.plan, ProvisionHome)
            else reservation.plan.replacement
        )
        if receipt != replacement:
            raise AgentHomeError("create-fenced", agent.actor, "create-completion")
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            pending = self._db.execute(
                "SELECT payload FROM lifecycle_resources WHERE resource_key = ?",
                (reservation.claim_key,),
            ).fetchone()
            if pending is None or str(pending["payload"]) != reservation.claim_payload:
                raise AgentHomeError("create-fenced", agent.actor, "create-commit")
            if self._db.execute(
                "SELECT 1 FROM agents WHERE actor = ?", (agent.actor,)
            ).fetchone() is not None:
                raise AgentHomeError("create-fenced", agent.actor, "create-commit")
            claim_payload = json.loads(reservation.claim_payload)
            expected_home = claim_payload.get("priorHome")
            expected_active = claim_payload.get("priorHomeActive")
            home_row = self._db.execute(
                "SELECT active,payload FROM lifecycle_resources WHERE resource_key=?",
                (f"agent-home:{agent.actor}",),
            ).fetchone()
            if expected_home is None:
                if home_row is not None:
                    raise AgentHomeError(
                        "create-fenced", agent.actor, "create-commit"
                    )
            elif (
                home_row is None
                or bool(home_row["active"]) != bool(expected_active)
                or json.loads(str(home_row["payload"])) != expected_home
            ):
                raise AgentHomeError("create-fenced", agent.actor, "create-commit")
            self._db.execute(*self._insert_statement(agent))
            self._record_external_resource_locked(
                f"agent-record:{agent.actor}", True, agent.to_json()
            )
            self._record_home_resource_locked(receipt, True)
            for adapter in agent.pinned_adapters:
                self._db.execute(
                    "INSERT INTO pins(adapter, agent) VALUES(?, ?)",
                    (adapter, agent.actor),
                )
            if agent.hosted_by == "host-invite":
                self._record_host_invite_locked(agent)
            self._db.execute(
                "DELETE FROM lifecycle_resources WHERE resource_key = ?",
                (reservation.claim_key,),
            )
        return agent
    def revoke_failed_create_record(
        self, reservation: AgentHomeReservation, receipt: HomeReceipt
    ) -> None:
        """Keep a failed commit's exact home under durable destroy custody.

        The create claim stays until ``commit_cleanup_home`` settles the
        filesystem effect.  No agent or grant-journal row is written here.
        """

        agent = reservation.agent
        expected_receipt = (
            reservation.plan.receipt
            if isinstance(reservation.plan, ProvisionHome)
            else reservation.plan.replacement
            if isinstance(reservation.plan, ReplaceHome)
            else None
        )
        if (
            reservation.operation != "create" or agent is None
            or reservation.claim_key is None or reservation.claim_payload is None
            or receipt != expected_receipt
            or receipt.actor != agent.actor or receipt.entity_token != agent.entity_token
        ):
            raise AgentHomeError("create-fenced", receipt.actor, "failed-create")
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            claim = self._db.execute(
                "SELECT payload FROM lifecycle_resources WHERE resource_key=?",
                (reservation.claim_key,),
            ).fetchone()
            record = self._db.execute(
                "SELECT active,payload FROM lifecycle_resources WHERE resource_key=?",
                (f"agent-record:{agent.actor}",),
            ).fetchone()
            incumbent = self._db.execute(
                "SELECT 1 FROM agents WHERE actor=?", (agent.actor,)
            ).fetchone()
            if (
                claim is None or str(claim["payload"]) != reservation.claim_payload
                or record is None or bool(record["active"])
                or json.loads(str(record["payload"])) != agent.to_json()
                or incumbent is not None
            ):
                raise AgentHomeError("create-fenced", agent.actor, "failed-create")
            prior = json.loads(reservation.claim_payload).get("priorHome")
            home_row = self._db.execute(
                "SELECT active,payload FROM lifecycle_resources WHERE resource_key=?",
                (f"agent-home:{agent.actor}",),
            ).fetchone()
            if prior is None:
                if home_row is not None:
                    raise AgentHomeError("create-fenced", agent.actor, "failed-create")
            elif (
                home_row is None or bool(home_row["active"])
                or json.loads(str(home_row["payload"])) != prior
            ):
                raise AgentHomeError("create-fenced", agent.actor, "failed-create")
            self._record_home_resource_locked(
                replace(receipt, status="revoked"), False
            )
    def abort_create_record(self, reservation: AgentHomeReservation) -> None:
        if reservation.claim_key is None or reservation.claim_payload is None:
            return
        agent = reservation.agent
        if agent is None:
            return
        with self._lock, self._db:
            pending = self._db.execute(
                "SELECT payload FROM lifecycle_resources WHERE resource_key = ?",
                (reservation.claim_key,),
            ).fetchone()
            if pending is None or str(pending["payload"]) != reservation.claim_payload:
                return
            self._db.execute(
                "DELETE FROM lifecycle_resources WHERE resource_key = ?",
                (reservation.claim_key,),
            )
            row = self._db.execute(
                "SELECT active, payload FROM lifecycle_resources WHERE resource_key = ?",
                (f"agent-record:{agent.actor}",),
            ).fetchone()
            if (
                row is not None
                and not bool(row["active"])
                and json.loads(str(row["payload"])) == agent.to_json()
            ):
                self._db.execute(
                    "DELETE FROM lifecycle_resources WHERE resource_key = ?",
                    (f"agent-record:{agent.actor}",),
                )
    def _create_record(self, agent: Agent) -> Agent:
        self._validate_config_location(agent)
        resume_claim: HomeReceipt | None = None
        if self._home is not None:
            with self._lock:
                home_row = self._db.execute(
                    "SELECT active, payload FROM lifecycle_resources "
                    "WHERE resource_key = ?",
                    (f"agent-home:{agent.actor}",),
                ).fetchone()
                record_row = self._db.execute(
                    "SELECT active, payload FROM lifecycle_resources "
                    "WHERE resource_key = ?",
                    (f"agent-record:{agent.actor}",),
                ).fetchone()
                incumbent = self._db.execute(
                    "SELECT 1 FROM agents WHERE actor = ?", (agent.actor,)
                ).fetchone()
            if (
                incumbent is None
                and home_row is not None
                and not bool(home_row["active"])
                and record_row is not None
                and not bool(record_row["active"])
            ):
                try:
                    pending_home = HomeReceipt.from_json(
                        json.loads(str(home_row["payload"]))
                    )
                    pending_agent_payload = json.loads(str(record_row["payload"]))
                    pending_agent = Agent.from_json(
                        pending_agent_payload, "agent create replay"
                    )
                except (ValueError, json.JSONDecodeError):
                    pending_home = None
                if (
                    pending_home is not None
                    and pending_home.status == "ready"
                    and pending_home.actor == agent.actor
                    and pending_home.entity_token == pending_agent.entity_token
                ):
                    # Admission succeeded before a crash/timeout. Replays
                    # converge that exact incarnation instead of allocating a
                    # new token or deleting the already-created filesystem.
                    agent = pending_agent
                    resume_claim = pending_home
        # A daemon may have crashed after committing destroy's durable revoke
        # but before finishing filesystem retirement.  The revoked receipt,
        # not the requested name, authorizes this retry cleanup.
        if resume_claim is None:
            self.cleanup_revoked_home(agent.actor)
        if self._home is None:
            with self._lock:
                try:
                    with self._db:
                        self._db.execute(*self._insert_statement(agent))
                        self._record_external_resource_locked(
                            f"agent-record:{agent.actor}", True, agent.to_json()
                        )
                        for adapter in agent.pinned_adapters:
                            self._db.execute(
                                "INSERT INTO pins(adapter, agent) VALUES(?, ?)",
                                (adapter, agent.actor),
                            )
                        if agent.hosted_by == "host-invite":
                            self._record_host_invite_locked(agent)
                except sqlite3.IntegrityError as error:
                    self._raise_agent_exists(agent, error)
            return agent

        claim = resume_claim or self._home.claim_receipt(
            actor=agent.actor, entity_token=agent.entity_token
        )
        if resume_claim is None:
            with self._lock:
                try:
                    with self._db:
                        self._db.execute("BEGIN IMMEDIATE")
                        if self._db.execute(
                            "SELECT 1 FROM agents WHERE actor = ?", (agent.actor,)
                        ).fetchone() is not None:
                            self._raise_agent_exists(agent)
                        row = self._db.execute(
                            "SELECT active, payload FROM lifecycle_resources "
                            "WHERE resource_key = ?",
                            (f"agent-home:{agent.actor}",),
                        ).fetchone()
                        if row is not None:
                            try:
                                prior = HomeReceipt.from_json(
                                    json.loads(str(row["payload"]))
                                )
                            except (ValueError, json.JSONDecodeError) as error:
                                raise AgentHomeError(
                                    "invalid-registry-receipt",
                                    agent.actor,
                                    "create-claim",
                                ) from error
                            if bool(row["active"]) or prior.status != "cleaned":
                                raise AgentHomeError(
                                    "operation-pending", agent.actor, "create-claim"
                                )
                        # The inactive exact receipt is the durable authority for
                        # the filesystem lane. A retry cannot allocate a new token
                        # or delete a newer incarnation behind this fence.
                        self._record_home_resource_locked(claim, False)
                        self._record_external_resource_locked(
                            f"agent-record:{agent.actor}", False, agent.to_json()
                        )
                except sqlite3.IntegrityError as error:
                    self._raise_agent_exists(agent, error)

        home_attempt: HomeProvisioningAttempt | None = None
        try:
            home_attempt = self._home.provision_claimed(claim)
            with self._lock, self._db:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute(
                    "SELECT active, payload FROM lifecycle_resources "
                    "WHERE resource_key = ?",
                    (f"agent-home:{agent.actor}",),
                ).fetchone()
                incumbent = self._db.execute(
                    "SELECT 1 FROM agents WHERE actor = ?", (agent.actor,)
                ).fetchone()
                if row is None or bool(row["active"]) or incumbent is not None:
                    raise AgentHomeError(
                        "create-fenced", agent.actor, "create-commit"
                    )
                try:
                    current = HomeReceipt.from_json(json.loads(str(row["payload"])))
                except (ValueError, json.JSONDecodeError) as error:
                    raise AgentHomeError(
                        "invalid-registry-receipt", agent.actor, "create-commit"
                    ) from error
                if current != claim:
                    raise AgentHomeError(
                        "create-fenced", agent.actor, "create-commit"
                    )
                self._db.execute(*self._insert_statement(agent))
                self._record_external_resource_locked(
                    f"agent-record:{agent.actor}", True, agent.to_json()
                )
                self._record_home_resource_locked(claim, True)
                for adapter in agent.pinned_adapters:
                    self._db.execute(
                        "INSERT INTO pins(adapter, agent) VALUES(?, ?)",
                        (adapter, agent.actor),
                    )
                if agent.hosted_by == "host-invite":
                    self._record_host_invite_locked(agent)
        except BaseException as error:
            if home_attempt is not None:
                self._compensate_home_fs(home_attempt, "create-commit")
            with self._lock, self._db:
                row = self._db.execute(
                    "SELECT active, payload FROM lifecycle_resources "
                    "WHERE resource_key = ?",
                    (f"agent-home:{agent.actor}",),
                ).fetchone()
                if row is not None and not bool(row["active"]):
                    try:
                        current = HomeReceipt.from_json(json.loads(str(row["payload"])))
                    except (ValueError, json.JSONDecodeError):
                        current = None
                    if current == claim:
                        self._record_home_resource_locked(
                            replace(claim, status="revoked"), False
                        )
            if isinstance(error, sqlite3.IntegrityError):
                self._raise_agent_exists(agent, error)
            raise
        return agent
    def _raise_agent_exists(
        self, agent: Agent, error: BaseException | None = None
    ) -> None:
        message = (
            f"the name {agent.actor!r} is already taken on this node "
            f"({self.owner}@{self.machine}) — {agent.uri} exists "
            f"({self.database}). One name is one agent, whether or "
            f"not anything is currently running under it. To reuse "
            f"the name, destroy that agent first ('hyprial agent destroy "
            f"{agent.actor}', which is irreversible); to run this "
            f"agent on a different harness, just start it there — "
            f"that is a rebinding of the same agent, not a new one."
        )
        if error is None:
            raise AgentExistsError(message)
        raise AgentExistsError(message) from error
    def _validate_config_location(self, agent: Agent) -> None:
        agent_home = (
            None
            if self._home is None
            else self._home.agents_root / agent.actor
        )
        validate_agent_config_location(
            agent.config,
            agent_home=agent_home,
            cwd=agent.cwd,
        )
