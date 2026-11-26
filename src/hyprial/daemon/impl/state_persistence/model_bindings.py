"""Model binding mutations use the existing serialized persistence authority.

Accepted writes join their original settlement; a caller timeout must not leave
an unobserved cleanup marker arriving after the destroy reservation was released.
"""

from dataclasses import dataclass
from typing import get_args
from uuid import uuid4

from hyprial.daemon.impl.desired_state.bindings.models import (
    Configuration,
    Coordination,
    MODEL_BINDING_OPERATION_LIMIT,
)
from hyprial.daemon.impl.desired_state.bindings.store import ModelBindingStore


@dataclass(frozen=True, slots=True)
class PrepareModelBinding:
    correlation_id: str
    agent_id: str
    member_id: str
    operation_id: str
    expected_revision: int
    candidate: Configuration
    operation_limit: int
    coordination: Coordination | None = None


@dataclass(frozen=True, slots=True)
class FinalizeModelBinding:
    correlation_id: str
    agent_id: str
    member_id: str
    operation_id: str
    request_digest: str
    expected_revision: int


@dataclass(frozen=True, slots=True)
class CancelModelBinding(FinalizeModelBinding):
    pass


@dataclass(frozen=True, slots=True)
class CheckpointModelBinding(FinalizeModelBinding):
    expected: Coordination
    replacement: Coordination


@dataclass(frozen=True, slots=True)
class MarkModelCleanup:
    correlation_id: str
    agent_id: str
    entity_token: str
    destroy_attempt: str
    expected_attempt: str | None


@dataclass(frozen=True, slots=True)
class RollbackModelCleanup:
    correlation_id: str
    agent_id: str
    entity_token: str
    destroy_attempt: str


@dataclass(frozen=True, slots=True)
class PurgeModelBindings(RollbackModelCleanup):
    pass


ModelBindingCommand = (
    PrepareModelBinding
    | FinalizeModelBinding
    | CancelModelBinding
    | CheckpointModelBinding
    | MarkModelCleanup
    | RollbackModelCleanup
    | PurgeModelBindings
)
MODEL_BINDING_COMMANDS = get_args(ModelBindingCommand)


def execute(store, command):
    if isinstance(command, PrepareModelBinding):
        return store.prepare(
            command.agent_id,
            command.member_id,
            command.operation_id,
            command.expected_revision,
            command.candidate.to_json(),
            operation_limit=command.operation_limit,
            coordination=(None if command.coordination is None
                          else command.coordination.to_json()),
        )
    if isinstance(command, CheckpointModelBinding):
        return store.checkpoint(
            command.agent_id, command.member_id, command.operation_id,
            command.request_digest, command.expected_revision,
            command.expected.to_json(), command.replacement.to_json(),
        )
    if isinstance(command, FinalizeModelBinding):
        finish = (
            store.cancel if isinstance(command, CancelModelBinding) else store.finalize
        )
        return finish(
            command.agent_id,
            command.member_id,
            command.operation_id,
            command.request_digest,
            command.expected_revision,
        )
    if isinstance(command, MarkModelCleanup):
        return store.mark_cleanup(
            command.agent_id,
            command.entity_token,
            command.destroy_attempt,
            expected_attempt=command.expected_attempt,
        )
    if isinstance(command, RollbackModelCleanup):
        finish = (
            store.purge_destroyed_agent
            if isinstance(command, PurgeModelBindings)
            else store.rollback_cleanup
        )
        return finish(command.agent_id, command.entity_token, command.destroy_attempt)
    raise TypeError("unsupported model binding command")


class ModelBindingPort:
    def __init__(self, desired_port):
        self._desired = desired_port

    def _call(self, command_type, *args):
        return self._desired._owner.call_settled(
            command_type(f"state-model-{uuid4().hex}", *args)
        )

    def prepare(
        self,
        agent_id,
        member_id,
        operation_id,
        expected_revision,
        candidate,
        *,
        operation_limit=MODEL_BINDING_OPERATION_LIMIT,
        coordination=None,
    ):
        return self._call(
            PrepareModelBinding,
            agent_id,
            member_id,
            operation_id,
            expected_revision,
            Configuration.from_json(candidate),
            operation_limit,
            None if coordination is None else Coordination.from_json(coordination),
        )

    def checkpoint(self, agent_id, member_id, operation_id, request_digest,
                   expected_revision, expected, replacement):
        return self._call(
            CheckpointModelBinding, agent_id, member_id, operation_id,
            request_digest, expected_revision, Coordination.from_json(expected),
            Coordination.from_json(replacement),
        )

    def finalize(
        self, agent_id, member_id, operation_id, request_digest, expected_revision
    ):
        return self._call(
            FinalizeModelBinding,
            agent_id,
            member_id,
            operation_id,
            request_digest,
            expected_revision,
        )

    def cancel(
        self, agent_id, member_id, operation_id, request_digest, expected_revision
    ):
        return self._call(
            CancelModelBinding,
            agent_id,
            member_id,
            operation_id,
            request_digest,
            expected_revision,
        )

    def status(self, agent_id, member_id):
        return ModelBindingStore(self._desired).status(agent_id, member_id)

    def mark_cleanup(
        self, agent_id, entity_token, destroy_attempt, *, expected_attempt=None
    ):
        return self._call(
            MarkModelCleanup, agent_id, entity_token, destroy_attempt, expected_attempt
        )

    def rollback_cleanup(self, agent_id, entity_token, destroy_attempt):
        return self._call(RollbackModelCleanup, agent_id, entity_token, destroy_attempt)

    def purge_destroyed_agent(self, agent_id, entity_token, destroy_attempt):
        return self._call(PurgeModelBindings, agent_id, entity_token, destroy_attempt)
