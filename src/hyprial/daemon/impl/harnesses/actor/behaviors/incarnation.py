"""Incarnation fence and start tracking owned by the harness mailbox.

No durable authority lives here.  ``_lifecycle_entity_error`` compares a
lifecycle request's expected incarnation with the identity projection.  A
launcher timeout does not finish its I/O: the start correlation stays tracked
until the process is owned by the runtime or its late cleanup positively
completes, and that tracking is the start admission width's accounting.
"""

from hyprial.kernel import ipc_errors


class _IncarnationBehavior:
    def _lifecycle_entity_error(self, request) -> tuple[str, str] | None:
        expected = request.expected_entity_token
        if expected is None:
            return None
        if self._agent_identity is None:
            return (ipc_errors.LIFECYCLE_INCARNATION_UNSUPPORTED,
                    "Harness owner has no incarnation projection port")
        payload = request.payload
        spec = getattr(payload, "spec", None)
        actor = getattr(spec, "name", None) if spec is not None else getattr(payload, "name", None)
        if not isinstance(actor, str) or not actor:
            return (ipc_errors.INVALID_ARGUMENT, "Harness lifecycle payload has no actor")
        current = self._agent_entity(actor)
        if current is None:
            return (ipc_errors.TRANSFER_CUSTODY_UNAVAILABLE,
                    "Harness owner cannot resolve incarnation")
        if current != expected:
            return (ipc_errors.AGENT_INCARNATION_MISMATCH,
                    "Harness lifecycle incarnation changed")
        return None

    def _init_start_tracking(self) -> None:
        self._start_inflight: dict[str, str] = {}
        self._start_stops: dict[str, str] = {}

    def _agent_entity(self, actor: str) -> str | None:
        if self._agent_identity is None:
            return None
        try:
            return self._agent_identity.entity_token(actor)
        except Exception:
            return None

    def _finish_start(self, correlation: str) -> None:
        self._start_inflight.pop(correlation, None)

    def _start_completed(self, event) -> None:
        if event.correlation_id in self._start_stops.values():
            return
        record = self._records.get(event.harness_id)
        if record is not None and record.completed_start is event:
            return
        self._finish_start(event.correlation_id)

    def _stop_completed(self, event) -> None:
        if event.stopped:
            start = self._start_stops.pop(event.correlation_id, None)
            if start is not None:
                self._finish_start(start)
