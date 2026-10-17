"""Session lifecycle admission checks and receipt-driven completion projection."""

from hyprial.kernel import ipc_errors, SESSION_CARRIER_SOURCES
from hyprial.daemon.impl.desired_state_io import DesiredStateOperation
from hyprial.daemon.impl.operations.session_ports import (
    RegisterSessionCommand, UnregisterSessionCommand,
    SessionMutationProjection, SessionMutationCompleted,
)
from hyprial.daemon.impl.state_persistence import StateCostOrigin


class _SessionLifecycle:
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
        if request.expected_entity_token is not None:
            if self.agent_identity is None:
                self._reject(request, ipc_errors.LIFECYCLE_INCARNATION_UNSUPPORTED,
                             "Session lifecycle incarnation fence is not wired")
                return
            try:
                current = self.agent_identity.entity_token(payload.actor)
            except Exception:
                current = None
            if current is None:
                self._reject(request, ipc_errors.TRANSFER_CUSTODY_UNAVAILABLE,
                             "Session incarnation projection is unavailable")
                return
            if current != request.expected_entity_token:
                self._reject(request, ipc_errors.AGENT_INCARNATION_MISMATCH,
                             "Session incarnation changed")
                return
        self._persist(
            DesiredStateOperation.APPLY_SESSION_LIFECYCLE,
            request,
            (request,),
            context=("lifecycle", request),
            cost_origin=StateCostOrigin.BACKGROUND,
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

