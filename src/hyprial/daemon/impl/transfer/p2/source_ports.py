"""Internal source journal commands; execution stays in the short SQLite lane.

These are not trusted IPC requests. Composition must resolve source identity
and validate the lifecycle receipt before sending a completion.
"""

from dataclasses import dataclass

from hyprial.kernel import agent_uri_actor

from .journal import AttentionEvent, StepState
from .model import AdmissionConflict, Reason, Role, identifier
from .projection import CustodyQuery


@dataclass(frozen=True, slots=True)
class SourceIntent:
    operation_id: str
    actor: str
    expected_entity_token: str
    source_agent_id: str
    fingerprint: str

    def __post_init__(self):
        for value in (self.operation_id, self.actor, self.expected_entity_token,
                      self.source_agent_id, self.fingerprint):
            identifier(value)

    @property
    def query(self):
        return CustodyQuery(self.actor, self.expected_entity_token)


@dataclass(frozen=True, slots=True)
class SourceFinish:
    expected: StepState
    receipt: str

    def __post_init__(self):
        if not isinstance(self.expected, StepState) or self.expected.step != 'quiesce':
            raise ValueError('source completion requires a quiesce intent')
        identifier(self.expected.entity_token)
        identifier(self.receipt)

    @property
    def query(self):
        return CustodyQuery(self.expected.actor, self.expected.entity_token)


def apply_source(store, command: SourceIntent | SourceFinish):
    if isinstance(command, SourceIntent):
        admission = store.get(command.operation_id)
        if admission is None:
            raise AdmissionConflict(Reason.UNKNOWN)
        binding = admission.binding
        if (binding.role is not Role.SOURCE
                or binding.source_entity_token != command.expected_entity_token
                or binding.source_agent_id != command.source_agent_id):
            raise AdmissionConflict(Reason.BINDING_MISMATCH)
        return store.journal.begin(command.operation_id, step='quiesce',
                                   fingerprint=command.fingerprint, actor=command.actor)
    return store.journal.finish(command.expected.operation_id, expected=command.expected,
                                receipt=command.receipt,
                                entity_token=command.expected.entity_token)


@dataclass(frozen=True, slots=True)
class PendingQuiesce:
    """Bounded read of durable quiesce intents that recorded no result.

    Recovery-only: the composition layer asks the authority for unresolved
    rows, then certifies each against existing owner receipts. This read
    publishes no custody and grants no execution.
    """

    limit: int = 32

    def __post_init__(self):
        if type(self.limit) is not int or self.limit < 1:
            raise ValueError("pending quiesce limit must be a positive int")

    @property
    def query(self):
        return None


@dataclass(frozen=True, slots=True)
class CustodyOverride:
    """Audit an already authenticated operator override of one custody step.

    This is an internal P2-lane command: the composition layer validates the
    principal and audits the alert, then sends this to record the durable
    attention row. It records an override; it never releases custody, clears
    the row or starts the entity itself.
    """

    operation_id: str
    actor: str
    expected_entity_token: str
    principal: str
    reason: str
    event_id: str
    action: str = "transfer-custody-override"

    def __post_init__(self):
        for value in (self.operation_id, self.actor, self.expected_entity_token,
                      self.principal, self.event_id, self.action):
            identifier(value)

    @property
    def query(self):
        # The audited override is a custody fact: publish the refreshed
        # observation so the guard stops suppressing this exact incarnation.
        return CustodyQuery(self.actor, self.expected_entity_token)


@dataclass(frozen=True, slots=True)
class OverrideAudit:
    """Durable audit receipt plus the step generation the release must name."""

    event: AttentionEvent
    state: StepState


def apply_override(store, command: CustodyOverride) -> OverrideAudit:
    state = store.journal.get(command.operation_id)
    if state is None or state.actor != (agent_uri_actor(command.actor) or command.actor):
        raise AdmissionConflict(Reason.UNKNOWN)
    updated = store.journal.attention(
        command.operation_id, expected=state, event_id=command.event_id,
        principal=command.principal, action=command.action, reason=command.reason,
    )
    event = store.journal.events(command.operation_id)[-1]
    return OverrideAudit(event, updated)
