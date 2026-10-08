from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from typing import Literal, Protocol, TypeAlias

from hyprial.kernel import CommandSink, EventSink, PortCommandRejected
from hyprial.identity.impl.agents.runtime.grants  import CapabilityGrant
from hyprial.identity.impl.agents.runtime.secrets  import SecretGrant, SecretSource

AgentDestroyDisposition: TypeAlias = Literal[
    "destroyed", "already-cleaned", "stale-incarnation"
]


@dataclass(frozen=True, slots=True)
class FrozenJsonMap:
    items: tuple[tuple[str, object], ...]


@dataclass(frozen=True, slots=True)
class FrozenJsonList:
    items: tuple[object, ...]


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Agent projection JSON keys must be strings")
        return FrozenJsonMap(tuple(
            (key, _freeze_json(child)) for key, child in sorted(value.items())
        ))
    if isinstance(value, list | tuple):
        return FrozenJsonList(tuple(_freeze_json(child) for child in value))
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, FrozenJsonMap | FrozenJsonList):
        return value
    raise TypeError(f"Agent projection JSON value is unsupported: {type(value).__name__}")


def _thaw_json(value: object) -> object:
    if isinstance(value, FrozenJsonMap):
        return {key: _thaw_json(child) for key, child in value.items}
    if isinstance(value, FrozenJsonList):
        return [_thaw_json(child) for child in value.items]
    return value


@dataclass(frozen=True, slots=True)
class CreateAgentCommand:
    correlation_id: str
    name: str
    reuse_existing: bool = False
    cwd: str | None = None
    config: object = None
    provider: str | None = None
    model: str | None = None
    capabilities: tuple[tuple[str, object], ...] = ()
    harness_args: tuple[tuple[str, tuple[str, ...]], ...] = ()
    preferred_harness: str | None = None
    launch_harness: str | None = None
    runtime: str = "headless"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "config", None if self.config is None else _freeze_json(self.config)
        )
        object.__setattr__(self, "capabilities", tuple(
            (key, _freeze_json(value)) for key, value in self.capabilities
        ))

    def config_payload(self) -> object:
        return _thaw_json(self.config)

    def capabilities_payload(self) -> dict[str, object]:
        return {key: _thaw_json(value) for key, value in self.capabilities}


@dataclass(frozen=True, slots=True)
class CreateTransferHostedAgentCommand:
    correlation_id: str
    name: str
    pinned_owner: str
    cwd: str | None = None
    harness_args: tuple[tuple[str, tuple[str, ...]], ...] = ()
    preferred_harness: str | None = None


@dataclass(frozen=True, slots=True)
class CreateHostInvitedAgentCommand:
    correlation_id: str
    name: str
    pinned_owner: str
    entity_token: str
    cwd: str | None = None
    harness_args: tuple[tuple[str, tuple[str, ...]], ...] = ()
    preferred_harness: str | None = None


@dataclass(frozen=True, slots=True)
class DestroyAgentCommand:
    correlation_id: str
    name: str
    expected_entity_token: str | None = None


@dataclass(frozen=True, slots=True)
class SettleAgentDestroyCommand:
    """Settle one exact Agent incarnation through durable home cleanup."""

    correlation_id: str
    name: str
    expected_entity_token: str


@dataclass(frozen=True, slots=True)
class CleanupRevokedAgentHomeCommand:
    correlation_id: str
    name: str


@dataclass(frozen=True, slots=True)
class UpdateAgentCommand:
    correlation_id: str
    name: str
    cwd: str | None = None
    config: object = None
    provider: str | None = None
    model: str | None = None
    capabilities: tuple[tuple[str, object], ...] = ()
    harness_args: tuple[tuple[str, tuple[str, ...]], ...] = ()
    preferred_harness: str | None = None
    last_harness: str | None = None
    last_session_id: str | None = None
    expected_version: int | None = None
    expected_entity_token: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "config", None if self.config is None else _freeze_json(self.config)
        )
        object.__setattr__(self, "capabilities", tuple(
            (key, _freeze_json(value)) for key, value in self.capabilities
        ))

    def config_payload(self) -> object:
        return _thaw_json(self.config)

    def capabilities_payload(self) -> dict[str, object]:
        return {key: _thaw_json(value) for key, value in self.capabilities}


@dataclass(frozen=True, slots=True)
class BindAgentCommand:
    correlation_id: str
    actor: str
    harness: str
    runtime: str
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReleaseAgentCommand:
    correlation_id: str
    actor: str


@dataclass(frozen=True, slots=True)
class RecordAgentActivityCommand:
    correlation_id: str
    actor: str


@dataclass(frozen=True, slots=True)
class SetRestoreDispositionCommand:
    correlation_id: str
    actor: str
    desired_generation: str
    last_active_at_ms: int | None
    idle_age_ms: int | None
    restore_threshold_ms: int
    restore_override: str = "none"
    activity_unknown: bool = False


@dataclass(frozen=True, slots=True)
class ClearRestoreDispositionCommand:
    correlation_id: str
    actor: str
    expected_entity_token: str | None = None
    expected_desired_generation: str | None = None
    expected_disposition_token: str | None = None


@dataclass(frozen=True, slots=True)
class BlockAgentCommand:
    correlation_id: str
    actor: str
    reason: str
    expected_entity_token: str | None = None


@dataclass(frozen=True, slots=True)
class UnblockAgentCommand:
    correlation_id: str
    actor: str


@dataclass(frozen=True, slots=True)
class PinAgentAdapterCommand:
    correlation_id: str
    actor: str
    adapter: str
    expected_actor: str | None = None
    compare: bool = False


@dataclass(frozen=True, slots=True)
class UnpinAgentAdapterCommand:
    correlation_id: str
    adapter: str
    expected_actor: str | None = None
    compare: bool = False


@dataclass(frozen=True, slots=True)
class AcquireAgentRuntimeLaunchCommand:
    correlation_id: str
    launch_token: str


@dataclass(frozen=True, slots=True)
class ReleaseAgentRuntimeLaunchCommand:
    correlation_id: str
    lease_token: str
    operation_id: str


@dataclass(frozen=True, slots=True)
class ReserveAgentDestroyCommand:
    correlation_id: str
    actor: str


@dataclass(frozen=True, slots=True)
class ReleaseAgentDestroyReservationCommand:
    correlation_id: str
    reservation_token: str


@dataclass(frozen=True, slots=True)
class RecordAgentSessionRefCommand:
    correlation_id: str
    actor: str
    entity_token: str
    session_ref: str


@dataclass(frozen=True, slots=True)
class RetireAgentSessionRefsCommand:
    correlation_id: str
    actor: str
    entity_token: str
    session_refs: tuple[str, ...]
    reason: str
    destroy_attempt: str


@dataclass(frozen=True, slots=True)
class RollbackRetiredAgentSessionRefsCommand:
    correlation_id: str
    destroy_attempt: str


@dataclass(frozen=True, slots=True)
class RetireAgentLifecycleReceiptCommand:
    correlation_id: str
    attempt_token: str
    resource_token: str


@dataclass(frozen=True, slots=True)
class ConfirmAgentLifecycleReceiptCommand:
    correlation_id: str
    attempt_token: str
    resource_token: str


@dataclass(frozen=True, slots=True)
class GrantAgentCapabilityCommand:
    correlation_id: str
    actor: str
    grant_id: str
    capability: str
    scope: str
    granted_by: str
    revision: int
    expected_entity_token: str | None = None


@dataclass(frozen=True, slots=True)
class RevokeAgentCapabilityCommand:
    correlation_id: str
    actor: str
    grant_id: str
    revoked_by: str
    expected_entity_token: str | None = None


@dataclass(frozen=True, slots=True)
class GrantAgentSecretCommand:
    correlation_id: str
    actor: str
    grant_id: str
    source: SecretSource
    entry_id: str
    field_name: str | None
    environment_names: tuple[str, ...]
    revision: int
    prevalidated_home_token: str | None = None


@dataclass(frozen=True, slots=True)
class RevokeAgentSecretCommand:
    correlation_id: str
    actor: str
    grant_id: str


AgentCommand: TypeAlias = (
    CreateAgentCommand
    | CreateTransferHostedAgentCommand
    | CreateHostInvitedAgentCommand
    | UpdateAgentCommand
    | DestroyAgentCommand
    | SettleAgentDestroyCommand
    | CleanupRevokedAgentHomeCommand
    | BindAgentCommand
    | ReleaseAgentCommand
    | RecordAgentActivityCommand
    | SetRestoreDispositionCommand
    | ClearRestoreDispositionCommand
    | BlockAgentCommand
    | UnblockAgentCommand
    | PinAgentAdapterCommand
    | UnpinAgentAdapterCommand
    | AcquireAgentRuntimeLaunchCommand
    | ReleaseAgentRuntimeLaunchCommand
    | ReserveAgentDestroyCommand
    | ReleaseAgentDestroyReservationCommand
    | RecordAgentSessionRefCommand
    | RetireAgentSessionRefsCommand
    | RollbackRetiredAgentSessionRefsCommand
    | RetireAgentLifecycleReceiptCommand
    | ConfirmAgentLifecycleReceiptCommand
    | GrantAgentCapabilityCommand
    | RevokeAgentCapabilityCommand
    | GrantAgentSecretCommand
    | RevokeAgentSecretCommand
)


@dataclass(frozen=True, slots=True)
class RestoreDispositionProjection:
    entity_token: str
    desired_generation: str
    disposition_token: str
    status: str
    last_active_at_ms: int | None
    idle_age_ms: int | None
    restore_threshold_ms: int
    restore_override: str
    activity_unknown: bool
    recorded_at_ms: int


@dataclass(frozen=True, slots=True)
class AgentBlockProjection:
    entity_token: str
    reason: str
    blocked_at_ms: int


@dataclass(frozen=True, slots=True)
class AgentProjection:
    version: int
    uri: str
    actor: str
    owner: str
    machine: str
    entity_token: str
    cwd: str | None = None
    config: object = None
    provider: str | None = None
    model: str | None = None
    capabilities: tuple[tuple[str, object], ...] = ()
    harness_args: tuple[tuple[str, tuple[str, ...]], ...] = ()
    preferred_harness: str | None = None
    last_harness: str | None = None
    last_session_id: str | None = None
    last_active_at_ms: int | None = None
    pinned_adapters: tuple[str, ...] = ()
    created_at_ms: int = 0
    hosted_by: str | None = None
    restore_disposition: RestoreDispositionProjection | None = None
    block: AgentBlockProjection | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "config", None if self.config is None else _freeze_json(self.config)
        )
        object.__setattr__(self, "capabilities", tuple(
            (key, _freeze_json(value)) for key, value in self.capabilities
        ))

    def config_payload(self) -> object:
        return _thaw_json(self.config)

    def capabilities_payload(self) -> dict[str, object]:
        return {key: _thaw_json(value) for key, value in self.capabilities}

    def to_payload(self) -> dict[str, object]:
        return {
            "uri": self.uri,
            "actor": self.actor,
            "owner": self.owner,
            "machine": self.machine,
            # entity_token is internal incarnation authority: home/grant fences
            # bind to it, but it is not part of the public ``ps`` wire.  The
            # field survives on the projection for the composition round-trip.
            "cwd": self.cwd,
            "config": self.config_payload(),
            "provider": self.provider,
            "model": self.model,
            "capabilities": self.capabilities_payload(),
            "harnessArgs": {harness: list(args) for harness, args in self.harness_args},
            "preferredHarness": self.preferred_harness,
            "lastHarness": self.last_harness,
            "lastSessionId": self.last_session_id,
            "lastActiveAtMs": self.last_active_at_ms,
            "pinnedAdapters": list(self.pinned_adapters),
            "createdAtMs": self.created_at_ms,
            "hosted": self.hosted_by is not None,
            "hostedBy": self.hosted_by,
        }


@dataclass(frozen=True, slots=True)
class AgentBindingProjection:
    actor: str
    harness: str
    runtime: str
    session_id: str | None
    bound_at_ms: int

    def to_payload(self) -> dict[str, object]:
        return {
            "actor": self.actor,
            "harness": self.harness,
            "runtime": self.runtime,
            "sessionId": self.session_id,
            "boundAtMs": self.bound_at_ms,
        }


@dataclass(frozen=True, slots=True)
class AgentRuntimeProjection:
    actor: str
    status: str
    online: bool | None
    binding: AgentBindingProjection | None
    heartbeat_age_seconds: float | None

    def to_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "harness": None if self.binding is None else self.binding.harness,
            "runtime": None if self.binding is None else self.binding.runtime,
            "sessionId": None if self.binding is None else self.binding.session_id,
            "heartbeatAgeSeconds": self.heartbeat_age_seconds,
        }


@dataclass(frozen=True, slots=True)
class AgentResolveProjection:
    input: str
    resolved: str
    known: bool
    reason: str

    def to_payload(self) -> dict[str, object]:
        return {
            "ok": True,
            "input": self.input,
            "resolved": self.resolved,
            "known": self.known,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class AgentsProjection:
    agents: tuple[AgentProjection, ...]

    def to_payload(self) -> dict[str, object]:
        return {"ok": True, "agents": [agent.to_payload() for agent in self.agents]}


@dataclass(frozen=True, slots=True)
class AgentItemProjection:
    agent: AgentProjection

    def to_payload(self) -> dict[str, object]:
        return {"ok": True, "agent": self.agent.to_payload()}


@dataclass(frozen=True, slots=True)
class AgentMutationCompleted:
    correlation_id: str
    generation: int
    version: int
    operation: str
    changed: bool
    agent: AgentProjection | None = None
    binding: AgentBindingProjection | None = None


@dataclass(frozen=True, slots=True)
class AgentLivenessIoCompleted:
    correlation_id: str
    generation: int
    version: int
    actor: str
    online: bool
    observed_at_ms: int
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class AgentLifecycleReceiptCompleted:
    correlation_id: str
    generation: int
    version: int
    attempt_token: str
    resource_token: str
    operation: str
    matched: bool


@dataclass(frozen=True, slots=True)
class AgentCapabilityGrantCompleted:
    correlation_id: str
    generation: int
    version: int
    operation: str
    changed: bool
    grant: CapabilityGrant | None = None


@dataclass(frozen=True, slots=True)
class AgentSecretGrantCompleted:
    correlation_id: str
    generation: int
    version: int
    operation: str
    changed: bool
    grant: SecretGrant | None = None


@dataclass(frozen=True, slots=True)
class AgentRuntimeLaunchLeaseCompleted:
    correlation_id: str
    generation: int
    version: int
    actor: str
    lease_token: str
    acquired: bool
    expires_at_ms: int | None = None
    secret_grants: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class AgentRuntimeLaunchCustodyProjection:
    operation_id: str
    actor: str
    lease_token: str


@dataclass(frozen=True, slots=True)
class AgentDestroyReservationCompleted:
    correlation_id: str
    generation: int
    version: int
    actor: str
    reservation_token: str
    reserved: bool


@dataclass(frozen=True, slots=True)
class AgentDestroySettled:
    correlation_id: str
    generation: int
    version: int
    actor: str
    expected_entity_token: str
    disposition: AgentDestroyDisposition


AgentEvent: TypeAlias = (
    AgentMutationCompleted
    | AgentLivenessIoCompleted
    | AgentLifecycleReceiptCompleted
    | AgentCapabilityGrantCompleted
    | AgentSecretGrantCompleted
    | AgentRuntimeLaunchLeaseCompleted
    | AgentDestroyReservationCompleted
    | AgentDestroySettled
    | PortCommandRejected
)
AgentCommandSink: TypeAlias = CommandSink[AgentCommand]
AgentEventSink: TypeAlias = EventSink[AgentEvent]


class AgentProjectionPort(Protocol):
    def read_agent(self, name: str) -> AgentProjection | None: ...

    def read_agents(self) -> tuple[AgentProjection, ...]: ...

    def read_resolution(self, name: str) -> AgentResolveProjection: ...

    def read_binding(self, actor: str) -> AgentBindingProjection | None: ...

    def read_runtime(self, actor: str) -> AgentRuntimeProjection: ...
