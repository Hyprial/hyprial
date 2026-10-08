"""The single-writer desired/journal ports the persistence authority serializes commands through."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal
from uuid import uuid4
from hyprial.kernel import CallCostCounters
from hyprial.daemon.impl.desired_state  import (
    DesiredState, DesiredStateStore, InteractiveSession,
    PendingSessionAgentEffect,
)
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.lifecycle_manager  import (
    LifecycleOperation,
    LifecycleState,
    _LifecycleStore,
    backfill_domain_attested_effects,
)
from hyprial.kernel  import (
    MutationProvenance,
)
from hyprial.kernel import DomainEffectClaim
from hyprial.kernel import LifecycleMutationRequest

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Annotation-only: the authority composes the ports, so importing it at
    # runtime here would be circular (same-domain typing reference, not a
    # hidden cross-domain edge).
    from .authority import StatePersistenceAuthority
from .commands import (
    ApplyHarnessLifecycle,
    ApplySessionLifecycle,
    BackfillDomainAttestedEffects,
    ClaimInteractiveSession,
    ClaimInteractiveWithAgentEffects,
    CompareAndSaveDesired,
    CompleteHarnessLifecycle,
    CompleteLifecycleEffect,
    CompleteSessionAgentEffect,
    ConfirmDomainReceipt,
    ConfirmHarnessLifecycle,
    DesiredQuery,
    ExpireInterruptedReceipts,
    FailHarnessRemoval,
    InterruptRunningLifecycle,
    JournalQuery,
    MarkHarnessFailed,
    MarkLifecycleEffectDispatched,
    MarkLifecycleReceiptRetired,
    NextLifecycleGeneration,
    NormalizeInteractiveSessions,
    PrepareLifecycleEffect,
    ReadDesiredQuery,
    ReadJournalQuery,
    RecordHarnessLifecycleFailure,
    RecordSessionAgentEffects,
    RemoveAdapterRegistration,
    RemoveChannelPin,
    RemoveHarness,
    RemoveMigratedChannelPins,
    ReserveLifecycle,
    RestoreAdapterRegistration,
    RetireDomainReceipt,
    RetireSharedDomainReceipt,
    RollbackHarnessLifecycle,
    SetChannelPin,
    SetLifecycleState,
    SetMailboxRole,
    StateCommand,
    StateCostOrigin,
    StateProjection,
    SetServiceConnection,
    RemoveServiceConnection,
    SetServiceRegistry,
    SyncHarnessSessionRefs,
    UnregisterInteractiveSession,
    UpdateZenohEndpoints,
    UpsertHarness,
    _snapshot_payload,
    _state_cost_key,
)
from .services import ServiceDesiredPortMixin


@dataclass(frozen=True, slots=True)
class StateCommandCompleted:
    """Caller-side settled result, built after local custody is consumed.

    The actor callback places mutable result values and original exceptions
    directly into a guarded local pending table. They never enter another
    actor mailbox or typed completion event.
    """
    correlation_id: str
    generation: int
    result: object = None
    error: Exception | None = None


class StatePersistenceBusy(RuntimeError):
    pass


class StatePersistenceTimeout(TimeoutError):
    def __init__(self, correlation_id: str) -> None:
        super().__init__(f"state operation remains unsettled: {correlation_id}")
        self.correlation_id = correlation_id


class StatePersistenceVersionConflict(RuntimeError):
    pass


class _Pending:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.generation: int | None = None
        self.result: object = None
        self.error: Exception | None = None
        self.abandoned = False


class _Projection:
    def __init__(self, initial: DesiredState) -> None:
        self._lock = threading.Lock()
        self._snapshot = StateProjection(0, initial)

    def read(self) -> StateProjection:
        with self._lock:
            snapshot = self._snapshot
        return StateProjection(
            snapshot.version,
            DesiredState.from_json(snapshot.desired.to_json()),
        )

    def harness_spec(
        self, name: str, *, excluding: str | None = None
    ) -> HarnessLaunchSpec | None:
        """Copy one published harness row without copying the document.

        ``HarnessLaunchSpec`` and its optional ``SmolvmRuntimeSpec`` are
        frozen and contain only scalars or tuples.  A shallow dataclass copy
        therefore isolates the returned row (including against deliberate
        ``object.__setattr__``) without serializing unrelated desired state.
        """

        with self._lock:
            return next(
                (
                    replace(spec)
                    for spec in self._snapshot.desired.harnesses
                    # Unique only on (harness, name): skip while searching,
                    # so an excluded row sorted first never hides the match.
                    if spec.name == name and spec.harness != excluding
                ),
                None,
            )

    def publish(self, desired: DesiredState) -> None:
        with self._lock:
            if desired == self._snapshot.desired:
                return
            self._snapshot = StateProjection(self._snapshot.version + 1, desired)



def run_journal_query(journal: _LifecycleStore, command: ReadJournalQuery) -> Any:
    """Answer one read-only journal query.

    Shared by the writer's handler and by callers that read directly: every
    ``journal`` read opens its own short-lived SQLite connection, so a read
    needs no turn on the single writer's mailbox.
    """

    op_id = command.operation_id
    if command.operation is JournalQuery.PENDING:
        result = journal.pending()
    else:
        assert op_id is not None
        if command.operation is JournalQuery.LOAD:
            result = journal.load(op_id)
        elif command.operation is JournalQuery.STATE:
            result = journal.state(op_id)
        elif command.operation is JournalQuery.RESULT:
            result = journal.result(op_id)
        elif command.operation is JournalQuery.COMPLETED_FORWARD:
            result = journal.completed_forward(op_id)
        elif command.operation is JournalQuery.COMPENSABLE_FORWARD:
            result = journal.compensable_forward(op_id)
        elif command.operation is JournalQuery.FORWARD_RESOURCE_TOKEN:
            assert command.effect_name is not None
            result = journal.forward_resource_token(op_id, command.effect_name)
        else:
            assert command.effect_name is not None and command.direction is not None
            if command.operation is JournalQuery.EFFECT_DONE:
                result = journal.effect_done(op_id, command.effect_name, command.direction)
            elif command.operation is JournalQuery.EFFECT_RECEIPT:
                result = journal.effect_receipt(op_id, command.effect_name, command.direction)
            elif command.operation is JournalQuery.RECEIPT_RETIREMENT:
                result = journal.receipt_retirement(op_id, command.effect_name, command.direction)
            elif command.operation is JournalQuery.COMPLETED_RECEIPT:
                result = journal.completed_receipt(op_id, command.effect_name, command.direction)
            else:
                raise TypeError("unsupported journal query")
    return result

class _Generation:
    def __init__(
        self,
        generation: int,
        desired: DesiredStateStore,
        journal: _LifecycleStore,
        projection: _Projection,
        complete: Any,
        command_costs: CallCostCounters,
        execution_wall: CallCostCounters | None = None,
    ) -> None:
        self.generation = generation
        self.execution_wall = execution_wall
        self.desired = desired
        self.journal = journal
        self.projection = projection
        self.complete = complete
        self.command_costs = command_costs

    def __call__(self, command: StateCommand) -> None:
        costs = self.command_costs
        failed = True
        started_cpu = time.thread_time() if costs.enabled else 0.0
        started_wall = time.perf_counter()
        try:
            if isinstance(command, ReadDesiredQuery):
                if command.operation is DesiredQuery.INCOMPLETE_HARNESS_LIFECYCLE_RESOURCES:
                    result = self.desired.incomplete_harness_lifecycle_resources()
                elif command.operation is DesiredQuery.INCOMPLETE_HARNESS_LIFECYCLE_RECEIPTS:
                    result = self.desired.incomplete_harness_lifecycle_receipts()
                elif command.operation is DesiredQuery.LIFECYCLE_EFFECT_CLAIMS:
                    result = self.desired.lifecycle_effect_claims()
                elif command.operation is DesiredQuery.HARNESS_LIFECYCLE_RECEIPT:
                    assert command.key is not None
                    result = self.desired.harness_lifecycle_receipt(command.key)
                elif command.operation is DesiredQuery.ADAPTER_REGISTRATION:
                    assert command.key is not None
                    result = self.desired.adapter_registration(command.key)
                else:
                    raise TypeError("unsupported desired query")
            elif isinstance(command, ReadJournalQuery):
                result = run_journal_query(self.journal, command)
            elif isinstance(command, RemoveMigratedChannelPins):
                result = self.desired.remove_matching_channel_pins(command.matches)
                self.projection.publish(self.desired.load())
            elif isinstance(command, NormalizeInteractiveSessions):
                result = self.desired.normalize_interactive_sessions(
                    rewrites=command.rewrites
                )
                self.projection.publish(self.desired.load())
            elif isinstance(command, CompareAndSaveDesired):
                if command.expected_version != self.projection.read().version:
                    raise StatePersistenceVersionConflict(
                        f"desired-state version {command.expected_version} is stale"
                    )
                self.desired.save(command.desired)
                result = command.desired
                self.projection.publish(self.desired.load())
            elif isinstance(command, ApplySessionLifecycle):
                result = self.desired.apply_session_lifecycle(command.request)
                self.projection.publish(self.desired.load())
            elif isinstance(command, ApplyHarnessLifecycle):
                result = self.desired.apply_harness_lifecycle(
                    command.request, generation=command.generation,
                    version=command.version,
                )
                self.projection.publish(self.desired.load())
            elif isinstance(command, RetireDomainReceipt):
                result = self.desired.retire_lifecycle_receipt(
                    command.domain, command.attempt_token, command.resource_token
                )
                self.projection.publish(self.desired.load())
            elif isinstance(command, ConfirmDomainReceipt):
                result = self.desired.confirm_lifecycle_receipt_retired(
                    command.domain, command.attempt_token, command.resource_token
                )
                self.projection.publish(self.desired.load())
            elif isinstance(command, ClaimInteractiveSession):
                result = self.desired.claim_interactive_with_effects(
                    command.session, command.effects
                )
                self.projection.publish(self.desired.load())
            elif isinstance(command, ClaimInteractiveWithAgentEffects):
                result = self.desired.claim_interactive_with_agent_effects(
                    command.session, command.bind_effect
                )
                self.projection.publish(self.desired.load())
            elif isinstance(command, UnregisterInteractiveSession):
                if command.session_ref is None:
                    result = self.desired.unregister_interactive(command.actor)
                else:
                    result = self.desired.unregister_interactive_if_current(
                        command.actor, command.session_ref, command.effects
                    )
                self.projection.publish(self.desired.load())
            elif isinstance(command, RecordSessionAgentEffects):
                result = self.desired.record_session_agent_effects(command.effects)
                # The store returns the state it just wrote; reading it back
                # again was a second whole-document load per effect.
                self.projection.publish(result)
            elif isinstance(command, CompleteSessionAgentEffect):
                result = self.desired.complete_session_agent_effect(command.effect_id)
                self.projection.publish(self.desired.load())
            elif isinstance(command, SetChannelPin):
                result = self.desired.set_channel_pin(command.channel, command.agent)
                self.projection.publish(self.desired.load())
            elif isinstance(command, RemoveChannelPin):
                result = self.desired.remove_channel_pin(command.channel)
                self.projection.publish(self.desired.load())
            elif isinstance(command, ConfirmHarnessLifecycle):
                result = self.desired.confirm_harness_lifecycle(
                    command.attempt_token, command.resource_token, command.spec,
                    generation=command.generation, version=command.version,
                )
                self.projection.publish(self.desired.load())
            elif isinstance(command, CompleteHarnessLifecycle):
                result = self.desired.complete_harness_lifecycle(
                    command.attempt_token, command.resource_token,
                    generation=command.generation, version=command.version,
                )
                self.projection.publish(self.desired.load())
            elif isinstance(command, RecordHarnessLifecycleFailure):
                result = self.desired.record_harness_lifecycle_failure(
                    command.attempt_token, command.resource_token
                )
                self.projection.publish(self.desired.load())
            elif isinstance(command, RollbackHarnessLifecycle):
                result = self.desired.rollback_harness_lifecycle(
                    command.attempt_token, command.resource_token
                )
                self.projection.publish(self.desired.load())
            elif isinstance(command, FailHarnessRemoval):
                result = self.desired.fail_harness_removal(
                    command.attempt_token, command.resource_token
                )
                self.projection.publish(self.desired.load())
            elif isinstance(command, MarkHarnessFailed):
                result = self.desired.mark_harness_failed(command.harness, command.name)
                self.projection.publish(self.desired.load())
            elif isinstance(command, UpsertHarness):
                result = self.desired.upsert_harness(command.spec)
                self.projection.publish(self.desired.load())
            elif isinstance(command, RemoveHarness):
                result = self.desired.remove_harness(command.harness, command.name)
                self.projection.publish(self.desired.load())
            elif isinstance(command, ExpireInterruptedReceipts):
                result = self.desired.expire_interrupted_lifecycle_receipts()
                self.projection.publish(self.desired.load())
            elif isinstance(command, RemoveAdapterRegistration):
                result = self.desired.remove_adapter_registration(
                    command.name, expected_spec=command.expected_spec,
                    expected_legacy_pin=command.expected_legacy_pin,
                )
                self.projection.publish(self.desired.load())
            elif isinstance(command, RestoreAdapterRegistration):
                result = self.desired.restore_adapter_registration(
                    command.name, spec=command.spec,
                    legacy_pin=command.legacy_pin,
                )
                self.projection.publish(self.desired.load())
            elif isinstance(command, SyncHarnessSessionRefs):
                result = self.desired.sync_harness_session_refs(dict(command.refs))
                self.projection.publish(self.desired.load())
            elif isinstance(command, SetMailboxRole):
                result = self.desired.set_mailbox_role(command.enabled)
                self.projection.publish(self.desired.load())
            elif isinstance(command, UpdateZenohEndpoints):
                result = self.desired.update_zenoh_endpoints(
                    listen=command.listen, connect=command.connect,
                )
                self.projection.publish(self.desired.load())
            elif isinstance(command, SetServiceConnection):
                result = self.desired.set_service_connection(
                    command.name, command.local_port
                )
                self.projection.publish(result)
            elif isinstance(command, RemoveServiceConnection):
                result = self.desired.remove_service_connection(command.name)
                self.projection.publish(result)
            elif isinstance(command, SetServiceRegistry):
                result = self.desired.set_service_registry(command.cache)
                self.projection.publish(result)
            elif isinstance(command, ReserveLifecycle):
                result = self.journal.reserve(command.operation)
            elif isinstance(command, PrepareLifecycleEffect):
                result = self.journal.prepare_effect(
                    command.operation_id, command.ordinal, command.effect_name,
                    command.direction, command.effect_correlation_id,
                    command.attempt_token,
                )
            elif isinstance(command, CompleteLifecycleEffect):
                result = self.journal.complete_effect(
                    command.operation_id, command.effect_name, command.direction,
                    command.effect_correlation_id, provenance=command.provenance,
                )
            elif isinstance(command, MarkLifecycleReceiptRetired):
                result = self.journal.mark_receipt_retired(
                    command.operation_id, command.effect_name, command.direction
                )
            elif isinstance(command, RetireSharedDomainReceipt):
                with self.desired.state_db.transaction() as db:
                    row = db.execute(
                        "SELECT resource_token,completed FROM lifecycle_receipts "
                        "WHERE domain=? AND attempt_token=?",
                        (command.domain, command.attempt_token),
                    ).fetchone()
                    if (
                        row is None or not bool(row[1])
                        or str(row[0]) != command.resource_token
                    ):
                        result = False
                    else:
                        updated = db.execute(
                            "UPDATE lifecycle_effects SET receipt_retired=1 "
                            "WHERE operation_id=? AND effect_name=? AND direction=? "
                            "AND attempt_token=? AND resource_token=? "
                            "AND status='completed'",
                            (
                                command.operation_id, command.effect_name,
                                command.direction, command.attempt_token,
                                command.resource_token,
                            ),
                        )
                        if updated.rowcount != 1:
                            raise RuntimeError(
                                "journal and shared domain receipt disagree"
                            )
                        db.execute(
                            "DELETE FROM lifecycle_receipts WHERE domain=? "
                            "AND attempt_token=? AND resource_token=?",
                            (
                                command.domain, command.attempt_token,
                                command.resource_token,
                            ),
                        )
                        result = True
                if result:
                    self.projection.publish(self.desired.load())
            elif isinstance(command, NextLifecycleGeneration):
                result = self.journal.next_generation()
            elif isinstance(command, InterruptRunningLifecycle):
                result = self.journal.interrupt_running_operations(
                    command.cause, command.error_code
                )
            elif isinstance(command, SetLifecycleState):
                result = self.journal.set_state(
                    command.operation_id, command.state,
                    command.error, command.error_code,
                )
            elif isinstance(command, MarkLifecycleEffectDispatched):
                result = self.journal.mark_dispatched(
                    command.operation_id, command.effect_name, command.direction
                )
            elif isinstance(command, BackfillDomainAttestedEffects):
                result = backfill_domain_attested_effects(
                    self.journal._state_db, command.claims
                )
            else:
                raise TypeError("unsupported state persistence command")
        except Exception as error:
            if isinstance(command, (RemoveMigratedChannelPins,
                                    NormalizeInteractiveSessions, CompareAndSaveDesired,
                                    ApplySessionLifecycle, ApplyHarnessLifecycle,
                                    RetireDomainReceipt, ConfirmDomainReceipt,
                                    ClaimInteractiveSession, ClaimInteractiveWithAgentEffects,
                                    UnregisterInteractiveSession, RecordSessionAgentEffects,
                                    CompleteSessionAgentEffect, SetChannelPin,
                                    RemoveChannelPin, ConfirmHarnessLifecycle,
                                    CompleteHarnessLifecycle,
                                    RecordHarnessLifecycleFailure,
                                    RollbackHarnessLifecycle, FailHarnessRemoval,
                                    MarkHarnessFailed, UpsertHarness, RemoveHarness,
                                    ExpireInterruptedReceipts, RemoveAdapterRegistration,
                                    RestoreAdapterRegistration, SyncHarnessSessionRefs,
                                    SetMailboxRole, UpdateZenohEndpoints,
                                    SetServiceConnection, RemoveServiceConnection,
                                    SetServiceRegistry)) and not isinstance(
                error, StatePersistenceVersionConflict
            ):
                try:
                    self.projection.publish(self.desired.load())
                except Exception:
                    pass
            self.complete(command.correlation_id, self.generation, None, error)
        else:
            failed = False
            self.complete(command.correlation_id, self.generation, result, None)
        finally:
            if costs.enabled:
                costs.record(
                    _state_cost_key(command),
                    cpu_seconds=time.thread_time() - started_cpu,
                    error=failed,
                )
            wall = self.execution_wall
            if wall is not None and wall.enabled:
                wall.record(
                    _state_cost_key(command),
                    cpu_seconds=0.0,
                    wall_seconds=time.perf_counter() - started_wall,
                    error=failed,
                )


class _SettlementWaitingOwner:
    """Internal view whose typed calls join the original accepted mutation."""

    def __init__(self, owner: StatePersistenceAuthority) -> None:
        self._owner = owner

    def _call(self, command: StateCommand) -> Any:
        return self._owner.call_settled(command)

    def call_journal(
        self, operation: JournalQuery, *args: object, **kwargs: object
    ) -> Any:
        return self._owner.call_journal_settled(operation, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._owner, name)


class _DesiredPort(ServiceDesiredPortMixin):
    # DesiredStateIoPort also accepts a raw DesiredStateStore for isolated
    # ownership tests.  Only this typed port can transfer the immutable origin
    # into the StateCommand without changing the raw store API.
    supports_state_cost_origin = True

    def __init__(
        self, owner: StatePersistenceAuthority | _SettlementWaitingOwner
    ) -> None:
        self._owner = owner
        self.state_db = owner.state_db
        self.legacy_path = owner._desired_store.legacy_path
        self.versioned_path = owner._desired_store.versioned_path

    def load(self) -> DesiredState:
        # _Projection.read() already returns a fresh deep copy.  Copying that
        # result again doubled every whole-document read without adding
        # isolation.
        return self._owner.projection().desired

    def harness_spec(
        self, name: str, *, excluding: str | None = None
    ) -> HarnessLaunchSpec | None:
        return self._owner.harness_spec(name, excluding=excluding)

    def settled_result(self, correlation_id: str) -> object:
        completion = self._owner.settled_result(correlation_id)
        if completion is None:
            raise StatePersistenceTimeout(correlation_id)
        if completion.error is not None:
            raise completion.error
        return completion.result

    def remove_channel_pin_settled(
        self, channel: str
    ) -> tuple[DesiredState, str | None]:
        """Internal I/O-worker port that joins an accepted late mutation."""

        try:
            return self.remove_channel_pin(channel)
        except StatePersistenceTimeout as error:
            result = self.settled_result(error.correlation_id)
            if not isinstance(result, tuple) or len(result) != 2:
                raise RuntimeError("channel-pin retirement returned an invalid receipt")
            return result  # type: ignore[return-value]

    def compare_and_save(self, state: DesiredState, *, expected_version: int) -> DesiredState:
        return self._owner._call(
            CompareAndSaveDesired(
                f"state-compare-{uuid4().hex}", expected_version,
                _snapshot_payload(state),  # type: ignore[arg-type]
            )
        )

    def remove_matching_channel_pins(
        self, matches: tuple[tuple[str, str], ...]
    ) -> DesiredState:
        command = RemoveMigratedChannelPins(
            f"state-pins-{uuid4().hex}", tuple(matches)
        )
        return self._owner._call(command)

    def normalize_interactive_sessions(
        self, *, rewrites: tuple[tuple[str, str | None, str], ...]
    ) -> DesiredState:
        command = NormalizeInteractiveSessions(
            f"state-sessions-{uuid4().hex}", tuple(rewrites)
        )
        return self._owner._call(command)

    def apply_session_lifecycle(
        self,
        request: LifecycleMutationRequest,
        *,
        _cost_origin: StateCostOrigin = StateCostOrigin.BACKGROUND,
    ) -> object:
        return self._owner._call(
            ApplySessionLifecycle(
                f"state-session-{uuid4().hex}",
                _snapshot_payload(request),  # type: ignore[arg-type]
                _cost_origin,
            )
        )

    def apply_harness_lifecycle(
        self, request: LifecycleMutationRequest, *, generation: int | None = None,
        version: int | None = None,
    ) -> object:
        return self._owner._call(
            ApplyHarnessLifecycle(
                f"state-harness-{uuid4().hex}",
                _snapshot_payload(request),  # type: ignore[arg-type]
                generation, version,
            )
        )

    def retire_lifecycle_receipt(
        self, domain: str, attempt_token: str, resource_token: str
    ) -> bool:
        return self._owner._call(
            RetireDomainReceipt(
                f"state-retire-{uuid4().hex}", domain, attempt_token,
                resource_token,
            )
        )

    def confirm_lifecycle_receipt_retired(
        self, domain: str, attempt_token: str, resource_token: str
    ) -> bool:
        return self._owner._call(
            ConfirmDomainReceipt(
                f"state-confirm-{uuid4().hex}", domain, attempt_token,
                resource_token,
            )
        )

    def register_interactive(self, session: InteractiveSession) -> DesiredState:
        return self.claim_interactive(session)[0]

    def claim_interactive(
        self,
        session: InteractiveSession,
        *,
        _cost_origin: StateCostOrigin = StateCostOrigin.BACKGROUND,
    ) -> object:
        return self.claim_interactive_with_effects(
            session, (), _cost_origin=_cost_origin
        )

    def claim_interactive_with_effects(
        self, session: InteractiveSession,
        effects: tuple[PendingSessionAgentEffect, ...],
        *,
        _cost_origin: StateCostOrigin = StateCostOrigin.BACKGROUND,
    ) -> object:
        return self._owner._call(
            ClaimInteractiveSession(
                f"state-claim-{uuid4().hex}",
                _snapshot_payload(session),  # type: ignore[arg-type]
                _snapshot_payload(effects),  # type: ignore[arg-type]
                _cost_origin,
            )
        )

    def claim_interactive_with_agent_effects(
        self, session: InteractiveSession, bind_effect: PendingSessionAgentEffect,
        *,
        _cost_origin: StateCostOrigin = StateCostOrigin.BACKGROUND,
    ) -> object:
        return self._owner._call(
            ClaimInteractiveWithAgentEffects(
                f"state-bind-{uuid4().hex}",
                _snapshot_payload(session),  # type: ignore[arg-type]
                _snapshot_payload(bind_effect),  # type: ignore[arg-type]
                _cost_origin,
            )
        )

    def unregister_interactive(self, actor: str) -> DesiredState:
        return self._owner._call(
            UnregisterInteractiveSession(f"state-unregister-{uuid4().hex}", actor)
        )

    def unregister_interactive_if_current(
        self, actor: str, session_ref: str,
        effects: tuple[PendingSessionAgentEffect, ...] = (),
        *,
        _cost_origin: StateCostOrigin = StateCostOrigin.BACKGROUND,
    ) -> object:
        return self._owner._call(
            UnregisterInteractiveSession(
                f"state-unregister-{uuid4().hex}", actor, session_ref,
                _snapshot_payload(effects),  # type: ignore[arg-type]
                _cost_origin,
            )
        )

    def record_session_agent_effects(
        self,
        effects: tuple[PendingSessionAgentEffect, ...],
        *,
        _cost_origin: StateCostOrigin = StateCostOrigin.BACKGROUND,
    ) -> DesiredState:
        return self._owner._call(
            RecordSessionAgentEffects(
                f"state-agent-effects-{uuid4().hex}",
                _snapshot_payload(effects),  # type: ignore[arg-type]
                _cost_origin,
            )
        )

    def complete_session_agent_effect(
        self,
        effect_id: str,
        *,
        _cost_origin: StateCostOrigin = StateCostOrigin.BACKGROUND,
    ) -> bool:
        return self._owner._call(
            CompleteSessionAgentEffect(
                f"state-effect-done-{uuid4().hex}", effect_id, _cost_origin
            )
        )

    def set_channel_pin(self, channel: str, agent: str) -> object:
        return self._owner._call(
            SetChannelPin(f"state-pin-{uuid4().hex}", channel, agent)
        )

    def remove_channel_pin(self, channel: str) -> object:
        return self._owner._call(
            RemoveChannelPin(f"state-unpin-{uuid4().hex}", channel)
        )

    def confirm_harness_lifecycle(
        self, attempt_token: str, resource_token: str, spec: HarnessLaunchSpec,
        *, generation: int | None = None, version: int | None = None,
    ) -> bool:
        return self._owner._call(
            ConfirmHarnessLifecycle(
                f"state-harness-confirm-{uuid4().hex}", attempt_token,
                resource_token, _snapshot_payload(spec),  # type: ignore[arg-type]
                generation, version,
            )
        )

    def complete_harness_lifecycle(
        self, attempt_token: str, resource_token: str,
        *, generation: int | None = None, version: int | None = None,
    ) -> bool:
        return self._owner._call(
            CompleteHarnessLifecycle(
                f"state-harness-complete-{uuid4().hex}", attempt_token,
                resource_token, generation, version,
            )
        )

    def record_harness_lifecycle_failure(
        self, attempt_token: str, resource_token: str
    ) -> int:
        return self._owner._call(
            RecordHarnessLifecycleFailure(
                f"state-harness-failure-{uuid4().hex}",
                attempt_token, resource_token,
            )
        )

    def rollback_harness_lifecycle(
        self, attempt_token: str, resource_token: str
    ) -> bool:
        return self._owner._call(
            RollbackHarnessLifecycle(
                f"state-harness-rollback-{uuid4().hex}",
                attempt_token, resource_token,
            )
        )

    def fail_harness_removal(
        self, attempt_token: str, resource_token: str
    ) -> bool:
        return self._owner._call(
            FailHarnessRemoval(
                f"state-harness-removal-{uuid4().hex}",
                attempt_token, resource_token,
            )
        )

    def mark_harness_failed(self, harness: str, name: str) -> bool:
        return self._owner._call(
            MarkHarnessFailed(
                f"state-harness-failed-{uuid4().hex}", harness, name,
            )
        )

    def upsert_harness(self, spec: HarnessLaunchSpec) -> DesiredState:
        return self._owner._call(
            UpsertHarness(
                f"state-harness-upsert-{uuid4().hex}",
                _snapshot_payload(spec),  # type: ignore[arg-type]
            )
        )

    def remove_harness(self, harness: str, name: str) -> DesiredState:
        return self._owner._call(
            RemoveHarness(f"state-harness-remove-{uuid4().hex}", harness, name)
        )

    def expire_interrupted_lifecycle_receipts(self) -> tuple[int, int]:
        return self._owner._call(
            ExpireInterruptedReceipts(f"state-expire-{uuid4().hex}")
        )

    def remove_adapter_registration(
        self, name: str, *, expected_spec: HarnessLaunchSpec | None,
        expected_legacy_pin: str | None,
    ) -> DesiredState:
        return self._owner._call(
            RemoveAdapterRegistration(
                f"state-adapter-remove-{uuid4().hex}", name,
                _snapshot_payload(expected_spec),  # type: ignore[arg-type]
                expected_legacy_pin,
            )
        )

    def restore_adapter_registration(
        self, name: str, *, spec: HarnessLaunchSpec | None,
        legacy_pin: str | None,
    ) -> DesiredState:
        return self._owner._call(
            RestoreAdapterRegistration(
                f"state-adapter-restore-{uuid4().hex}", name,
                _snapshot_payload(spec),  # type: ignore[arg-type]
                legacy_pin,
            )
        )

    def sync_harness_session_refs(
        self, refs: Mapping[tuple[str, str], str]
    ) -> DesiredState:
        copied = tuple(sorted((tuple(key), value) for key, value in refs.items()))
        return self._owner._call(
            SyncHarnessSessionRefs(f"state-harness-refs-{uuid4().hex}", copied)
        )

    def set_mailbox_role(self, enabled: bool) -> DesiredState:
        return self._owner._call(
            SetMailboxRole(f"state-mailbox-{uuid4().hex}", enabled)
        )

    def update_zenoh_endpoints(
        self, *, listen: tuple[str, ...] | None = None,
        connect: tuple[str, ...] | None = None,
    ) -> DesiredState:
        return self._owner._call(
            UpdateZenohEndpoints(
                f"state-zenoh-{uuid4().hex}", listen, connect,
            )
        )

    def __getattr__(self, name: str) -> Any:
        operation = DesiredQuery(name)  # read-only closed set
        return lambda *args, **kwargs: self._owner.call_desired(
            operation, *args, **kwargs
        )


class _JournalPort:
    def __init__(
        self, owner: StatePersistenceAuthority | _SettlementWaitingOwner
    ) -> None:
        self._owner = owner
        self._state_db = owner.state_db

    def next_generation(self) -> int:
        return self._owner._call(NextLifecycleGeneration(f"state-generation-{uuid4().hex}"))

    def interrupt_running_operations(self, cause: str, error_code: str) -> tuple[str, ...]:
        return self._owner._call(
            InterruptRunningLifecycle(
                f"state-interrupt-{uuid4().hex}", cause, error_code
            )
        )

    def backfill_domain_attested_effects(
        self, claims: tuple[DomainEffectClaim, ...]
    ) -> tuple[str, ...]:
        return self._owner._call(
            BackfillDomainAttestedEffects(
                f"state-backfill-{uuid4().hex}", tuple(claims)
            )
        )

    def set_state(
        self, operation_id: str, state: LifecycleState,
        error: str | None = None, error_code: str | None = None,
    ) -> None:
        self._owner._call(
            SetLifecycleState(
                f"state-status-{uuid4().hex}", operation_id, state, error,
                error_code,
            )
        )

    def mark_dispatched(self, operation_id: str, name: str, direction: str) -> None:
        self._owner._call(
            MarkLifecycleEffectDispatched(
                f"state-dispatched-{uuid4().hex}", operation_id, name,
                direction,
            )
        )

    def reserve(self, operation: LifecycleOperation) -> object:
        return self._owner._call(
            ReserveLifecycle(f"state-reserve-{uuid4().hex}", operation)
        )

    def prepare_effect(
        self, operation_id: str, ordinal: int, name: str, direction: str,
        correlation_id: str, attempt_token: str,
    ) -> object:
        return self._owner._call(
            PrepareLifecycleEffect(
                f"state-effect-{uuid4().hex}", operation_id, ordinal, name,
                direction, correlation_id, attempt_token,
            )
        )

    def complete_effect(
        self, operation_id: str, name: str, direction: str,
        correlation_id: str, *, provenance: MutationProvenance,
    ) -> None:
        self._owner._call(
            CompleteLifecycleEffect(
                f"state-effect-complete-{uuid4().hex}", operation_id, name,
                direction, correlation_id, provenance,
            )
        )

    def mark_receipt_retired(
        self, operation_id: str, name: str, direction: str
    ) -> None:
        self._owner._call(
            MarkLifecycleReceiptRetired(
                f"state-receipt-{uuid4().hex}", operation_id, name, direction,
            )
        )

    def retire_shared_receipt(
        self, domain: Literal["session", "harness"], operation_id: str,
        effect_name: str, direction: str, attempt_token: str,
        resource_token: str,
    ) -> bool:
        return self._owner._call(RetireSharedDomainReceipt(
            f"state-shared-receipt-{uuid4().hex}", domain, operation_id,
            effect_name, direction, attempt_token, resource_token,
        ))

    def __getattr__(self, name: str) -> Any:
        if name == "close":
            return lambda: None  # the composition root owns the shared actor
        operation = JournalQuery(name)
        return lambda *args, **kwargs: self._owner.call_journal(
            operation, *args, **kwargs
        )
