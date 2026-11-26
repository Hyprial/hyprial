"""Persist preparation and cancellation before Cell effects, then prove the result.

Per-Cell serialization covers only this daemon's coordinators. All other writers
are checked by Cell CAS; the global persistence fence never spans Cell I/O.
Accepted binding mutations use the existing StatePersistenceAuthority port.
"""

from contextlib import contextmanager
from dataclasses import replace
from threading import Lock, RLock
from weakref import WeakValueDictionary

from hyprial.error import codes
from hyprial.daemon.impl.desired_state.bindings.models import (
    Coordination, ModelBindingError, MODEL_BINDING_OPERATION_LIMIT, request_digest,
)
from .cell import CellIntent, refuse

_LOCKS = WeakValueDictionary()
_LOCKS_GUARD = Lock()


@contextmanager
def cell_fence(app, cell_id):
    with _LOCKS_GUARD:
        key = (id(app), cell_id)
        lock = _LOCKS.setdefault(key, RLock())
    with lock:
        yield


class Coordinator:
    def __init__(self, app, agent, member, *, authorize):
        self.app, self.agent, self.member = app, agent, member
        self.store = app.desired_state.model_binding_store
        self.cell = CellIntent(app, agent, member)
        self.authorize = authorize

    def _call(self, method, *args, **kwargs):
        try:
            return getattr(self.store, method)(*args, **kwargs)
        except ModelBindingError as error:
            refuse(error.code, error.reason, error.phase)
        except Exception:
            refuse(codes.MODEL_CONFIGURATION_INCOMPLETE, "state-unreadable")

    def status(self):
        result = self._call("status", self.agent.agent_id, self.member)
        if result.cleanup is not None:
            refuse(codes.MODEL_CONFIGURATION_INCOMPLETE, "destroy-cleanup-pending")
        return result

    def _checkpoint(self, operation, **changes):
        self.status()
        return self._call("checkpoint", *self._args(operation),
                          operation.coordination.to_json(),
                          replace(operation.coordination, **changes).to_json())

    def _args(self, operation):
        return (self.agent.agent_id, self.member, operation.operation_id,
                operation.request_digest, operation.expected_revision)

    def configure(self, operation_id, expected_binding, expected_cell, config,
                  *, operation_limit=MODEL_BINDING_OPERATION_LIMIT):
        with cell_fence(self.app, self.cell.cell_id):
            state = self.status()
            old = next((item for item in state.operations
                        if item.operation_id == operation_id), None)
            if old is not None:
                if (old.coordination is None
                        or old.coordination.expected_revision != expected_cell
                        or old.request_digest != request_digest(
                            self.agent.agent_id, self.member, expected_binding, config,
                            old.coordination)):
                    refuse(codes.MODEL_CONFIGURATION_CONFLICT, "operation-digest-mismatch")
                if old.phase != "prepared":
                    return old
                if old.coordination.phase in {"cancelling", "compensated"}:
                    refuse(codes.MODEL_CONFIGURATION_INCOMPLETE, "operation-in-progress")
            else:
                if state.pending is not None:
                    refuse(codes.MODEL_CONFIGURATION_INCOMPLETE, "operation-in-progress")
                if (state.binding.binding_revision if state.binding else 0) != expected_binding:
                    refuse(codes.MODEL_CONFIGURATION_CONFLICT, "revision-mismatch")
                cell, previous = self.cell.read()
                if cell.revision != expected_cell:
                    refuse(codes.MODEL_CONFIGURATION_CONFLICT, "cell-revision-mismatch")
                plan = Coordination(cell.cell_id, expected_cell, tuple(sorted(previous.items())))
                self.authorize(config)
                old = self._call(
                    "prepare", self.agent.agent_id, self.member, operation_id,
                    expected_binding, config.to_json(), operation_limit=operation_limit,
                    coordination=plan.to_json(),
                )
            if state.pending is not None:
                self.authorize(config)
            return self._apply(old)

    def _apply(self, operation):
        plan = operation.coordination
        expected = {"kind": operation.candidate.harness, "model": operation.candidate.model}
        self.status()
        cell, observed = self.cell.read()
        if cell.cell_id != plan.cell_id or cell.revision < plan.expected_revision:
            refuse(codes.MODEL_CONFIGURATION_CONFLICT, "cell-revision-mismatch")
        if observed != expected:
            if (plan.phase != "planned" or observed != dict(plan.previous_intent)
                    or cell.revision != plan.expected_revision):
                refuse(codes.MODEL_CONFIGURATION_CONFLICT, "cell-intent-mismatch")
            self.cell.write(cell, expected)
            cell, observed = self.cell.read()
        if observed != expected:
            refuse(codes.MODEL_CONFIGURATION_CONFLICT, "cell-intent-mismatch")
        if plan.phase == "planned":
            operation = self._checkpoint(operation, phase="applied", observed_revision=cell.revision)
        return self._call("finalize", *self._args(operation))

    def cancel(self, operation_id, expected_binding):
        with cell_fence(self.app, self.cell.cell_id):
            state = self.status()
            operation = next((item for item in state.operations
                              if item.operation_id == operation_id), None)
            if operation is None or operation.expected_revision != expected_binding:
                refuse(codes.MODEL_CONFIGURATION_CONFLICT, "revision-mismatch")
            if operation.phase == "cancelled":
                return operation
            if operation.phase == "finalized":
                refuse(codes.MODEL_CONFIGURATION_CONFLICT, "operation-already-completed")
            if operation.coordination is None:
                refuse(codes.MODEL_CONFIGURATION_INCOMPLETE, "coordination-unavailable")
            if operation.coordination.phase in {"planned", "applied"}:
                operation = self._checkpoint(operation, phase="cancelling")
            plan = operation.coordination
            cell, observed = self.cell.read()
            if cell.cell_id != plan.cell_id or cell.revision < plan.expected_revision:
                refuse(codes.MODEL_CONFIGURATION_CONFLICT, "cell-revision-mismatch")
            previous = dict(plan.previous_intent)
            candidate = {"kind": operation.candidate.harness, "model": operation.candidate.model}
            if observed != previous:
                if observed != candidate or plan.phase == "compensated":
                    refuse(codes.MODEL_CONFIGURATION_CONFLICT, "cell-intent-mismatch")
                self.status()
                self.cell.write(cell, previous)
                cell, observed = self.cell.read()
            if observed != previous:
                refuse(codes.MODEL_CONFIGURATION_CONFLICT, "cell-intent-mismatch")
            if plan.phase != "compensated":
                operation = self._checkpoint(operation, phase="compensated", observed_revision=cell.revision)
            return self._call("cancel", *self._args(operation))
