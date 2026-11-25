# Decision record (phase D, package D1, 2026-11-25):
# - Plan §5 first row: a new mutating method of the state host must run on
#   the space's command loop.  ``state_command`` is ``_facade_locked``'s
#   space-state branch for methods that the legacy ``_SPACE_STATE_METHODS``
#   set does not name (``apply_remote``, ``repair_frontiers``,
#   ``update_space_meta``, F4's ``blob_arrived``) or names with a different signature
#   (``remove(permanent=)``); ``StateDispatch`` freezes
#   them into their own command values and dispatches them back.  A nested
#   call on the lane only takes the space lock, as ``_facade_locked`` does.
# - ``update_space_meta`` is a write the legacy set leaves off the command
#   loop, so the legacy host neither reserves effect custody for it nor
#   publishes it (C4 report §12-1).  On the state host it is a command and its
#   commit is published like every other (design §2.5 step 3).
# - Every space command brackets the registry's pin scope (design §2.5).
# - Design §4.7 / §9 R12: ``SpaceSyncHost`` is the ``SyncHost`` of one space.
#   ``read_vectors`` reads the immutable vector table without the space lock
#   and only falls back to the lock for content documents not in it;
#   ``read_update`` and ``pending_content`` hold the space lock on the calling
#   thread; ``apply_remote`` goes through the command loop and maps its
#   ``resource-exhausted`` to ``Pending("dependency")``.  The node gate is the
#   caller's callback (§3.5); ``requester_allowed`` reads the member cache;
#   ``requester_removed`` (§9.2) reads the removed-row cache of the same refresh.
# - ``pending_content`` first verifies a bounded batch of unverified documents
#   (§9 R4) and, outside the lock, submits the frontier repairs that
#   verification found (§4.2).
"""Command-loop plumbing, admission view and ``SyncHost`` of the state host."""

from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
import inspect
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from hyprial.daemon.impl.orgfs.api import OrgFsError, NodeRef
from hyprial.daemon.impl.orgfs.document.model import _Space, _SpaceStateCommand, _SpaceStateOutcome
from hyprial.daemon.impl.orgfs.membership import Pending

if TYPE_CHECKING:
    from hyprial.daemon.impl.orgfs.document.coordinator import LocalOrgFs

EMPTY_VECTOR = b"\x00"


@dataclass(frozen=True, slots=True)
class _ApplyRemote:
    space_id: str
    doc_id: str
    update: bytes
    expected_vector: bytes
    peer: str


@dataclass(frozen=True, slots=True)
class _StateRemove:
    space_id: str
    node: NodeRef
    permanent: bool


@dataclass(frozen=True, slots=True)
class _RepairFrontiers:
    space_id: str
    doc_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _UpdateSpaceMeta:
    space_id: str
    values: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _BlobArrived:
    space_id: str
    digest: str


_STATE_METHODS = frozenset(
    {"apply_remote", "blob_arrived", "remove", "repair_frontiers", "update_space_meta"}
)


def state_command(method: Callable[..., Any]) -> Callable[..., Any]:
    """Run a state-host mutation as a command on its space's command loop."""

    @wraps(method)
    def command(self: Any, *args: Any, **kwargs: Any) -> Any:
        if getattr(self._space_state_context, "running", False):
            with self._space_lock(str(args[0])):
                return method(self, *args, **kwargs)
        return self._submit_space_state(method, args, kwargs)

    return command


class StateDispatch:
    """Freeze and dispatch the state host's own space commands."""

    def _make_space_operation(
        self, method: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        name = method.__name__
        if name not in _STATE_METHODS:
            return super()._make_space_operation(method, args, kwargs)
        bound = inspect.signature(method).bind(self, *args, **kwargs)
        bound.apply_defaults()
        values = bound.arguments
        if name == "apply_remote":
            return _ApplyRemote(
                values["space_id"],
                str(values["doc_id"]),
                bytes(values["update"]),
                bytes(values["expected_vector"]),
                str(values["peer"]),
            )
        if name == "remove":
            return _StateRemove(values["space_id"], values["node"], bool(values["permanent"]))
        if name == "update_space_meta":
            raw = values["values"]
            frozen = MappingProxyType(dict(raw)) if isinstance(raw, Mapping) else raw
            return _UpdateSpaceMeta(values["space_id"], frozen)
        if name == "blob_arrived":
            return _BlobArrived(values["space_id"], str(values["digest"]))
        return _RepairFrontiers(values["space_id"], tuple(values["doc_ids"]))

    def _execute_space_operation(self, operation: Any) -> object:
        if isinstance(operation, _ApplyRemote):
            return type(self).apply_remote.__wrapped__(
                self,
                operation.space_id,
                operation.doc_id,
                operation.update,
                operation.expected_vector,
                operation.peer,
            )
        if isinstance(operation, _StateRemove):
            return type(self).remove.__wrapped__(
                self, operation.space_id, operation.node, permanent=operation.permanent
            )
        if isinstance(operation, _RepairFrontiers):
            return type(self).repair_frontiers.__wrapped__(self, operation.space_id, operation.doc_ids)
        if isinstance(operation, _UpdateSpaceMeta):
            return type(self).update_space_meta.__wrapped__(self, operation.space_id, operation.values)
        if isinstance(operation, _BlobArrived):
            return type(self).blob_arrived.__wrapped__(self, operation.space_id, operation.digest)
        return super()._execute_space_operation(operation)

    def _execute_space_state(
        self, space_id: str, command: _SpaceStateCommand
    ) -> _SpaceStateOutcome:
        with self._lock:
            registry = self._registries.get(space_id)
        if registry is not None:
            registry.begin_command()
        try:
            return super()._execute_space_state(space_id, command)
        finally:
            if registry is not None:
                with self._space_lock(space_id):
                    registry.end_command()


class AdmissionViewOf:
    """``AdmissionView`` over the current state of one space (design §3.6)."""

    def __init__(self, facade: "LocalOrgFs", space: _Space) -> None:
        self._facade = facade
        self._space = space
        self.space_id = space.info.space_id
        self.space_name = space.info.name
        self.owner = space.info.owner

    def member_mode(self, user: str) -> str | None:
        member = self._space.members.get(user)
        return member.mode if member is not None else None

    def tree_row(self, node_id: str) -> Mapping[str, object] | None:
        node = self._space.nodes.get(node_id)
        return None if node is None else self._row(node.node_id)

    def node_for_doc(self, doc_id: str) -> Mapping[str, object] | None:
        node = self._facade._doc_node(self._space, doc_id)
        return None if node is None else self._row(node.node_id)

    def path_of(self, node_id: str) -> str | None:
        if node_id not in self._space.nodes:
            return None
        return self._facade._path(self._space, node_id)

    def _row(self, node_id: str) -> Mapping[str, object]:
        node = self._space.nodes[node_id]
        stored = self._space.tree.row(node_id) or {}
        value = dict(stored)
        value.update(
            {
                "node_id": node.node_id,
                "parent": node.parent,
                "name": node.name,
                "kind": node.kind,
                "doc_id": node.doc_id,
                "deleted": node.deleted,
            }
        )
        return value


class SpaceSyncHost:
    """The ``SyncHost`` of one space of a ``LocalOrgFs`` (design §4.7)."""

    def __init__(
        self, facade: "LocalOrgFs", space_id: str, node_gate: Callable[[str], bool]
    ) -> None:
        self._facade = facade
        self.space_id = space_id
        self._node_gate = node_gate

    def read_vectors(self, doc_ids: Sequence[str]) -> Mapping[str, bytes]:
        registry = self._facade._registry(self.space_id)
        table = registry.vectors
        result = {doc_id: table[doc_id] for doc_id in doc_ids if doc_id in table}
        missing = [doc_id for doc_id in doc_ids if doc_id not in result]
        if missing:
            result.update(self._facade._read_vectors_locked(self.space_id, tuple(missing)))
        return {doc_id: result.get(doc_id, EMPTY_VECTOR) for doc_id in doc_ids}

    def read_update(self, doc_id: str, since: bytes) -> tuple[bytes, bytes] | None:
        return self._facade._read_update_locked(self.space_id, doc_id, since)

    def pending_content(self, limit: int) -> tuple[str, ...]:
        pending, repairs = self._facade._pending_content_locked(self.space_id, limit)
        if repairs:
            try:
                self._facade.repair_frontiers(self.space_id, repairs)
            except OrgFsError as exc:
                self._facade._log(
                    "warn", "orgfs.frontier.repair-deferred", spaceId=self.space_id, code=exc.code
                )
        return pending

    def apply_remote(
        self, doc_id: str, update: bytes, expected_vector: bytes, peer: str
    ) -> object:
        try:
            return self._facade.apply_remote(self.space_id, doc_id, update, expected_vector, peer)
        except OrgFsError as exc:
            if exc.code == "resource-exhausted":
                return Pending("dependency")
            raise

    def requester_allowed(self, author: str) -> bool:
        space = self._facade._existing_space(self.space_id)
        if space is None:
            return False
        return author in space.members

    def requester_removed(self, author: str) -> bool:
        space = self._facade._existing_space(self.space_id)
        if space is None:
            return False
        return author in space.removed_members and author not in space.members

    def supplier_allowed(self, node: str) -> bool:
        return bool(self._node_gate(node))

    def local_generation(self) -> int:
        return self._facade._registry(self.space_id).generation


__all__ = [
    "AdmissionViewOf",
    "SpaceSyncHost",
    "StateDispatch",
    "state_command",
]
