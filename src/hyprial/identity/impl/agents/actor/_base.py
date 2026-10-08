from __future__ import annotations

from hyprial.identity.impl.agents.registry._base import Agent
from hyprial.identity.impl.agents.state.liveness import AgentBinding
from hyprial.identity.impl.agents.actor.ports import AgentBindingProjection
from hyprial.identity.impl.agents.actor.ports import AgentBlockProjection
from hyprial.identity.impl.agents.registry._base import AgentError
from hyprial.identity.impl.agents.registry._base import AgentHomeReservation
from hyprial.identity.impl.agents.state.liveness import AgentLiveness
from hyprial.identity.impl.agents.actor.ports import AgentProjection
from hyprial.identity.impl.agents.registry._core import AgentRegistry
from hyprial.identity.impl.agents.runtime.context import AgentRuntimeContext
from hyprial.identity.impl.agents.actor.ports import AgentRuntimeLaunchCustodyProjection
from hyprial.identity.impl.agents.runtime.context import AgentToolProfile
from hyprial.identity.impl.agents.home.provisioner import HomePayloadFile
from hyprial.identity.impl.agents.home.provisioner import HomeReceipt
from hyprial.identity.impl.agents.migration.actor import MigrationOutcome
from hyprial.identity.impl.agents.migration.actor import MigrationRequest
from collections import OrderedDict
from hyprial.identity.impl.agents.actor.ports import PinAgentAdapterCommand
from typing import Protocol
from hyprial.identity.impl.agents.actor.ports import RestoreDispositionProjection
from hyprial.identity.impl.agents.actor.ports import UnpinAgentAdapterCommand
from dataclasses import dataclass
from hyprial.kernel import ipc_errors
from dataclasses import replace
import threading
import time
from uuid import uuid4

"""Actor-owned agent registry mutations and public projections.

SQLite remains the final authority for identity and pin uniqueness.  This
actor adds serialized mutation, correlated events, runtime bindings and
restart recovery without weakening the database constraints or reaching into
Harness/Adapter implementations.
"""
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
class AgentHomeAccessPending(AgentHomeAccessError):
    home_io_pending = True
class AgentRuntimeContextStale(AgentError):
    code = "AGENT_RUNTIME_CONTEXT_STALE"
class AgentRuntimeLaunchMismatch(AgentError):
    code = "AGENT_RUNTIME_LAUNCH_MISMATCH"
class AgentMigrationBusy(AgentError):
    code = ipc_errors.AGENT_MIGRATION_BUSY
class AgentLaunchInProgress(AgentError):
    code = "AGENT_LAUNCH_IN_PROGRESS"
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
    "RecordAgentSessionRefCommand",
    "RetireAgentSessionRefsCommand",
    "RollbackRetiredAgentSessionRefsCommand",
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
class _MaterialiseLandingHomeCommand:
    correlation_id: str
    actor: str
    expected_entity_token: str
    staged_root: str
    expected_files: tuple[HomePayloadFile, ...]
@dataclass(frozen=True, slots=True)
class _ConfirmLandingHomeCommand:
    correlation_id: str
    actor: str
    expected_entity_token: str
@dataclass(frozen=True, slots=True)
class _PendingHomeAccess:
    command: _PrepareWorkspaceCommand | _PrepareRuntimeCommand | _MaterialiseLandingHomeCommand | _ConfirmLandingHomeCommand
    receipt: HomeReceipt
@dataclass(frozen=True, slots=True)
class _LegacyRuntimeResult:
    actor: str
    reason: str
class _HomeCall:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.value: str | AgentRuntimeContext | _LegacyRuntimeResult | None = None
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
    secret_grants: tuple[tuple[str, int], ...]
    consumed: bool = False
@dataclass(frozen=True, slots=True)
class _RuntimeLaunchLease:
    operation_id: str
    actor: str
    receipt: HomeReceipt
    secret_grants: tuple[tuple[str, int], ...]
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
        self,
        context: AgentRuntimeContext,
        receipt: HomeReceipt,
        secret_grants: tuple[tuple[str, int], ...],
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
            self.contexts[token] = _RuntimeContextFence(
                context.actor, receipt, secret_grants
            )
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
        lease = _RuntimeLaunchLease(
            operation_id, fence.actor, fence.receipt, fence.secret_grants
        )
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

    def destroy_reserved(self, actor: str) -> bool:
        with self.condition:
            return actor in self.destroying

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
