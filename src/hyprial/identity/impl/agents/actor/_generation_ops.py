from __future__ import annotations

from hyprial.identity.impl.agents.actor.ports import AcquireAgentRuntimeLaunchCommand
from hyprial.kernel import AdmissionResult
from hyprial.identity.impl.agents.registry._base import Agent
from hyprial.identity.impl.agents.state.liveness import AgentAlreadyRunning
from hyprial.identity.impl.agents.state.liveness import AgentBinding
from hyprial.identity.impl.agents.actor.ports import AgentCommand
from hyprial.identity.impl.agents.actor.ports import AgentDestroyDisposition
from hyprial.identity.impl.agents.actor.ports import AgentDestroyReservationCompleted
from hyprial.identity.impl.agents.actor.ports import AgentDestroySettled
from hyprial.identity.impl.agents.registry._base import AgentError
from hyprial.identity.impl.agents.actor.ports import AgentEvent
from hyprial.identity.impl.agents.home.provisioner import AgentHomeError
from hyprial.identity.impl.agents.actor.ports import AgentMutationCompleted
from hyprial.identity.impl.agents.actor.ports import AgentRuntimeLaunchLeaseCompleted
from hyprial.identity.impl.agents.actor.ports import BindAgentCommand
from hyprial.identity.impl.agents.actor.ports import CreateAgentCommand
from hyprial.identity.impl.agents.actor.ports import DestroyAgentCommand
from hyprial.kernel import EffectRequest
from hyprial.identity.impl.agents.actor.ports import PinAgentAdapterCommand
from hyprial.kernel import PortCommandRejected
from hyprial.identity.impl.agents.state.liveness import RUNTIME_INTERACTIVE
from hyprial.identity.impl.agents.actor.ports import RecordAgentActivityCommand
from hyprial.identity.impl.agents.actor.ports import ReleaseAgentCommand
from hyprial.identity.impl.agents.actor.ports import ReleaseAgentDestroyReservationCommand
from hyprial.identity.impl.agents.actor.ports import ReleaseAgentRuntimeLaunchCommand
from hyprial.identity.impl.agents.actor.ports import ReserveAgentDestroyCommand
from hyprial.identity.impl.agents.actor.ports import SettleAgentDestroyCommand
from hyprial.identity.impl.agents.actor.ports import UnpinAgentAdapterCommand
from hyprial.identity.impl.agents.actor.ports import UpdateAgentCommand
from hyprial.kernel import ipc_errors

from ._base import (
    AgentLaunchInProgress,
    AgentRuntimeLaunchMismatch,
    AgentUpdateConflict,
    _AgentStateEffectCompleted,
    _PendingPinMutation,
    _PinStateRequest,
    _agent_projection,
    _binding_projection,
    _publish,
    _refresh_projection,
)


class _GenerationOpsMixin:
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
            lease.secret_grants,
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
        from hyprial.kernel import LifecycleMutationRequest

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
                launches = self.runtime_launches
                if launches is not None and launches.destroy_reserved(agent.actor):
                    raise AgentLaunchInProgress("agent destroy is already reserved")
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
        from hyprial.kernel import MutationProvenance
        from hyprial.kernel import LifecycleMutationCompleted
        from hyprial.kernel import LifecycleMutationRequest

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
        launches = self.runtime_launches
        if launches is not None and launches.destroy_reserved(agent.actor):
            raise AgentLaunchInProgress("agent destroy is already reserved")
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
        launches = self.runtime_launches
        if launches is not None and launches.destroy_reserved(agent.actor):
            raise AgentLaunchInProgress("agent destroy is already reserved")
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
