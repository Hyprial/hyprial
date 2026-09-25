from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias

from hyprial.contracts.ports import CommandSink, EventSink, PortCommandRejected


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
    cwd: str | None = None
    harness_args: tuple[tuple[str, tuple[str, ...]], ...] = ()
    preferred_harness: str | None = None


@dataclass(frozen=True, slots=True)
class DestroyAgentCommand:
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


AgentCommand: TypeAlias = (
    CreateAgentCommand
    | CreateTransferHostedAgentCommand
    | CreateHostInvitedAgentCommand
    | UpdateAgentCommand
    | DestroyAgentCommand
    | BindAgentCommand
    | ReleaseAgentCommand
    | PinAgentAdapterCommand
    | UnpinAgentAdapterCommand
)


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
    pinned_adapters: tuple[str, ...] = ()
    created_at_ms: int = 0
    hosted_by: str | None = None

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
            "config": self.config,
            "provider": self.provider,
            "model": self.model,
            "capabilities": dict(self.capabilities),
            "harnessArgs": {harness: list(args) for harness, args in self.harness_args},
            "preferredHarness": self.preferred_harness,
            "lastHarness": self.last_harness,
            "lastSessionId": self.last_session_id,
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


AgentEvent: TypeAlias = (
    AgentMutationCompleted | AgentLivenessIoCompleted | PortCommandRejected
)
AgentCommandSink: TypeAlias = CommandSink[AgentCommand]
AgentEventSink: TypeAlias = EventSink[AgentEvent]


class AgentProjectionPort(Protocol):
    def read_agent(self, name: str) -> AgentProjection | None: ...

    def read_agents(self) -> tuple[AgentProjection, ...]: ...

    def read_resolution(self, name: str) -> AgentResolveProjection: ...

    def read_binding(self, actor: str) -> AgentBindingProjection | None: ...

    def read_runtime(self, actor: str) -> AgentRuntimeProjection: ...
