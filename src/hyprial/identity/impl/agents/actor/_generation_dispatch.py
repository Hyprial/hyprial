from __future__ import annotations

from hyprial.identity.impl.agents.actor.ports import AcquireAgentRuntimeLaunchCommand
from hyprial.kernel import AdmissionResult
from hyprial.identity.impl.agents.state.liveness import AgentAlreadyRunning
from hyprial.identity.impl.agents.actor.ports import AgentCapabilityGrantCompleted
from hyprial.identity.impl.agents.registry._base import AgentError
from hyprial.identity.impl.agents.home.effects import AgentHomeEffect
from hyprial.identity.impl.agents.home.effects import AgentHomeEffectCompleted
from hyprial.identity.impl.agents.home.provisioner import AgentHomeError
from hyprial.identity.impl.agents.registry._base import AgentHomeReservation
from hyprial.identity.impl.agents.actor.ports import AgentLifecycleReceiptCompleted
from hyprial.identity.impl.agents.runtime.context import AgentRuntimeContext
from hyprial.identity.impl.agents.actor.ports import AgentSecretGrantCompleted
from hyprial.identity.impl.agents.actor.ports import BindAgentCommand
from hyprial.identity.impl.agents.actor.ports import BlockAgentCommand
from hyprial.identity.impl.agents.actor.ports import CleanupRevokedAgentHomeCommand
from hyprial.identity.impl.agents.actor.ports import ClearRestoreDispositionCommand
from hyprial.identity.impl.agents.actor.ports import ConfirmAgentLifecycleReceiptCommand
from hyprial.identity.impl.agents.actor.ports import CreateAgentCommand
from hyprial.identity.impl.agents.actor.ports import CreateHostInvitedAgentCommand
from hyprial.identity.impl.agents.actor.ports import CreateTransferHostedAgentCommand
from hyprial.identity.impl.agents.actor.ports import DestroyAgentCommand
from hyprial.kernel import EffectCompleted
from hyprial.identity.impl.agents.actor.ports import GrantAgentCapabilityCommand
from hyprial.identity.impl.agents.actor.ports import GrantAgentSecretCommand
from hyprial.identity.impl.agents.home.effects import MaterialiseLandingHome
from hyprial.identity.impl.agents.migration.actor import MigrationOwnerCall
from hyprial.identity.impl.agents.migration.actor import MigrationRequest
from hyprial.identity.impl.agents.actor.ports import PinAgentAdapterCommand
from hyprial.identity.impl.agents.home.effects import PrepareRuntimeHome
from hyprial.identity.impl.agents.home.effects import PrepareWorkspaceHome
from hyprial.identity.impl.agents.actor.ports import RecordAgentActivityCommand
from hyprial.identity.impl.agents.actor.ports import ReleaseAgentCommand
from hyprial.identity.impl.agents.actor.ports import ReleaseAgentDestroyReservationCommand
from hyprial.identity.impl.agents.actor.ports import ReleaseAgentRuntimeLaunchCommand
from hyprial.identity.impl.agents.actor.ports import ReserveAgentDestroyCommand
from hyprial.identity.impl.agents.actor.ports import RecordAgentSessionRefCommand
from hyprial.identity.impl.agents.actor.ports import RetireAgentSessionRefsCommand
from hyprial.identity.impl.agents.actor.ports import RetireAgentLifecycleReceiptCommand
from hyprial.identity.impl.agents.actor.ports import RollbackRetiredAgentSessionRefsCommand
from hyprial.identity.impl.agents.actor.ports import RevokeAgentCapabilityCommand
from hyprial.identity.impl.agents.actor.ports import RevokeAgentSecretCommand
from hyprial.identity.impl.agents.actor.ports import SetRestoreDispositionCommand
from hyprial.identity.impl.agents.actor.ports import SettleAgentDestroyCommand
from hyprial.identity.impl.agents.actor.ports import UnblockAgentCommand
from hyprial.identity.impl.agents.actor.ports import UnpinAgentAdapterCommand
from hyprial.identity.impl.agents.actor.ports import UpdateAgentCommand
from hyprial.identity.impl.agents.runtime.context import agent_home_mode
from hyprial.identity.impl.agents.runtime.context import build_agent_runtime_preparation
from hyprial.kernel import ipc_errors
import sqlite3
import time

from ._base import (
    AGENT_COST_ORIGIN_OTHER,
    AGENT_COST_ORIGIN_SESSION,
    AgentHomeAccessError,
    AgentUpdateConflict,
    _AGENT_COST_TYPES,
    _AgentStateEffectCompleted,
    _ConfirmLandingHomeCommand,
    _LegacyRuntimeResult,
    _MaterialiseLandingHomeCommand,
    _PendingHomeAccess,
    _PendingHomeMutation,
    _PrepareRuntimeCommand,
    _PrepareWorkspaceCommand,
)


class _GenerationDispatchMixin:
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
        from hyprial.kernel import LifecycleMutationRequest

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
        if isinstance(command, (_PrepareWorkspaceCommand, _PrepareRuntimeCommand, _MaterialiseLandingHomeCommand, _ConfirmLandingHomeCommand)):
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
                RecordAgentSessionRefCommand,
                RetireAgentSessionRefsCommand,
                RollbackRetiredAgentSessionRefsCommand,
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
            elif isinstance(command, RecordAgentSessionRefCommand):
                self.registry.record_session_ref(
                    command.actor, command.entity_token, command.session_ref
                )
                self._completed(
                    command,
                    operation="record-session-ref",
                    changed=True,
                    version=self.version.bump(),
                )
            elif isinstance(command, RetireAgentSessionRefsCommand):
                changed = bool(
                    self.registry.retire_session_refs(
                        command.actor,
                        command.entity_token,
                        command.session_refs,
                        reason=command.reason,
                        destroy_attempt=command.destroy_attempt,
                    )
                )
                self._completed(
                    command,
                    operation="retire-session-refs",
                    changed=changed,
                    version=self.version.bump() if changed else self.version.read(),
                )
            elif isinstance(command, RollbackRetiredAgentSessionRefsCommand):
                changed = bool(
                    self.registry.rollback_retired_session_refs(
                        command.destroy_attempt
                    )
                )
                self._completed(
                    command,
                    operation="rollback-retired-session-refs",
                    changed=changed,
                    version=self.version.bump() if changed else self.version.read(),
                )
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
                    expected_entity_token=command.expected_entity_token,
                )
                self._publish(AgentCapabilityGrantCompleted(
                    command.correlation_id, self.generation, self.version.bump(),
                    "grant", True, grant,
                ))
            elif isinstance(command, RevokeAgentCapabilityCommand):
                changed = self.registry.revoke_capability(
                    command.actor, command.grant_id,
                    revoked_by=command.revoked_by,
                    expected_entity_token=command.expected_entity_token,
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
        from hyprial.kernel import LifecycleMutationRequest

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
        self, command: _PrepareWorkspaceCommand | _PrepareRuntimeCommand | _MaterialiseLandingHomeCommand | _ConfirmLandingHomeCommand
    ) -> None:
        lane = self.home_effects
        pending_home = self.pending_home
        complete = self.complete_home_call
        assert lane is not None and pending_home is not None and complete is not None
        if isinstance(command, _ConfirmLandingHomeCommand):
            try:
                home = self.registry.confirm_landing_home(
                    command.actor, expected_entity_token=command.expected_entity_token,
                )
            except BaseException as error:
                complete(command.correlation_id, None, error)
            else:
                complete(command.correlation_id, home, None)
            return
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
            elif isinstance(command, _MaterialiseLandingHomeCommand):
                receipt = self.registry.home_receipt(
                    command.actor, expected_entity_token=command.expected_entity_token,
                    validate_mirror=False,
                )
                plan = MaterialiseLandingHome(receipt, command.staged_root, command.expected_files)
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
                    agent = self.registry.require(command.actor)
                    _mode, reason = agent_home_mode(agent, command.harness)
                    assert reason is not None
                    complete(
                        command.correlation_id,
                        _LegacyRuntimeResult(agent.uri, reason),
                        None,
                    )
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
                    if isinstance(pending.command, (_PrepareWorkspaceCommand, _MaterialiseLandingHomeCommand))
                    else event.runtime_context
                )
                if value is None:
                    raise AgentHomeAccessError(
                        "agent home effect returned no prepared value"
                    )
                if isinstance(value, AgentRuntimeContext):
                    launches = self.runtime_launches
                    assert launches is not None
                    grants = tuple(
                        (grant.grant_id, grant.revision)
                        for grant in self.registry.secret_inventory(value.actor)
                    )
                    value = launches.register_context(
                        value, pending.receipt, grants
                    )
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
            from hyprial.kernel import LifecycleMutationRequest
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
