from __future__ import annotations
from dataclasses import dataclass

from hyprial.identity.impl.agents.actor.ports import AcquireAgentRuntimeLaunchCommand
from hyprial.kernel import ActorRuntime
from hyprial.kernel import ActorSpec
from hyprial.kernel import AdmissionResult
from hyprial.identity.impl.agents.registry._base import Agent
from hyprial.identity.impl.agents.state.read_port import AgentActivityHints
from hyprial.identity.impl.agents.actor.ports import AgentBindingProjection
from hyprial.identity.impl.agents.home.config import AgentConfig
from hyprial.identity.impl.agents.registry._base import AgentError
from hyprial.identity.impl.agents.home.effects import AgentHomeEffectCompleted
from hyprial.identity.impl.agents.home.effects import AgentHomeEffectLane
from hyprial.identity.impl.agents.home.effects import HomeRuntimePreparer
from hyprial.identity.impl.agents.state.liveness import AgentLiveness
from hyprial.identity.impl.agents.actor.ports import AgentProjection
from hyprial.identity.impl.agents.state.read_port import AgentReadEffectPort
from hyprial.identity.impl.agents.registry._core import AgentRegistry
from hyprial.identity.impl.agents.actor.ports import AgentResolveProjection
from hyprial.identity.impl.agents.runtime.context import AgentRuntimeContext
from hyprial.identity.impl.agents.actor.ports import AgentRuntimeLaunchCustodyProjection
from hyprial.identity.impl.agents.actor.ports import AgentRuntimeProjection
from hyprial.identity.impl.agents.runtime.context import AgentToolProfile
from hyprial.kernel import CallCostCounters
from collections.abc import Callable, Mapping
from hyprial.identity.impl.agents.runtime.grants import CapabilityGrant
from hyprial.identity.impl.agents.actor.ports import ConfirmAgentLifecycleReceiptCommand
from hyprial.kernel import DrainReport
from hyprial.kernel import EffectCompleted
from hyprial.kernel import EffectLane
from hyprial.kernel import EffectRequest
from hyprial.identity.impl.agents.runtime.grants import GrantJournalEntry
from hyprial.identity.impl.agents.home.provisioner import HomePayloadFile
from hyprial.identity.impl.agents.home.provisioner import HomeReceipt
from hyprial.identity.impl.agents.migration.entry import MigrationAuthorizationWindow
from hyprial.identity.impl.agents.migration.actor import MigrationEffect
from hyprial.identity.impl.agents.migration.actor import MigrationFailure
from hyprial.identity.impl.agents.migration.actor import MigrationOutcome
from hyprial.identity.impl.agents.migration.actor import MigrationOwnerCall
from hyprial.identity.impl.agents.migration.actor import MigrationOwnerResult
from hyprial.identity.impl.agents.migration.entry import MigrationPreflightManifest
from hyprial.identity.impl.agents.migration.actor import MigrationRequest
from collections import OrderedDict
from hyprial.kernel import PortAdmission
from hyprial.kernel import PortCommandRejected
from hyprial.identity.impl.agents.state.liveness import RUNTIME_INTERACTIVE
from hyprial.identity.impl.agents.actor.ports import ReleaseAgentDestroyReservationCommand
from hyprial.identity.impl.agents.actor.ports import ReleaseAgentRuntimeLaunchCommand
from hyprial.identity.impl.agents.actor.ports import RetireAgentLifecycleReceiptCommand
from hyprial.identity.impl.agents.runtime.secrets import SecretGrant
from hyprial.identity.impl.agents.actor.ports import UnpinAgentAdapterCommand
from hyprial.identity.impl.agents.home.provisioner import WorkspaceSummary
from hyprial.kernel import canonical_agent_uri
from hyprial.kernel import parse_agent_uri
from hyprial.identity.impl.agents.migration.actor import run_migration_effect
import threading
import time
from uuid import uuid4

from ._base import (
    AgentHomeAccessError,
    AgentHomeAccessPending,
    AgentMigrationBusy,
    SenderIdentityError,
    _AGENT_COST_KEYS,
    _AgentProjectionState,
    _AgentStateEffectCompleted,
    _ConfirmLandingHomeCommand,
    _DesiredStatePort,
    _HomeCall,
    _LegacyRuntimeResult,
    _MaterialiseLandingHomeCommand,
    _MigrationWaiter,
    _PendingHomeAccess,
    _PendingHomeMutation,
    _PendingPinMutation,
    _PinStateRequest,
    _PinStateResult,
    _PrepareRuntimeCommand,
    _PrepareWorkspaceCommand,
    _RuntimeLaunchState,
    _Version,
    _binding_projection,
    _publish,
    _refresh_projection,
)
from ._generation_dispatch import _GenerationDispatchMixin
from ._generation_ops import _GenerationOpsMixin
@dataclass(slots=True)

class _AgentGeneration(_GenerationDispatchMixin, _GenerationOpsMixin):
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
        [str, str | AgentRuntimeContext | _LegacyRuntimeResult | None, BaseException | None], None
    ] | None = None
    state_effects: EffectLane[_PinStateRequest, _PinStateResult] | None = None
    pending_pins: dict[tuple[str, int], _PendingPinMutation] | None = None
    pending_pin_adapters: set[str] | None = None
    runtime_launches: _RuntimeLaunchState | None = None
    migration_begin: Callable[[MigrationRequest], None] | None = None
    migration_owner_call: Callable[[MigrationOwnerCall], None] | None = None
    migration_completed: Callable[[EffectCompleted[MigrationOutcome]], None] | None = None
    migration_conflicts: Callable[[object], bool] | None = None


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
        home_runtime_preparers: Mapping[str, HomeRuntimePreparer | None] | None = None,
    ) -> None:
        self._registry = registry
        # Owned by the actor, not a generation: totals survive restarts.
        self._command_costs = CallCostCounters(_AGENT_COST_KEYS, wall=False)
        self._session_originated = session_originated
        self._home_runtime_preparers = home_runtime_preparers
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
                    runtime_preparers=self._home_runtime_preparers,
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
        if isinstance(command, (_PrepareWorkspaceCommand, _PrepareRuntimeCommand, _MaterialiseLandingHomeCommand, _ConfirmLandingHomeCommand)):
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
        value: str | AgentRuntimeContext | _LegacyRuntimeResult | None,
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

    def read_session_ref_retired(self, actor: str, session_ref: str) -> bool:
        return self._registry.is_session_ref_retired(actor, session_ref)

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
        legacy_reporter: Callable[[str, str], None] | None = None,
    ) -> AgentRuntimeContext | None:
        result = self._call_home_access(_PrepareRuntimeCommand(
            uuid4().hex,
            actor,
            harness,
            cwd,
            tool_profile,
            containerized,
        ))
        if isinstance(result, _LegacyRuntimeResult):
            # Emit only after owner settlement, using its immutable decision;
            # a later projection update cannot rewrite the observed branch.
            if legacy_reporter is not None:
                legacy_reporter(result.actor, result.reason)
            return None
        if result is not None and not isinstance(result, AgentRuntimeContext):
            raise AgentHomeAccessError("runtime preparation returned an invalid context")
        return result

    def materialise_landing_home(
        self, actor: str, staged_root: str, *, expected_entity_token: str,
        expected_files: tuple[HomePayloadFile, ...],
    ) -> str:
        result = self._call_home_access(_MaterialiseLandingHomeCommand(
            uuid4().hex, actor, expected_entity_token, staged_root, expected_files,
        ))
        if not isinstance(result, str):
            raise AgentHomeAccessError("landing materialisation returned no home")
        return result

    def confirm_landing_home(self, actor: str, *, expected_entity_token: str) -> str:
        result = self._call_home_access(_ConfirmLandingHomeCommand(
            uuid4().hex, actor, expected_entity_token,
        ))
        if not isinstance(result, str):
            raise AgentHomeAccessError("landing confirmation returned no home")
        return result

    def _call_home_access(
        self,
        command: _PrepareWorkspaceCommand | _PrepareRuntimeCommand | _MaterialiseLandingHomeCommand | _ConfirmLandingHomeCommand,
        timeout: float = 70.0,
    ) -> str | AgentRuntimeContext | _LegacyRuntimeResult | None:
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
            raise AgentHomeAccessPending(
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
        if not home_complete or not state_complete:
            # Keep the owner available to ACK accepted effects. A later close
            # can finish once those exact receipts have settled.
            return DrainReport(False, time.monotonic() - started, (self._handle,))
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
AgentRegistryActor = AgentActor
