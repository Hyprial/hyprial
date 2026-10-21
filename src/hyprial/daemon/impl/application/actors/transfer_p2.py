"""Composition of the read-only P2 custody guard with existing restore policy.

Hydration waits run on the existing restore/wake callers, never in a mailbox.
The P2 short effect lane is the only journal reader/writer for this projection.
"""

import json
import os
import time
from typing import TYPE_CHECKING, Any
from dataclasses import dataclass, replace
from hyprial.identity import desired_generation
from hyprial.kernel import PortAdmission, canonical_user_uri, DaemonRequestError, ipc_errors
from hyprial.daemon.impl.lifecycle_manager import LifecycleOperation, LifecycleState
from hyprial.daemon.impl.lifecycle.lifecycle_coordinator import (
    LifecycleReservationOutcomeUnknown,
)
from uuid import uuid4

from hyprial.daemon.impl.harnesses.runtime.transfer_ports import (
    HoldStartsForTransfer,
    ReleaseStartsHold,
    StartsHeld,
    StartsHoldReleased,
)
from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.transfer.p2.actor import AdmissionActor
from hyprial.daemon.impl.transfer.p2.journal import MAX_ATTENTION_REASON
from hyprial.daemon.impl.transfer.p2.model import AdmissionConflict, Reason
from hyprial.daemon.impl.transfer.p2.source_ports import (
    CustodyOverride,
    PendingQuiesce,
    SourceFinish,
)
from hyprial.daemon.impl.transfer.p2.store import AdmissionStore

if TYPE_CHECKING:  # a runtime import here would cycle through the facade
    from hyprial.daemon.impl.transfer.p2.orchestration.lift import ChainPlan
from hyprial.daemon.impl.transfer.p2.source.effect import QuiesceEffect, QuiesceRequest
from hyprial.daemon.impl.transfer.p2.source.runner import SourceRunner
from hyprial.daemon.impl.ipc.params import JsonObject, _required_string

SOURCE_CUSTODY_QUERY_SECONDS = 10.0
SOURCE_CUSTODY_RETRIES = 3
SOURCE_CUSTODY_RETRY_BATCH = 32
SOURCE_CUSTODY_DEFERRED_CAPACITY = 10_000


@dataclass(frozen=True)
class _DeferredRestore:
    spec: object
    token: str
    version: int
    attempts: int = 0
    operation: str | None = None


class _TransferP2Mixin:
    def _transfer_receive_p2(self, params: JsonObject) -> JsonObject:
        """The P2 target entry: admit this half, then land and activate.

        Reached only when ``transfer.receive`` is called with chain facts
        (``planDigest``); the composition -- including the unproven-harness
        fence and the wire codes -- lives in the P2 orchestration package, so
        this module stays the thin application seam it has always been.
        """

        from hyprial.kernel import HarnessLaunchSpec

        from hyprial.daemon.impl.transfer.p2.orchestration.receive import (
            receive_into_target,
        )
        from hyprial.daemon.impl.transfer.p2.orchestration.lift import (
            ENTRYPOINT_HEADLESS,
            half_to_json,
        )
        from hyprial.daemon.impl.transfer.landing.signature_gate import (
            trust_policy_for,
        )

        spec = HarnessLaunchSpec.from_json(params.get("spec"), "spec")
        received = receive_into_target(
            params,
            spec=spec,
            owner=self.owner,
            node_id=self.node_id,
            state_db=self.state_db,
            # The identity *registry*, not the directory facade: the landing
            # half needs the facade's own surface and the activation half needs
            # ``home_receipt``/``bind_agent_config``.  The facade carries only
            # the former, so the target entry takes the one object that is both
            # (the same handle the migration coordinator binds).
            registry=self._agent_registry,
            home_root=self.hyprial_home / "agents",
            machine_home=self.hyprial_home,
            staging_dir=self.state_dir / "transfer-landing",
            environment=os.environ,
            # AT06 slice 2b: this target decides attribution from its own
            # offline trust anchor before it records or writes anything.
            signature=trust_policy_for(self.hyprial_home),
            # The chain's entrypoint rides the same receive call; a TUI is
            # landed/activated here (position only) and started later by the
            # target's own CLI (target_tui.drive_target_start).
            entrypoint=str(params.get("entrypoint") or ENTRYPOINT_HEADLESS),
        )
        payload: JsonObject = {
            "ok": True,
            "chain": {
                "operationId": received.plan.operation_id,
                "sourceNode": received.plan.target.source_node,
                "targetActor": received.plan.target.target_actor,
                "planDigest": received.plan.target.plan_digest,
                # The target's own report of its half, for the source's
                # completion decision.  A report, never proof.
                "report": half_to_json(received.half),
            },
        }
        payload.update(received.landing.to_json())
        self._log(
            "info",
            "transfer",
            "transfer.received",
            actor=received.landing.actor,
            operationId=received.plan.operation_id,
            phases=",".join(received.landing.phases),
        )
        return payload

    def _start_transfer_source(self, harnesses, lifecycle):
        self._transfer_source = SourceRunner(QuiesceEffect(self._transfer_p2, harnesses, lifecycle))

    def _quiesce_p2_source(self, request: QuiesceRequest):
        """Internal admitted-operation seam; no new public IPC/CLI admission."""
        if self._transfer_source is None:
            raise RuntimeError('P2 source effects are not available')
        return self._transfer_source.submit(request)

    def _transfer_source_running(self, actor: str, entity_token: str) -> bool | None:
        """Observe whether the source incarnation is still running.

        The same ingredients as ``agents_admin.migration``'s probe -- the
        registry row must still be this incarnation before anything else is
        read -- but the chain's own polarity: ``True`` = running, ``False`` =
        observed **not** running, ``None`` = unknown.  Only a direct ``False``
        may unlock a completion, so a cache is never asked, and a probe that
        cannot tell holds.
        """

        agent = self.agents.get(actor)
        if agent is None or agent.entity_token != entity_token:
            # A different incarnation (or none) is not the one this chain
            # quiesced; the answer is unknown, not "not running".
            return None
        running = self._agent_liveness.verdict(agent.uri)
        if running is not None:
            return running
        # A local identity with no durable binding is the daemon's own
        # post-``down``/never-started state: observed not running.  A retained
        # binding with no verdict stays unknown, so it can never unlock a
        # completion.
        return False if self._agent_liveness.binding(agent.uri) is None else None

    def _transfer_complete_p2(self, params: JsonObject) -> JsonObject:
        """Source-side completion when the caller brings the target's report.

        Reached only when ``transfer.complete`` is called with chain facts.  The
        reported half is parsed to the exact shape (no defaults, damaged is not
        absent), and then the **existing** decision reads this daemon's own
        source row plus that claim: every source-side rule -- its own quiesce
        row, its own attention row, the seven-field plan join, a stopped
        incarnation -- stays in force, so a well-formed claim can satisfy the
        target-side rules and can never let the source skip one of its own.

        The half is a **claim**, not proof: it travels at the same L0 tier as
        the P0 flow's completion receipt.  This branch adds no capability --
        it is strictly narrower than the P0 retire path it falls through to,
        because it can only run for a chain this daemon already recorded.
        """

        from hyprial.daemon.impl.transfer.p2.orchestration.lift import (
            ChainPlanError,
            decide_with_shipped_target,
            half_from_json,
        )

        operation_id = _required_string(params.get("operationId"), "operationId")
        chain = params.get("chain")
        if not isinstance(chain, dict) or set(chain) != {"report"}:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "chain must carry exactly one 'report' field: the target's own"
                " report of its half, as returned by transfer.receive",
            )
        try:
            half = half_from_json(chain["report"], operation_id=operation_id)
        except ChainPlanError as error:
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
        store = AdmissionStore(self.state_db, node=self.node_id, owner=self.owner)
        try:
            decision = decide_with_shipped_target(
                operation_id,
                source_store=store,
                target_half=half,
                source_live=self._transfer_source_running,
            )
        except AdmissionConflict as error:
            # A corrupt row is a different fact from "no row", and stays loud.
            raise DaemonRequestError(
                ipc_errors.TRANSFER_CUSTODY_UNAVAILABLE,
                f"the source half of {operation_id} cannot be read: {error.reason}",
                {"operationId": operation_id, "reason": str(error.reason)},
            ) from error
        if not decision.may_complete:
            raise DaemonRequestError(
                ipc_errors.TRANSFER_CONFLICT,
                f"the chain {operation_id} is not complete: {decision.reason}",
                decision.to_json(),
            )
        payload = self._transfer_complete(
            {key: value for key, value in params.items() if key != "chain"}
        )
        payload["chain"] = decision.to_json()
        return payload

    def _make_transfer_authority(self):
        return AdmissionActor(AdmissionStore(
            self.state_db, node=self.node_id, owner=self.owner,
        ))

    def _transfer_source_state(self, actor, token):
        authority = self._transfer_p2
        return authority.source_custody(actor, expected_entity_token=token)

    def _hydrate_transfer_source(self, actor, token, *, deadline=None):
        deadline = (time.monotonic() + SOURCE_CUSTODY_QUERY_SECONDS
                    if deadline is None else deadline)
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("source custody fleet budget exhausted")
            key = (actor, token)
            with self._transfer_restore_lock:
                future = self._transfer_custody_reads.get(key)
                if future is None:
                    if len(self._transfer_custody_reads) >= SOURCE_CUSTODY_DEFERRED_CAPACITY:
                        raise RuntimeError("source custody read capacity exhausted")
                    future = self._transfer_p2.hydrate_custody(actor, expected_entity_token=token)
                    self._transfer_custody_reads[key] = future
            try:
                fact = future.result(remaining)
            finally:
                # A timeout retains the accepted read. The next observation
                # attaches to it instead of flooding the short journal lane.
                if future.done():
                    with self._transfer_restore_lock:
                        if self._transfer_custody_reads.get(key) is future:
                            self._transfer_custody_reads.pop(key)

            if fact.holds and not fact.overridden:
                # An authenticated override already released this exact hold.
                # Re-deriving it here would silently re-arm the fence the
                # operator cleared, so overridden custody derives no hold.
                agent = self.agents.get(actor)
                if agent is None or agent.entity_token != token:
                    raise RuntimeError("source incarnation changed during custody hydration")
                # Boot has a fresh harness mailbox. Rebuild its derived hold
                # before relying on a filtered restore candidate list. The
                # owner rechecks identity; this caller read is not its fence.
                for hold in fact.holds:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("source hold replay remains unknown")
                    command = HoldStartsForTransfer(
                        uuid4().hex, actor=agent.uri, expected_entity_token=token,
                        operation_id=hold.operation_id, generation=hold.generation,
                    )
                    receipt = self._harnesses.call(command, StartsHeld, timeout=remaining)
                    if (not isinstance(receipt, StartsHeld) or receipt.disposition != "held"
                            or (receipt.correlation_id, receipt.actor, receipt.expected_entity_token,
                                receipt.operation_id, receipt.generation) !=
                            (command.correlation_id, command.actor, command.expected_entity_token,
                             command.operation_id, command.generation)):
                        raise RuntimeError("source hold replay was not confirmed")
            return fact
        except Exception as error:
            # An observer timeout is not absence and does not cancel its read.
            self._log("warn", "daemon", "transfer.custody.unavailable",
                      actor=actor, errorType=type(error).__name__)
            return None

    def _filter_transfer_restore_specs(self, specs):
        # Recovery runs on this boot/classification path too: after a crash the
        # restore-wake cadence may not tick on its own, and a durable quiesce
        # intent must be certified from owner receipts before the entity is
        # considered for restart.
        self._reconcile_transfer_quiesce()
        restored = []
        deadline = time.monotonic() + SOURCE_CUSTODY_QUERY_SECONDS
        for spec in specs:
            agent = self.agents.get(spec.name)
            if agent is None:
                # Positive absence in identity means there is no incarnation
                # to suppress. A future creation gets a fresh entity token.
                restored.append(spec)
                continue
            fact = self._hydrate_transfer_source(agent.actor, agent.entity_token, deadline=deadline)
            if fact is not None and not fact.blocks_start:
                restored.append(spec)
                continue
            observed = self._transfer_source_state(agent.actor, agent.entity_token)
            with self._transfer_restore_lock:
                key = (agent.actor, agent.entity_token)
                if key in self._transfer_deferred or len(self._transfer_deferred) < SOURCE_CUSTODY_DEFERRED_CAPACITY:
                    self._transfer_deferred[key] = _DeferredRestore(spec, agent.entity_token, observed.version)
                else:
                    self._log("warn", "daemon", "transfer.restore.retry_capacity", actor=agent.actor)
            self._publish_restore_eligibility(
                spec=spec, entity_token=agent.entity_token, suppressed=True,
            )
            self._log("warn", "daemon", "transfer.restore.suppressed",
                      actor=agent.actor, reason="query-timeout" if fact is None else fact.reason,
                      operations=() if fact is None else fact.operations)
        return tuple(restored)

    def _transfer_override_alert(self, *, principal, actor, operation_id,
                                 reason, event_id) -> bool:
        """Durable structured alert; never replaces the journal audit row."""

        notice = InboxMessage(
            message_id=f"transfer-override:{event_id}",
            conversation_id=f"transfer-override:{operation_id}",
            sender=f"system:hyprial:{self.node_id}",
            recipient=self.node_id,
            payload=json.dumps({
                "systemNotice": True,
                "reason": "transfer-custody-override",
                "operationId": operation_id,
                "actor": actor,
                "principal": principal,
                "note": reason,
            }, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
            intent="system",
            lifecycle=DeliveryLifecycle.ONLINE_ONLY,
            idempotency_key=f"transfer-override:{event_id}",
            created_at_ms=time.time_ns() // 1_000_000,
        )
        try:
            return bool(self._inbox.receive_system_notice(notice))
        except Exception as error:
            self._log("warn", "daemon", "transfer.override.alert_unavailable",
                      actor=actor, errorType=type(error).__name__)
            return False

    def _override_transfer_custody(self, principal, *, operation_id, actor, reason):
        """Owner-authenticated override: audit, alert, then release the hold.

        Order is the contract: principal -> journal audit -> durable alert ->
        (needs-attention is set by the audit itself) -> exact release ack ->
        clear eligibility. The durable custody row is never deleted and the
        migration stops at needs-attention. Nothing is released when the audit
        or the alert cannot be proven durable.
        """

        if not isinstance(principal, str) or not principal.strip():
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT,
                                     "override requires a principal")
        if canonical_user_uri(principal) != canonical_user_uri(self.owner):
            raise DaemonRequestError(ipc_errors.CALLER_NOT_AUTHORIZED,
                                     "transfer custody override requires the daemon owner")
        if (not isinstance(reason, str) or not reason.strip()
                or len(reason) > MAX_ATTENTION_REASON):
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT,
                                     "override requires a bounded reason")
        agent = self.agents.get(actor)
        if agent is None:
            raise DaemonRequestError(ipc_errors.TRANSFER_CUSTODY_UNAVAILABLE,
                                     "override target has no local incarnation")
        event_id = f"override:{operation_id}:{uuid4().hex}"
        try:
            audit = self._transfer_p2.submit(CustodyOverride(
                operation_id, actor=agent.actor, expected_entity_token=agent.entity_token,
                principal=principal, reason=reason, event_id=event_id,
            )).result(SOURCE_CUSTODY_QUERY_SECONDS)
        except AdmissionConflict as error:
            raise DaemonRequestError(ipc_errors.TRANSFER_CONFLICT,
                                     f"override audit rejected: {error.reason.value}") from error
        except Exception as error:
            raise DaemonRequestError(ipc_errors.TRANSFER_CUSTODY_UNAVAILABLE,
                                     f"override audit unavailable: {type(error).__name__}") from error
        if getattr(audit, "state", None) is None:
            raise DaemonRequestError(ipc_errors.TRANSFER_CUSTODY_UNAVAILABLE,
                                     "override audit returned no custody step")
        state = audit.state
        if not self._transfer_override_alert(
                principal=principal, actor=agent.actor, operation_id=operation_id,
                reason=reason, event_id=event_id):
            raise DaemonRequestError(ipc_errors.TRANSFER_CUSTODY_UNAVAILABLE,
                                     "override alert was not durably recorded; hold retained")
        command = ReleaseStartsHold(
            f"override-release:{event_id}", actor=agent.uri,
            expected_entity_token=state.entity_token,
            operation_id=operation_id, generation=state.generation,
        )
        try:
            released = self._harnesses.call(command, StartsHoldReleased,
                                            timeout=SOURCE_CUSTODY_QUERY_SECONDS)
        except Exception as error:
            released = None
            self._log("warn", "daemon", "transfer.override.release_unavailable",
                      actor=agent.actor, errorType=type(error).__name__)
        expected = (command.correlation_id, command.actor, command.expected_entity_token,
                    command.operation_id, command.generation)
        if (not isinstance(released, StartsHoldReleased)
                or (released.correlation_id, released.actor, released.expected_entity_token,
                    released.operation_id, released.generation) != expected):
            # Audit and alert stay durable; the hold is not proven released, so
            # the guard keeps suppressing rather than starting a held entity.
            raise DaemonRequestError(
                ipc_errors.TRANSFER_CONFLICT,
                "override audit recorded but the exact hold release was not acknowledged",
            )
        with self._transfer_restore_lock:
            self._transfer_deferred.pop((agent.actor, state.entity_token), None)
        spec = self._transfer_desired_spec_for(agent.actor)
        if spec is not None:
            self._publish_restore_eligibility(
                spec=spec, entity_token=state.entity_token, suppressed=False,
            )
        self._log("warn", "daemon", "transfer.override.applied",
                  actor=agent.actor, operationId=operation_id, principal=principal)
        return {
            "ok": True,
            "operationId": operation_id,
            "actor": agent.uri,
            "principal": principal,
            "eventId": event_id,
            "custody": "held-overridden",
            "holdReleased": True,
        }

    def _ipc_transfer_custody_override(self, params):
        """Owner-authenticated admission for the manual override.

        No shell command and no boolean force flag: the caller must name the
        owner principal, and the audit receipt is this command plus its journal
        row. Custody is never deleted by this path.
        """

        return self._override_transfer_custody(
            _required_string(params.get("principal"), "principal"),
            operation_id=_required_string(params.get("operationId"), "operationId"),
            actor=_required_string(params.get("actor") or params.get("name"), "actor"),
            reason=_required_string(params.get("reason"), "reason"),
        )

    def _reserved_preparation_refusal(self, chain: "ChainPlan", reason: str) -> DaemonRequestError:
        """Name the preparation that still reserves this worker.

        A bare ``reserved`` cannot be acted on: it does not say which earlier
        attempt holds the incarnation, and no retry can proceed while one does.
        This refusal names that operation, so the operator can see what is
        holding the worker instead of reading the same word again.
        """

        if reason != Reason.RESERVED.value:
            return DaemonRequestError(
                ipc_errors.TRANSFER_CONFLICT,
                f"the P2 source lane refused the preparation of "
                f"{chain.operation_id}: {reason}",
                {"operationId": chain.operation_id, "reason": reason},
            )
        store = AdmissionStore(self.state_db, node=self.node_id, owner=self.owner)
        try:
            reserved = store.source_custody(
                owner=chain.source.owner, node=chain.source.source_node,
                agent_id=chain.source.source_agent_id,
                entity_token=chain.source.source_entity_token,
            )
        except AdmissionConflict as error:
            # An unreadable ledger is not "no reservation": fail closed.
            return DaemonRequestError(
                ipc_errors.TRANSFER_CUSTODY_UNAVAILABLE,
                f"this worker's preparation cannot be read "
                f"({error.reason.value}); refusing to open a second chain",
                {"operationId": chain.operation_id, "reason": str(error.reason)},
            )
        if reserved is None:
            return DaemonRequestError(
                ipc_errors.TRANSFER_CONFLICT,
                f"the P2 source lane refused the preparation of "
                f"{chain.operation_id} ({reason}); no live reservation was "
                f"readable for worker {chain.source.source_agent_id}",
                {"operationId": chain.operation_id, "reason": reason},
            )
        try:
            latest = store.journal.get(reserved.operation_id)
        except AdmissionConflict as error:
            # A step that cannot be read is not "no step": fail closed instead
            # of sending the operator down a path the ledger will contradict.
            return DaemonRequestError(
                ipc_errors.TRANSFER_CUSTODY_UNAVAILABLE,
                f"the preparation holding {chain.source.source_agent_id} "
                f"({reserved.operation_id}) cannot be read "
                f"({error.reason.value}); refusing to claim the worker can be "
                "prepared again",
                {"operationId": chain.operation_id, "reason": reason,
                 "reservedOperationId": reserved.operation_id},
            )
        if latest is None:
            # Nothing recorded a step, so an audited override has nothing to
            # mark (``apply_override`` answers ``UNKNOWN``) and no automatic
            # release path exists for this reservation.  Saying "resolve that
            # attempt" would point at a door that is not there, and each miss
            # costs the operator a round trip.
            remedy = (
                "that preparation recorded no step, so no audited override can "
                "release it and there is no automatic release path for this "
                "reservation"
            )
        else:
            remedy = (
                f"resolve that attempt with an audited transfer.custody.override "
                f"for {reserved.operation_id} (the slot is freed once every step "
                "of that attempt records the override)"
            )
        return DaemonRequestError(
            ipc_errors.TRANSFER_CONFLICT,
            f"worker {chain.source.source_agent_id} is still reserved by "
            f"preparation {reserved.operation_id} ({reason}); {remedy}",
            {"operationId": chain.operation_id, "reason": reason,
             "reservedOperationId": reserved.operation_id,
             "reservedHasStep": latest is not None},
        )

    def _transfer_desired_spec_for(self, actor):
        desired = self.desired_state.load()
        for spec in desired.harnesses:
            if self.agents.native_actor(spec.name) == actor:
                return spec
        return None

    def _reconcile_transfer_quiesce(self):
        """Certify durable quiesce intents left unresolved by a crashed effect.

        Runs on the existing restore cadence: no new timer, launcher or durable
        authority. A pending row is closed only when the existing lifecycle
        owner returns the matching COMPLETED stop receipt and the harness owner
        positively re-acknowledges the derived hold for that exact incarnation,
        operation and generation. Everything else stays unavailable; the
        accepted effect is never re-executed or compensated here.
        """

        if (self._transfer_p2 is None or self._lifecycle_manager is None
                or self._harnesses is None):
            return
        deadline = time.monotonic() + SOURCE_CUSTODY_QUERY_SECONDS
        try:
            states = self._transfer_p2.submit(
                PendingQuiesce(SOURCE_CUSTODY_RETRY_BATCH)).result(
                max(0.0, deadline - time.monotonic()))
        except Exception as error:
            self._log("warn", "daemon", "transfer.reconcile.unavailable",
                      errorType=type(error).__name__)
            return
        for state in states:
            if time.monotonic() >= deadline:
                return
            agent = self.agents.get(state.actor)
            if agent is None or agent.entity_token != state.entity_token:
                continue
            operation_id = f"transfer-quiesce:{state.operation_id}:{state.generation}"
            try:
                receipt = self._lifecycle_manager.wait(operation_id, 0)
            except Exception:
                receipt = None
            if (receipt is None
                    or getattr(receipt, "operation_id", None) != operation_id
                    or receipt.state is not LifecycleState.COMPLETED):
                continue
            try:
                held = self._harnesses.call(HoldStartsForTransfer(
                    uuid4().hex, actor=agent.uri,
                    expected_entity_token=state.entity_token,
                    operation_id=state.operation_id, generation=state.generation,
                ), StartsHeld, timeout=max(0.0, deadline - time.monotonic()))
            except Exception:
                held = None
            if (not isinstance(held, StartsHeld) or held.disposition != "held"
                    or (held.operation_id, held.generation, held.expected_entity_token)
                    != (state.operation_id, state.generation, state.entity_token)):
                continue
            try:
                self._transfer_p2.submit(SourceFinish(
                    state, receipt.operation_id)).result(
                    max(0.0, deadline - time.monotonic()))
            except Exception as error:
                self._log("warn", "daemon", "transfer.reconcile.finish_unavailable",
                          actor=state.actor, errorType=type(error).__name__)
                continue
            self._log("warn", "daemon", "transfer.reconcile.completed",
                      actor=agent.uri, operationId=state.operation_id)

    def _retry_transfer_restore(self):
        """Existing restore-wake cadence owns these bounded observation retries.

        No new launcher/timer or durable state: after a newer clear observation,
        reapply activity policy and publish eligibility to the existing runtime.
        Exhaustion leaves the entity suppressed, with an explicit warning.
        """
        self._reconcile_transfer_quiesce()
        deadline = time.monotonic() + SOURCE_CUSTODY_QUERY_SECONDS
        with self._transfer_restore_lock:
            batch = tuple(self._transfer_deferred.items())[:SOURCE_CUSTODY_RETRY_BATCH]
        if not batch:
            return
        # One whole-document read per invocation; the per-entity match below
        # is a membership test against this snapshot, not a per-item reload.
        desired_harnesses = self.desired_state.load().harnesses
        for key, deferred in batch:
            if time.monotonic() >= deadline:
                break
            actor, token = key
            agent = self.agents.get(actor)
            spec = next((s for s in desired_harnesses
                         if s.name == actor and s.harness == deferred.spec.harness), None)
            if (agent is None or agent.entity_token != token or spec is None
                    or desired_generation(spec) != desired_generation(deferred.spec)):
                with self._transfer_restore_lock:
                    if self._transfer_deferred.get(key) is deferred:
                        self._transfer_deferred.pop(key)
                continue
            if deferred.operation is not None:
                # Already accepted: observe the same durable lifecycle receipt,
                # never submit another launch on a tick/observer timeout.
                try:
                    receipt = self._lifecycle_manager.wait(deferred.operation, 0)
                except TimeoutError:
                    receipt = None
                except Exception as error:
                    self._log("warn", "daemon", "transfer.restore.receipt_unavailable",
                              actor=actor, errorType=type(error).__name__)
                    receipt = None
                if receipt is not None:
                    if receipt.state is not LifecycleState.COMPLETED:
                        self._log("warn", "daemon", "transfer.restore.wake_failed", actor=actor)
                    with self._transfer_restore_lock:
                        if self._transfer_deferred.get(key) is deferred:
                            self._transfer_deferred.pop(key)
                    continue
                self._rotate_transfer_deferred(key, deferred, deferred)
                continue
            fact = self._transfer_source_state(actor, token)
            if deferred.attempts < SOURCE_CUSTODY_RETRIES:
                fact = self._hydrate_transfer_source(actor, token, deadline=deadline)
            fresh = fact is not None and fact.version > deferred.version
            if fresh and not fact.blocks_start:
                allowed = self._classify_activity_restore_specs((spec,))
                if not allowed:
                    # Activity suppression now owns wakeup, as before.
                    with self._transfer_restore_lock:
                        if self._transfer_deferred.get(key) is deferred:
                            self._transfer_deferred.pop(key)
                    continue
                manager = self._lifecycle_manager
                if manager is not None:
                    operation = LifecycleOperation.resume(
                        f"resume-wake:{actor}:{token}:custody:{desired_generation(spec)}",
                        replace(self._lifecycle_spec(spec), expected_entity_token=token),
                    )
                    try:
                        admitted = manager.submit(operation)
                    except LifecycleReservationOutcomeUnknown:
                        self._rotate_transfer_deferred(
                            key,
                            deferred,
                            replace(deferred, operation=operation.operation_id),
                        )
                        continue
                    except Exception as error:
                        admitted = None
                        self._log("warn", "daemon", "transfer.restore.wake_unavailable",
                                  actor=actor, errorType=type(error).__name__)
                    if admitted is PortAdmission.ACCEPTED:
                        self._rotate_transfer_deferred(
                            key, deferred, replace(deferred, operation=operation.operation_id))
                        continue
            attempts = min(SOURCE_CUSTODY_RETRIES, deferred.attempts + 1)
            if attempts == SOURCE_CUSTODY_RETRIES and deferred.attempts < attempts:
                self._log("warn", "daemon", "transfer.restore.retry_exhausted", actor=actor)
            updated = replace(deferred, attempts=attempts,
                              version=fact.version if fresh else deferred.version)
            self._rotate_transfer_deferred(key, deferred, updated)

    def _rotate_transfer_deferred(self, key, previous, updated):
        with self._transfer_restore_lock:
            if self._transfer_deferred.get(key) is previous:
                # Rotate pending/exhausted entries too, never starve the fleet.
                self._transfer_deferred.pop(key)
                self._transfer_deferred[key] = updated

    # -- T2: this machine's credential-envelope recipient key ---------------

    def _ipc_transfer_recipient_key(self, params) -> Any:
        return self._transfer_recipient_key(params)

    def _transfer_recipient_key(self, params: JsonObject) -> JsonObject:
        """This machine's credential-envelope public half (mints on first use).

        Called by a source machine over SSH before it seals a bundle's
        credentials.  Only the public half leaves the machine: the private
        half lives under this daemon's own home
        (``secrets/transfer/recipient-key.json``, 0600) and is what the
        landing uses to open the sealed envelope.  Idempotent: the first call
        mints, every later call reads what the first one wrote.
        """

        from hyprial.daemon.impl.transfer.archive.envelope import (
            ensure_recipient_key,
            recipient_key_path,
            recipient_public_key_b64,
        )

        _private, public = ensure_recipient_key(
            recipient_key_path(self.hyprial_home)
        )
        return {
            "ok": True,
            "algorithm": "x25519",
            "publicKey": recipient_public_key_b64(public),
        }
