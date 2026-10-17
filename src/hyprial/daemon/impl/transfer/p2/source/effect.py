"""Bounded quiesce effect, executed outside every authority mailbox.

Unknown is never automatic rollback or permission to submit a second accepted
effect. After restart/pending replay the recovery caller must inspect durable
owners before deciding; this first-execution path deliberately does not retry.
"""

from dataclasses import dataclass
import hashlib
import time
from uuid import uuid4

from hyprial.kernel import PortAdmission, agent_uri_actor
from hyprial.daemon.impl.harnesses.runtime.transfer_ports import HoldStartsForTransfer, StartsHeld
from hyprial.daemon.impl.lifecycle_manager import LifecycleOperation, LifecycleSpec, LifecycleState
from hyprial.daemon.impl.lifecycle_manager.steps import _operation_json

from ..model import identifier
from ..source_ports import SourceIntent, SourceFinish

SOURCE_QUIESCE_DEADLINE_SECONDS = 70.0


@dataclass(frozen=True, slots=True)
class QuiesceRequest:
    operation_id: str
    source_agent_id: str
    spec: LifecycleSpec

    def __post_init__(self):
        identifier(self.operation_id)
        identifier(self.source_agent_id)
        if not isinstance(self.spec, LifecycleSpec):
            raise ValueError('quiesce requires a lifecycle spec')
        identifier(self.spec.actor)
        identifier(self.spec.expected_entity_token)
        if (self.spec.agent_name != self.spec.harness.name
                or (agent_uri_actor(self.spec.actor) or self.spec.actor) != self.spec.agent_name):
            raise ValueError('source lifecycle spec identities do not match')

    @property
    def fingerprint(self) -> str:
        payload = _operation_json(LifecycleOperation.deactivate(self.operation_id, self.spec))
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class QuiesceOutcome:
    operation_id: str
    state: str
    receipt: str | None = None
    reason: str = ''


class QuiesceEffect:
    def __init__(self, journal, harnesses, lifecycle, *, deadline_seconds=SOURCE_QUIESCE_DEADLINE_SECONDS):
        if deadline_seconds <= 0:
            raise ValueError('deadline must be positive')
        self._journal = journal
        self._harnesses = harnesses
        self._lifecycle = lifecycle
        self._deadline_seconds = deadline_seconds

    def __call__(self, request: QuiesceRequest) -> QuiesceOutcome:
        deadline = time.monotonic() + self._deadline_seconds

        def remaining():
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise TimeoutError('source quiesce outcome remains unknown')
            return budget

        try:
            start = self._journal.submit(SourceIntent(
                # Journal/restore projection is keyed by identity's local actor;
                # network routes and hold receipts retain the canonical URI.
                request.operation_id, request.spec.agent_name, request.spec.expected_entity_token,
                request.source_agent_id, request.fingerprint,
            )).result(remaining())
            if not start.execute:
                # A pending intent may conceal an accepted effect; never use a
                # missing in-memory waiter as proof that resubmission is safe.
                completed = start.state.phase == 'quiesced' and not start.state.needs_attention
                return QuiesceOutcome(request.operation_id, 'quiesced' if completed else 'unknown',
                                      start.state.receipt if completed else None, 'journal-replay')
            state = start.state
            command = HoldStartsForTransfer(
                uuid4().hex, actor=request.spec.actor,
                expected_entity_token=state.entity_token,
                operation_id=request.operation_id, generation=state.generation,
            )
            held = self._harnesses.call(command, StartsHeld, timeout=remaining())
            if (not isinstance(held, StartsHeld) or held.disposition != 'held'
                    or (held.correlation_id, held.actor, held.expected_entity_token,
                        held.operation_id, held.generation) !=
                    (command.correlation_id, command.actor, command.expected_entity_token,
                     command.operation_id, command.generation)):
                return QuiesceOutcome(request.operation_id, 'unknown', reason='hold-not-proven')
            operation = LifecycleOperation.deactivate(
                f'transfer-quiesce:{request.operation_id}:{state.generation}', request.spec)
            if self._lifecycle.submit(operation) is not PortAdmission.ACCEPTED:
                return QuiesceOutcome(request.operation_id, 'unknown', reason='lifecycle-not-admitted')
            receipt = self._lifecycle.wait(operation.operation_id, remaining())
            if (receipt.operation_id != operation.operation_id
                    or receipt.state is not LifecycleState.COMPLETED):
                return QuiesceOutcome(request.operation_id, 'unknown', reason='stop-not-proven')
            finished = self._journal.submit(SourceFinish(state, receipt.operation_id)).result(remaining())
            return QuiesceOutcome(request.operation_id, 'quiesced', finished.receipt)
        except Exception as error:
            # Domain rejections and interrupted waits retain durable intent and
            # any derived hold. Recovery queries the owners; nothing is undone.
            return QuiesceOutcome(request.operation_id, 'unknown', reason=type(error).__name__)
