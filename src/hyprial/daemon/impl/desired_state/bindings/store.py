"""Binding operations share the desired-state lock, database and committed projection."""

from . import cleanup, transactions
from .schema import read
from .models import (
    Configuration,
    Coordination,
    Status,
    MODEL_BINDING_OPERATION_LIMIT,
    digest,
    integer,
    text,
)


class ModelBindingStore:
    def __init__(self, owner):
        self._owner = owner

    def _write(self, action, *args, **kwargs):
        from ..sqlite import _SCHEMA

        owner = self._owner
        with owner.config_rebind_fence():
            with owner.state_db.transaction(schema=_SCHEMA) as db:
                version = db.execute(
                    "SELECT schema_version FROM root_state WHERE id=1"
                ).fetchone()
                if version and version[0] not in (1, 2, 3, 4):
                    from hyprial.kernel import DesiredStateError

                    raise DesiredStateError("unsupported desired-state schema version")
                if version and version[0] == 4:
                    read(db)
                result = action(db, *args, **kwargs)
                read(db)
            owner._published_state = None
            owner.load()
            return result

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
        config = Configuration.from_json(candidate)
        return self._write(
            transactions.prepare,
            text(agent_id),
            text(member_id),
            text(operation_id),
            integer(expected_revision),
            config,
            operation_limit,
            None if coordination is None else Coordination.from_json(coordination),
        )

    def checkpoint(self, agent_id, member_id, operation_id, request_digest,
                   expected_revision, expected, replacement):
        return self._write(
            transactions.checkpoint, text(agent_id), text(member_id), text(operation_id),
            digest(request_digest), integer(expected_revision),
            Coordination.from_json(expected), Coordination.from_json(replacement),
        )

    def finalize(
        self, agent_id, member_id, operation_id, request_digest, expected_revision
    ):
        return self._finish(
            agent_id,
            member_id,
            operation_id,
            request_digest,
            expected_revision,
            cancel=False,
        )

    def cancel(
        self, agent_id, member_id, operation_id, request_digest, expected_revision
    ):
        return self._finish(
            agent_id,
            member_id,
            operation_id,
            request_digest,
            expected_revision,
            cancel=True,
        )

    def _finish(
        self,
        agent_id,
        member_id,
        operation_id,
        request_digest,
        expected_revision,
        *,
        cancel,
    ):
        return self._write(
            transactions.finish,
            text(agent_id),
            text(member_id),
            text(operation_id),
            digest(request_digest),
            integer(expected_revision),
            cancel=cancel,
        )

    def status(self, agent_id, member_id):
        text(agent_id)
        text(member_id)
        state = self._owner.load()
        binding = next(
            (
                item
                for item in state.model_bindings
                if (item.agent_id, item.member_id) == (agent_id, member_id)
            ),
            None,
        )
        operations = tuple(
            item
            for item in state.model_binding_operations
            if (item.agent_id, item.member_id) == (agent_id, member_id)
        )
        pending = next((item for item in operations if item.phase == "prepared"), None)
        marker = next(
            (item for item in state.model_agent_lifecycle if item.agent_id == agent_id),
            None,
        )
        return Status(binding, pending, operations, marker)

    def mark_cleanup(
        self, agent_id, entity_token, destroy_attempt, *, expected_attempt=None
    ):
        if expected_attempt is not None:
            text(expected_attempt)
        return self._write(
            cleanup.mark,
            text(agent_id),
            text(entity_token),
            text(destroy_attempt),
            expected_attempt,
        )

    def rollback_cleanup(self, agent_id, entity_token, destroy_attempt):
        return self._write(
            cleanup.clear, text(agent_id), text(entity_token), text(destroy_attempt)
        )

    def purge_destroyed_agent(self, agent_id, entity_token, destroy_attempt):
        return self._write(
            cleanup.clear,
            text(agent_id),
            text(entity_token),
            text(destroy_attempt),
            purge=True,
        )
