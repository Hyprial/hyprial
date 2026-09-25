"""Actor-owned agent registry mutations and public projections.

SQLite remains the final authority for identity and pin uniqueness.  This
actor adds serialized mutation, correlated events, runtime bindings and
restart recovery without weakening the database constraints or reaching into
Harness/Adapter implementations.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from hyprial.actor_runtime import ActorRuntime, ActorSpec, AdmissionResult, DrainReport
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
    AgentMutationCompleted,
    AgentProjection,
    AgentRuntimeProjection,
    AgentResolveProjection,
    BindAgentCommand,
    CreateAgentCommand,
    CreateHostInvitedAgentCommand,
    CreateTransferHostedAgentCommand,
    DestroyAgentCommand,
    PinAgentAdapterCommand,
    ReleaseAgentCommand,
    UnpinAgentAdapterCommand,
    UpdateAgentCommand,
)
from .registry import Agent, AgentError, AgentHomeError, AgentRegistry

__all__ = ["AgentActor", "AgentRegistryActor", "SenderIdentityError"]


class _DesiredStatePort(Protocol):
    def load(self) -> object: ...

    def remove_channel_pin(self, channel: str) -> tuple[object, str | None]: ...


class SenderIdentityError(AgentError):
    code = ipc_errors.SENDER_UNRESOLVED


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


@dataclass(slots=True)
class _AgentGeneration:
    generation: int
    registry: AgentRegistry
    liveness: AgentLiveness
    desired_state: _DesiredStatePort | None
    events: object
    version: _Version
    projection: _AgentProjectionState

    def __call__(self, command: object) -> None:
        from hyprial.daemon.lifecycle_receipts import LifecycleMutationRequest

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
                BindAgentCommand,
                ReleaseAgentCommand,
                PinAgentAdapterCommand,
                UnpinAgentAdapterCommand,
            ),
        ):
            self._reject(
                command, ipc_errors.INVALID_ARGUMENT, "unsupported agent command"
            )
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
            elif isinstance(command, BindAgentCommand):
                self._bind(command)
            elif isinstance(command, ReleaseAgentCommand):
                self._release(command)
            elif isinstance(command, PinAgentAdapterCommand):
                self._pin(command)
            else:
                self._unpin(command)
        except (AgentError, AgentHomeError, AgentAlreadyRunning) as error:
            self._reject(
                command, str(getattr(error, "code", "AGENT_ERROR")), str(error)
            )
        except ValueError as error:
            self._reject(command, ipc_errors.INVALID_ARGUMENT, str(error))

    def _lifecycle(self, request: object) -> None:
        from hyprial.daemon.lifecycle_receipts import (
            LifecycleMutationCompleted,
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
            version = (
                self.version.bump()
                if provenance.changed
                else self.version.read()
            )
            _refresh_projection(
                self.projection, self.registry, self.liveness, version
            )
            base = AgentMutationCompleted(
                correlation_id=request.correlation_id,
                generation=self.generation,
                version=version,
                operation=operation,
                changed=provenance.changed,
                agent=None if agent is None else _agent_projection(agent, version),
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
        except (AgentError, AgentHomeError, AgentAlreadyRunning) as error:
            self._reject(
                request, str(getattr(error, "code", "AGENT_ERROR")), str(error)
            )
        except (TypeError, ValueError) as error:
            self._reject(request, ipc_errors.INVALID_ARGUMENT, str(error))

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
                config=command.config,
                provider=command.provider,
                model=command.model,
                capabilities=dict(command.capabilities),
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
        agent = self.registry.save(
            Agent(
                uri=existing.uri,
                actor=existing.actor,
                owner=existing.owner,
                machine=existing.machine,
                entity_token=existing.entity_token,
                cwd=command.cwd,
                config=command.config,
                provider=command.provider,
                model=command.model,
                capabilities=dict(command.capabilities),
                harness_args=dict(command.harness_args),
                preferred_harness=command.preferred_harness,
                last_harness=command.last_harness,
                last_session_id=command.last_session_id,
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
            agent = self.registry.require(agent.actor)
        if changed:
            self.registry.record_external_binding(
                agent.uri,
                harness=command.harness,
                runtime=command.runtime,
                session_id=command.session_id,
            )
        version = self.version.bump() if changed else self.version.read()
        self._completed(
            command,
            operation="bind",
            changed=changed,
            version=version,
            agent=agent,
            binding=binding,
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
        self.registry.pin(command.adapter, agent.actor)
        # Explicit pin mutation consumes any unresolved schema-v1 staging row;
        # otherwise a later migration could resurrect the old value.
        if self.desired_state is not None:
            self.desired_state.remove_channel_pin(command.adapter)
        refreshed = self.registry.require(agent.actor)
        changed = before != refreshed.uri
        version = self.version.bump() if changed else self.version.read()
        self._completed(
            command,
            operation="pin",
            changed=changed,
            version=version,
            agent=refreshed,
        )

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
        self.registry.unpin(command.adapter)
        legacy = None
        if self.desired_state is not None:
            _state, legacy = self.desired_state.remove_channel_pin(command.adapter)
        changed = before is not None or legacy is not None
        version = self.version.bump() if changed else self.version.read()
        self._completed(
            command,
            operation="unpin",
            changed=changed,
            version=version,
            agent=None if actor is None else self.registry.get(actor),
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
    ) -> None:
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
                agent=None if agent is None else _agent_projection(agent, version),
                binding=(None if binding is None else _binding_projection(binding)),
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
    ) -> None:
        self._registry = registry
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
        self._seed_bindings()
        _refresh_projection(
            self._projection,
            self._registry,
            self._liveness,
            self._version.read(),
        )

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
            )

        self._handle = self._runtime.start(
            ActorSpec(
                name="agent-registry-authority",
                handler_factory=factory,
                mailbox_capacity=mailbox_capacity,
            )
        )

    @property
    def generation(self) -> int:
        return self._runtime.snapshot(self._handle).generation

    @property
    def version(self) -> int:
        return self._version.read()

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
        return self._runtime.drain(timeout)

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


def _agent_projection(agent: Agent, version: int) -> AgentProjection:
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
        pinned_adapters=agent.pinned_adapters,
        created_at_ms=agent.created_at_ms,
        hosted_by=agent.hosted_by,
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
            _agent_projection(agent, version) for agent in registry.list()
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
