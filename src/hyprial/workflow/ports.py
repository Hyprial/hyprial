from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias

from hyprial.contracts.agent_task import (
    AgentTaskCancelled,
    AgentTaskObserved,
    AgentTaskStarted,
    CancelAgentTaskCommand,
    ObserveAgentTaskCommand,
    StartAgentTaskCommand,
)
from hyprial.contracts.ports import CommandSink, EventSink, PortCommandRejected
from hyprial.inbox.api import InboxMessage
from hyprial.inbox.io import DeliveredMessage


@dataclass(frozen=True, slots=True)
class StartWorkflowCommand:
    correlation_id: str
    yaml_text: str
    sender: str


@dataclass(frozen=True, slots=True)
class StartWorkflowIdempotentCommand:
    correlation_id: str
    external_ref: str
    yaml_text: str
    sender: str


@dataclass(frozen=True, slots=True)
class CancelWorkflowCommand:
    correlation_id: str
    run_id: str


@dataclass(frozen=True, slots=True)
class RecoverWorkflowsCommand:
    correlation_id: str


@dataclass(frozen=True, slots=True)
class WorkflowTimerElapsedCommand:
    correlation_id: str
    generation: int
    version: int
    observed_at_ms: int


@dataclass(frozen=True, slots=True)
class ObserveWorkflowActivityCommand:
    """Legacy workflow reply/ack activity; agent.task has its own command."""

    correlation_id: str
    run_id: str
    target_ref: str
    conversation_id: str
    kind: str
    payload_json: bytes
    message_id: str | None = None
    result_ref: str | None = None


WorkflowCommand: TypeAlias = (
    StartWorkflowCommand
    | StartWorkflowIdempotentCommand
    | CancelWorkflowCommand
    | RecoverWorkflowsCommand
    | WorkflowTimerElapsedCommand
    | ObserveWorkflowActivityCommand
    | StartAgentTaskCommand
    | CancelAgentTaskCommand
    | ObserveAgentTaskCommand
)


@dataclass(frozen=True, slots=True)
class WorkflowTargetProjection:
    target: str
    conversation_id: str
    state: str
    attempts: int
    extend_count: int = 0
    reply_excerpt: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "target": self.target,
            "conversationId": self.conversation_id,
            "state": self.state,
            "attempts": self.attempts,
            **({"extendCount": self.extend_count} if self.extend_count else {}),
            **(
                {"replyExcerpt": self.reply_excerpt}
                if self.reply_excerpt is not None
                else {}
            ),
        }


@dataclass(frozen=True, slots=True)
class WorkflowRunProjection:
    version: int
    run_id: str
    name: str
    state: str
    sender: str
    nonce: str
    targets: tuple[WorkflowTargetProjection, ...]
    report: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "runId": self.run_id,
            "name": self.name,
            "state": self.state,
            "sender": self.sender,
            "nonce": self.nonce,
            "targets": [target.to_payload() for target in self.targets],
            **({"report": self.report} if self.report is not None else {}),
        }


@dataclass(frozen=True, slots=True)
class WorkflowStartProjection:
    run_id: str
    state: str
    targets: int

    def to_payload(self) -> dict[str, object]:
        return {"runId": self.run_id, "state": self.state, "targets": self.targets}


@dataclass(frozen=True, slots=True)
class WorkflowRunsProjection:
    runs: tuple[WorkflowRunProjection, ...]

    def to_payload(self) -> dict[str, object]:
        return {"runs": [run.to_payload() for run in self.runs]}


@dataclass(frozen=True, slots=True)
class WorkflowMutationProjection:
    run_id: str
    state: str

    def to_payload(self) -> dict[str, object]:
        return {"runId": self.run_id, "state": self.state}


@dataclass(frozen=True, slots=True)
class WorkflowStarted:
    correlation_id: str
    generation: int
    version: int
    result: WorkflowStartProjection


@dataclass(frozen=True, slots=True)
class WorkflowCancelled:
    correlation_id: str
    generation: int
    version: int
    result: WorkflowMutationProjection


@dataclass(frozen=True, slots=True)
class WorkflowsRecovered:
    correlation_id: str
    generation: int
    version: int
    adopted: int


@dataclass(frozen=True, slots=True)
class WorkflowTimerCompleted:
    correlation_id: str
    generation: int
    version: int
    active_runs: int


@dataclass(frozen=True, slots=True)
class WorkflowIoCompleted:
    correlation_id: str
    generation: int
    version: int
    run_id: str
    target_ref: str
    operation: str
    succeeded: bool
    message_id: str | None = None
    code: str | None = None
    detail: str | None = None
    permanent: bool = False
    # The canonical actor this delivery reached, when it succeeded.  Carried
    # so the assign ledger can record the actor that was actually written to
    # instead of re-resolving a mutable alias afterwards.
    recipient: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowActivityObserved:
    correlation_id: str
    generation: int
    version: int
    run_id: str
    target_ref: str
    conversation_id: str
    kind: str
    payload_json: bytes
    message_id: str | None = None
    result_ref: str | None = None


WorkflowEvent: TypeAlias = (
    WorkflowStarted
    | WorkflowCancelled
    | WorkflowsRecovered
    | WorkflowTimerCompleted
    | WorkflowIoCompleted
    | WorkflowActivityObserved
    | AgentTaskStarted
    | AgentTaskCancelled
    | AgentTaskObserved
    | PortCommandRejected
)
WorkflowCommandSink: TypeAlias = CommandSink[WorkflowCommand]
WorkflowEventSink: TypeAlias = EventSink[WorkflowEvent]


class WorkflowProjectionPort(Protocol):
    def read_run(self, run_id: str) -> WorkflowRunProjection | None: ...

    def read_runs(self, limit: int = 50) -> tuple[WorkflowRunProjection, ...]: ...


class WorkflowDeliveryIoPort(Protocol):
    """Idempotent delivery keyed by the registry's durable effect id."""

    def deliver(
        self,
        *,
        effect_id: str,
        sender: str,
        target: str,
        conversation_id: str,
        text: str,
    ) -> DeliveredMessage: ...


class WorkflowAcknowledgeIoPort(Protocol):
    """Typed acknowledgement mutation; reads never leak through this seam."""

    def acknowledge(
        self, *, effect_id: str, recipient: str, message_id: str
    ) -> bool: ...


class WorkflowInboxProjectionPort(Protocol):
    """Read-only inbox view used by workflow observation and ACK lookup."""

    def read_pending(self) -> tuple[InboxMessage, ...]: ...

    def read_consumption_state(self, message_id: str) -> str | None: ...
