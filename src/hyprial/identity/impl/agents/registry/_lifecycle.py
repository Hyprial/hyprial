from __future__ import annotations

from hyprial.identity.impl.agents.home.provisioner import AgentHomeError
from typing import Any
from hyprial.identity.impl.agents.home.effects import CleanupHome
from hyprial.identity.impl.agents.home.effects import HomeFilesystemPlan
from hyprial.identity.impl.agents.home.provisioner import HomeReceipt
from hyprial.identity.impl.agents.home.effects import ProvisionHome
from hyprial.identity.impl.agents.home.effects import ReplaceHome
import json
import sqlite3
import uuid

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hyprial.kernel import DomainEffectClaim

from ._base import (
    Agent,
    AgentHomeReservation,
    _is_compensating_attempt,
)


class _RegistryLifecycleMixin:
    def apply_lifecycle(self, request: object) -> tuple[Any, str, bool]:
        """Apply identity/binding mutation with its token receipt atomically.

        The returned tuple is ``(MutationProvenance, operation, replayed)``.
        Runtime liveness is a projection of the durable binding resource and
        is refreshed by :class:`AgentActor` after this transaction commits.
        """

        from hyprial.identity.impl.agents.actor.ports import (
            BindAgentCommand,
            CreateAgentCommand,
            DestroyAgentCommand,
            ReleaseAgentCommand,
        )
        from hyprial.kernel import MutationProvenance
        from hyprial.kernel import LifecycleMutationRequest

        if not isinstance(request, LifecycleMutationRequest):
            raise TypeError("request must be LifecycleMutationRequest")
        payload = request.payload
        if self._home is not None and isinstance(
            payload, (CreateAgentCommand, DestroyAgentCommand)
        ):
            reservation, settled, operation, replayed = (
                self.prepare_home_lifecycle(request)
            )
            if reservation is None:
                assert settled is not None
                return settled, operation, replayed
            receipt = (
                None
                if reservation.plan is None
                else self._execute_home_plan(reservation.plan)
            )
            return (
                self.complete_home_lifecycle(reservation, receipt),
                operation,
                False,
            )
        agent_name: str | None = None
        if isinstance(payload, (CreateAgentCommand, DestroyAgentCommand)):
            agent_name = (
                self.native_actor(payload.name) if isinstance(payload, CreateAgentCommand)
                else self.normalize_actor(payload.name)
            )
            key = f"agent-record:{agent_name}"
            operation = "create" if isinstance(payload, CreateAgentCommand) else "destroy"
        elif isinstance(payload, (BindAgentCommand, ReleaseAgentCommand)):
            agent = self.require(payload.actor)
            key = f"binding:{agent.uri}"
            operation = "bind" if isinstance(payload, BindAgentCommand) else "release"
        else:
            raise TypeError(f"unsupported Agent lifecycle payload: {type(payload).__name__}")
        with self._home_transaction() as home_attempts:
            prior = self._db.execute(
                "SELECT * FROM lifecycle_receipts WHERE attempt_token = ?",
                (request.attempt_token,),
            ).fetchone()
            if prior is not None:
                if (
                    prior["operation_id"] != request.operation_id
                    or prior["resource_key"] != key
                    or prior["expected_resource_token"]
                    != request.expected_resource_token
                ):
                    raise ValueError("lifecycle attempt token was reused")
                return (
                    MutationProvenance(
                        bool(prior["created_by_operation"]),
                        bool(prior["changed"]),
                        str(prior["resource_token"]),
                    ),
                    operation,
                    True,
                )

            row = self._db.execute(
                "SELECT resource_token, active, payload FROM lifecycle_resources "
                "WHERE resource_key = ?",
                (key,),
            ).fetchone()
            actual_payload: dict[str, object] = {}
            actual_active = False
            if agent_name is not None:
                agent_row = self._db.execute(
                    "SELECT * FROM agents WHERE actor = ?", (agent_name,)
                ).fetchone()
                if agent_row is not None:
                    actual_active = True
                    actual_agent = self._row_agent(
                        agent_row, self._pinned_adapters_locked(str(agent_row["actor"]))
                    )
                    actual_payload = actual_agent.to_json()
            elif row is not None and bool(row["active"]):
                actual_active = True
                loaded = json.loads(str(row["payload"]))
                if isinstance(loaded, dict):
                    actual_payload = loaded

            if row is None or bool(row["active"]) != actual_active or (
                actual_active and json.loads(str(row["payload"])) != actual_payload
            ):
                token = uuid.uuid4().hex
                self._db.execute(
                    "INSERT INTO lifecycle_resources VALUES(?, ?, ?, ?) "
                    "ON CONFLICT(resource_key) DO UPDATE SET "
                    "resource_token=excluded.resource_token, active=excluded.active, "
                    "payload=excluded.payload",
                    (key, token, int(actual_active), json.dumps(actual_payload, sort_keys=True)),
                )
            else:
                token = str(row["resource_token"])

            expected = request.expected_resource_token
            is_create = operation in {"create", "bind"}
            changed = False
            created = False
            stored_payload = actual_payload
            if operation == "bind" and expected is None:
                desired_payload = self._lifecycle_create_payload(payload, row)
                if not actual_active or actual_payload != desired_payload:
                    # A dead connector may leave a durable binding receipt
                    # behind.  Explicitly binding a different harness is a
                    # replacement generation, not a no-op; rotate its fence
                    # while the Agent actor updates projection and resource in
                    # the same SQLite transaction.
                    token = uuid.uuid4().hex
                    changed = created = True
                    stored_payload = desired_payload
            elif is_create:
                if expected is None:
                    if not actual_active:
                        token = uuid.uuid4().hex
                        changed = created = True
                        stored_payload = self._lifecycle_create_payload(payload, row)
                elif not actual_active and token == expected:
                    changed = created = True
                    stored_payload = self._lifecycle_create_payload(
                        payload, row, reuse_resource_payload=True
                    )
            elif actual_active and (expected is None or token == expected):
                changed = created = True

            if changed and operation == "create":
                agent = Agent.from_json(stored_payload, "lifecycle.agent")
                home_attempt = self._provision_home_locked(
                    agent, allow_revoked=expected is not None
                )
                if home_attempt is not None:
                    home_attempts.append(home_attempt)
                self._db.execute(*self._insert_statement(agent))
                if home_attempt is not None:
                    self._record_home_resource_locked(home_attempt.receipt, True)
                for adapter in agent.pinned_adapters:
                    self._db.execute(
                        "INSERT INTO pins(adapter, agent) VALUES(?, ?)",
                        (adapter, agent.actor),
                    )
            elif changed and operation == "destroy":
                assert agent_name is not None
                prior_agent = self.require(agent_name)
                if _is_compensating_attempt(request.attempt_token):
                    # A create saga compensating its own half-built agent: the
                    # home is a half-product of THIS operation, never a
                    # credential-bearing residue, so the rollback removes it
                    # fully instead of leaving it for an explicit operator
                    # cleanup (the T13 residue is only for a forward destroy).
                    self._compensate_home_locked(prior_agent)
                else:
                    self._revoke_home_locked(prior_agent)
                self._db.execute(
                    "DELETE FROM agents WHERE actor = ?", (agent_name,)
                )
            elif changed and operation == "bind":
                assert isinstance(payload, BindAgentCommand)
                agent = self.require(payload.actor)
                self._db.execute(
                    "UPDATE agents SET last_harness = ?, last_session_id = ?, "
                    "preferred_harness = COALESCE(preferred_harness, ?) "
                    "WHERE actor = ?",
                    (
                        payload.harness,
                        payload.session_id,
                        payload.harness,
                        agent.actor,
                    ),
                )
                # U0c: the saga's own bind drifts the agents row (last_harness,
                # preferred_harness), and the agent-record fence compares its
                # payload against exactly that row.  Without this sync the
                # next lifecycle mutation's reconcile would rotate the fence
                # token and the create saga's compensation (destroy with the
                # CREATE token) would be fenced off as if an EXTERNAL
                # replacement had happened -- leaving the half-built agent
                # behind (U0c ④).  Same token, refreshed payload: external
                # mutations still drift and still fence.
                record_key = f"agent-record:{agent.actor}"
                refreshed = self.require(payload.actor)
                self._db.execute(
                    "UPDATE lifecycle_resources SET payload = ? "
                    "WHERE resource_key = ?",
                    (
                        json.dumps(refreshed.to_json(), sort_keys=True),
                        record_key,
                    ),
                )

            active = actual_active
            if changed:
                active = is_create
            self._db.execute(
                "INSERT INTO lifecycle_resources VALUES(?, ?, ?, ?) "
                "ON CONFLICT(resource_key) DO UPDATE SET "
                "resource_token=excluded.resource_token, active=excluded.active, "
                "payload=excluded.payload",
                (key, token, int(active), json.dumps(stored_payload, sort_keys=True)),
            )
            self._db.execute(
                "INSERT INTO lifecycle_receipts VALUES(?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    request.attempt_token,
                    request.operation_id,
                    key,
                    expected,
                    int(created),
                    int(changed),
                    token,
                ),
            )
            return MutationProvenance(created, changed, token), operation, False
    def _begin_home_lifecycle(
        self, request: object
    ) -> tuple[dict[str, object] | None, Any | None, str, bool]:
        """Durably claim Agent-home I/O without holding SQLite across it."""

        from hyprial.identity.impl.agents.actor.ports import CreateAgentCommand, DestroyAgentCommand
        from hyprial.kernel import MutationProvenance
        from hyprial.kernel import LifecycleMutationRequest

        assert isinstance(request, LifecycleMutationRequest)
        payload = request.payload
        assert isinstance(payload, (CreateAgentCommand, DestroyAgentCommand))
        operation = "create" if isinstance(payload, CreateAgentCommand) else "destroy"
        name = (
            self.native_actor(payload.name)
            if operation == "create"
            else self.normalize_actor(payload.name)
        )
        key = f"agent-record:{name}"
        effect_key = f"agent-home-effect:{request.attempt_token}"
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            prior = self._db.execute(
                "SELECT * FROM lifecycle_receipts WHERE attempt_token = ?",
                (request.attempt_token,),
            ).fetchone()
            if prior is not None:
                if (
                    prior["operation_id"] != request.operation_id
                    or prior["resource_key"] != key
                    or prior["expected_resource_token"]
                    != request.expected_resource_token
                ):
                    raise ValueError("lifecycle attempt token was reused")
                return (
                    None,
                    MutationProvenance(
                        bool(prior["created_by_operation"]),
                        bool(prior["changed"]),
                        str(prior["resource_token"]),
                    ),
                    operation,
                    True,
                )
            pending = self._db.execute(
                "SELECT payload FROM lifecycle_resources WHERE resource_key = ?",
                (effect_key,),
            ).fetchone()
            if pending is not None:
                claim = json.loads(str(pending["payload"]))
                if not isinstance(claim, dict) or any(
                    claim.get(field) != expected
                    for field, expected in (
                        ("attemptToken", request.attempt_token),
                        ("operationId", request.operation_id),
                        ("expectedResourceToken", request.expected_resource_token),
                    )
                ):
                    raise ValueError("lifecycle attempt token was reused")
                return claim, None, operation, False

            row = self._db.execute(
                "SELECT resource_token, active, payload FROM lifecycle_resources "
                "WHERE resource_key = ?",
                (key,),
            ).fetchone()
            agent_row = self._db.execute(
                "SELECT * FROM agents WHERE actor = ?", (name,)
            ).fetchone()
            actual_active = agent_row is not None
            actual_payload: dict[str, object] = {}
            if agent_row is not None:
                actual_payload = self._row_agent(
                    agent_row, self._pinned_adapters_locked(name)
                ).to_json()
            if row is None or bool(row["active"]) != actual_active or (
                actual_active and json.loads(str(row["payload"])) != actual_payload
            ):
                token = uuid.uuid4().hex
            else:
                token = str(row["resource_token"])
            expected = request.expected_resource_token
            changed = created = False
            stored_payload = actual_payload
            if operation == "create":
                if expected is None and not actual_active:
                    token = uuid.uuid4().hex
                    changed = created = True
                    stored_payload = self._lifecycle_create_payload(payload, row)
                elif not actual_active and token == expected:
                    changed = created = True
                    stored_payload = self._lifecycle_create_payload(
                        payload, row, reuse_resource_payload=True
                    )
            elif actual_active and (expected is None or token == expected):
                changed = created = True

            if not changed:
                self._db.execute(
                    "INSERT INTO lifecycle_resources VALUES(?, ?, ?, ?) "
                    "ON CONFLICT(resource_key) DO UPDATE SET "
                    "resource_token=excluded.resource_token, active=excluded.active, "
                    "payload=excluded.payload",
                    (key, token, int(actual_active), json.dumps(stored_payload, sort_keys=True)),
                )
                self._db.execute(
                    "INSERT INTO lifecycle_receipts VALUES(?, ?, ?, ?, ?, ?, ?, 0)",
                    (
                        request.attempt_token, request.operation_id, key, expected,
                        0, 0, token,
                    ),
                )
                return None, MutationProvenance(False, False, token), operation, False

            revoked_home: HomeReceipt | None = None
            replacement_home: HomeReceipt | None = None
            prior_home: HomeReceipt | None = None
            prior_home_active: bool | None = None
            if operation == "create":
                assert self._home is not None
                desired_agent = Agent.from_json(stored_payload, "lifecycle.agent")
                home_row = self._db.execute(
                    "SELECT active, payload FROM lifecycle_resources "
                    "WHERE resource_key = ?",
                    (f"agent-home:{name}",),
                ).fetchone()
                if home_row is not None:
                    prior_home = HomeReceipt.from_json(
                        json.loads(str(home_row["payload"]))
                    )
                    prior_home_active = bool(home_row["active"])
                    if bool(home_row["active"]):
                        raise AgentHomeError(
                            "create-fenced", name, "lifecycle-claim"
                        )
                    if prior_home.status == "revoked":
                        revoked_home = prior_home
                    elif (
                        prior_home.status == "ready"
                        and prior_home.entity_token == desired_agent.entity_token
                    ):
                        replacement_home = prior_home
                    elif prior_home.status != "cleaned":
                        raise AgentHomeError(
                            "operation-pending", name, "lifecycle-claim"
                        )
                if replacement_home is None:
                    replacement_home = self._home.claim_receipt(
                        actor=name, entity_token=desired_agent.entity_token
                    )
            if operation == "destroy":
                assert agent_row is not None
                prior_agent = self._row_agent(
                    agent_row, self._pinned_adapters_locked(name)
                )
                revoked_home = self._revoke_home_locked(prior_agent)
                self._db.execute("DELETE FROM agents WHERE actor = ?", (name,))
            self._db.execute(
                "INSERT INTO lifecycle_resources VALUES(?, ?, ?, ?) "
                "ON CONFLICT(resource_key) DO UPDATE SET "
                "resource_token=excluded.resource_token, active=excluded.active, "
                "payload=excluded.payload",
                (key, token, 0, json.dumps(stored_payload, sort_keys=True)),
            )
            claim: dict[str, object] = {
                "attemptToken": request.attempt_token,
                "operationId": request.operation_id,
                "expectedResourceToken": expected,
                "operation": operation,
                "actor": name,
                "resourceToken": token,
                "createdByOperation": created,
                "payload": stored_payload,
                "homeReplacement": (
                    None
                    if replacement_home is None
                    else replacement_home.to_json()
                ),
                "homeRevoked": (
                    None if revoked_home is None else revoked_home.to_json()
                ),
                "priorHome": (
                    None if prior_home is None else prior_home.to_json()
                ),
                "priorHomeActive": prior_home_active,
            }
            self._db.execute(
                "INSERT INTO lifecycle_resources VALUES(?, ?, 1, ?)",
                (effect_key, uuid.uuid4().hex, json.dumps(claim, sort_keys=True)),
            )
            return claim, None, operation, False
    def prepare_home_lifecycle(
        self, request: object
    ) -> tuple[AgentHomeReservation | None, Any | None, str, bool]:
        claim, settled, operation, replayed = self._begin_home_lifecycle(request)
        if operation == "destroy" and claim is not None:
            actor = str(claim["actor"])
            with self._lock:
                self._restore_dispositions.pop(actor, None)
                self._agent_blocks.pop(actor, None)
        if claim is None:
            return None, settled, operation, replayed
        payload = claim.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("invalid Agent home lifecycle payload")
        actor = str(claim["actor"])
        raw_replacement = claim.get("homeReplacement")
        raw_revoked = claim.get("homeRevoked")
        replacement = (
            None
            if raw_replacement is None
            else HomeReceipt.from_json(raw_replacement)
        )
        revoked = (
            None if raw_revoked is None else HomeReceipt.from_json(raw_revoked)
        )
        plan: HomeFilesystemPlan | None
        agent: Agent | None
        if operation == "create":
            if replacement is None:
                raise AgentHomeError(
                    "invalid-registry-receipt", actor, "lifecycle-claim"
                )
            plan = (
                ProvisionHome(replacement)
                if revoked is None
                else ReplaceHome(revoked, replacement)
            )
            agent = Agent.from_json(payload, "lifecycle.agent")
        else:
            plan = None if revoked is None else CleanupHome(revoked)
            agent = Agent.from_json(payload, "lifecycle.agent")
        encoded = json.dumps(claim, sort_keys=True)
        return (
            AgentHomeReservation(
                operation,
                agent,
                plan,
                f"agent-home-effect:{claim['attemptToken']}",
                encoded,
                str(claim["attemptToken"]),
                changed=True,
            ),
            None,
            operation,
            replayed,
        )
    def _execute_home_plan(self, plan: HomeFilesystemPlan) -> HomeReceipt:
        home = self.home_provisioner
        if isinstance(plan, ProvisionHome):
            return home.provision_claimed(plan.receipt).receipt
        if isinstance(plan, ReplaceHome):
            try:
                return home.provision_claimed(plan.replacement).receipt
            except AgentHomeError as error:
                if error.category not in {"receipt-mismatch", "unowned-residue"}:
                    raise
                home.cleanup(
                    plan.revoked, expected_token=plan.revoked.resource_token
                )
                return home.provision_claimed(plan.replacement).receipt
        if isinstance(plan, CleanupHome):
            return home.cleanup(
                plan.receipt, expected_token=plan.receipt.resource_token
            )
        raise TypeError("unsupported Agent home filesystem plan")
    def complete_home_lifecycle(
        self,
        reservation: AgentHomeReservation,
        receipt: HomeReceipt | None,
    ) -> Any:
        from hyprial.kernel import MutationProvenance

        if reservation.claim_payload is None:
            raise TypeError("lifecycle home reservation is not durable")
        claim = json.loads(reservation.claim_payload)
        if not isinstance(claim, dict):
            raise TypeError("invalid lifecycle home reservation")
        attempt = str(claim["attemptToken"])
        operation_id = str(claim["operationId"])
        operation = str(claim["operation"])
        actor = str(claim["actor"])
        token = str(claim["resourceToken"])
        expected = claim.get("expectedResourceToken")
        payload = claim.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("invalid Agent home lifecycle payload")
        key = f"agent-record:{actor}"
        effect_key = f"agent-home-effect:{attempt}"
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            pending = self._db.execute(
                "SELECT payload FROM lifecycle_resources WHERE resource_key = ?",
                (effect_key,),
            ).fetchone()
            if pending is None or json.loads(str(pending["payload"])) != dict(claim):
                raise AgentHomeError("create-fenced", actor, "lifecycle-commit")
            incumbent = self._db.execute(
                "SELECT * FROM agents WHERE actor = ?", (actor,)
            ).fetchone()
            if operation == "create":
                if incumbent is not None or reservation.agent is None:
                    raise AgentHomeError("create-fenced", actor, "lifecycle-commit")
                replacement = (
                    reservation.plan.receipt
                    if isinstance(reservation.plan, ProvisionHome)
                    else reservation.plan.replacement
                    if isinstance(reservation.plan, ReplaceHome)
                    else None
                )
                if receipt is None or receipt != replacement:
                    raise AgentHomeError("create-fenced", actor, "lifecycle-commit")
                expected_home = claim.get("priorHome")
                expected_active = claim.get("priorHomeActive")
                current_home = self._db.execute(
                    "SELECT active,payload FROM lifecycle_resources "
                    "WHERE resource_key=?",
                    (f"agent-home:{actor}",),
                ).fetchone()
                if expected_home is None:
                    if current_home is not None:
                        raise AgentHomeError(
                            "create-fenced", actor, "lifecycle-commit"
                        )
                elif (
                    current_home is None
                    or bool(current_home["active"]) != bool(expected_active)
                    or json.loads(str(current_home["payload"])) != expected_home
                ):
                    raise AgentHomeError(
                        "create-fenced", actor, "lifecycle-commit"
                    )
                agent = reservation.agent
                self._db.execute(*self._insert_statement(agent))
                self._record_home_resource_locked(receipt, True)
                for adapter in agent.pinned_adapters:
                    self._db.execute(
                        "INSERT INTO pins(adapter, agent) VALUES(?, ?)",
                        (adapter, agent.actor),
                    )
            elif incumbent is not None:
                raise AgentHomeError("cleanup-fenced", actor, "lifecycle-commit")
            elif isinstance(reservation.plan, CleanupHome):
                if receipt is None or receipt.status != "cleaned":
                    raise AgentHomeError(
                        "cleanup-fenced", actor, "lifecycle-commit"
                    )
                current_home = self._db.execute(
                    "SELECT active,payload FROM lifecycle_resources "
                    "WHERE resource_key=?",
                    (f"agent-home:{actor}",),
                ).fetchone()
                if current_home is None or bool(current_home["active"]):
                    raise AgentHomeError(
                        "cleanup-fenced", actor, "lifecycle-commit"
                    )
                if HomeReceipt.from_json(
                    json.loads(str(current_home["payload"]))
                ) != reservation.plan.receipt:
                    raise AgentHomeError(
                        "cleanup-fenced", actor, "lifecycle-commit"
                    )
                self._record_home_resource_locked(receipt, False)
            self._db.execute(
                "INSERT INTO lifecycle_resources VALUES(?, ?, ?, ?) "
                "ON CONFLICT(resource_key) DO UPDATE SET "
                "resource_token=excluded.resource_token, active=excluded.active, "
                "payload=excluded.payload",
                (key, token, int(operation == "create"), json.dumps(payload, sort_keys=True)),
            )
            self._db.execute(
                "INSERT INTO lifecycle_receipts VALUES(?, ?, ?, ?, ?, 1, ?, 0)",
                (
                    attempt,
                    operation_id,
                    key,
                    expected,
                    int(bool(claim["createdByOperation"])),
                    token,
                ),
            )
            self._db.execute(
                "DELETE FROM lifecycle_resources WHERE resource_key = ?",
                (effect_key,),
            )
        return MutationProvenance(
            bool(claim["createdByOperation"]), True, token
        )
    def _lifecycle_create_payload(
        self,
        payload: object,
        resource_row: sqlite3.Row | None,
        *,
        reuse_resource_payload: bool = False,
    ) -> dict[str, object]:
        from hyprial.identity.impl.agents.actor.ports import BindAgentCommand, CreateAgentCommand

        if isinstance(payload, BindAgentCommand):
            agent = self.require(payload.actor)
            return {
                "actor": agent.uri,
                "harness": payload.harness,
                "runtime": payload.runtime,
                "sessionId": payload.session_id,
            }
        if resource_row is not None and reuse_resource_payload:
            old = json.loads(str(resource_row["payload"]))
            if isinstance(old, dict) and old:
                return old
        if isinstance(payload, CreateAgentCommand):
            name = self.normalize_actor(payload.name)
            return Agent(
                uri=self.uri_for(name),
                actor=name,
                owner=self.owner,
                machine=self.machine,
                cwd=payload.cwd,
                config=payload.config_payload(),
                provider=payload.provider,
                model=payload.model,
                capabilities=payload.capabilities_payload(),
                harness_args=dict(payload.harness_args),
                preferred_harness=payload.preferred_harness,
                created_at_ms=int(self._clock()),
            ).to_json()
        raise TypeError(f"unsupported lifecycle create payload: {type(payload).__name__}")
    def lifecycle_binding(self, actor: str) -> dict[str, object] | None:
        agent = self.get(actor)
        if agent is None:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT active, payload FROM lifecycle_resources WHERE resource_key = ?",
                (f"binding:{agent.uri}",),
            ).fetchone()
            if row is None or not bool(row["active"]):
                return None
            payload = json.loads(str(row["payload"]))
            return payload if isinstance(payload, dict) else None
    def retire_lifecycle_receipt(
        self, attempt_token: str, resource_token: str
    ) -> bool:
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT resource_token, retired FROM lifecycle_receipts "
                "WHERE attempt_token = ?",
                (attempt_token,),
            ).fetchone()
            if row is None or str(row["resource_token"]) != resource_token:
                return False
            if not bool(row["retired"]):
                self._db.execute(
                    "UPDATE lifecycle_receipts SET retired = 1 WHERE attempt_token = ?",
                    (attempt_token,),
                )
            return True
    def confirm_lifecycle_receipt_retired(
        self, attempt_token: str, resource_token: str
    ) -> bool:
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT resource_token, retired FROM lifecycle_receipts "
                "WHERE attempt_token = ?",
                (attempt_token,),
            ).fetchone()
            if row is None:
                return False
            if str(row["resource_token"]) != resource_token or not bool(row["retired"]):
                raise ValueError("lifecycle receipt retirement mismatch")
            self._db.execute(
                "DELETE FROM lifecycle_receipts WHERE attempt_token = ?",
                (attempt_token,),
            )
            return True
    def lifecycle_effect_claims(self) -> list["DomainEffectClaim"]:
        """Every registry receipt as a U0c backfill claim.

        Registry receipts commit atomically with the agent mutation, so a
        receipt whose journal effect never completed is a mutation the dead
        generation applied and never reported -- the backfill turns it into
        compensable journal truth."""

        from hyprial.kernel import DomainEffectClaim

        with self._lock:
            rows = self._db.execute(
                "SELECT operation_id, attempt_token, changed, "
                "created_by_operation, resource_token FROM lifecycle_receipts"
            ).fetchall()
        return [
            DomainEffectClaim(
                str(row["operation_id"]),
                str(row["attempt_token"]),
                bool(row["changed"]),
                bool(row["created_by_operation"]),
                str(row["resource_token"]),
            )
            for row in rows
        ]
    def expire_lifecycle_receipts(self) -> int:
        """U0c: delete every lifecycle receipt at daemon startup.

        Registry receipts have no completion phase (an agent mutation and
        its receipt commit in one transaction), so anything still on disk
        at startup is a crash-window leftover of the retirement handshake
        from a dead generation -- worthless, because the restart never
        re-sends a previous generation's attempt tokens (interrupted sagas
        are compensated, not resumed).  Agents rows and resource fences
        are untouched: they are the durable world the restart's
        compensation converges against.  Returns how many rows went.
        """

        with self._lock, self._db:
            row = self._db.execute(
                "SELECT COUNT(*) FROM lifecycle_receipts"
            ).fetchone()
            count = int(row[0]) if row is not None else 0
            if count:
                self._db.execute("DELETE FROM lifecycle_receipts")
        return count
