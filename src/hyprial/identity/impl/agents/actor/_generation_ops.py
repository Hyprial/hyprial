from __future__ import annotations

from hyprial.identity.impl.agents.actor.ports import AcquireAgentRuntimeLaunchCommand
from hyprial.identity.impl.agents.registry._base import Agent
from hyprial.identity.impl.agents.state.liveness import AgentAlreadyRunning
from hyprial.identity.impl.agents.state.liveness import AgentBinding
from hyprial.identity.impl.agents.actor.ports import AgentCommand
from hyprial.identity.impl.agents.actor.ports import AgentDestroyDisposition
from hyprial.identity.impl.agents.actor.ports import AgentDestroyReservationCompleted
from hyprial.identity.impl.agents.actor.ports import AgentDestroySettled
from hyprial.identity.impl.agents.registry._base import AgentError
from hyprial.identity.impl.agents.actor.ports import AgentEvent
from hyprial.identity.impl.agents.actor.ports import AgentHarnessArgsUpdated
from hyprial.identity.impl.agents.home.provisioner import AgentHomeError
from hyprial.identity.impl.agents.actor.ports import AgentMutationCompleted
from hyprial.identity.impl.agents.actor.ports import AgentRuntimeLaunchLeaseCompleted
from hyprial.identity.impl.agents.actor.ports import BindAgentCommand
from hyprial.identity.impl.agents.actor.ports import CreateAgentCommand
from hyprial.identity.impl.agents.actor.ports import DestroyAgentCommand
from hyprial.kernel import PortCommandRejected
from hyprial.identity.impl.agents.state.liveness import RUNTIME_INTERACTIVE
from hyprial.identity.impl.agents.actor.ports import RecordAgentActivityCommand
from hyprial.identity.impl.agents.actor.ports import RecordAgentActivityIfBindingCommand
from hyprial.identity.impl.agents.actor.ports import ReleaseAgentCommand
from hyprial.identity.impl.agents.actor.ports import ReleaseAgentDestroyReservationCommand
from hyprial.identity.impl.agents.actor.ports import ReleaseAgentRuntimeLaunchCommand
from hyprial.identity.impl.agents.actor.ports import ReserveAgentDestroyCommand
from hyprial.identity.impl.agents.actor.ports import SettleAgentDestroyCommand
from hyprial.identity.impl.agents.actor.ports import BindAgentConfigCommand
from hyprial.identity.impl.agents.actor.ports import UpdateAgentCommand
from hyprial.identity.impl.agents.actor.ports import SetHarnessArgsCommand
from hyprial.identity.impl.agents.actor.ports import RenameAgentCommand
from hyprial.kernel import ipc_errors
from hyprial.identity.impl.agents.registry.config_binding import agent_config_digest

from ._base import (
    AgentLaunchInProgress,
    AgentRuntimeContextStale,
    AgentRuntimeLaunchMismatch,
    AgentUpdateConflict,
    _agent_projection,
    _binding_projection,
    _patch_agent_projection,
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
            current = self.registry.require(fence.actor)
            if agent_config_digest(current.config) != fence.config_digest:
                raise AgentRuntimeContextStale(
                    "runtime context config binding is stale"
                )
        except (AgentError, AgentHomeError) as error:
            if fence is not None:
                launches.retire_context(command.launch_token, fence)
            self._reject(
                command, str(getattr(error, "code", ipc_errors.AGENT_RUNTIME_CONTEXT_STALE)),
                str(error),
            )
            return
        try:
            lease_token, lease = launches.commit_lease(
                command.launch_token, fence, command.correlation_id
            )
        except (AgentError, AgentHomeError) as error:
            self._reject(
                command, str(getattr(error, "code", ipc_errors.AGENT_RUNTIME_CONTEXT_STALE)),
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
            actor = self.registry.actor_key(command.actor)
            self.registry.require(actor)
            token = launches.reserve_destroy(actor)
        except (AgentError, AgentHomeError) as error:
            self._reject(
                command,
                str(getattr(error, "code", ipc_errors.AGENT_LAUNCH_IN_PROGRESS)),
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
                    raise AgentAlreadyRunning(
                        agent.uri, incumbent, payload.harness, name=agent.actor
                    )
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
                        agent.uri, incumbent, command.launch_harness, name=agent.actor
                    )
        else:
            # The incumbent check precedes the INSERT.  A rejected
            # create+launch must never leave a partial agent row behind.
            candidate_uri = self.registry.uri_for(name)
            if command.launch_harness is not None:
                incumbent = self.liveness.live_binding(candidate_uri)
                if incumbent is not None:
                    raise AgentAlreadyRunning(
                        candidate_uri, incumbent, command.launch_harness, name=name
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
    def _bind_agent_config(self, command: BindAgentConfigCommand) -> None:
        agent = self.registry.bind_agent_config(
            command.name, expected_entity_token=command.expected_entity_token,
            config=command.config_payload(),
            expected_current_config_digest=command.expected_current_config_digest,
        )
        changed = agent_config_digest(agent.config) != command.expected_current_config_digest
        self._completed(
            command, operation="bind-config", changed=changed,
            version=self.version.bump() if changed else self.version.read(), agent=agent,
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
                created_at_ms=existing.created_at_ms,
                hosted_by=existing.hosted_by,
                display_name=existing.display_name,
            ),
            preserve_harness_args=command.preserve_harness_args,
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
    def _set_harness_args(self, command: SetHarnessArgsCommand) -> None:
        mutation = self.registry.set_harness_args(
            command.actor,
            harness=command.harness,
            args=command.args,
            expected_harness_args=dict(command.expected_harness_args),
            operator=command.operator,
            reason=command.reason,
            correlation_id=command.correlation_id,
            confirmed_dangerous=command.confirmed_dangerous,
        )
        version = (
            self.version.bump() if mutation.changed else self.version.read()
        )
        _refresh_projection(
            self.projection, self.registry, self.liveness, version
        )
        self._publish(
            AgentHarnessArgsUpdated(
                command.correlation_id,
                self.generation,
                version,
                mutation.changed,
                _agent_projection(mutation.agent, version, self.registry),
                mutation.audit_id,
            )
        )
    def _rename(self, command: RenameAgentCommand) -> None:
        before = self.registry.require(command.name)
        agent = self.registry.rename(command.name, command.display_name)
        changed = agent != before
        self._completed(
            command, operation="rename", changed=changed,
            version=self.version.bump() if changed else self.version.read(),
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
            raise AgentAlreadyRunning(
                agent.uri, incumbent, command.harness, name=agent.actor
            )
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
        if activity_changed and not changed:
            _patch_agent_projection(
                self.projection, self.registry, agent, version
            )
            refresh_projection = False
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
        projected = self.projection.read_agent(command.actor)
        if projected is not None:
            self.liveness.touch(projected.uri)
        self._complete_activity(command, operation="activity")

    def _record_activity_if_binding(
        self, command: RecordAgentActivityIfBindingCommand
    ) -> None:
        projected = self.projection.read_agent(command.actor)
        actor = command.actor if projected is None else projected.uri
        matched, committed = self.liveness.if_binding_current(
            actor,
            expected_revision=command.expected_binding_revision,
            harness=command.harness,
            runtime=command.runtime,
            session_id=command.session_id,
            effect=command.on_match,
        )
        if not matched or not committed:
            self._completed(
                command,
                operation=(
                    "activity-binding-mismatch"
                    if not matched
                    else "activity-binding-aborted"
                ),
                changed=False,
                version=self.version.read(),
                refresh_projection=False,
            )
            return
        self.liveness.touch(actor)
        self._complete_activity(
            command, operation="activity-binding-matched"
        )

    def _complete_activity(
        self,
        command: RecordAgentActivityCommand | RecordAgentActivityIfBindingCommand,
        *,
        operation: str,
    ) -> None:
        changed = self.registry.record_activity(command.actor)
        agent = self.registry.get(command.actor) if changed else None
        version = self.version.bump() if changed else self.version.read()
        if changed and agent is not None:
            _patch_agent_projection(
                self.projection, self.registry, agent, version
            )
        self._completed(
            command,
            operation=operation,
            changed=changed,
            version=version,
            agent=agent,
            refresh_projection=False,
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
