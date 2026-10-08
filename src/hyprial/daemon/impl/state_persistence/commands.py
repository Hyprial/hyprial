"""The state persistence command vocabulary: one dataclass per serialized state mutation plus its cost keys."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, fields, is_dataclass
from enum import StrEnum
from typing import Literal, get_args
from hyprial.daemon.impl.desired_state  import (
    DesiredState, InteractiveSession,
    PendingSessionAgentEffect,
    ServiceRegistry,
)
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.lifecycle_manager  import (
    LifecycleOperation,
    LifecycleState,
)
from hyprial.kernel  import (
    MutationProvenance,
)
from hyprial.kernel import DomainEffectClaim
from hyprial.kernel import LifecycleMutationRequest


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
class SetServiceConnection:
    correlation_id: str
    name: str
    local_port: int


@dataclass(frozen=True, slots=True)
class RemoveServiceConnection:
    correlation_id: str
    name: str


@dataclass(frozen=True, slots=True)
class SetServiceRegistry:
    correlation_id: str
    cache: ServiceRegistry | None


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
    | SetServiceConnection
    | RemoveServiceConnection
    | SetServiceRegistry
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
