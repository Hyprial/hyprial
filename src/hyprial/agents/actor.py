"""Actor-owned agent registry mutations and public projections.

SQLite remains the final authority for identity and pin uniqueness.  This
actor adds serialized mutation, correlated events, runtime bindings and
restart recovery without weakening the database constraints or reaching into
Harness/Adapter implementations.
"""

from __future__ import annotations

import threading
import time
import sqlite3
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol
from uuid import uuid4

from hyprial.actor_runtime import (
    ActorRuntime,
    ActorSpec,
    AdmissionResult,
    DrainReport,
)
from hyprial.actor_runtime.effects import EffectCompleted, EffectLane, EffectRequest
from hyprial.contracts import ipc_errors
from hyprial.contracts.ports import PortAdmission, PortCommandRejected

from .liveness import (
    RUNTIME_INTERACTIVE,
    AgentAlreadyRunning,
    AgentBinding,
    AgentLiveness,
)
from .ports import (
    AgentBindingProjection,
    AgentCommand,
    AgentEvent,
    AgentCapabilityGrantCompleted,
    AgentSecretGrantCompleted,
    AgentLifecycleReceiptCompleted,
    AgentMutationCompleted,
    AgentDestroySettled,
    AgentDestroyDisposition,
    AgentDestroyReservationCompleted,
    AgentRuntimeLaunchCustodyProjection,
    AgentRuntimeLaunchLeaseCompleted,
    AgentProjection,
    AgentRuntimeProjection,
    AgentResolveProjection,
    BindAgentCommand,
    BlockAgentCommand,
    ClearRestoreDispositionCommand,
    AcquireAgentRuntimeLaunchCommand,
    CreateAgentCommand,
    CreateHostInvitedAgentCommand,
    CreateTransferHostedAgentCommand,
    CleanupRevokedAgentHomeCommand,
    ConfirmAgentLifecycleReceiptCommand,
    GrantAgentCapabilityCommand,
    RevokeAgentCapabilityCommand,
    GrantAgentSecretCommand,
    RevokeAgentSecretCommand,
    DestroyAgentCommand,
    SettleAgentDestroyCommand,
    PinAgentAdapterCommand,
    RecordAgentActivityCommand,
    SetRestoreDispositionCommand,
    ReserveAgentDestroyCommand,
    ReleaseAgentCommand,
    ReleaseAgentRuntimeLaunchCommand,
    ReleaseAgentDestroyReservationCommand,
    RetireAgentLifecycleReceiptCommand,
    UnpinAgentAdapterCommand,
    UnblockAgentCommand,
    UpdateAgentCommand,
    AgentBlockProjection,
    RestoreDispositionProjection,
)
from .registry import (
    Agent,
    AgentError,
    AgentHomeError,
    AgentHomeReservation,
    AgentRegistry,
)
from .grants import CapabilityGrant, GrantJournalEntry
from .home import HomeReceipt, WorkspaceSummary
from .config import AgentConfig
from .secrets import SecretGrant
from .read_port import AgentActivityHints, AgentReadEffectPort
from .home_effects import (
    AgentHomeEffect,
    AgentHomeEffectCompleted,
    AgentHomeEffectLane,
    PrepareRuntimeHome,
    PrepareWorkspaceHome,
)
from .runtime import (
    AgentRuntimeContext,
    AgentToolProfile,
    build_agent_runtime_preparation,
)
from .migration_actor import (
    MigrationEffect,
    MigrationFailure,
    MigrationOutcome,
    MigrationOwnerCall,
    MigrationOwnerResult,
    MigrationRequest,
    run_migration_effect,
)
from .migration_entry import MigrationAuthorizationWindow, MigrationPreflightManifest
from hyprial.cost_counters import CallCostCounters
from hyprial.uri import canonical_agent_uri, parse_agent_uri

__all__ = [
    "AgentActor",
    "AgentRegistryActor",
    "SenderIdentityError",
    "AgentUpdateConflict",
    "AgentHomeAccessError",
]


class _DesiredStatePort(Protocol):
    def load(self) -> object: ...

    def remove_channel_pin(self, channel: str) -> tuple[object, str | None]: ...

    def remove_channel_pin_settled(
        self, channel: str
    ) -> tuple[object, str | None]: ...


class SenderIdentityError(AgentError):
    code = ipc_errors.SENDER_UNRESOLVED


class AgentUpdateConflict(AgentError):
    code = "AGENT_VERSION_CONFLICT"


class AgentHomeAccessError(AgentError):
    code = "AGENT_HOME_ACCESS_FAILED"


class AgentRuntimeContextStale(AgentError):
    code = "AGENT_RUNTIME_CONTEXT_STALE"


class AgentRuntimeLaunchMismatch(AgentError):
    code = "AGENT_RUNTIME_LAUNCH_MISMATCH"


class AgentMigrationBusy(AgentError):
    code = ipc_errors.AGENT_MIGRATION_BUSY


class AgentLaunchInProgress(AgentError):
    code = "AGENT_LAUNCH_IN_PROGRESS"


# The fixed key set of the Agent actor's cost counters: every message type
# ``_AgentGeneration`` dispatches, split by origin.  ``/session`` is work a
# SessionActor effect asked for (heartbeat/register binds and releases),
# ``/other`` is everything else; the split is what lets the calibration
# contract count the session-originated share into the IPC path.
_AGENT_COST_TYPES = (
    "LifecycleMutationRequest",
    "CreateAgentCommand",
    "CreateHostInvitedAgentCommand",
    "CreateTransferHostedAgentCommand",
    "UpdateAgentCommand",
    "DestroyAgentCommand",
    "SettleAgentDestroyCommand",
    "CleanupRevokedAgentHomeCommand",
    "BindAgentCommand",
    "ReleaseAgentCommand",
    "RecordAgentActivityCommand",
    "SetRestoreDispositionCommand",
    "ClearRestoreDispositionCommand",
    "BlockAgentCommand",
    "UnblockAgentCommand",
    "PinAgentAdapterCommand",
    "UnpinAgentAdapterCommand",
    "AcquireAgentRuntimeLaunchCommand",
    "ReleaseAgentRuntimeLaunchCommand",
    "ReserveAgentDestroyCommand",
    "ReleaseAgentDestroyReservationCommand",
    "RetireAgentLifecycleReceiptCommand",
    "ConfirmAgentLifecycleReceiptCommand",
    "GrantAgentCapabilityCommand",
    "RevokeAgentCapabilityCommand",
    "GrantAgentSecretCommand",
    "RevokeAgentSecretCommand",
)
AGENT_COST_ORIGIN_SESSION = "session"
AGENT_COST_ORIGIN_OTHER = "other"
_AGENT_COST_KEYS = frozenset(
    f"{name}/{origin}"
    for name in _AGENT_COST_TYPES
    for origin in (AGENT_COST_ORIGIN_SESSION, AGENT_COST_ORIGIN_OTHER)
)


class _Version:
    def __init__(self) -> None:
        self._value = 0
        self._lock = threading.Lock()

    def read(self) -> int:
        with self._lock:
            return self._value

    def bump(self) -> int:
        with self._lock:
            self._value += 1
            return self._value


class _AgentProjectionState:
    """Atomically swapped immutable read face for the Agent authority."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._version = 0
        self._agents: tuple[AgentProjection, ...] = ()
        self._bindings: tuple[AgentBindingProjection, ...] = ()

    def replace(
        self,
        *,
        version: int,
        agents: tuple[AgentProjection, ...],
        bindings: tuple[AgentBindingProjection, ...],
    ) -> None:
        with self._lock:
            self._version = version
            self._agents = agents
            self._bindings = bindings

    def read_agent(self, name: str) -> AgentProjection | None:
        with self._lock:
            return next(
                (
                    agent
                    for agent in self._agents
                    if agent.actor == name or agent.uri == name
                ),
                None,
            )
    def read_agents(self) -> tuple[AgentProjection, ...]:
        with self._lock:
            return self._agents

    def read_binding(self, actor: str) -> AgentBindingProjection | None:
        with self._lock:
            return next(
                (binding for binding in self._bindings if binding.actor == actor),
                None,
            )


@dataclass(frozen=True, slots=True)
class _PendingHomeMutation:
    command: object
    reservation: AgentHomeReservation


@dataclass(frozen=True, slots=True)
class _PrepareWorkspaceCommand:
    correlation_id: str
    actor: str


@dataclass(frozen=True, slots=True)
class _PrepareRuntimeCommand:
    correlation_id: str
    actor: str
    harness: str
    cwd: str | None
    tool_profile: AgentToolProfile
    containerized: bool


@dataclass(frozen=True, slots=True)
class _PendingHomeAccess:
    command: _PrepareWorkspaceCommand | _PrepareRuntimeCommand
    receipt: HomeReceipt


class _HomeCall:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.value: str | AgentRuntimeContext | None = None
        self.error: BaseException | None = None


@dataclass(slots=True)
class _MigrationWaiter:
    request: MigrationRequest
    ready: threading.Event
    outcome: MigrationOutcome | None = None
    consumers: int = 0
    consumed: bool = False


@dataclass(frozen=True, slots=True)
class _RuntimeContextFence:
    actor: str
    receipt: HomeReceipt
    consumed: bool = False


@dataclass(frozen=True, slots=True)
class _RuntimeLaunchLease:
    operation_id: str
    actor: str
    receipt: HomeReceipt


class _RuntimeLaunchState:
    def __init__(
        self, *, context_capacity: int = 1024, lease_capacity: int = 64,
    ) -> None:
        self.condition = threading.Condition()
        self.context_capacity = context_capacity
        self.lease_capacity = lease_capacity
        self.contexts: OrderedDict[str, _RuntimeContextFence] = OrderedDict()
        self.leases: dict[str, _RuntimeLaunchLease] = {}
        self.destroying: dict[str, str] = {}
        self.closing = False

    def register_context(
        self, context: AgentRuntimeContext, receipt: HomeReceipt
    ) -> AgentRuntimeContext:
        if context.home_resource_token != receipt.resource_token:
            raise AgentHomeAccessError("runtime context home receipt changed")
        token = uuid4().hex
        with self.condition:
            if self.closing:
                raise AgentHomeAccessError("Agent runtime launch authority is closing")
            if len(self.contexts) >= self.context_capacity:
                evictable = next(
                    (
                        current_token
                        for current_token, fence in self.contexts.items()
                        if not fence.consumed
                    ),
                    None,
                )
                if evictable is None:
                    raise AgentHomeAccessError(
                        "Agent runtime context offer capacity is full"
                    )
                self.contexts.pop(evictable, None)
            self.contexts[token] = _RuntimeContextFence(context.actor, receipt)
            self.contexts.move_to_end(token)
        return replace(context, launch_token=token)

    def acquire_candidate(self, token: str) -> _RuntimeContextFence:
        with self.condition:
            if self.closing:
                raise AgentHomeAccessError("Agent runtime launch authority is closing")
            fence = self.contexts.get(token)
            if fence is None:
                raise AgentRuntimeContextStale("runtime context launch token is stale")
            if fence.receipt.actor in self.destroying:
                raise AgentRuntimeContextStale("agent destroy is already in progress")
            if len(self.leases) >= self.lease_capacity:
                raise AgentHomeAccessError("Agent runtime launch lease capacity is full")
            self.contexts.move_to_end(token)
            return fence

    def commit_lease(
        self,
        context_token: str,
        fence: _RuntimeContextFence,
        operation_id: str,
    ) -> tuple[str, _RuntimeLaunchLease]:
        lease = _RuntimeLaunchLease(operation_id, fence.actor, fence.receipt)
        with self.condition:
            if self.closing:
                raise AgentHomeAccessError("Agent runtime launch authority is closing")
            if len(self.leases) >= self.lease_capacity:
                raise AgentHomeAccessError("Agent runtime launch lease capacity is full")
            current = self.contexts.get(context_token)
            if current != fence:
                raise AgentRuntimeContextStale(
                    "runtime context launch token is stale"
                )
            self.contexts[context_token] = replace(current, consumed=True)
            self.contexts.move_to_end(context_token)
            token = uuid4().hex
            self.leases[token] = lease
            return token, lease

    def custody(self) -> tuple[AgentRuntimeLaunchCustodyProjection, ...]:
        with self.condition:
            return tuple(
                AgentRuntimeLaunchCustodyProjection(
                    lease.operation_id, lease.actor, token
                )
                for token, lease in self.leases.items()
            )

    def retire_context(
        self, token: str, fence: _RuntimeContextFence
    ) -> None:
        with self.condition:
            if self.contexts.get(token) == fence:
                self.contexts.pop(token, None)

    def release(
        self, token: str, operation_id: str
    ) -> _RuntimeLaunchLease | None:
        with self.condition:
            lease = self.leases.get(token)
            if lease is not None and lease.operation_id != operation_id:
                raise AgentRuntimeLaunchMismatch(
                    "runtime launch operation and lease do not match"
                )
            self.leases.pop(token, None)
            self.condition.notify_all()
            return lease

    def active_for(self, actor: str) -> bool:
        with self.condition:
            return any(lease.receipt.actor == actor for lease in self.leases.values())

    def reserve_destroy(self, actor: str) -> str:
        with self.condition:
            if self.closing:
                raise AgentHomeAccessError("Agent runtime launch authority is closing")
            if actor in self.destroying:
                raise AgentLaunchInProgress("agent destroy is already reserved")
            if any(lease.receipt.actor == actor for lease in self.leases.values()):
                raise AgentLaunchInProgress(
                    "agent has a native process launch in progress"
                )
            token = uuid4().hex
            self.destroying[actor] = token
            return token

    def release_destroy(self, token: str) -> str | None:
        with self.condition:
            actor = next(
                (actor for actor, current in self.destroying.items() if current == token),
                None,
            )
            if actor is not None:
                self.destroying.pop(actor, None)
                self.condition.notify_all()
            return actor

    def consume_destroy(self, actor: str) -> None:
        with self.condition:
            self.destroying.pop(actor, None)
            stale = [
                token
                for token, fence in self.contexts.items()
                if fence.receipt.actor == actor
            ]
            for token in stale:
                self.contexts.pop(token, None)
            self.condition.notify_all()

    def begin_close(self) -> None:
        with self.condition:
            self.closing = True
            self.contexts.clear()
            self.condition.notify_all()

    def wait_drained(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self.condition:
            while True:
                if not self.leases and not self.destroying:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(min(remaining, 0.05))


@dataclass(frozen=True, slots=True)
class _PinStateRequest:
    adapter: str


@dataclass(frozen=True, slots=True)
class _PinStateResult:
    legacy_actor: str | None


@dataclass(frozen=True, slots=True)
class _AgentStateEffectCompleted:
    correlation_id: str
    generation: int
    result: _PinStateResult | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class _PendingPinMutation:
    command: PinAgentAdapterCommand | UnpinAgentAdapterCommand
    before: str | None
    actor: str | None


@dataclass(slots=True)
class _AgentGeneration:
    generation: int
    registry: AgentRegistry
    liveness: AgentLiveness
    desired_state: _DesiredStatePort | None
    events: object
    version: _Version
    projection: _AgentProjectionState
    command_costs: CallCostCounters | None = None
    session_originated: Callable[[str], bool] | None = None
    home_effects: AgentHomeEffectLane | None = None
    pending_home: dict[
        tuple[str, int], _PendingHomeMutation | _PendingHomeAccess
    ] | None = None
    complete_home_call: Callable[
        [str, str | AgentRuntimeContext | None, BaseException | None], None
    ] | None = None
    state_effects: EffectLane[_PinStateRequest, _PinStateResult] | None = None
    pending_pins: dict[tuple[str, int], _PendingPinMutation] | None = None
    pending_pin_adapters: set[str] | None = None
    runtime_launches: _RuntimeLaunchState | None = None
    migration_begin: Callable[[MigrationRequest], None] | None = None
    migration_owner_call: Callable[[MigrationOwnerCall], None] | None = None
    migration_completed: Callable[[EffectCompleted[MigrationOutcome]], None] | None = None
    migration_conflicts: Callable[[object], bool] | None = None

    def __call__(self, command: object) -> None:
        """Dispatch one command, charging this actor thread's CPU to it.

        Runs on the Agent actor's own thread, so the thread-CPU delta is only
        Agent work: the SessionActor or IPC thread that asked for it records
        its own CPU on its own side (the ipc_stats attribution rule).  The
        origin is decided BEFORE dispatch, while the SessionActor still holds
        the attempt; its completion retires the attempt during dispatch.
        """

        costs = self.command_costs
        command_name = type(command).__name__
        if (
            costs is None
            or not costs.enabled
            or command_name not in _AGENT_COST_TYPES
        ):
            self._dispatch(command)
            return
        origin_of = self.session_originated
        origin = (
            AGENT_COST_ORIGIN_SESSION
            if origin_of is not None
            and origin_of(str(getattr(command, "correlation_id", "")))
            else AGENT_COST_ORIGIN_OTHER
        )
        failed = True
        started_cpu = time.thread_time()
        try:
            self._dispatch(command)
            failed = False
        finally:
            costs.record(
                f"{command_name}/{origin}",
                cpu_seconds=time.thread_time() - started_cpu,
                error=failed,
            )

    def _dispatch(self, command: object) -> None:
        from hyprial.daemon.lifecycle_receipts import LifecycleMutationRequest

        if isinstance(command, MigrationRequest):
            assert self.migration_begin is not None
            self.migration_begin(command)
            return
        if isinstance(command, MigrationOwnerCall):
            assert self.migration_owner_call is not None
            self.migration_owner_call(command)
            return
        if isinstance(command, EffectCompleted):
            assert self.migration_completed is not None
            self.migration_completed(command)
            return
        if isinstance(command, AgentHomeEffectCompleted):
            self._home_completed(command)
            return
        if isinstance(command, _AgentStateEffectCompleted):
            self._pin_state_completed(command)
            return
        if self.migration_conflicts is not None and self.migration_conflicts(command):
            self._reject(command, ipc_errors.AGENT_MIGRATION_BUSY, "Agent migration has an unsettled lease")
            return
        if isinstance(command, (_PrepareWorkspaceCommand, _PrepareRuntimeCommand)):
            self._schedule_home_access(command)
            return
        if isinstance(command, AcquireAgentRuntimeLaunchCommand):
            self._acquire_runtime_launch(command)
            return
        if isinstance(command, ReleaseAgentRuntimeLaunchCommand):
            self._release_runtime_launch(command)
            return
        if isinstance(command, ReserveAgentDestroyCommand):
            self._reserve_agent_destroy(command)
            return
        if isinstance(command, ReleaseAgentDestroyReservationCommand):
            self._release_agent_destroy(command)
            return
        if (
            isinstance(command, LifecycleMutationRequest)
            and self.registry.home_enabled
            and isinstance(command.payload, (CreateAgentCommand, DestroyAgentCommand))
        ):
            self._schedule_home(command)
            return
        if isinstance(command, LifecycleMutationRequest):
            self._lifecycle(command)
            return
        if not isinstance(
            command,
            (
                CreateAgentCommand,
                CreateHostInvitedAgentCommand,
                CreateTransferHostedAgentCommand,
                UpdateAgentCommand,
                DestroyAgentCommand,
                SettleAgentDestroyCommand,
                CleanupRevokedAgentHomeCommand,
                BindAgentCommand,
                ReleaseAgentCommand,
                RecordAgentActivityCommand,
                SetRestoreDispositionCommand,
                ClearRestoreDispositionCommand,
                BlockAgentCommand,
                UnblockAgentCommand,
                PinAgentAdapterCommand,
                UnpinAgentAdapterCommand,
                RetireAgentLifecycleReceiptCommand,
                ConfirmAgentLifecycleReceiptCommand,
                GrantAgentCapabilityCommand,
                RevokeAgentCapabilityCommand,
                GrantAgentSecretCommand,
                RevokeAgentSecretCommand,
            ),
        ):
            self._reject(
                command, ipc_errors.INVALID_ARGUMENT, "unsupported agent command"
            )
            return
        if self.registry.home_enabled and isinstance(
            command,
            (
                CreateAgentCommand,
                CreateHostInvitedAgentCommand,
                CreateTransferHostedAgentCommand,
                DestroyAgentCommand,
                SettleAgentDestroyCommand,
                CleanupRevokedAgentHomeCommand,
            ),
        ):
            self._schedule_home(command)
            return
        try:
            if isinstance(command, CreateAgentCommand):
                self._create(command)
            elif isinstance(command, CreateHostInvitedAgentCommand):
                agent = self.registry.create_host_invited(
                    command.name, pinned_owner=command.pinned_owner,
                    cwd=command.cwd, harness_args=dict(command.harness_args),
                    preferred_harness=command.preferred_harness,
                )
                self._completed(
                    command, operation="create", changed=True,
                    version=self.version.bump(), agent=agent,
                )
            elif isinstance(command, CreateTransferHostedAgentCommand):
                agent = self.registry.create_transfer_hosted(
                    command.name, pinned_owner=command.pinned_owner,
                    cwd=command.cwd, harness_args=dict(command.harness_args),
                    preferred_harness=command.preferred_harness,
                )
                self._completed(
                    command, operation="create", changed=True,
                    version=self.version.bump(), agent=agent,
                )
            elif isinstance(command, UpdateAgentCommand):
                self._update(command)
            elif isinstance(command, DestroyAgentCommand):
                self._destroy(command)
            elif isinstance(command, SettleAgentDestroyCommand):
                disposition, reservation = self.registry.prepare_destroy_settlement(
                    command.name, command.expected_entity_token
                )
                if reservation is not None:
                    raise RuntimeError(
                        "home-disabled Agent destroy returned a cleanup reservation"
                    )
                assert disposition is not None
                self._destroy_settled(command, disposition)
            elif isinstance(command, CleanupRevokedAgentHomeCommand):
                cleaned = self.registry.cleanup_revoked_home(command.name)
                self._completed(
                    command,
                    operation="cleanup-home",
                    changed=cleaned is not None,
                    version=self.version.read(),
                )
            elif isinstance(command, BindAgentCommand):
                self._bind(command)
            elif isinstance(command, ReleaseAgentCommand):
                self._release(command)
            elif isinstance(command, RecordAgentActivityCommand):
                self._record_activity(command)
            elif isinstance(command, SetRestoreDispositionCommand):
                self.registry.suppress_restore(
                    command.actor,
                    desired_generation=command.desired_generation,
                    last_active_at_ms=command.last_active_at_ms,
                    idle_age_ms=command.idle_age_ms,
                    restore_threshold_ms=command.restore_threshold_ms,
                    restore_override=command.restore_override,
                    activity_unknown=command.activity_unknown,
                )
                self._completed(
                    command,
                    operation="suppress-restore",
                    changed=True,
                    version=self.version.bump(),
                    agent=self.registry.get(command.actor),
                )
            elif isinstance(command, ClearRestoreDispositionCommand):
                changed = self.registry.clear_restore_disposition(
                    command.actor,
                    expected_entity_token=command.expected_entity_token,
                    expected_desired_generation=(
                        command.expected_desired_generation
                    ),
                    expected_disposition_token=(
                        command.expected_disposition_token
                    ),
                )
                self._completed(
                    command,
                    operation="clear-restore",
                    changed=changed,
                    version=self.version.bump() if changed else self.version.read(),
                    agent=self.registry.get(command.actor),
                )
            elif isinstance(command, BlockAgentCommand):
                _block, changed = self.registry.block_agent(
                    command.actor,
                    reason=command.reason,
                    expected_entity_token=command.expected_entity_token,
                )
                self._completed(
                    command,
                    operation="block",
                    changed=changed,
                    version=self.version.bump() if changed else self.version.read(),
                    agent=self.registry.get(command.actor),
                )
            elif isinstance(command, UnblockAgentCommand):
                changed = self.registry.unblock_agent(command.actor)
                self._completed(
                    command,
                    operation="unblock",
                    changed=changed,
                    version=self.version.bump() if changed else self.version.read(),
                    agent=self.registry.get(command.actor),
                )
            elif isinstance(command, PinAgentAdapterCommand):
                self._pin(command)
            elif isinstance(command, RetireAgentLifecycleReceiptCommand):
                matched = self.registry.retire_lifecycle_receipt(
                    command.attempt_token, command.resource_token
                )
                self._publish(
                    AgentLifecycleReceiptCompleted(
                        command.correlation_id,
                        self.generation,
                        self.version.read(),
                        command.attempt_token,
                        command.resource_token,
                        "retire",
                        matched,
                    )
                )
            elif isinstance(command, ConfirmAgentLifecycleReceiptCommand):
                matched = self.registry.confirm_lifecycle_receipt_retired(
                    command.attempt_token, command.resource_token
                )
                self._publish(
                    AgentLifecycleReceiptCompleted(
                        command.correlation_id,
                        self.generation,
                        self.version.read(),
                        command.attempt_token,
                        command.resource_token,
                        "confirm",
                        matched,
                    )
                )
            elif isinstance(command, GrantAgentCapabilityCommand):
                grant = self.registry.grant_capability(
                    command.actor, grant_id=command.grant_id,
                    capability=command.capability, scope=command.scope,
                    granted_by=command.granted_by, revision=command.revision,
                )
                self._publish(AgentCapabilityGrantCompleted(
                    command.correlation_id, self.generation, self.version.bump(),
                    "grant", True, grant,
                ))
            elif isinstance(command, RevokeAgentCapabilityCommand):
                changed = self.registry.revoke_capability(
                    command.actor, command.grant_id,
                    revoked_by=command.revoked_by,
                )
                version = self.version.bump() if changed else self.version.read()
                self._publish(AgentCapabilityGrantCompleted(
                    command.correlation_id, self.generation, version,
                    "revoke", changed,
                ))
            elif isinstance(command, GrantAgentSecretCommand):
                grant = self.registry.grant_secret(
                    command.actor, grant_id=command.grant_id,
                    source=command.source, entry_id=command.entry_id,
                    field_name=command.field_name,
                    environment_names=command.environment_names,
                    revision=command.revision,
                    prevalidated_home_token=command.prevalidated_home_token,
                )
                self._publish(AgentSecretGrantCompleted(
                    command.correlation_id, self.generation, self.version.bump(),
                    "grant", True, grant,
                ))
            elif isinstance(command, RevokeAgentSecretCommand):
                changed = self.registry.revoke_secret_grant(
                    command.actor, command.grant_id,
                )
                version = self.version.bump() if changed else self.version.read()
                self._publish(AgentSecretGrantCompleted(
                    command.correlation_id, self.generation, version,
                    "revoke", changed,
                ))
            else:
                self._unpin(command)
        except (AgentError, AgentHomeError, AgentAlreadyRunning) as error:
            self._reject(
                command, str(getattr(error, "code", "AGENT_ERROR")), str(error)
            )
        except ValueError as error:
            self._reject(command, ipc_errors.INVALID_ARGUMENT, str(error))

    def _schedule_home(self, command: object) -> None:
        lane = self.home_effects
        pending_home = self.pending_home
        assert lane is not None and pending_home is not None
        from hyprial.daemon.lifecycle_receipts import LifecycleMutationRequest

        payload = command.payload if isinstance(command, LifecycleMutationRequest) else command
        if isinstance(
            payload,
            (
                DestroyAgentCommand,
                SettleAgentDestroyCommand,
                CleanupRevokedAgentHomeCommand,
            ),
        ):
            launches = self.runtime_launches
            assert launches is not None
            actor = self.registry.normalize_actor(payload.name)
            if launches.active_for(actor):
                self._reject(
                    command,
                    "AGENT_LAUNCH_IN_PROGRESS",
                    f"agent {actor!r} has a native process launch in progress",
                )
                return
            if isinstance(payload, DestroyAgentCommand):
                current = self.registry.require(payload.name)
                if (
                    payload.expected_entity_token is not None
                    and payload.expected_entity_token != current.entity_token
                ):
                    self._reject(
                        command,
                        str(
                            getattr(
                                AgentUpdateConflict,
                                "code",
                                "AGENT_UPDATE_CONFLICT",
                            )
                        ),
                        f"agent {payload.name!r} changed since destroy was requested",
                    )
                    return
                launches.consume_destroy(actor)
            elif isinstance(payload, SettleAgentDestroyCommand):
                current = self.registry.get(payload.name)
                if (
                    current is not None
                    and current.entity_token == payload.expected_entity_token
                ):
                    launches.consume_destroy(actor)
        correlation_id = str(getattr(command, "correlation_id", ""))
        if not correlation_id:
            self._reject(command, ipc_errors.INVALID_ARGUMENT, "correlation_id must not be empty")
            return
        if isinstance(command, CreateAgentCommand) and command.launch_harness is not None:
            name = self.registry.native_actor(command.name)
            existing = self.registry.get(name)
            actor = self.registry.uri_for(name) if existing is None else existing.uri
            incumbent = self.liveness.live_binding(actor)
            if incumbent is not None:
                self._reject(
                    command,
                    str(getattr(AgentAlreadyRunning, "code", "AGENT_ALREADY_RUNNING")),
                    str(AgentAlreadyRunning(actor, incumbent, command.launch_harness)),
                )
                return
        admission = lane.reserve(correlation_id, self.generation)
        if admission is not AdmissionResult.ACCEPTED:
            self._reject(
                command,
                "PORT_OVERLOADED" if admission is AdmissionResult.OVERLOADED else "PORT_CLOSING",
                f"agent home effect admission is {admission.value}",
            )
            return
        try:
            reservation: AgentHomeReservation | None
            if isinstance(command, LifecycleMutationRequest):
                reservation, settled, operation, _replayed = (
                    self.registry.prepare_home_lifecycle(command)
                )
                if reservation is None:
                    assert settled is not None
                    lane.cancel_reservation(correlation_id, self.generation)
                    self._finish_lifecycle(
                        command, settled, operation, prior_agent=None
                    )
                    return
            elif isinstance(
                command,
                (
                    CreateAgentCommand,
                    CreateHostInvitedAgentCommand,
                    CreateTransferHostedAgentCommand,
                ),
            ):
                reservation = self.registry.prepare_create_command(command)
            elif isinstance(command, DestroyAgentCommand):
                changed, reservation = self.registry.prepare_destroy_record(
                    command.name, command.expected_entity_token
                )
                if reservation is None:
                    lane.cancel_reservation(correlation_id, self.generation)
                    version = self.version.bump() if changed else self.version.read()
                    self._completed(
                        command,
                        operation="destroy",
                        changed=changed,
                        version=version,
                    )
                    return
            elif isinstance(command, SettleAgentDestroyCommand):
                disposition, reservation = self.registry.prepare_destroy_settlement(
                    command.name, command.expected_entity_token
                )
                if reservation is None:
                    lane.cancel_reservation(correlation_id, self.generation)
                    assert disposition is not None
                    self._destroy_settled(command, disposition)
                    return
            elif isinstance(command, CleanupRevokedAgentHomeCommand):
                reservation = self.registry.prepare_cleanup_revoked_home(
                    command.name
                )
                if reservation is None:
                    lane.cancel_reservation(correlation_id, self.generation)
                    self._completed(
                        command,
                        operation="cleanup-home",
                        changed=False,
                        version=self.version.read(),
                    )
                    return
            else:
                raise TypeError("unsupported Agent home command")
            if reservation.plan is None:
                assert isinstance(command, LifecycleMutationRequest)
                provenance = self.registry.complete_home_lifecycle(
                    reservation, None
                )
                lane.cancel_reservation(correlation_id, self.generation)
                self._finish_lifecycle(
                    command,
                    provenance,
                    reservation.operation,
                    prior_agent=reservation.agent,
                )
                return
            pending_home[(correlation_id, self.generation)] = _PendingHomeMutation(
                command, reservation
            )
            admitted = lane.submit_reserved(
                AgentHomeEffect(correlation_id, self.generation, reservation.plan)
            )
            if admitted is not AdmissionResult.ACCEPTED:
                pending_home.pop((correlation_id, self.generation), None)
                raise RuntimeError("reserved Agent home effect was not admitted")
        except (AgentError, AgentHomeError) as error:
            lane.cancel_reservation(correlation_id, self.generation)
            self._reject(
                command, str(getattr(error, "code", "AGENT_ERROR")), str(error)
            )
        except (TypeError, ValueError) as error:
            lane.cancel_reservation(correlation_id, self.generation)
            self._reject(command, ipc_errors.INVALID_ARGUMENT, str(error))
        except sqlite3.Error as error:
            lane.cancel_reservation(correlation_id, self.generation)
            self._reject(
                command, ipc_errors.DAEMON_ERROR,
                f"Agent home reservation remains unsettled: {type(error).__name__}: {error}",
            )

    def _schedule_home_access(
        self, command: _PrepareWorkspaceCommand | _PrepareRuntimeCommand
    ) -> None:
        lane = self.home_effects
        pending_home = self.pending_home
        complete = self.complete_home_call
        assert lane is not None and pending_home is not None and complete is not None
        admission = lane.reserve(command.correlation_id, self.generation)
        if admission is not AdmissionResult.ACCEPTED:
            complete(
                command.correlation_id,
                None,
                AgentHomeAccessError(
                    f"agent home effect admission is {admission.value}"
                ),
            )
            return
        try:
            if isinstance(command, _PrepareWorkspaceCommand):
                receipt = self.registry.home_receipt(
                    command.actor, validate_mirror=False
                )
                plan = PrepareWorkspaceHome(receipt)
            else:
                preparation = build_agent_runtime_preparation(
                    registry=self.registry,
                    agent_name=command.actor,
                    harness=command.harness,
                    cwd=command.cwd,
                    tool_profile=command.tool_profile,
                    containerized=command.containerized,
                    validate_home=False,
                )
                if preparation is None:
                    lane.cancel_reservation(command.correlation_id, self.generation)
                    complete(command.correlation_id, None, None)
                    return
                receipt = preparation.home_receipt
                plan = PrepareRuntimeHome(preparation)
            pending_home[(command.correlation_id, self.generation)] = (
                _PendingHomeAccess(command, receipt)
            )
            admitted = lane.submit_reserved(
                AgentHomeEffect(command.correlation_id, self.generation, plan)
            )
            if admitted is not AdmissionResult.ACCEPTED:
                pending_home.pop((command.correlation_id, self.generation), None)
                raise RuntimeError("reserved Agent home access was not admitted")
        except BaseException as error:
            lane.cancel_reservation(command.correlation_id, self.generation)
            complete(command.correlation_id, None, error)

    def _home_completed(self, event: AgentHomeEffectCompleted) -> None:
        lane = self.home_effects
        pending_home = self.pending_home
        assert lane is not None and pending_home is not None
        if not lane.owns(event.correlation_id, event.generation):
            return
        pending = pending_home.pop(
            (event.correlation_id, event.generation), None
        )
        try:
            if isinstance(pending, _PendingHomeAccess):
                complete = self.complete_home_call
                assert complete is not None
                if event.generation != self.generation:
                    complete(
                        event.correlation_id,
                        None,
                        AgentHomeAccessError(
                            "agent home effect completed after its actor generation retired"
                        ),
                    )
                    return
                if event.error_code is not None:
                    complete(
                        event.correlation_id,
                        None,
                        AgentHomeAccessError(
                            event.error_detail or event.error_code
                        ),
                    )
                    return
                if event.receipt != pending.receipt:
                    raise AgentHomeError(
                        "receipt-mismatch", pending.receipt.actor,
                        "home-access-completion",
                    )
                self.registry.confirm_home_authority(pending.receipt)
                value: str | AgentRuntimeContext | None = (
                    event.workspace
                    if isinstance(pending.command, _PrepareWorkspaceCommand)
                    else event.runtime_context
                )
                if value is None:
                    raise AgentHomeAccessError(
                        "agent home effect returned no prepared value"
                    )
                if isinstance(value, AgentRuntimeContext):
                    launches = self.runtime_launches
                    assert launches is not None
                    value = launches.register_context(value, pending.receipt)
                complete(event.correlation_id, value, None)
                return
            if event.generation != self.generation or pending is None:
                return
            command = pending.command
            reservation = pending.reservation
            if not event.settled:
                # Preserve the existing process-death contract: an unexpected
                # filesystem worker failure leaves the durable revoke/claim
                # for replay and does not mint a false terminal response.
                return
            if event.error_code is not None:
                if reservation.operation == "create" and reservation.lifecycle_attempt is None:
                    self.registry.abort_create_record(reservation)
                self._reject(command, event.error_code, event.error_detail or event.error_code)
                return
            from hyprial.daemon.lifecycle_receipts import LifecycleMutationRequest
            if isinstance(command, LifecycleMutationRequest):
                provenance = self.registry.complete_home_lifecycle(
                    reservation, event.receipt
                )
                self._finish_lifecycle(
                    command,
                    provenance,
                    reservation.operation,
                    prior_agent=reservation.agent,
                )
                return
            if event.receipt is None:
                raise AgentHomeError(
                    "receipt-mismatch", str(getattr(command, "name", "")),
                    "effect-completion",
                )
            agent = reservation.agent
            if reservation.operation == "create":
                try:
                    agent = (
                        self.registry.commit_create_record(reservation, event.receipt)
                        if reservation.changed
                        else self.registry.commit_existing_home(
                            reservation, event.receipt
                        )
                    )
                except Exception as error:
                    if not isinstance(command, CreateHostInvitedAgentCommand):
                        raise
                    # An invited home exists after the filesystem effect even
                    # when its journal/Agent row transaction rolls back. Keep
                    # the exact create claim and receipt until the facade
                    # joins typed, entity-fenced destroy settlement.
                    try:
                        self.registry.revoke_failed_create_record(
                            reservation, event.receipt
                        )
                    except Exception as cleanup_error:
                        self._reject(
                            command, ipc_errors.AGENT_CREATE_ROLLBACK_PENDING,
                            f"create commit failed ({type(error).__name__}: {error}); "
                            f"durable cleanup claim remains ({type(cleanup_error).__name__}: "
                            f"{cleanup_error})",
                        )
                        return
                    self._reject(
                        command, ipc_errors.AGENT_CREATE_COMMIT_FAILED,
                        f"{type(error).__name__}: {error}",
                    )
                    return
            else:
                self.registry.commit_cleanup_home(reservation, event.receipt)
            if (
                isinstance(command, (DestroyAgentCommand, SettleAgentDestroyCommand))
                and reservation.changed
                and agent is not None
            ):
                self.liveness.release(agent.uri)
            version = (
                self.version.bump()
                if reservation.changed and reservation.operation != "cleanup-home"
                else self.version.read()
            )
            if isinstance(command, SettleAgentDestroyCommand):
                self._destroy_settled(command, "destroyed", version=version)
            else:
                self._completed(
                    command,
                    operation=reservation.operation,
                    changed=reservation.changed,
                    version=version,
                    agent=agent if reservation.operation == "create" else None,
                )
        except (AgentError, AgentHomeError) as error:
            command = None if pending is None else pending.command
            if isinstance(pending, _PendingHomeAccess):
                assert self.complete_home_call is not None
                self.complete_home_call(event.correlation_id, None, error)
            else:
                self._reject(
                    command, str(getattr(error, "code", "AGENT_ERROR")), str(error)
                )
        except (TypeError, ValueError) as error:
            command = None if pending is None else pending.command
            if isinstance(pending, _PendingHomeAccess):
                assert self.complete_home_call is not None
                self.complete_home_call(event.correlation_id, None, error)
            else:
                self._reject(command, ipc_errors.INVALID_ARGUMENT, str(error))
        except sqlite3.Error as error:
            command = None if pending is None else pending.command
            if isinstance(pending, _PendingHomeAccess):
                assert self.complete_home_call is not None
                self.complete_home_call(event.correlation_id, None, error)
            else:
                self._reject(
                    command, ipc_errors.DAEMON_ERROR,
                    f"Agent home settlement remains unsettled: {type(error).__name__}: {error}",
                )
        finally:
            lane.acknowledge(event.correlation_id, event.generation)

    def _acquire_runtime_launch(
        self, command: AcquireAgentRuntimeLaunchCommand
    ) -> None:
        launches = self.runtime_launches
        assert launches is not None
        fence = None
        try:
            fence = launches.acquire_candidate(command.launch_token)
            self.registry.confirm_home_authority(fence.receipt)
        except (AgentError, AgentHomeError) as error:
            if fence is not None:
                launches.retire_context(command.launch_token, fence)
            self._reject(
                command, str(getattr(error, "code", "AGENT_RUNTIME_CONTEXT_STALE")),
                str(error),
            )
            return
        try:
            lease_token, lease = launches.commit_lease(
                command.launch_token, fence, command.correlation_id
            )
        except (AgentError, AgentHomeError) as error:
            self._reject(
                command, str(getattr(error, "code", "AGENT_RUNTIME_CONTEXT_STALE")),
                str(error),
            )
            return
        self._publish(AgentRuntimeLaunchLeaseCompleted(
            command.correlation_id,
            self.generation,
            self.version.read(),
            fence.actor,
            lease_token,
            True,
            None,
        ))

    def _release_runtime_launch(
        self, command: ReleaseAgentRuntimeLaunchCommand
    ) -> None:
        launches = self.runtime_launches
        assert launches is not None
        try:
            lease = launches.release(command.lease_token, command.operation_id)
        except AgentRuntimeLaunchMismatch as error:
            self._reject(command, error.code, str(error))
            return
        self._publish(AgentRuntimeLaunchLeaseCompleted(
            command.correlation_id,
            self.generation,
            self.version.read(),
            "" if lease is None else lease.actor,
            command.lease_token,
            False,
            None,
        ))

    def _reserve_agent_destroy(self, command: ReserveAgentDestroyCommand) -> None:
        launches = self.runtime_launches
        assert launches is not None
        try:
            actor = self.registry.normalize_actor(command.actor)
            self.registry.require(actor)
            token = launches.reserve_destroy(actor)
        except (AgentError, AgentHomeError) as error:
            self._reject(
                command,
                str(getattr(error, "code", "AGENT_LAUNCH_IN_PROGRESS")),
                str(error),
            )
            return
        self._publish(AgentDestroyReservationCompleted(
            command.correlation_id,
            self.generation,
            self.version.read(),
            actor,
            token,
            True,
        ))

    def _release_agent_destroy(
        self, command: ReleaseAgentDestroyReservationCommand
    ) -> None:
        launches = self.runtime_launches
        assert launches is not None
        actor = launches.release_destroy(command.reservation_token)
        self._publish(AgentDestroyReservationCompleted(
            command.correlation_id,
            self.generation,
            self.version.read(),
            "" if actor is None else actor,
            command.reservation_token,
            False,
        ))

    def _lifecycle(self, request: object) -> None:
        from hyprial.daemon.lifecycle_receipts import (
            LifecycleMutationRequest,
        )

        assert isinstance(request, LifecycleMutationRequest)
        payload = request.payload
        try:
            prior_agent = (
                self.registry.get(payload.name)
                if isinstance(payload, DestroyAgentCommand)
                else None
            )
            if isinstance(payload, BindAgentCommand):
                agent = self.registry.require(payload.actor)
                incumbent = self.liveness.live_binding(agent.uri)
                exact = bool(
                    incumbent is not None
                    and incumbent.harness == payload.harness
                    and incumbent.runtime == payload.runtime
                    and incumbent.session_id == payload.session_id
                )
                interactive_handoff = bool(
                    incumbent is not None
                    and incumbent.runtime == RUNTIME_INTERACTIVE
                    and payload.runtime == RUNTIME_INTERACTIVE
                )
                if incumbent is not None and not exact and not interactive_handoff:
                    raise AgentAlreadyRunning(agent.uri, incumbent, payload.harness)
            provenance, operation, _replayed = self.registry.apply_lifecycle(request)
            self._finish_lifecycle(
                request, provenance, operation, prior_agent=prior_agent
            )
        except (AgentError, AgentHomeError, AgentAlreadyRunning) as error:
            self._reject(
                request, str(getattr(error, "code", "AGENT_ERROR")), str(error)
            )
        except (TypeError, ValueError) as error:
            self._reject(request, ipc_errors.INVALID_ARGUMENT, str(error))

    def _finish_lifecycle(
        self,
        request: object,
        provenance: object,
        operation: str,
        *,
        prior_agent: Agent | None,
    ) -> None:
        from hyprial.daemon.lifecycle_receipts import (
            LifecycleMutationCompleted,
            LifecycleMutationRequest,
            MutationProvenance,
        )

        assert isinstance(request, LifecycleMutationRequest)
        assert isinstance(provenance, MutationProvenance)
        payload = request.payload
        agent = None
        binding = None
        actor = getattr(payload, "actor", getattr(payload, "name", ""))
        if operation == "destroy":
            if provenance.changed:
                self.liveness.release(
                    str(actor) if prior_agent is None else prior_agent.uri
                )
        elif operation == "bind":
            durable = self.registry.lifecycle_binding(str(actor))
            if durable is not None:
                agent = self.registry.require(str(actor))
                binding = self.liveness.bind(
                    agent.uri,
                    harness=str(durable["harness"]),
                    runtime=str(durable["runtime"]),
                    session_id=(
                        None
                        if durable.get("sessionId") is None
                        else str(durable["sessionId"])
                    ),
                )
                self.liveness.touch(agent.uri)
        elif operation == "release":
            if provenance.changed:
                self.liveness.release(str(actor))
        else:
            agent = self.registry.get(str(actor))
        version = self.version.bump() if provenance.changed else self.version.read()
        _refresh_projection(self.projection, self.registry, self.liveness, version)
        base = AgentMutationCompleted(
            correlation_id=request.correlation_id,
            generation=self.generation,
            version=version,
            operation=operation,
            changed=provenance.changed,
            agent=(
                None
                if agent is None
                else _agent_projection(agent, version, self.registry)
            ),
            binding=None if binding is None else _binding_projection(binding),
        )
        self._publish(
            LifecycleMutationCompleted(
                request.correlation_id,
                request.attempt_token,
                self.generation,
                version,
                "agent",
                provenance,
                base,
            )
        )

    def _create(self, command: CreateAgentCommand) -> None:
        if not command.correlation_id:
            raise ValueError("correlation_id must not be empty")
        name = self.registry.native_actor(command.name)
        existing = self.registry.get(name)
        if existing is not None:
            if not command.reuse_existing:
                # Preserve the registry's typed duplicate-identity error and
                # database-backed final constraint.
                self.registry.create(name)
                raise AssertionError("duplicate create unexpectedly succeeded")
            agent = existing
            # Existing means the same registry incarnation, not merely the
            # same short name.  Provision/validate its home before any caller
            # may proceed to a launch phase.
            if self.registry.home_enabled:
                self.registry.ensure_home(agent.actor)
            changed = False
            if command.launch_harness is not None:
                incumbent = self.liveness.live_binding(agent.uri)
                if incumbent is not None:
                    raise AgentAlreadyRunning(
                        agent.uri, incumbent, command.launch_harness
                    )
        else:
            # The incumbent check precedes the INSERT.  A rejected
            # create+launch must never leave a partial agent row behind.
            candidate_uri = self.registry.uri_for(name)
            if command.launch_harness is not None:
                incumbent = self.liveness.live_binding(candidate_uri)
                if incumbent is not None:
                    raise AgentAlreadyRunning(
                        candidate_uri, incumbent, command.launch_harness
                    )
            # Do not pre-claim or emulate uniqueness in actor memory.  Even
            # with serialized delivery another process may race this daemon;
            # the PRIMARY KEY remains the final constraint and its typed
            # translation remains AgentRegistry's responsibility.
            agent = self.registry.create(
                name,
                cwd=command.cwd,
                config=command.config_payload(),
                provider=command.provider,
                model=command.model,
                capabilities=command.capabilities_payload(),
                harness_args=dict(command.harness_args),
                preferred_harness=(command.preferred_harness or command.launch_harness),
            )
            changed = True
        version = self.version.bump() if changed else self.version.read()
        self._completed(
            command,
            operation="create",
            changed=changed,
            version=version,
            agent=agent,
        )

    def _destroy(self, command: DestroyAgentCommand) -> None:
        agent = self.registry.require(command.name)
        if (
            command.expected_entity_token is not None
            and command.expected_entity_token != agent.entity_token
        ):
            raise AgentUpdateConflict(
                f"agent {command.name!r} changed since destroy was requested"
            )
        self.liveness.release(agent.uri)
        changed = self.registry.destroy(agent.actor)
        version = self.version.bump() if changed else self.version.read()
        self._completed(
            command,
            operation="destroy",
            changed=changed,
            version=version,
        )

    def _update(self, command: UpdateAgentCommand) -> None:
        existing = self.registry.require(command.name)
        if (
            command.expected_version is not None
            and command.expected_version != self.version.read()
        ) or (
            command.expected_entity_token is not None
            and command.expected_entity_token != existing.entity_token
        ):
            raise AgentUpdateConflict(
                f"agent {command.name!r} changed since the caller projection"
            )
        agent = self.registry.save(
            Agent(
                uri=existing.uri,
                actor=existing.actor,
                owner=existing.owner,
                machine=existing.machine,
                entity_token=existing.entity_token,
                cwd=command.cwd,
                config=command.config_payload(),
                provider=command.provider,
                model=command.model,
                capabilities=command.capabilities_payload(),
                harness_args=dict(command.harness_args),
                preferred_harness=command.preferred_harness,
                last_harness=command.last_harness,
                last_session_id=command.last_session_id,
                last_active_at_ms=existing.last_active_at_ms,
                pinned_adapters=existing.pinned_adapters,
                created_at_ms=existing.created_at_ms,
                hosted_by=existing.hosted_by,
            )
        )
        changed = agent != existing
        version = self.version.bump() if changed else self.version.read()
        self._completed(
            command,
            operation="update",
            changed=changed,
            version=version,
            agent=agent,
        )

    def _bind(self, command: BindAgentCommand) -> None:
        agent = self.registry.require(command.actor)
        incumbent = self.liveness.live_binding(agent.uri)
        exact = bool(
            incumbent is not None
            and incumbent.harness == command.harness
            and incumbent.runtime == command.runtime
            and incumbent.session_id == command.session_id
        )
        interactive_handoff = bool(
            incumbent is not None
            and incumbent.runtime == RUNTIME_INTERACTIVE
            and command.runtime == RUNTIME_INTERACTIVE
        )
        if incumbent is not None and not exact and not interactive_handoff:
            raise AgentAlreadyRunning(agent.uri, incumbent, command.harness)
        if exact:
            binding = incumbent
            self.liveness.touch(agent.uri)
            changed = False
            # The binding did not move. Activity below may still refresh the
            # projection once per throttle window; ordinary heartbeats do not.
            refresh_projection = False
        else:
            self.registry.record_session(
                agent.actor,
                harness=command.harness,
                session_id=command.session_id,
            )
            binding = self.liveness.bind(
                agent.uri,
                harness=command.harness,
                runtime=command.runtime,
                session_id=command.session_id,
            )
            # BindAgentCommand is issued only after the connector's current
            # daemon contact (session register or supervised start).
            self.liveness.touch(agent.uri)
            changed = True
            refresh_projection = True
            agent = self.registry.require(agent.actor)
        activity_changed = self.registry.record_activity(agent.actor)
        if activity_changed:
            agent = self.registry.require(agent.actor)
            refresh_projection = True
        if changed:
            self.registry.record_external_binding(
                agent.uri,
                harness=command.harness,
                runtime=command.runtime,
                session_id=command.session_id,
            )
        version = (
            self.version.bump() if changed or activity_changed else self.version.read()
        )
        self._completed(
            command,
            operation="bind",
            changed=changed,
            version=version,
            agent=agent,
            binding=binding,
            refresh_projection=refresh_projection,
        )

    def _record_activity(self, command: RecordAgentActivityCommand) -> None:
        changed = self.registry.record_activity(command.actor)
        agent = self.registry.get(command.actor)
        version = self.version.bump() if changed else self.version.read()
        self._completed(
            command,
            operation="activity",
            changed=changed,
            version=version,
            agent=agent,
            refresh_projection=changed,
        )

    def _release(self, command: ReleaseAgentCommand) -> None:
        agent = self.registry.get(command.actor)
        actor = command.actor if agent is None else agent.uri
        binding = self.liveness.release(actor)
        changed = binding is not None
        if changed and agent is not None:
            self.registry.record_external_binding(
                agent.uri, harness=None, runtime=None, session_id=None
            )
        version = self.version.bump() if changed else self.version.read()
        self._completed(
            command,
            operation="release",
            changed=changed,
            version=version,
            agent=agent,
            binding=None,
        )

    def _pin(self, command: PinAgentAdapterCommand) -> None:
        if not command.adapter:
            raise ValueError("adapter must not be empty")
        agent = self.registry.require(command.actor)
        before = self.registry.pins().get(command.adapter)
        if (
            command.compare
            and before != agent.uri
            and before != command.expected_actor
        ):
            raise ValueError(
                f"adapter {command.adapter!r} pin replacement blocks rollback"
            )
        if not self._reserve_pin_state(command):
            return
        try:
            self.registry.pin(command.adapter, agent.actor)
            self._submit_pin_state(command, before=before, actor=agent.actor)
        except BaseException:
            self._cancel_pin_state(command)
            raise

    def _unpin(self, command: UnpinAgentAdapterCommand) -> None:
        if not command.adapter:
            raise ValueError("adapter must not be empty")
        before = self.registry.pins().get(command.adapter)
        if command.compare and before != command.expected_actor:
            raise ValueError(
                f"adapter {command.adapter!r} pin changed during management"
            )
        actor = None
        if before is not None:
            prior = self.registry.get(before)
            actor = None if prior is None else prior.actor
        if not self._reserve_pin_state(command):
            return
        try:
            self.registry.unpin(command.adapter)
            self._submit_pin_state(command, before=before, actor=actor)
        except BaseException:
            self._cancel_pin_state(command)
            raise

    def _reserve_pin_state(
        self, command: PinAgentAdapterCommand | UnpinAgentAdapterCommand
    ) -> bool:
        lane = self.state_effects
        pending_adapters = self.pending_pin_adapters
        if self.desired_state is None or lane is None or pending_adapters is None:
            return True
        if command.adapter in pending_adapters:
            self._reject(
                command, "AGENT_PIN_PENDING",
                f"adapter {command.adapter!r} already has a pending state mutation",
            )
            return False
        admission = lane.reserve(command.correlation_id, self.generation)
        if admission is not AdmissionResult.ACCEPTED:
            self._reject(
                command,
                "PORT_OVERLOADED" if admission is AdmissionResult.OVERLOADED else "PORT_CLOSING",
                f"agent state effect admission is {admission.value}",
            )
            return False
        pending_adapters.add(command.adapter)
        return True

    def _cancel_pin_state(
        self, command: PinAgentAdapterCommand | UnpinAgentAdapterCommand
    ) -> None:
        if self.state_effects is not None:
            self.state_effects.cancel_reservation(
                command.correlation_id, self.generation
            )
        if self.pending_pin_adapters is not None:
            self.pending_pin_adapters.discard(command.adapter)

    def _submit_pin_state(
        self,
        command: PinAgentAdapterCommand | UnpinAgentAdapterCommand,
        *,
        before: str | None,
        actor: str | None,
    ) -> None:
        lane = self.state_effects
        pending = self.pending_pins
        if self.desired_state is None or lane is None or pending is None:
            self._finish_pin_state(command, before=before, actor=actor, legacy=None)
            return
        pending[(command.correlation_id, self.generation)] = _PendingPinMutation(
            command, before, actor
        )
        admission = lane.submit_reserved(EffectRequest(
            command.correlation_id,
            self.generation,
            _PinStateRequest(command.adapter),
        ))
        if admission is not AdmissionResult.ACCEPTED:
            pending.pop((command.correlation_id, self.generation), None)
            raise RuntimeError("reserved Agent state effect was not admitted")

    def _pin_state_completed(self, event: _AgentStateEffectCompleted) -> None:
        lane = self.state_effects
        pending = self.pending_pins
        adapters = self.pending_pin_adapters
        assert lane is not None and pending is not None and adapters is not None
        item = pending.pop((event.correlation_id, event.generation), None)
        try:
            if item is None:
                return
            adapters.discard(item.command.adapter)
            if event.generation != self.generation:
                self._reject(
                    item.command, "AGENT_STATE_GENERATION_CHANGED",
                    "Agent state effect completed after its actor generation retired",
                )
                return
            if event.error_code is not None or event.result is None:
                self._reject(
                    item.command,
                    event.error_code or "AGENT_STATE_EFFECT_FAILED",
                    "Agent state mutation did not settle",
                )
                return
            self._finish_pin_state(
                item.command,
                before=item.before,
                actor=item.actor,
                legacy=event.result.legacy_actor,
            )
        finally:
            if item is not None:
                adapters.discard(item.command.adapter)
            lane.acknowledge(event.correlation_id, event.generation)

    def _finish_pin_state(
        self,
        command: PinAgentAdapterCommand | UnpinAgentAdapterCommand,
        *,
        before: str | None,
        actor: str | None,
        legacy: str | None,
    ) -> None:
        if isinstance(command, PinAgentAdapterCommand):
            assert actor is not None
            refreshed = self.registry.require(actor)
            changed = before != refreshed.uri
            result_agent = refreshed
            operation = "pin"
        else:
            changed = before is not None or legacy is not None
            result_agent = None if actor is None else self.registry.get(actor)
            operation = "unpin"
        version = self.version.bump() if changed else self.version.read()
        self._completed(
            command,
            operation=operation,
            changed=changed,
            version=version,
            agent=result_agent,
        )

    def _completed(
        self,
        command: AgentCommand,
        *,
        operation: str,
        changed: bool,
        version: int,
        agent: Agent | None = None,
        binding: AgentBinding | None = None,
        refresh_projection: bool = True,
    ) -> None:
        if refresh_projection:
            _refresh_projection(
                self.projection,
                self.registry,
                self.liveness,
                version,
            )
        self._publish(
            AgentMutationCompleted(
                correlation_id=command.correlation_id,
                generation=self.generation,
                version=version,
                operation=operation,
                changed=changed,
                agent=(
                    None
                    if agent is None
                    else _agent_projection(agent, version, self.registry)
                ),
                binding=(None if binding is None else _binding_projection(binding)),
            )
        )

    def _destroy_settled(
        self,
        command: SettleAgentDestroyCommand,
        disposition: AgentDestroyDisposition,
        *,
        version: int | None = None,
    ) -> None:
        if disposition not in {
            "destroyed", "already-cleaned", "stale-incarnation"
        }:
            raise ValueError(f"invalid destroy settlement: {disposition}")
        resolved_version = (
            self.version.bump()
            if version is None and disposition == "destroyed"
            else self.version.read()
            if version is None
            else version
        )
        _refresh_projection(
            self.projection, self.registry, self.liveness, resolved_version
        )
        self._publish(
            AgentDestroySettled(
                command.correlation_id,
                self.generation,
                resolved_version,
                command.name,
                command.expected_entity_token,
                disposition,
            )
        )

    def _reject(self, command: object, code: str, detail: str) -> None:
        self._publish(
            PortCommandRejected(
                correlation_id=str(getattr(command, "correlation_id", "")),
                domain="agent",
                generation=self.generation,
                version=self.version.read(),
                code=code,
                detail=detail,
            )
        )

    def _publish(self, event: AgentEvent) -> None:
        _publish(self.events, event)


class AgentActor:
    """Serialized registry command sink with synchronous immutable projections."""

    def __init__(
        self,
        registry: AgentRegistry,
        *,
        event_sink: object,
        liveness: AgentLiveness | None = None,
        desired_state: _DesiredStatePort | None = None,
        worker_running: Callable[..., bool | None] | None = None,
        clock: Callable[[], float] | None = None,
        mailbox_capacity: int = 128,
        runtime: ActorRuntime | None = None,
        session_originated: Callable[[str], bool] | None = None,
    ) -> None:
        self._registry = registry
        # Owned by the actor, not a generation: totals survive restarts.
        self._command_costs = CallCostCounters(_AGENT_COST_KEYS, wall=False)
        self._session_originated = session_originated
        self._machine = registry.machine
        self._liveness = liveness or AgentLiveness(
            node_id=registry.machine,
            worker_running=worker_running,
            clock=clock if clock is not None else time.monotonic,
        )
        self._desired_state = desired_state
        self._events = event_sink
        self._version = _Version()
        self._projection = _AgentProjectionState()
        self._runtime = runtime or ActorRuntime()
        self._generation = 0
        self._generation_lock = threading.Lock()
        self._pending_home: dict[
            tuple[str, int], _PendingHomeMutation | _PendingHomeAccess
        ] = {}
        self._home_call_lock = threading.Lock()
        self._home_calls: dict[str, _HomeCall] = {}
        self._home_call_capacity = 32
        self._migration_lock = threading.Lock()
        self._migration_waiters: dict[str, _MigrationWaiter] = {}
        self._migration_settled: OrderedDict[str, tuple[MigrationRequest, MigrationOutcome]] = OrderedDict()
        self._migration_call_capacity = 32
        self._migration_owner_waiters: dict[str, tuple[threading.Event, MigrationOwnerResult | None]] = {}
        self._migration_owner_responses: dict[str, tuple[MigrationOwnerCall, MigrationOwnerResult]] = {}
        self._migration_lease: tuple[str, str, str, str] | None = None
        self._pending_pins: dict[
            tuple[str, int], _PendingPinMutation
        ] = {}
        self._pending_pin_adapters: set[str] = set()
        self._runtime_launches = _RuntimeLaunchState()
        self._home_effects: AgentHomeEffectLane | None = None
        self._state_effects: EffectLane[_PinStateRequest, _PinStateResult] | None = None
        try:
            self._home_effects = (
                AgentHomeEffectLane(
                    self._registry.home_provisioner,
                    complete=self._admit_home_completion,
                )
                if self._registry.home_enabled
                else None
            )
            self._state_effects = (
                EffectLane[
                    _PinStateRequest, _PinStateResult
                ](
                    name="agent-state-effect",
                    execute=self._execute_state_effect,
                    complete=self._admit_state_completion,
                    workers=1,
                )
                if self._desired_state is not None
                else None
            )
            self._seed_bindings()
            _refresh_projection(
                self._projection,
                self._registry,
                self._liveness,
                self._version.read(),
            )
            self._migration_effects = EffectLane[MigrationEffect, MigrationOutcome](
                name="agent-migration-effect",
                execute=self._execute_migration,
                complete=self._admit_migration_completion,
                capacity=1,
                workers=1,
            )

        except Exception:
            if self._home_effects is not None:
                self._home_effects.close(1.0)
            if self._state_effects is not None:
                self._state_effects.close(1.0)
            raise
        def factory() -> _AgentGeneration:
            with self._generation_lock:
                self._generation += 1
                generation = self._generation
            return _AgentGeneration(
                generation=generation,
                registry=self._registry,
                liveness=self._liveness,
                desired_state=self._desired_state,
                events=self._events,
                version=self._version,
                projection=self._projection,
                command_costs=self._command_costs,
                session_originated=self._session_originated,
                home_effects=self._home_effects,
                pending_home=self._pending_home,
                complete_home_call=self._complete_home_call,
                state_effects=self._state_effects,
                pending_pins=self._pending_pins,
                pending_pin_adapters=self._pending_pin_adapters,
                runtime_launches=self._runtime_launches,
                migration_begin=self._migration_begin,
                migration_owner_call=self._migration_owner_call,
                migration_completed=self._migration_completed,
                migration_conflicts=self._migration_conflicts,
            )

        self._handle = None
        try:
            self._handle = self._runtime.start(
                ActorSpec(
                    name="agent-registry-authority",
                    handler_factory=factory,
                    mailbox_capacity=mailbox_capacity,
                    undelivered_sink=self._undelivered,
                )
            )
            self._read_effects = AgentReadEffectPort(self._registry)
        except Exception:
            self._migration_effects.close(1.0)
            if self._home_effects is not None:
                self._home_effects.close(1.0)
            if self._state_effects is not None:
                self._state_effects.close(1.0)
            if self._handle is not None:
                self._runtime.stop(self._handle, timeout=1.0)
            raise

    def _undelivered(self, command: object, reason: str) -> None:
        if isinstance(command, MigrationRequest):
            self._finish_migration_waiter(command.operation_id, MigrationOutcome.failed(RuntimeError(reason)))
            return
        if isinstance(command, MigrationOwnerCall):
            self._finish_migration_owner_waiter(command.call_id, MigrationOwnerResult(failure=MigrationFailure.from_exception(RuntimeError(reason))))
            return
        if isinstance(command, EffectCompleted):
            return  # Effect lane retains completion until Agent owner ACK.
        if isinstance(command, (AgentHomeEffectCompleted, _AgentStateEffectCompleted)):
            return  # Effect owners retain and retry completion until ACK.
        correlation = str(getattr(command, "correlation_id", ""))
        if isinstance(command, (_PrepareWorkspaceCommand, _PrepareRuntimeCommand)):
            self._complete_home_call(correlation, None, RuntimeError(reason))
            return
        if correlation:
            _publish(self._events, PortCommandRejected(
                correlation_id=correlation,
                domain="agent",
                generation=self._generation,
                version=self.version,
                code=reason,
                detail="accepted agent command did not begin before owner retirement",
            ))

    def _admit_home_completion(
        self, event: AgentHomeEffectCompleted
    ) -> AdmissionResult:
        return self._runtime.tell(self._handle, event)

    def _admit_migration_completion(
        self, event: EffectCompleted[MigrationOutcome]
    ) -> AdmissionResult:
        return self._runtime.tell(self._handle, event)

    def _execute_migration(self, effect: MigrationEffect) -> MigrationOutcome:
        return run_migration_effect(effect, self)

    def _migration_command_actor(self, command: object) -> str | None:
        """Resolve a typed Agent command's affected local identity."""

        # Retiring an earlier launch/destroy receipt must continue so the
        # daemon can drain. Migration admission itself checked those lanes.
        if isinstance(command, (
            ReleaseAgentRuntimeLaunchCommand,
            ReleaseAgentDestroyReservationCommand,
            RetireAgentLifecycleReceiptCommand,
            ConfirmAgentLifecycleReceiptCommand,
        )):
            return None
        if isinstance(command, AcquireAgentRuntimeLaunchCommand):
            with self._runtime_launches.condition:
                offer = self._runtime_launches.contexts.get(command.launch_token)
            return None if offer is None else offer.receipt.actor
        if isinstance(command, UnpinAgentAdapterCommand):
            candidate = self._registry.pins().get(command.adapter)
        else:
            payload = getattr(command, "payload", command)
            candidate = getattr(payload, "actor", None)
            if not isinstance(candidate, str):
                candidate = getattr(payload, "name", None)
        if not isinstance(candidate, str):
            return None
        return self._registry.local_actor(candidate)

    def _migration_conflicts(self, command: object) -> bool:
        """Fence only commands that can change the migrating incarnation."""

        with self._migration_lock:
            lease = self._migration_lease
        return lease is not None and self._migration_command_actor(command) == lease[1]

    def _finish_migration_waiter(self, operation_id: str, outcome: MigrationOutcome) -> None:
        with self._migration_lock:
            entry = self._migration_waiters.get(operation_id)
            if entry is None:
                return
            entry.outcome = outcome
            entry.ready.set()

    def _finish_migration_owner_waiter(
        self, call_id: str, result: MigrationOwnerResult
    ) -> None:
        with self._migration_lock:
            entry = self._migration_owner_waiters.get(call_id)
            if entry is None:
                return
            event, _ = entry
            self._migration_owner_waiters[call_id] = (event, result)
            event.set()

    def _migration_begin(self, request: MigrationRequest) -> None:
        """Agent mailbox owns the lease and freezes exact incarnation/home facts."""

        try:
            agent = self._registry.require(request.actor)
            with self._migration_lock:
                busy = self._migration_lease is not None
            with self._runtime_launches.condition:
                launch_busy = (
                    agent.actor in self._runtime_launches.destroying
                    or any(
                        lease.receipt.actor == agent.actor
                        for lease in self._runtime_launches.leases.values()
                    )
                )
            home_busy = any(
                self._migration_command_actor(pending.command) == agent.actor
                for pending in self._pending_home.values()
            )
            pin_busy = any(
                pending.actor == agent.actor
                or self._migration_command_actor(pending.command) == agent.actor
                for pending in self._pending_pins.values()
            )
            if busy or launch_busy or home_busy or pin_busy:
                raise AgentMigrationBusy("Agent migration or target effect is busy")
            receipt = self._registry.home_receipt(agent.actor, validate_mirror=False)
            operation_id = request.operation_id
            admission = self._migration_effects.reserve(operation_id, 0)
            if admission is not AdmissionResult.ACCEPTED:
                raise AgentMigrationBusy(f"Agent migration effect admission is {admission.value}")
            with self._migration_lock:
                self._migration_lease = (
                    operation_id, agent.actor, agent.entity_token, receipt.resource_token
                )
            effect = MigrationEffect(
                request,
                Agent.from_json(agent.to_json(), "migration.agent"),
                HomeReceipt.from_json(receipt.to_json()),
                self._registry.database.parent,
                self._registry.home_provisioner.hyprial_home,
            )
            self._migration_effects.submit_reserved(EffectRequest(operation_id, 0, effect))
        except BaseException as error:
            self._migration_effects.cancel_reservation(request.operation_id, 0)
            with self._migration_lock:
                if self._migration_lease is not None and self._migration_lease[0] == request.operation_id:
                    self._migration_lease = None
            self._finish_migration_waiter(request.operation_id, MigrationOutcome.failed(error))

    def _migration_owner_call(self, command: MigrationOwnerCall) -> None:
        """Answer an effect worker with owner-fenced immutable facts or one CAS write."""

        with self._migration_lock:
            live_waiters = frozenset(self._migration_owner_waiters)
        # This runs on the Agent owner thread. A result stays cached only
        # while its effect-side caller can still consume or retry that exact
        # call ID. A late duplicate without a waiter remains a no-op.
        self._migration_owner_responses = {
            call_id: response
            for call_id, response in self._migration_owner_responses.items()
            if call_id in live_waiters
        }
        if command.call_id not in live_waiters:
            return
        prior = self._migration_owner_responses.get(command.call_id)
        if prior is not None:
            result = (
                prior[1] if prior[0] == command else
                MigrationOwnerResult(failure=MigrationFailure.from_exception(AgentError("migration owner call identity changed")))
            )
            self._finish_migration_owner_waiter(command.call_id, result)
            return
        try:
            with self._migration_lock:
                lease = self._migration_lease
            if lease is None or lease[:3] != (
                command.operation_id, command.actor, command.expected_entity_token
            ):
                raise AgentError("Agent migration lease or incarnation changed")
            agent = self._registry.require(command.actor)
            if agent.entity_token != lease[2]:
                raise AgentError("Agent migration incarnation changed")
            receipt = self._registry.home_receipt(agent.actor, validate_mirror=False)
            if receipt.resource_token != lease[3]:
                raise AgentError("Agent migration home receipt changed")
            if command.kind == "snapshot":
                value: object = Agent.from_json(agent.to_json(), "migration.agent")
            elif command.kind == "home-receipt":
                value = HomeReceipt.from_json(receipt.to_json())
            elif command.kind == "stopped":
                running = self._liveness.verdict(agent.uri)
                value = not running if running is not None else (
                    True if self._liveness.binding(agent.uri) is None else None
                )
            elif command.kind == "update-config":
                if command.config is not None and not isinstance(command.config, AgentConfig):
                    raise ValueError("migration config must be a typed AgentConfig")
                updated = self._registry.update(agent.actor, config=command.config)
                value = Agent.from_json(updated.to_json(), "migration.agent")
                _refresh_projection(
                    self._projection, self._registry, self._liveness, self._version.bump()
                )
            else:
                raise ValueError("unsupported migration owner call")
            result = MigrationOwnerResult(value=value)
        except BaseException as error:
            result = MigrationOwnerResult(failure=MigrationFailure.from_exception(error))
        self._migration_owner_responses[command.call_id] = (command, result)
        self._finish_migration_owner_waiter(command.call_id, result)

    def _migration_completed(self, event: EffectCompleted[MigrationOutcome]) -> None:
        with self._migration_lock:
            lease = self._migration_lease
            if lease is not None and lease[0] == event.operation_id:
                self._migration_lease = None
        if lease is not None and lease[0] == event.operation_id:
            outcome = event.result or MigrationOutcome.failed(
                RuntimeError(event.error or "migration effect returned no result")
            )
            self._finish_migration_waiter(event.operation_id, outcome)
            self._migration_owner_responses = {
                key: item for key, item in self._migration_owner_responses.items()
                if item[0].operation_id != event.operation_id
            }
        self._migration_effects.acknowledge(event.operation_id, event.generation)

    def migration_owner_call(self, command: MigrationOwnerCall) -> MigrationOwnerResult:
        event = threading.Event()
        with self._migration_lock:
            self._migration_owner_waiters[command.call_id] = (event, None)
        generation = self._runtime.snapshot(self._handle).generation
        admission = self._runtime.tell(self._handle, command)
        if admission is not AdmissionResult.ACCEPTED:
            self._finish_migration_owner_waiter(
                command.call_id, MigrationOwnerResult(failure=MigrationFailure.from_exception(AgentError(f"migration owner admission is {admission.value}")))
            )
        while not event.wait(1.0):
            # A healthy generation owns the accepted envelope: do not queue
            # duplicates while it may still be blocked inside this CAS. If
            # the owner restarted after beginning it, retry the same call ID
            # against the new generation; a refused retry cannot negate the
            # earlier accepted custody or an already committed result.
            try:
                current = self._runtime.snapshot(self._handle)
            except Exception:
                continue
            if current.generation == generation:
                continue
            retry = self._runtime.tell(self._handle, command)
            if retry is AdmissionResult.ACCEPTED:
                generation = current.generation
        with self._migration_lock:
            _, result = self._migration_owner_waiters.pop(command.call_id)
        assert result is not None
        return result

    def run_migration(
        self, method: str, actor: str, *,
        manifest: MigrationPreflightManifest | None = None,
        migration_id: str | None = None,
        authorization_window: MigrationAuthorizationWindow | None = None,
        operation_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, object]:
        """Submit an immutable operator request and join retained settlement."""

        token = operation_id or uuid4().hex
        request = MigrationRequest(
            token, method, actor, manifest, migration_id, authorization_window
        )
        with self._migration_lock:
            settled = self._migration_settled.get(token)
            if settled is not None:
                if settled[0] != request:
                    raise AgentError("migration operation identity changed")
                self._migration_settled.move_to_end(token)
                return settled[1].result()
            entry = self._migration_waiters.get(token)
            if entry is None:
                if len(self._migration_waiters) >= self._migration_call_capacity:
                    raise AgentMigrationBusy("Agent migration call capacity is full")
                entry = _MigrationWaiter(request, threading.Event())
                self._migration_waiters[token] = entry
                submit = True
            else:
                if entry.request != request:
                    raise AgentError("migration operation identity changed")
                submit = False
            entry.consumers += 1
        if submit:
            admission = self._runtime.tell(
                self._handle, request,
            )
            if admission is not AdmissionResult.ACCEPTED:
                self._finish_migration_waiter(
                    token, MigrationOutcome.failed(AgentError(f"migration admission is {admission.value}"))
                )
        if not entry.ready.wait(timeout):
            with self._migration_lock:
                entry.consumers -= 1
            raise TimeoutError(f"Agent migration remains pending: {token}")
        with self._migration_lock:
            outcome = entry.outcome
        assert outcome is not None
        try:
            return outcome.result()
        finally:
            with self._migration_lock:
                entry.consumers -= 1
                entry.consumed = True
                if entry.consumers == 0:
                    self._migration_waiters.pop(token, None)
                    self._migration_settled[token] = (request, outcome)
                    self._migration_settled.move_to_end(token)
                    while len(self._migration_settled) > 128:
                        self._migration_settled.popitem(last=False)

    def _complete_home_call(
        self,
        correlation_id: str,
        value: str | AgentRuntimeContext | None,
        error: BaseException | None,
    ) -> None:
        with self._home_call_lock:
            call = self._home_calls.pop(correlation_id, None)
        if call is None:
            return
        call.value = value
        call.error = error
        call.event.set()

    def _execute_state_effect(self, request: _PinStateRequest) -> _PinStateResult:
        if self._desired_state is None:
            return _PinStateResult(None)
        settled = getattr(self._desired_state, "remove_channel_pin_settled", None)
        _state, legacy = (
            settled(request.adapter)
            if callable(settled)
            else self._desired_state.remove_channel_pin(request.adapter)
        )
        return _PinStateResult(legacy)

    def _admit_state_completion(
        self, event: EffectCompleted[_PinStateResult]
    ) -> AdmissionResult:
        return self._runtime.tell(
            self._handle,
            _AgentStateEffectCompleted(
                event.operation_id,
                event.generation,
                result=event.result,
                error_code=event.error,
            ),
        )

    @property
    def generation(self) -> int:
        return self._runtime.snapshot(self._handle).generation

    @property
    def version(self) -> int:
        return self._version.read()

    @property
    def command_costs(self) -> CallCostCounters:
        """Per-command-type/origin thread CPU on this actor (``ps`` ipcStats)."""

        return self._command_costs

    def submit(self, command: object) -> PortAdmission:
        admission = self._runtime.tell(self._handle, command)
        if admission is AdmissionResult.ACCEPTED:
            return PortAdmission.ACCEPTED
        port_admission = (
            PortAdmission.OVERLOADED
            if admission is AdmissionResult.OVERLOADED
            else PortAdmission.CLOSING
        )
        _publish(
            self._events,
            PortCommandRejected(
                correlation_id=str(getattr(command, "correlation_id", "")),
                domain="agent",
                generation=self.generation,
                version=self.version,
                code=(
                    "PORT_OVERLOADED"
                    if port_admission is PortAdmission.OVERLOADED
                    else "PORT_CLOSING"
                ),
                detail=f"agent command admission is {port_admission.value}",
                admission=port_admission,
            ),
        )
        return port_admission

    def read_agent(self, name: str) -> AgentProjection | None:
        return self._projection.read_agent(name)

    def read_agents(self) -> tuple[AgentProjection, ...]:
        return self._projection.read_agents()

    def read_resolution(self, name: str) -> AgentResolveProjection:
        if ":" in name:
            return AgentResolveProjection(name, name, True, "scheme")
        agent = self._projection.read_agent(name)
        if agent is not None:
            return AgentResolveProjection(name, agent.uri, True, "alias")
        if name == self._machine:
            return AgentResolveProjection(name, name, True, "node")
        return AgentResolveProjection(name, name, False, "unknown")

    def read_binding(self, actor: str) -> AgentBindingProjection | None:
        agent = self._projection.read_agent(actor)
        return self._projection.read_binding(actor if agent is None else agent.uri)

    def read_local_actor(self, value: str) -> str | None:
        parsed = parse_agent_uri(value)
        if parsed is not None:
            if parsed[:2] != (self._registry.owner, self._registry.machine):
                hosted = self._projection.read_agent(value)
                return (
                    hosted.actor
                    if hosted is not None and hosted.uri == value
                    and hosted.hosted_by is not None else None
                )
            candidate = parsed[2]
        elif ":" in value:
            return None
        else:
            candidate = value
        try:
            return self._registry.normalize_actor(candidate)
        except AgentError:
            return None

    def read_uri_for(self, actor: str) -> str:
        name = self._registry.normalize_actor(actor)
        projection = self._projection.read_agent(name)
        if projection is not None and projection.hosted_by is not None:
            return projection.uri
        return canonical_agent_uri(self._registry.owner, self._registry.machine, name)

    def read_pins(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(
            (adapter, agent.uri)
            for agent in self._projection.read_agents()
            for adapter in agent.pinned_adapters
        ))

    def read_capability_grants(
        self, actor: str | None = None
    ) -> tuple[CapabilityGrant, ...]:
        return self._read_effects.capability_grants(actor)

    def read_secret_inventory(
        self, actor: str | None = None
    ) -> tuple[SecretGrant, ...]:
        return self._read_effects.secret_inventory(actor)

    def prevalidate_home(self, actor: str) -> HomeReceipt:
        return self._read_effects.validate_home(actor)

    def read_grant_journal(self, actor: str) -> tuple[GrantJournalEntry, ...]:
        return self._read_effects.grant_journal(actor)

    def read_workspace_summary(self, actor: str) -> WorkspaceSummary:
        return self._read_effects.workspace_summary(actor)

    def resolve_workspace_path(self, actor: str) -> str:
        return self._read_effects.workspace_path(actor)

    def prepare_workspace(self, actor: str) -> str:
        result = self._call_home_access(
            _PrepareWorkspaceCommand(uuid4().hex, actor)
        )
        if not isinstance(result, str):
            raise AgentHomeAccessError("workspace preparation returned no path")
        return result

    def prepare_runtime_context(
        self,
        actor: str,
        harness: str,
        cwd: str | None,
        tool_profile: AgentToolProfile,
        *,
        containerized: bool = False,
    ) -> AgentRuntimeContext | None:
        result = self._call_home_access(_PrepareRuntimeCommand(
            uuid4().hex,
            actor,
            harness,
            cwd,
            tool_profile,
            containerized,
        ))
        if result is not None and not isinstance(result, AgentRuntimeContext):
            raise AgentHomeAccessError("runtime preparation returned an invalid context")
        return result

    def _call_home_access(
        self,
        command: _PrepareWorkspaceCommand | _PrepareRuntimeCommand,
        timeout: float = 70.0,
    ) -> str | AgentRuntimeContext | None:
        if self._home_effects is None:
            raise AgentHomeAccessError("Agent home filesystem authority is unavailable")
        call = _HomeCall()
        with self._home_call_lock:
            if len(self._home_calls) >= self._home_call_capacity:
                raise AgentHomeAccessError("Agent home access capacity is full")
            self._home_calls[command.correlation_id] = call
        admission = self._runtime.tell(self._handle, command)
        if admission is not AdmissionResult.ACCEPTED:
            with self._home_call_lock:
                self._home_calls.pop(command.correlation_id, None)
            raise AgentHomeAccessError(
                f"Agent home access admission is {admission.value}"
            )
        if not call.event.wait(timeout):
            raise AgentHomeAccessError(
                f"Agent home access remains unsettled: {command.correlation_id}"
            )
        if call.error is not None:
            raise call.error
        return call.value

    def read_activity_hints(self, actor: str) -> AgentActivityHints:
        return self._read_effects.activity_hints(actor)

    def read_runtime_launch_custody(
        self,
    ) -> tuple[AgentRuntimeLaunchCustodyProjection, ...]:
        return self._runtime_launches.custody()

    def read_runtime(
        self, actor: str, desired_state: object | None = None
    ) -> AgentRuntimeProjection:
        """One actor's runtime projection; ``desired_state`` is card 259's

        snapshot-scoped already-loaded desired state, handed down to the
        worker probe so a whole snapshot parses the document once.  ``None``
        (every pre-snapshot caller) probes exactly as before.
        """

        agent = self._projection.read_agent(actor)
        spelling = actor if agent is None else agent.uri
        binding = self._liveness.binding(spelling)
        snapshot = self._liveness.snapshot(spelling, desired_state)
        verdict = self._liveness.verdict(spelling, desired_state)
        return AgentRuntimeProjection(
            actor=spelling,
            status=str(snapshot["status"]),
            online=verdict,
            binding=None if binding is None else _binding_projection(binding),
            heartbeat_age_seconds=(
                float(snapshot["heartbeatAgeSeconds"])
                if snapshot["heartbeatAgeSeconds"] is not None
                else None
            ),
        )

    def resolve_sender(self, sender: str) -> str:
        """Resolve only a registry-backed local sender; never impersonate peers."""

        agent = self._projection.read_agent(sender)
        if agent is None:
            raise SenderIdentityError(
                f"sender {sender!r} is not a registered agent on this node"
            )
        return agent.uri

    def drain(self, timeout: float = 5.0) -> DrainReport:
        started = time.monotonic()
        self._runtime_launches.begin_close()
        migration_complete = self._migration_effects.close(timeout * 0.20)
        if not migration_complete:
            return DrainReport(False, time.monotonic() - started, (self._handle,))
        if not self._runtime_launches.wait_drained(timeout * 0.20):
            return DrainReport(
                False,
                time.monotonic() - started,
                (self._handle,),
            )
        home_complete = (
            True
            if self._home_effects is None
            else self._home_effects.close(timeout * 0.35)
        )
        state_complete = (
            True
            if self._state_effects is None
            else self._state_effects.close(timeout * 0.20)
        )
        report = self._runtime.drain(max(0.0, timeout * 0.15))
        read_complete = self._read_effects.close(
            max(0.0, timeout - (time.monotonic() - started))
        )
        return DrainReport(
            home_complete and state_complete and report.complete and read_complete,
            time.monotonic() - started,
            report.remaining,
        )

    def _seed_bindings(self) -> None:
        if self._desired_state is None:
            return
        state = self._desired_state.load()
        harnesses = getattr(state, "harnesses", ())
        for spec in harnesses:
            if getattr(spec, "harness", None) == "lark":
                continue
            agent = self._registry.get(str(getattr(spec, "name", "")))
            if agent is None:
                continue
            self._liveness.bind(
                agent.uri,
                harness=str(spec.harness),
                runtime="headless",
                session_id=getattr(spec, "session_ref", None),
            )
        sessions = getattr(state, "interactive_sessions", ())
        for session in sessions:
            agent = self._registry.get(str(getattr(session, "actor", "")))
            if agent is None:
                continue
            runtime = str(getattr(session, "runtime", "") or "")
            source = str(getattr(session, "source", "") or "")
            harness = (
                runtime.split("_", 1)[0]
                if runtime
                else source.split("-", 1)[0]
                if source
                else "claude"
            )
            self._liveness.bind(
                agent.uri,
                harness=harness,
                runtime=RUNTIME_INTERACTIVE,
                session_id=getattr(session, "session_ref", None),
            )


# Descriptive compatibility name for composition roots that spell out the
# state authority.  It is the same public class, not a second implementation.
AgentRegistryActor = AgentActor


def _agent_projection(
    agent: Agent, version: int, registry: AgentRegistry
) -> AgentProjection:
    disposition = registry.restore_disposition(agent)
    block = registry.agent_block(agent)
    return AgentProjection(
        version=version,
        uri=agent.uri,
        actor=agent.actor,
        owner=agent.owner,
        machine=agent.machine,
        entity_token=agent.entity_token,
        cwd=agent.cwd,
        config=None if agent.config is None else agent.config.to_json(),
        provider=agent.provider,
        model=agent.model,
        capabilities=tuple(sorted(agent.capabilities.items())),
        harness_args=tuple(
            (harness, tuple(args))
            for harness, args in sorted(agent.harness_args.items())
        ),
        preferred_harness=agent.preferred_harness,
        last_harness=agent.last_harness,
        last_session_id=agent.last_session_id,
        last_active_at_ms=agent.last_active_at_ms,
        pinned_adapters=agent.pinned_adapters,
        created_at_ms=agent.created_at_ms,
        hosted_by=agent.hosted_by,
        restore_disposition=(
            None
            if disposition is None
            else RestoreDispositionProjection(
                disposition.entity_token,
                disposition.desired_generation,
                disposition.disposition_token,
                disposition.status,
                disposition.last_active_at_ms,
                disposition.idle_age_ms,
                disposition.restore_threshold_ms,
                disposition.restore_override,
                disposition.activity_unknown,
                disposition.recorded_at_ms,
            )
        ),
        block=(
            None
            if block is None
            else AgentBlockProjection(
                block.entity_token, block.reason, block.blocked_at_ms
            )
        ),
    )


def _binding_projection(binding: AgentBinding) -> AgentBindingProjection:
    return AgentBindingProjection(
        actor=binding.actor,
        harness=binding.harness,
        runtime=binding.runtime,
        session_id=binding.session_id,
        bound_at_ms=binding.bound_at_ms,
    )


def _refresh_projection(
    projection: _AgentProjectionState,
    registry: AgentRegistry,
    liveness: AgentLiveness,
    version: int,
) -> None:
    projection.replace(
        version=version,
        agents=tuple(
            _agent_projection(agent, version, registry) for agent in registry.list()
        ),
        bindings=tuple(
            _binding_projection(binding) for binding in liveness.bindings()
        ),
    )


def _publish(sink: object, event: object) -> None:
    publish = getattr(sink, "publish", None)
    if callable(publish):
        publish(event)
        return
    if callable(sink):
        sink(event)
        return
    raise TypeError("event_sink must be callable or expose publish(event)")
