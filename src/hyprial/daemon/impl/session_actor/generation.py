"""The agent-effect worker and session generation: replay, custody and effect execution."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from hyprial.kernel import (
    AdmissionResult,
)
from hyprial.kernel import EffectCompleted
from hyprial.kernel import ipc_errors
from hyprial.kernel import PortCommandRejected
from hyprial.kernel import SESSION_CARRIER_SOURCES
from hyprial.daemon.impl.desired_state  import (
    DesiredStateStore,
    InteractiveSession,
    PendingSessionAgentEffect,
)
from hyprial.daemon.impl.desired_state_io  import (
    DesiredStateIoCompleted,
    DesiredStateIoRequest,
    DesiredStateOperation,
)
from hyprial.kernel import CallCostCounters
from hyprial.daemon.impl.operations.session_ports  import (
    HeartbeatSessionCommand,
    RefreshSessionCommand,
    RegisterSessionCommand,
    SessionEvent,
    SessionLeaseElapsedCommand,
    SessionLeaseExpired,
    SessionLeaseSweepCompleted,
    SessionMutationCompleted,
    SessionMutationProjection,
    UnregisterSessionCommand,
)
from hyprial.daemon.impl.state_persistence  import StateCostOrigin

from .internals import (
    SessionOwnershipError,
    _AgentEffectResult,
    _AgentEffectUnavailable,
    _PendingMutation,
    _SessionProjectionState,
    _SessionRuntimeState,
    _Version,
    _bind_effect,
    _lease_digest_for_registration,
    _publish,
    _release_effect,
    _required,
    _verify_lease,
    owner_only_relocation,
)


@dataclass(slots=True)
class _SessionGeneration:
    generation: int
    store: DesiredStateStore
    daemon_epoch: str
    events: object
    request_effect: Callable[
        [PendingSessionAgentEffect, int, StateCostOrigin], bool
    ]
    retire_effect: Callable[[str, str], None]
    effect_cost_origin: Callable[[str, str], StateCostOrigin]
    version: _Version
    clock_ms: Callable[[], int]
    lease_ttl_seconds: float
    runtime_projection: _SessionRuntimeState
    session_projection: _SessionProjectionState
    persist: Callable[[DesiredStateIoRequest], AdmissionResult]
    acknowledge_persistence: Callable[[DesiredStateIoRequest], bool]
    redeliver_persistence: Callable[[DesiredStateIoRequest], None]
    deferred_persistence: dict[str, DesiredStateIoRequest]
    deferred_capacity: int
    command_costs: CallCostCounters | None = None
    _mutations: dict[str, _PendingMutation] = field(default_factory=dict)
    _effect_owners: dict[str, str] = field(default_factory=dict)
    # Durably bound (actor, session_ref) pairs; their heartbeats bind lightly.
    _bound_sessions: set[tuple[str, str]] = field(default_factory=set)
    _volatile_effects: dict[str, PendingSessionAgentEffect] = field(default_factory=dict)

    def __call__(self, command: object) -> None:
        """Dispatch one command, charging this actor thread's CPU to its type.

        This runs on the session actor's own thread, so the thread-CPU delta
        is exactly the session side of a request: the IPC thread that sent
        it is parked in ``call_session`` meanwhile and records only its own
        CPU (the ipc_stats attribution rule).  Queue wait is not measured --
        it is not CPU.
        """

        costs = self.command_costs
        failed = True
        started_cpu = time.thread_time() if costs is not None and costs.enabled else 0.0
        try:
            self._dispatch(command)
            failed = False
        finally:
            if costs is not None and costs.enabled:
                costs.record(
                    type(command).__name__,
                    cpu_seconds=time.thread_time() - started_cpu,
                    error=failed,
                )

    def _dispatch(self, command: object) -> None:
        from hyprial.daemon.impl.lifecycle.lifecycle_receipts import (
            ConfirmLifecycleReceiptCommand,
            RetireLifecycleReceiptCommand,
        )
        from hyprial.kernel import (
            LifecycleMutationRequest,
            )

        if isinstance(command, RetireLifecycleReceiptCommand):
            self._persist(
                DesiredStateOperation.RETIRE_LIFECYCLE_RECEIPT,
                command,
                ("session", command.attempt_token, command.resource_token),
                context=("receipt", command, "retire"),
                cost_origin=StateCostOrigin.BACKGROUND,
            )
            return
        if isinstance(command, ConfirmLifecycleReceiptCommand):
            self._persist(
                DesiredStateOperation.CONFIRM_LIFECYCLE_RECEIPT_RETIRED,
                command,
                ("session", command.attempt_token, command.resource_token),
                context=("receipt", command, "confirm"),
                cost_origin=StateCostOrigin.BACKGROUND,
            )
            return

        if isinstance(command, LifecycleMutationRequest):
            self._lifecycle(command)
            return
        if isinstance(command, EffectCompleted) and isinstance(
            command.result, DesiredStateIoCompleted
        ):
            try:
                self._persistence_completed(command.result)
            except BaseException:
                self.redeliver_persistence(command.result.request)
                raise
            self.acknowledge_persistence(command.result.request)
            self._pump_deferred_persistence()
            return
        if isinstance(command, _AgentEffectResult):
            self._effect_result(command)
            return
        if isinstance(command, _AgentEffectUnavailable):
            self._effect_unavailable(command)
            return
        if not isinstance(
            command,
            (
                RegisterSessionCommand,
                RefreshSessionCommand,
                HeartbeatSessionCommand,
                UnregisterSessionCommand,
                SessionLeaseElapsedCommand,
            ),
        ):
            self._reject(
                command, ipc_errors.INVALID_ARGUMENT, "unsupported session command"
            )
            return
        try:
            if isinstance(command, RegisterSessionCommand):
                self._register(command)
            elif isinstance(command, RefreshSessionCommand):
                self._refresh(command)
            elif isinstance(command, HeartbeatSessionCommand):
                self._heartbeat(command)
            elif isinstance(command, UnregisterSessionCommand):
                self._unregister(command)
            else:
                self._lease_elapsed(command)
        except SessionOwnershipError as error:
            self._reject(command, error.code, error.detail)
        except ValueError as error:
            self._reject(command, ipc_errors.INVALID_ARGUMENT, str(error))

    def _lifecycle(self, request: object) -> None:
        from hyprial.kernel import LifecycleMutationRequest

        assert isinstance(request, LifecycleMutationRequest)
        payload = request.payload
        if not isinstance(payload, (RegisterSessionCommand, UnregisterSessionCommand)):
            self._reject(
                request,
                ipc_errors.INVALID_ARGUMENT,
                f"unsupported Session lifecycle payload: {type(payload).__name__}",
            )
            return
        self._persist(
            DesiredStateOperation.APPLY_SESSION_LIFECYCLE,
            request,
            (request,),
            context=("lifecycle", request),
            cost_origin=StateCostOrigin.BACKGROUND,
        )

    def _register(self, command: RegisterSessionCommand) -> None:
        _required(command.correlation_id, "correlation_id")
        _required(command.actor, "actor")
        _required(command.cwd, "cwd")
        _required(command.source, "source")
        _required(command.session_ref, "session_ref")
        if not command.command or any(not item for item in command.command):
            raise ValueError("command must contain non-empty strings")
        digest = _lease_digest_for_registration(
            source=command.source,
            protocol_version=command.channel_protocol_version,
            token=command.channel_lease_token,
        )
        session = InteractiveSession(
            actor=command.actor,
            cwd=command.cwd,
            command=command.command,
            source=command.source,
            session_ref=command.session_ref,
            runtime=command.runtime,
            channel_confirmed=command.channel_confirmed,
            channel_build_version=command.channel_build_version,
            channel_protocol_version=command.channel_protocol_version,
            owner_fence=command.owner_fence,
            channel_lease_digest=digest,
            tmux_session=command.tmux_session,
            process_pid=command.process_pid,
            process_identity=command.process_identity,
        )
        if command.manage_agent:
            bind = _bind_effect(command.correlation_id, session)
            self._persist(
                DesiredStateOperation.CLAIM_INTERACTIVE_WITH_AGENT_EFFECTS,
                command,
                (session, bind),
                context=("register", command),
                cost_origin=StateCostOrigin.IPC,
            )
        else:
            self._persist(
                DesiredStateOperation.CLAIM_INTERACTIVE,
                command,
                (session,),
                context=("register", command),
                cost_origin=StateCostOrigin.IPC,
            )

    def _refresh(self, command: RefreshSessionCommand) -> None:
        current = self._owned_session(command.actor, command.session_ref)
        # ``current.actor`` is the canonical spelling of this session: after an
        # owner-only relocation it is the migrated URI, and every downstream
        # key (runtime liveness, result projection) must follow it so the
        # carrier continues as the canonical actor, not its stale spelling.
        if current.source not in SESSION_CARRIER_SOURCES:
            raise SessionOwnershipError(
                ipc_errors.INVALID_SESSION_SOURCE,
                "only a persisted session-carrier session can refresh its daemon generation",
            )
        if current.channel_lease_digest is not None:
            _verify_lease(current, command.channel_lease_token)
        effects = (
            (_bind_effect(command.correlation_id, current),)
            if command.manage_agent
            else ()
        )
        if effects and (current.actor, command.session_ref) in self._bound_sessions:
            # The heartbeat's light path: once this generation bound the
            # session durably, a repeated refresh (a channel retrying after a
            # slow answer) must not rewrite desired state again (2026-10-06).
            for effect in effects:
                self._volatile_effects[effect.effect_id] = effect
            self._finish_refresh(command, current, effects, StateCostOrigin.IPC)
        elif effects:
            self._persist(
                DesiredStateOperation.RECORD_SESSION_AGENT_EFFECTS,
                command,
                (effects,),
                context=("refresh", command, current, effects),
                cost_origin=StateCostOrigin.IPC,
            )
        else:
            self._finish_refresh(command, current, effects, StateCostOrigin.IPC)

    def _heartbeat(self, command: HeartbeatSessionCommand) -> None:
        current = self._owned_session(command.actor, command.session_ref)
        # Same canonical-actor rule as _refresh: after an owner-only
        # relocation the lease, liveness, and verdict all key on the
        # session's current spelling.
        if current.source not in SESSION_CARRIER_SOURCES:
            raise SessionOwnershipError(
                ipc_errors.INVALID_SESSION_SOURCE,
                "only a persisted session-carrier session can renew liveness",
            )
        if current.channel_lease_digest is not None:
            _verify_lease(current, command.channel_lease_token)
        if self.runtime_projection.confirmed(current.actor) != command.session_ref:
            raise SessionOwnershipError(
                ipc_errors.STALE_DAEMON_GENERATION,
                "channel must refresh this daemon generation before heartbeat",
            )
        effects = (
            (_bind_effect(command.correlation_id, current),)
            if command.manage_agent
            else ()
        )
        if effects and (current.actor, command.session_ref) in self._bound_sessions:
            # Once durably bound, a 1 Hz heartbeat's bind is only a liveness
            # touch: no custody, no desired-state rewrite (#1143, 2026-10-05).
            for effect in effects:
                self._volatile_effects[effect.effect_id] = effect
            self._finish_heartbeat(command, current, effects, StateCostOrigin.IPC)
        elif effects:
            self._persist(
                DesiredStateOperation.RECORD_SESSION_AGENT_EFFECTS,
                command,
                (effects,),
                context=("heartbeat", command, current, effects),
                cost_origin=StateCostOrigin.IPC,
            )
        else:
            self._finish_heartbeat(
                command, current, effects, StateCostOrigin.IPC
            )

    def _unregister(self, command: UnregisterSessionCommand) -> None:
        # Ending a session leaves the light path (bounded cache; #1144).
        self._bound_sessions = {
            pair for pair in self._bound_sessions if pair[1] != command.session_ref
        }
        effects = (
            (_release_effect(command.correlation_id, command.actor),)
            if command.manage_agent
            else ()
        )
        self._persist(
            DesiredStateOperation.UNREGISTER_INTERACTIVE_IF_CURRENT,
            command,
            (command.actor, command.session_ref, effects),
            context=("unregister", command, effects),
            cost_origin=StateCostOrigin.IPC,
        )

    def _lease_elapsed(self, command: SessionLeaseElapsedCommand) -> None:
        if command.generation != self.generation:
            raise SessionOwnershipError(
                "STALE_GENERATION",
                f"lease sweep generation {command.generation} does not own generation {self.generation}",
            )
        current_version = self.version.read()
        if command.version != current_version:
            raise SessionOwnershipError(
                "STALE_VERSION",
                f"lease sweep version {command.version} does not match {current_version}",
            )
        if command.observed_at_ms < 0:
            raise ValueError("observed_at_ms must not be negative")
        sessions = self.session_projection.read()[0]
        effects: list[PendingSessionAgentEffect] = []
        finals: list[SessionEvent] = []
        for session in sessions:
            if session.channel_lease_digest is None or session.session_ref is None:
                continue
            heartbeat = self.runtime_projection.heartbeat(session.actor)
            alive = (
                heartbeat is not None
                and heartbeat[0] == session.session_ref
                and max(0, command.observed_at_ms - heartbeat[1])
                <= int(self.lease_ttl_seconds * 1000)
            )
            if alive:
                continue
            effect = _release_effect(command.correlation_id, session.actor)
            effects.append(effect)
            finals.append(
                SessionLeaseExpired(
                    correlation_id=command.correlation_id,
                    generation=self.generation,
                    version=0,
                    actor=session.actor,
                    session_ref=session.session_ref,
                )
            )
        if effects:
            frozen_effects = tuple(effects)
            self._persist(
                DesiredStateOperation.RECORD_SESSION_AGENT_EFFECTS,
                command,
                (frozen_effects,),
                context=("lease", command, frozen_effects, tuple(finals), sessions),
                cost_origin=StateCostOrigin.BACKGROUND,
            )
        else:
            self._finish_lease(
                command, (), (), sessions, StateCostOrigin.BACKGROUND
            )

    def _persist(
        self,
        operation: DesiredStateOperation,
        command: object,
        args: tuple[object, ...],
        *,
        kwargs: tuple[tuple[str, object], ...] = (),
        context: object,
        cost_origin: StateCostOrigin,
    ) -> None:
        request = DesiredStateIoRequest(
            operation_id=f"session-state-{uuid.uuid4().hex}",
            owner_generation=self.generation,
            owner_version=self.version.read(),
            operation=operation,
            args=args,
            kwargs=kwargs,
            context=context,
            cost_origin=cost_origin,
        )
        admission = self.persist(request)
        if admission is AdmissionResult.ACCEPTED:
            return
        if (
            admission is AdmissionResult.OVERLOADED
            and isinstance(command, _AgentEffectResult)
            and len(self.deferred_persistence) < self.deferred_capacity
        ):
            # Retain the already completed Agent result and its custody. Only
            # retry the storage write, after this lane's next owner ACK.
            self.deferred_persistence[request.operation_id] = request
            return
        self._reject_persistence(command, admission)

    def _reject_persistence(self, command: object, admission: AdmissionResult) -> None:
        if isinstance(command, _AgentEffectResult):
            self._effect_unavailable(
                _AgentEffectUnavailable(
                    command.effect_id,
                    command.custody_token,
                    "SESSION_PERSISTENCE_OVERLOADED",
                    f"session persistence admission is {admission.value}",
                )
            )
        else:
            self._reject(
                command,
                "SESSION_PERSISTENCE_OVERLOADED",
                f"session persistence admission is {admission.value}",
            )

    def _pump_deferred_persistence(self) -> None:
        while self.deferred_persistence:
            operation_id = next(iter(self.deferred_persistence))
            request = self.deferred_persistence[operation_id]
            admission = self.persist(request)
            if admission is AdmissionResult.OVERLOADED:
                return
            del self.deferred_persistence[operation_id]
            if admission is not AdmissionResult.ACCEPTED:
                self._reject_persistence(request.context[1], admission)

    def _persistence_completed(self, completion: DesiredStateIoCompleted) -> None:
        from hyprial.daemon.impl.lifecycle.lifecycle_receipts import LifecycleReceiptCompleted

        request = completion.request
        context = request.context
        if not isinstance(context, tuple) or not context:
            raise TypeError("session persistence completion has no typed context")
        kind = context[0]
        command = context[1]
        if (
            request.owner_generation > self.generation
            or request.owner_version > self.version.read()
        ):
            self._reject(
                command,
                "SESSION_PERSISTENCE_FENCE_INVALID",
                "session persistence completion is from a future owner fence",
            )
            return
        self.session_projection.replace(
            completion.snapshot.interactive_sessions, self.version.read()
        )
        if completion.error_code is not None:
            if kind == "effect_complete" and isinstance(command, _AgentEffectResult):
                effect = self._pending_effect(command.effect_id)
                if effect is not None:
                    self._fail_effect(
                        effect,
                        command.custody_token,
                        "SESSION_PERSISTENCE_FAILED",
                        f"{completion.error_code}: {completion.error_detail}",
                    )
                    return
            self._reject(
                command,
                "SESSION_PERSISTENCE_FAILED",
                f"{completion.error_code}: {completion.error_detail}",
            )
            return
        try:
            if kind == "receipt":
                operation = context[2]
                self._publish(
                    LifecycleReceiptCompleted(
                        command.correlation_id,
                        self.generation,
                        self.version.read(),
                        "session",
                        command.attempt_token,
                        command.resource_token,
                        operation,
                        bool(completion.result),
                    )
                )
            elif kind == "lifecycle":
                self._finish_lifecycle(command, completion.result, completion.snapshot)
            elif kind == "register":
                self._finish_register(
                    command, completion.result, request.cost_origin
                )
            elif kind == "refresh":
                self._finish_refresh(
                    command, context[2], context[3], request.cost_origin
                )
            elif kind == "heartbeat":
                self._finish_heartbeat(
                    command, context[2], context[3], request.cost_origin
                )
            elif kind == "unregister":
                self._finish_unregister(
                    command, context[2], completion.result, request.cost_origin
                )
            elif kind == "lease":
                self._finish_lease(
                    command,
                    context[2],
                    context[3],
                    context[4],
                    request.cost_origin,
                )
            elif kind == "effect_complete":
                self._finish_effect_result(command)
            else:
                raise TypeError(f"unsupported session persistence context: {kind}")
        finally:
            self.session_projection.replace(
                completion.snapshot.interactive_sessions, self.version.read()
            )

    def _finish_lifecycle(
        self, request: object, result: object, snapshot: object
    ) -> None:
        from hyprial.kernel import (
            LifecycleMutationCompleted,
            LifecycleMutationRequest,
            MutationProvenance,
        )

        assert isinstance(request, LifecycleMutationRequest)
        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or not isinstance(result[0], MutationProvenance)
        ):
            raise TypeError("session lifecycle persistence returned invalid result")
        provenance, superseded = result
        payload = request.payload
        if isinstance(payload, RegisterSessionCommand):
            if provenance.changed:
                for actor in superseded:
                    self.runtime_projection.drop(actor)
            sessions = getattr(snapshot, "interactive_sessions")
            persisted = next(
                (item for item in sessions if item.actor == payload.actor), None
            )
            if persisted is not None and persisted.session_ref is not None:
                self.runtime_projection.register(
                    persisted.actor,
                    persisted.session_ref,
                    self.clock_ms(),
                    confirmed=persisted.source in SESSION_CARRIER_SOURCES,
                )
            projection = SessionMutationProjection(
                actor=payload.actor,
                session_ref=payload.session_ref,
                daemon_epoch=self.daemon_epoch if provenance.changed else None,
                registered=provenance.changed,
                superseded_actors=tuple(superseded),
            )
        elif isinstance(payload, UnregisterSessionCommand):
            if provenance.changed:
                self.runtime_projection.drop(payload.actor)
            projection = SessionMutationProjection(
                actor=payload.actor,
                session_ref=payload.session_ref,
                daemon_epoch=None,
                unregistered=provenance.changed,
            )
        else:
            raise TypeError(
                f"unsupported Session lifecycle payload: {type(payload).__name__}"
            )
        version = self.version.bump() if provenance.changed else self.version.read()
        base = SessionMutationCompleted(
            request.correlation_id, self.generation, version, projection
        )
        self._publish(
            LifecycleMutationCompleted(
                request.correlation_id,
                request.attempt_token,
                self.generation,
                version,
                "session",
                provenance,
                base,
            )
        )

    def _finish_register(
        self,
        command: object,
        result: object,
        cost_origin: StateCostOrigin,
    ) -> None:
        assert isinstance(command, RegisterSessionCommand)
        if not isinstance(result, tuple) or len(result) not in {2, 3}:
            raise TypeError("session registration persistence returned invalid result")
        superseded = tuple(result[1])
        effects = tuple(result[2]) if len(result) == 3 else ()
        for actor in superseded:
            self.runtime_projection.drop(actor)
        self.runtime_projection.register(
            command.actor,
            command.session_ref,
            self.clock_ms(),
            confirmed=command.source in SESSION_CARRIER_SOURCES,
        )
        version = self.version.bump()
        event = SessionMutationCompleted(
            command.correlation_id,
            self.generation,
            version,
            SessionMutationProjection(
                actor=command.actor,
                session_ref=command.session_ref,
                registered=True,
                daemon_epoch=self.daemon_epoch,
                channel_current_epoch=(
                    self.daemon_epoch
                    if command.source in SESSION_CARRIER_SOURCES
                    else None
                ),
                superseded_actors=superseded,
            ),
        )
        self._stage(command.correlation_id, effects, (event,), cost_origin)

    def _finish_refresh(
        self,
        command: RefreshSessionCommand,
        current: InteractiveSession,
        effects: tuple[PendingSessionAgentEffect, ...],
        cost_origin: StateCostOrigin,
    ) -> None:
        self.runtime_projection.register(
            current.actor, command.session_ref, self.clock_ms(), confirmed=True
        )
        version = self.version.bump()
        self._stage(
            command.correlation_id,
            effects,
            (
                SessionMutationCompleted(
                    command.correlation_id,
                    self.generation,
                    version,
                    SessionMutationProjection(
                        actor=current.actor,
                        session_ref=command.session_ref,
                        refreshed=True,
                        daemon_epoch=self.daemon_epoch,
                        channel_current_epoch=self.daemon_epoch,
                    ),
                ),
            ),
            cost_origin,
        )

    def _finish_heartbeat(
        self,
        command: HeartbeatSessionCommand,
        current: InteractiveSession,
        effects: tuple[PendingSessionAgentEffect, ...],
        cost_origin: StateCostOrigin,
    ) -> None:
        self.runtime_projection.register(
            current.actor, command.session_ref, self.clock_ms(), confirmed=True
        )
        version = self.version.bump()
        self._stage(
            command.correlation_id,
            effects,
            (
                SessionMutationCompleted(
                    command.correlation_id,
                    self.generation,
                    version,
                    SessionMutationProjection(
                        actor=current.actor,
                        session_ref=command.session_ref,
                        alive=True,
                        daemon_epoch=self.daemon_epoch,
                    ),
                ),
            ),
            cost_origin,
        )

    def _finish_unregister(
        self,
        command: UnregisterSessionCommand,
        effects: tuple[PendingSessionAgentEffect, ...],
        result: object,
        cost_origin: StateCostOrigin,
    ) -> None:
        if not isinstance(result, tuple) or len(result) != 2:
            raise TypeError("session unregister persistence returned invalid result")
        changed = bool(result[1])
        committed_effects = effects if changed else ()
        if changed:
            self.runtime_projection.drop(command.actor)
        version = self.version.bump() if changed else self.version.read()
        self._stage(
            command.correlation_id,
            committed_effects,
            (
                SessionMutationCompleted(
                    command.correlation_id,
                    self.generation,
                    version,
                    SessionMutationProjection(
                        actor=command.actor,
                        session_ref=command.session_ref,
                        unregistered=changed,
                        daemon_epoch=None,
                    ),
                ),
            ),
            cost_origin,
        )

    def _finish_lease(
        self,
        command: SessionLeaseElapsedCommand,
        effects: tuple[PendingSessionAgentEffect, ...],
        finals: tuple[SessionEvent, ...],
        sessions: tuple[InteractiveSession, ...],
        cost_origin: StateCostOrigin,
    ) -> None:
        completed: list[SessionEvent] = []
        for final in finals:
            assert isinstance(final, SessionLeaseExpired)
            self.runtime_projection.drop(final.actor)
            self._bound_sessions.discard((final.actor, final.session_ref))
            completed.append(replace(final, version=self.version.bump()))
        completed.append(
            SessionLeaseSweepCompleted(
                command.correlation_id,
                self.generation,
                self.version.read(),
                len(sessions),
            )
        )
        self._stage(command.correlation_id, effects, tuple(completed), cost_origin)

    def _stage(
        self,
        correlation_id: str,
        effects: tuple[PendingSessionAgentEffect, ...],
        final_events: tuple[SessionEvent, ...],
        cost_origin: StateCostOrigin,
    ) -> None:
        if not effects:
            for event in final_events:
                self._publish(event)
            return
        mutation = _PendingMutation(
            correlation_id=correlation_id,
            effect_ids={effect.effect_id for effect in effects},
            final_events=final_events,
        )
        self._mutations[correlation_id] = mutation
        for effect in effects:
            self._effect_owners[effect.effect_id] = correlation_id
            accepted = self.request_effect(
                effect,
                self.generation,
                cost_origin,
            )
            if not accepted and effect.effect_id in self._volatile_effects:
                # No durable row will be recovered for it: settle now and
                # make the next heartbeat take the durable path.
                self._volatile_effects.pop(effect.effect_id, None)
                self._bound_sessions.discard((effect.actor, effect.session_id))
                self._settle_effect(effect.effect_id)

    def _effect_result(self, result: _AgentEffectResult) -> None:
        volatile = self._volatile_effects.pop(result.effect_id, None)
        effect = volatile if volatile is not None else self._pending_effect(result.effect_id)
        if effect is None:
            self.retire_effect(result.effect_id, result.custody_token)
            if result.effect_id in self._effect_owners:
                # Orphaned light bind of a replaced generation: still answer it.
                if isinstance(result.event, PortCommandRejected):
                    self._reject_owner(
                        result.effect_id,
                        "AGENT_EFFECT_REJECTED",
                        f"Agent rejected bind: {result.event.code}: {result.event.detail}",
                    )
                elif result.event.operation != "bind":  # only binds go light
                    self._reject_owner(
                        result.effect_id,
                        "AGENT_EFFECT_RESULT_MISMATCH",
                        f"expected bind result, received {result.event.operation}",
                    )
                else:
                    self._settle_effect(result.effect_id)
            return
        event = result.event
        if isinstance(event, PortCommandRejected):
            self._fail_effect(
                effect,
                result.custody_token,
                "AGENT_EFFECT_REJECTED",
                f"Agent rejected {effect.operation}: {event.code}: {event.detail}",
            )
            return
        if event.operation != effect.operation:
            self._fail_effect(
                effect,
                result.custody_token,
                "AGENT_EFFECT_RESULT_MISMATCH",
                f"expected {effect.operation} result, received {event.operation}",
            )
            return
        if volatile is not None:
            self._finish_effect_result(result)  # no durable row to complete
            return
        if effect.operation == "bind" and effect.session_id is not None:
            # Only a durable bind arms the light path; a late light result
            # never re-arms it.
            self._bound_sessions.add((effect.actor, effect.session_id))
        self._persist(
            DesiredStateOperation.COMPLETE_SESSION_AGENT_EFFECT,
            result,
            (effect.effect_id,),
            context=("effect_complete", result),
            cost_origin=self.effect_cost_origin(
                effect.effect_id, result.custody_token
            ),
        )

    def _finish_effect_result(self, result: _AgentEffectResult) -> None:
        # The durable row has already been removed by the storage completion,
        # so reconstruct only the owner relationship needed for settlement.
        self.retire_effect(result.effect_id, result.custody_token)
        self._settle_effect(result.effect_id)

    def _settle_effect(self, effect_id: str) -> None:
        owner = self._effect_owners.pop(effect_id, None)
        if owner is None:
            return
        mutation = self._mutations.get(owner)
        if mutation is None:
            return
        mutation.effect_ids.discard(effect_id)
        if mutation.effect_ids:
            return
        self._mutations.pop(owner, None)
        for final in mutation.final_events:
            self._publish(final)

    def _effect_unavailable(self, failure: _AgentEffectUnavailable) -> None:
        volatile = self._volatile_effects.pop(failure.effect_id, None)
        effect = volatile if volatile is not None else self._pending_effect(failure.effect_id)
        if effect is not None:
            self._fail_effect(
                effect,
                failure.custody_token,
                failure.code,
                failure.detail,
            )
        else:
            self.retire_effect(failure.effect_id, failure.custody_token)
            if failure.effect_id in self._effect_owners:
                # Orphaned light bind (see _effect_result): answer it.
                self._reject_owner(failure.effect_id, failure.code, failure.detail)

    def _fail_effect(
        self,
        effect: PendingSessionAgentEffect,
        custody_token: str,
        code: str,
        detail: str,
    ) -> None:
        # A correlated Agent failure is a terminal result for the surviving
        # worker custody.  The durable effect intentionally remains, but a
        # newer Session generation may now take it over.
        self.retire_effect(effect.effect_id, custody_token)
        self._bound_sessions.discard((effect.actor, effect.session_id))
        self._reject_owner(effect.effect_id, code, detail, effect.correlation_id)

    def _reject_owner(
        self,
        effect_id: str,
        code: str,
        detail: str,
        fallback_correlation: str | None = None,
    ) -> None:
        owner = self._effect_owners.get(effect_id)
        correlation_id = fallback_correlation if owner is None else owner
        if correlation_id is None:
            return
        mutation = self._mutations.pop(correlation_id, None)
        if mutation is not None:
            for owned in mutation.effect_ids:
                self._effect_owners.pop(owned, None)
        self._publish(
            PortCommandRejected(
                correlation_id=correlation_id,
                domain="session",
                generation=self.generation,
                version=self.version.read(),
                code=code,
                detail=detail,
            )
        )

    def _pending_effect(self, effect_id: str) -> PendingSessionAgentEffect | None:
        return next(
            (
                effect
                for effect in self.store.load().pending_session_agent_effects
                if effect.effect_id == effect_id
            ),
            None,
        )

    def _owned_session(self, actor: str, session_ref: str) -> InteractiveSession:
        sessions = self.store.load().interactive_sessions
        current = next((item for item in sessions if item.actor == actor), None)
        if current is not None and current.session_ref == session_ref:
            return current
        if current is not None:
            raise SessionOwnershipError(
                ipc_errors.SESSION_SUPERSEDED,
                # Never name the current owner's session_ref (a credential).
                f"actor {actor} is now owned by another interactive session",
            )
        relocated = next(
            (item for item in sessions if item.session_ref == session_ref), None
        )
        if relocated is not None:
            if owner_only_relocation(actor, relocated.actor):
                # An owner migration rewrote this session's stored address; the
                # same carrier is calling under its pre-migration spelling.
                # Same session: the caller continues under the canonical URI.
                return relocated
            raise SessionOwnershipError(
                ipc_errors.SESSION_SUPERSEDED,
                f"interactive session {session_ref} moved from actor {actor} to {relocated.actor}",
            )
        raise SessionOwnershipError(
            ipc_errors.STALE_SESSION,
            f"interactive session {session_ref} no longer owns actor {actor}",
        )

    def _reject(self, command: object, code: str, detail: str) -> None:
        self._publish(
            PortCommandRejected(
                correlation_id=str(getattr(command, "correlation_id", "")),
                domain="session",
                generation=self.generation,
                version=self.version.read(),
                code=code,
                detail=detail,
            )
        )

    def _publish(self, event: SessionEvent) -> None:
        _publish(self.events, event)
