"""One mailbox for desired-state and lifecycle-journal operations.

The production composition root can give `desired` to Session/Harness and
`journal` to LifecycleProcessManager.  Every allowed write then reaches the
same actor and the same StateDatabase.  Calls are typed by a closed operation
catalogue; callers cannot submit SQL or executable closures.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, fields, is_dataclass
from enum import StrEnum
from typing import Any, Literal, cast, get_args
from uuid import uuid4

from hyprial.actor_runtime import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.cost_counters import CallCostCounters

from .desired_state import (
    DesiredState, DesiredStateStore, HarnessLaunchSpec, InteractiveSession,
    PendingSessionAgentEffect,
)
from .lifecycle_manager import (
    LifecycleOperation,
    LifecycleState,
    _LifecycleStore,
    backfill_domain_attested_effects,
)
from .lifecycle_receipts import (
    DomainEffectClaim,
    LifecycleMutationRequest,
    MutationProvenance,
)
from .state_db import StateDatabase


class DesiredQuery(StrEnum):
    INCOMPLETE_HARNESS_LIFECYCLE_RESOURCES = "incomplete_harness_lifecycle_resources"
    INCOMPLETE_HARNESS_LIFECYCLE_RECEIPTS = "incomplete_harness_lifecycle_receipts"
    HARNESS_LIFECYCLE_RECEIPT = "harness_lifecycle_receipt"
    LIFECYCLE_EFFECT_CLAIMS = "lifecycle_effect_claims"
    ADAPTER_REGISTRATION = "adapter_registration"


class JournalQuery(StrEnum):
    PENDING = "pending"
    LOAD = "load"
    STATE = "state"
    EFFECT_DONE = "effect_done"
    EFFECT_RECEIPT = "effect_receipt"
    RECEIPT_RETIREMENT = "receipt_retirement"
    COMPLETED_RECEIPT = "completed_receipt"
    FORWARD_RESOURCE_TOKEN = "forward_resource_token"
    COMPLETED_FORWARD = "completed_forward"
    COMPENSABLE_FORWARD = "compensable_forward"
    RESULT = "result"


class StateCostOrigin(StrEnum):
    IPC = "ipc"
    BACKGROUND = "background"


@dataclass(frozen=True, slots=True)
class StateProjection:
    version: int
    desired: DesiredState


@dataclass(frozen=True, slots=True)
class ReadDesiredQuery:
    correlation_id: str
    operation: DesiredQuery
    key: str | None = None


@dataclass(frozen=True, slots=True)
class ReadJournalQuery:
    correlation_id: str
    operation: JournalQuery
    operation_id: str | None = None
    effect_name: str | None = None
    direction: str | None = None


@dataclass(frozen=True, slots=True)
class RemoveMigratedChannelPins:
    correlation_id: str
    matches: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class NormalizeInteractiveSessions:
    correlation_id: str
    rewrites: tuple[tuple[str, str | None, str], ...]


@dataclass(frozen=True, slots=True)
class CompareAndSaveDesired:
    correlation_id: str
    expected_version: int
    desired: DesiredState


@dataclass(frozen=True, slots=True)
class ApplySessionLifecycle:
    correlation_id: str
    request: LifecycleMutationRequest
    cost_origin: StateCostOrigin = StateCostOrigin.BACKGROUND


@dataclass(frozen=True, slots=True)
class ApplyHarnessLifecycle:
    correlation_id: str
    request: LifecycleMutationRequest
    generation: int | None = None
    version: int | None = None


@dataclass(frozen=True, slots=True)
class RetireDomainReceipt:
    correlation_id: str
    domain: str
    attempt_token: str
    resource_token: str


@dataclass(frozen=True, slots=True)
class ConfirmDomainReceipt:
    correlation_id: str
    domain: str
    attempt_token: str
    resource_token: str


@dataclass(frozen=True, slots=True)
class ClaimInteractiveSession:
    correlation_id: str
    session: InteractiveSession
    effects: tuple[PendingSessionAgentEffect, ...] = ()
    cost_origin: StateCostOrigin = StateCostOrigin.BACKGROUND


@dataclass(frozen=True, slots=True)
class ClaimInteractiveWithAgentEffects:
    correlation_id: str
    session: InteractiveSession
    bind_effect: PendingSessionAgentEffect
    cost_origin: StateCostOrigin = StateCostOrigin.BACKGROUND


@dataclass(frozen=True, slots=True)
class UnregisterInteractiveSession:
    correlation_id: str
    actor: str
    session_ref: str | None = None
    effects: tuple[PendingSessionAgentEffect, ...] = ()
    cost_origin: StateCostOrigin = StateCostOrigin.BACKGROUND


@dataclass(frozen=True, slots=True)
class RecordSessionAgentEffects:
    correlation_id: str
    effects: tuple[PendingSessionAgentEffect, ...]
    cost_origin: StateCostOrigin = StateCostOrigin.BACKGROUND


@dataclass(frozen=True, slots=True)
class CompleteSessionAgentEffect:
    correlation_id: str
    effect_id: str
    cost_origin: StateCostOrigin = StateCostOrigin.BACKGROUND


@dataclass(frozen=True, slots=True)
class SetChannelPin:
    correlation_id: str
    channel: str
    agent: str


@dataclass(frozen=True, slots=True)
class RemoveChannelPin:
    correlation_id: str
    channel: str


@dataclass(frozen=True, slots=True)
class ConfirmHarnessLifecycle:
    correlation_id: str
    attempt_token: str
    resource_token: str
    spec: HarnessLaunchSpec
    generation: int | None = None
    version: int | None = None


@dataclass(frozen=True, slots=True)
class CompleteHarnessLifecycle:
    correlation_id: str
    attempt_token: str
    resource_token: str
    generation: int | None = None
    version: int | None = None


@dataclass(frozen=True, slots=True)
class RecordHarnessLifecycleFailure:
    correlation_id: str
    attempt_token: str
    resource_token: str


@dataclass(frozen=True, slots=True)
class RollbackHarnessLifecycle:
    correlation_id: str
    attempt_token: str
    resource_token: str


@dataclass(frozen=True, slots=True)
class FailHarnessRemoval:
    correlation_id: str
    attempt_token: str
    resource_token: str


@dataclass(frozen=True, slots=True)
class MarkHarnessFailed:
    correlation_id: str
    harness: str
    name: str


@dataclass(frozen=True, slots=True)
class UpsertHarness:
    correlation_id: str
    spec: HarnessLaunchSpec


@dataclass(frozen=True, slots=True)
class RemoveHarness:
    correlation_id: str
    harness: str
    name: str


@dataclass(frozen=True, slots=True)
class ExpireInterruptedReceipts:
    correlation_id: str


@dataclass(frozen=True, slots=True)
class RemoveAdapterRegistration:
    correlation_id: str
    name: str
    expected_spec: HarnessLaunchSpec | None
    expected_legacy_pin: str | None


@dataclass(frozen=True, slots=True)
class RestoreAdapterRegistration:
    correlation_id: str
    name: str
    spec: HarnessLaunchSpec | None
    legacy_pin: str | None


@dataclass(frozen=True, slots=True)
class SyncHarnessSessionRefs:
    correlation_id: str
    refs: tuple[tuple[tuple[str, str], str], ...]


@dataclass(frozen=True, slots=True)
class SetMailboxRole:
    correlation_id: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class UpdateZenohEndpoints:
    correlation_id: str
    listen: tuple[str, ...] | None
    connect: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class ReserveLifecycle:
    correlation_id: str
    operation: LifecycleOperation


@dataclass(frozen=True, slots=True)
class PrepareLifecycleEffect:
    correlation_id: str
    operation_id: str
    ordinal: int
    effect_name: str
    direction: str
    effect_correlation_id: str
    attempt_token: str


@dataclass(frozen=True, slots=True)
class CompleteLifecycleEffect:
    correlation_id: str
    operation_id: str
    effect_name: str
    direction: str
    effect_correlation_id: str
    provenance: MutationProvenance


@dataclass(frozen=True, slots=True)
class MarkLifecycleReceiptRetired:
    correlation_id: str
    operation_id: str
    effect_name: str
    direction: str


@dataclass(frozen=True, slots=True)
class RetireSharedDomainReceipt:
    correlation_id: str
    domain: Literal["session", "harness"]
    operation_id: str
    effect_name: str
    direction: str
    attempt_token: str
    resource_token: str


@dataclass(frozen=True, slots=True)
class NextLifecycleGeneration:
    correlation_id: str


@dataclass(frozen=True, slots=True)
class InterruptRunningLifecycle:
    correlation_id: str
    cause: str
    error_code: str


@dataclass(frozen=True, slots=True)
class SetLifecycleState:
    correlation_id: str
    operation_id: str
    state: LifecycleState
    error: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class MarkLifecycleEffectDispatched:
    correlation_id: str
    operation_id: str
    effect_name: str
    direction: str


@dataclass(frozen=True, slots=True)
class BackfillDomainAttestedEffects:
    correlation_id: str
    claims: tuple[DomainEffectClaim, ...]


StateCommand = (
    ReadDesiredQuery
    | ReadJournalQuery
    | RemoveMigratedChannelPins
    | NormalizeInteractiveSessions
    | CompareAndSaveDesired
    | ApplySessionLifecycle
    | ApplyHarnessLifecycle
    | RetireDomainReceipt
    | ConfirmDomainReceipt
    | ClaimInteractiveSession
    | ClaimInteractiveWithAgentEffects
    | UnregisterInteractiveSession
    | RecordSessionAgentEffects
    | CompleteSessionAgentEffect
    | SetChannelPin
    | RemoveChannelPin
    | ConfirmHarnessLifecycle
    | CompleteHarnessLifecycle
    | RecordHarnessLifecycleFailure
    | RollbackHarnessLifecycle
    | FailHarnessRemoval
    | MarkHarnessFailed
    | UpsertHarness
    | RemoveHarness
    | ExpireInterruptedReceipts
    | RemoveAdapterRegistration
    | RestoreAdapterRegistration
    | SyncHarnessSessionRefs
    | SetMailboxRole
    | UpdateZenohEndpoints
    | ReserveLifecycle
    | PrepareLifecycleEffect
    | CompleteLifecycleEffect
    | MarkLifecycleReceiptRetired
    | RetireSharedDomainReceipt
    | NextLifecycleGeneration
    | InterruptRunningLifecycle
    | SetLifecycleState
    | MarkLifecycleEffectDispatched
    | BackfillDomainAttestedEffects
)


_STATE_COST_KEYS = frozenset(
    f"{command_type.__name__}/{origin.value}"
    for command_type in get_args(StateCommand)
    for origin in StateCostOrigin
)


def _state_cost_key(command: StateCommand) -> str:
    origin = getattr(command, "cost_origin", StateCostOrigin.BACKGROUND)
    suffix = (
        StateCostOrigin.IPC.value
        if origin is StateCostOrigin.IPC
        else StateCostOrigin.BACKGROUND.value
    )
    return f"{type(command).__name__}/{suffix}"


def _snapshot_payload(value: object) -> object:
    """Copy data before admission and refuse executable/connection payloads."""

    if callable(value):
        raise TypeError("state commands cannot carry executable callbacks")
    if value is None or isinstance(value, (str, bytes, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {
            _snapshot_payload(key): _snapshot_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list, frozenset)):
        copied = tuple(_snapshot_payload(item) for item in value)
        return copied if not isinstance(value, frozenset) else frozenset(copied)
    if is_dataclass(value) and not isinstance(value, type):
        if not getattr(type(value), "__dataclass_params__").frozen:
            raise TypeError("state commands require frozen data records")
        for field in fields(value):
            _snapshot_payload(getattr(value, field.name))
        return deepcopy(value)
    raise TypeError(f"unsupported state command payload: {type(value).__name__}")


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

    def publish(self, desired: DesiredState) -> None:
        with self._lock:
            if desired == self._snapshot.desired:
                return
            self._snapshot = StateProjection(self._snapshot.version + 1, desired)


class _Generation:
    def __init__(
        self,
        generation: int,
        desired: DesiredStateStore,
        journal: _LifecycleStore,
        projection: _Projection,
        complete: Any,
        command_costs: CallCostCounters,
    ) -> None:
        self.generation = generation
        self.desired = desired
        self.journal = journal
        self.projection = projection
        self.complete = complete
        self.command_costs = command_costs

    def __call__(self, command: StateCommand) -> None:
        costs = self.command_costs
        failed = True
        started_cpu = time.thread_time() if costs.enabled else 0.0
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
                op_id = command.operation_id
                if command.operation is JournalQuery.PENDING:
                    result = self.journal.pending()
                else:
                    assert op_id is not None
                    if command.operation is JournalQuery.LOAD:
                        result = self.journal.load(op_id)
                    elif command.operation is JournalQuery.STATE:
                        result = self.journal.state(op_id)
                    elif command.operation is JournalQuery.RESULT:
                        result = self.journal.result(op_id)
                    elif command.operation is JournalQuery.COMPLETED_FORWARD:
                        result = self.journal.completed_forward(op_id)
                    elif command.operation is JournalQuery.COMPENSABLE_FORWARD:
                        result = self.journal.compensable_forward(op_id)
                    elif command.operation is JournalQuery.FORWARD_RESOURCE_TOKEN:
                        assert command.effect_name is not None
                        result = self.journal.forward_resource_token(op_id, command.effect_name)
                    else:
                        assert command.effect_name is not None and command.direction is not None
                        if command.operation is JournalQuery.EFFECT_DONE:
                            result = self.journal.effect_done(op_id, command.effect_name, command.direction)
                        elif command.operation is JournalQuery.EFFECT_RECEIPT:
                            result = self.journal.effect_receipt(op_id, command.effect_name, command.direction)
                        elif command.operation is JournalQuery.RECEIPT_RETIREMENT:
                            result = self.journal.receipt_retirement(op_id, command.effect_name, command.direction)
                        elif command.operation is JournalQuery.COMPLETED_RECEIPT:
                            result = self.journal.completed_receipt(op_id, command.effect_name, command.direction)
                        else:
                            raise TypeError("unsupported journal query")
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
                self.projection.publish(self.desired.load())
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
                                    SetMailboxRole, UpdateZenohEndpoints)) and not isinstance(
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


class _DesiredPort:
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
        # Defensive copy prevents callers from mutating a nested JSON value
        # after it has been published as part of a shared projection.
        return DesiredState.from_json(self._owner.projection().desired.to_json())

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


class StatePersistenceAuthority:
    """Bounded one-writer authority for the shared lifecycle SQLite file.

    `call_*` returns only after settlement. A timeout does not cancel a
    submitted command; internal I/O owners can join it through
    `settled_result`, or use the typed `settled_desired` / `settled_journal`
    views to wait through that observational deadline on the original cell.
    Abandoned completions retire active capacity into a bounded late-result
    table instead of filling the writer forever.
    """

    def __init__(
        self,
        desired_store: DesiredStateStore,
        *,
        mailbox_capacity: int = 128,
        completion_capacity: int = 256,
        call_timeout: float = 70.0,
    ) -> None:
        if completion_capacity < mailbox_capacity or mailbox_capacity < 1:
            raise ValueError("completion capacity must cover the mailbox")
        self.state_db: StateDatabase = desired_store.state_db
        self._desired_store = desired_store
        self._journal_store = _LifecycleStore(self.state_db)
        self._projection = _Projection(desired_store.load())
        self._pending: dict[str, _Pending] = {}
        self._late_results: OrderedDict[str, StateCommandCompleted] = OrderedDict()
        self._lock = threading.Lock()
        self._closed = False
        self._completion_capacity = completion_capacity
        self._call_timeout = call_timeout
        self._generation = 0
        # Owned by the authority rather than one supervised generation so
        # diagnostics remain monotonic across an actor restart.
        self._command_costs = CallCostCounters(_STATE_COST_KEYS, wall=False)
        self._runtime = ActorRuntime()

        def factory() -> _Generation:
            self._generation += 1
            return _Generation(
                self._generation,
                self._desired_store,
                self._journal_store,
                self._projection,
                self._complete,
                self._command_costs,
            )

        self._handle = self._runtime.start(
            ActorSpec(
                name="state-persistence-authority",
                handler_factory=factory,
                mailbox_capacity=mailbox_capacity,
                supervision_profile="state_authority",
                undelivered_sink=self._undelivered,
            )
        )
        self.desired = _DesiredPort(self)
        self.journal = _JournalPort(self)
        settlement_owner = _SettlementWaitingOwner(self)
        self.settled_desired = _DesiredPort(settlement_owner)
        self.settled_journal = _JournalPort(settlement_owner)

    @property
    def command_costs(self) -> CallCostCounters:
        return self._command_costs

    def _undelivered(self, command: object, reason: str) -> None:
        correlation = getattr(command, "correlation_id", None)
        if correlation:
            self._complete(
                correlation, self._generation, None, StatePersistenceBusy(reason)
            )

    def projection(self) -> StateProjection:
        return self._projection.read()

    def call_desired(self, operation: DesiredQuery, *args: object, **kwargs: object) -> Any:
        if any(callable(item) for item in args) or any(callable(item) for item in kwargs.values()):
            raise TypeError("state commands cannot carry executable callbacks")
        if kwargs:
            raise TypeError("desired queries do not accept keyword arguments")
        key_queries = {
            DesiredQuery.HARNESS_LIFECYCLE_RECEIPT,
            DesiredQuery.ADAPTER_REGISTRATION,
        }
        if operation in key_queries:
            if len(args) != 1 or not isinstance(args[0], str):
                raise TypeError(f"{operation.value} requires one string key")
            key = args[0]
        elif not args:
            key = None
        else:
            raise TypeError(f"{operation.value} accepts no arguments")
        return self._call(ReadDesiredQuery(
            f"state-desired-{uuid4().hex}", operation, key,
        ))

    def call_journal(self, operation: JournalQuery, *args: object, **kwargs: object) -> Any:
        return self._call(self._journal_query(operation, *args, **kwargs))

    def call_journal_settled(
        self, operation: JournalQuery, *args: object, **kwargs: object
    ) -> Any:
        """Join the exact accepted read for internal lifecycle owners."""

        return self.call_settled(self._journal_query(operation, *args, **kwargs))

    @staticmethod
    def _journal_query(
        operation: JournalQuery, *args: object, **kwargs: object
    ) -> ReadJournalQuery:
        if any(callable(item) for item in args) or any(callable(item) for item in kwargs.values()):
            raise TypeError("state commands cannot carry executable callbacks")
        if kwargs:
            raise TypeError("journal queries do not accept keyword arguments")
        arity = {
            JournalQuery.PENDING: 0,
            JournalQuery.LOAD: 1,
            JournalQuery.STATE: 1,
            JournalQuery.RESULT: 1,
            JournalQuery.COMPLETED_FORWARD: 1,
            JournalQuery.COMPENSABLE_FORWARD: 1,
            JournalQuery.FORWARD_RESOURCE_TOKEN: 2,
            JournalQuery.EFFECT_DONE: 3,
            JournalQuery.EFFECT_RECEIPT: 3,
            JournalQuery.RECEIPT_RETIREMENT: 3,
            JournalQuery.COMPLETED_RECEIPT: 3,
        }[operation]
        if len(args) != arity or any(not isinstance(item, str) for item in args):
            raise TypeError(f"{operation.value} requires {arity} string arguments")
        return ReadJournalQuery(
            f"state-journal-{uuid4().hex}", operation,
            args[0] if arity >= 1 else None,  # type: ignore[arg-type]
            args[1] if arity >= 2 else None,  # type: ignore[arg-type]
            args[2] if arity >= 3 else None,  # type: ignore[arg-type]
        )

    def call_settled(self, command: StateCommand) -> Any:
        """Submit one frozen typed command and join its exact completion.

        This port is for bounded internal I/O lanes. It deliberately waits
        through the public observational deadline after admission; callers
        must not wrap a timeout by constructing and submitting a replacement
        command with a new correlation ID.
        """

        if not isinstance(command, get_args(StateCommand)):
            raise TypeError("call_settled requires a typed StateCommand")
        if not command.correlation_id.strip():
            raise ValueError("state command correlation_id must not be blank")
        frozen = cast(StateCommand, _snapshot_payload(command))
        return self._call(frozen, wait_for_settlement=True)

    def _call(
        self,
        command: StateCommand,
        *,
        wait_for_settlement: bool = False,
    ) -> Any:
        pending = _Pending()
        with self._lock:
            if self._closed:
                raise StatePersistenceBusy("state authority is closed")
            if len(self._pending) >= self._completion_capacity:
                raise StatePersistenceBusy("state completion capacity is full")
            if command.correlation_id in self._pending:
                raise StatePersistenceBusy(
                    f"state correlation is already pending: {command.correlation_id}"
                )
            self._pending[command.correlation_id] = pending
            admission = self._runtime.tell(self._handle, command)
            if admission is not AdmissionResult.ACCEPTED:
                del self._pending[command.correlation_id]
                raise StatePersistenceBusy(f"state command admission: {admission.value}")
        if not pending.event.wait(self._call_timeout):
            if wait_for_settlement:
                # Internal I/O owners already hold bounded effect custody.
                # Keep the original pending cell and wait for its exact
                # completion; retrying the command could repeat a mutation.
                pending.event.wait()
            else:
                with self._lock:
                    if pending.generation is None:
                        pending.abandoned = True
                        raise StatePersistenceTimeout(command.correlation_id)
        completion = self.result(command.correlation_id)
        assert completion is not None
        if completion.error is not None:
            raise completion.error
        return completion.result

    def result(self, correlation_id: str) -> StateCommandCompleted | None:
        with self._lock:
            late = self._late_results.pop(correlation_id, None)
            if late is not None:
                return late
            pending = self._pending.get(correlation_id)
            if pending is None or pending.generation is None:
                return None
            del self._pending[correlation_id]
            return StateCommandCompleted(
                correlation_id, pending.generation, pending.result, pending.error
            )

    def settled_result(
        self, correlation_id: str, timeout: float | None = None
    ) -> StateCommandCompleted | None:
        """Join an accepted command after its synchronous caller timed out.

        Internal I/O owners use this port to retain the exact mutation receipt.
        Completed abandoned calls move to a bounded late-result table, so they
        no longer consume active command capacity while remaining retrievable.
        """

        with self._lock:
            late = self._late_results.pop(correlation_id, None)
            if late is not None:
                return late
            pending = self._pending.get(correlation_id)
        if pending is None or not pending.event.wait(timeout):
            return None
        with self._lock:
            late = self._late_results.pop(correlation_id, None)
            if late is not None:
                return late
            current = self._pending.get(correlation_id)
            if pending.generation is None:
                return None
            if current is pending:
                del self._pending[correlation_id]
            # A live joiner owns this exact cell even if the bounded late-result
            # cache has already evicted its entry before this waiter resumes.
            return StateCommandCompleted(
                correlation_id, pending.generation, pending.result, pending.error
            )

    def _complete(
        self, correlation_id: str, generation: int,
        result: object, error: Exception | None,
    ) -> None:
        with self._lock:
            pending = self._pending.get(correlation_id)
            if pending is None:
                return
            pending.generation = generation
            pending.result = result
            pending.error = error
            pending.event.set()
            if pending.abandoned:
                del self._pending[correlation_id]
                self._late_results[correlation_id] = StateCommandCompleted(
                    correlation_id, generation, result, error
                )
                while len(self._late_results) > self._completion_capacity:
                    self._late_results.popitem(last=False)

    def close(self, timeout: float = 5.0) -> bool:
        with self._lock:
            self._closed = True
        return self._runtime.drain(timeout).complete
