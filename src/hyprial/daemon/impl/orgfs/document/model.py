"""The local orgfs document and tree engine.

The tree is represented by a pycrdt document containing node metadata and an
append-only move array.  The move array is intentionally separate from the
current parent pointers: replicas replay the same total order and skip a move
which would create a cycle.  That is the small Kleppmann-style movable-tree
core required by P1 and makes concurrent ``A -> B``/``B -> A`` deterministic.
"""

from __future__ import annotations

import base64
import collections.abc
from dataclasses import dataclass, field
from contextlib import nullcontext
from datetime import datetime, timezone
import difflib
from functools import wraps
import json
import threading
import uuid
from typing import TYPE_CHECKING, Any, Callable, Mapping

if TYPE_CHECKING:
    from hyprial.daemon.impl.orgfs.docs import LocalOrgFs

from pycrdt import Array, Doc, Map, Text
from hyprial.kernel import (
    ActorHandle,
    ActorRuntime,
    AdmissionResult)
from hyprial.kernel import EffectLane, EffectRequest

from hyprial.kernel import (
    canonical_orgfs_uri,
    parse_user_uri)

from hyprial.daemon.impl.orgfs.api  import (
    ChangeEvent,
    MemberInfo,
    MemberMode,
    NodeKind,
    NodeRef,
    OrgFsError,
    SpaceInfo)
from hyprial.daemon.impl.orgfs.storage.store  import CommitRecord

ORGFS_TEXT_MAX = 4 * 1024 * 1024
_FACADE_EFFECT_CAPACITY = 128
_SPACE_STATE_CAPACITY = 64
_MUTATING_FACADE_METHODS = frozenset(
    {
        "create_space",
        "invite",
        "remove_member",
        "join",
        "load_space",
        "apply_envelope",
        "install_replacement_snapshot",
        "acknowledge_purge",
        "write_text",
        "write_bytes",
        "mkdir",
        "move",
        "remove",
        "restore",
        "purge_plan",
        "purge",
        "unban",
        "apply_tree_update",
        "apply_content_update",
        "hydrate_content_snapshot",
        "_apply_structured_update",
    }
)
_SPACE_STATE_METHODS = frozenset(
    {
        "create_space",
        "load_space",
        "invite",
        "remove_member",
        "apply_envelope",
        "install_replacement_snapshot",
        "acknowledge_purge",
        "write_text",
        "write_bytes",
        "mkdir",
        "move",
        "remove",
        "restore",
        "purge_plan",
        "purge",
        "unban",
        "apply_tree_update",
        "apply_content_update",
        "hydrate_content_snapshot",
        "_apply_structured_update",
    }
)


@dataclass(frozen=True, slots=True)
class _WatchNotification:
    watch_id: str
    event: ChangeEvent


@dataclass(frozen=True, slots=True)
class _MetaWatchNotification:
    watch_id: str


@dataclass(frozen=True, slots=True)
class _BroadcastPending:
    space_id: str
    records: tuple[CommitRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class _ReconcileReplicaBlobs:
    space_id: str


@dataclass(frozen=True, slots=True)
class _FacadeEffectBatch:
    sequence: int
    effects: tuple[
        _WatchNotification
        | _MetaWatchNotification
        | _ReconcileReplicaBlobs
        | _BroadcastPending,
        ...,
    ]


@dataclass(frozen=True, slots=True)
class _FacadeEffectCompletion:
    operation_id: str
    generation: int
    sequence: int
    failures: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _FacadeEffectResult:
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _InviteMember:
    space_id: str
    user: str
    mode: MemberMode


@dataclass(frozen=True, slots=True)
class _CreateSpace:
    name: str


@dataclass(frozen=True, slots=True)
class _LoadSpace:
    space_id: str


@dataclass(frozen=True, slots=True)
class _RemoveMember:
    space_id: str
    user: str


@dataclass(frozen=True, slots=True)
class _ApplyEnvelope:
    space_id: str
    envelope: bytes


@dataclass(frozen=True, slots=True)
class _InstallReplacement:
    space_id: str
    old_doc_id: str
    new_doc_id: str
    snapshot: bytes


@dataclass(frozen=True, slots=True)
class _AcknowledgePurge:
    space_id: str
    plan_id: str


@dataclass(frozen=True, slots=True)
class _WriteText:
    space_id: str
    node: NodeRef
    content: str
    base_version: str | None
    expect_version: str | None
    create_only: bool


@dataclass(frozen=True, slots=True)
class _WriteBytes:
    space_id: str
    node: NodeRef
    content: bytes
    expect_version: str | None


@dataclass(frozen=True, slots=True)
class _Mkdir:
    space_id: str
    path: str


@dataclass(frozen=True, slots=True)
class _Move:
    space_id: str
    source: NodeRef
    destination: NodeRef


@dataclass(frozen=True, slots=True)
class _RemoveNode:
    space_id: str
    node: NodeRef


@dataclass(frozen=True, slots=True)
class _Restore:
    space_id: str
    node: NodeRef
    version: str
    recursive: bool


@dataclass(frozen=True, slots=True)
class _PurgePlanCommand:
    space_id: str
    targets: tuple[tuple[tuple[str, object], ...], ...]


@dataclass(frozen=True, slots=True)
class _Purge:
    space_id: str
    plan_id: str


@dataclass(frozen=True, slots=True)
class _Unban:
    space_id: str
    sha: str


@dataclass(frozen=True, slots=True)
class _ApplyTreeUpdate:
    space_id: str
    update: bytes


@dataclass(frozen=True, slots=True)
class _ApplyContentUpdate:
    space_id: str
    node: NodeRef
    update: bytes


@dataclass(frozen=True, slots=True)
class _HydrateContentSnapshot:
    space_id: str
    node: NodeRef
    expected_doc_id: str
    snapshot: bytes


@dataclass(frozen=True, slots=True)
class _ApplyStructuredUpdate:
    space_id: str
    node_id: str
    doc_id: str
    client_id: int
    base_state: bytes
    update: bytes


_SpaceStateOperation = (
    _CreateSpace
    | _LoadSpace
    | _InviteMember
    | _RemoveMember
    | _ApplyEnvelope
    | _InstallReplacement
    | _AcknowledgePurge
    | _WriteText
    | _WriteBytes
    | _Mkdir
    | _Move
    | _RemoveNode
    | _Restore
    | _PurgePlanCommand
    | _Purge
    | _Unban
    | _ApplyTreeUpdate
    | _ApplyContentUpdate
    | _HydrateContentSnapshot
    | _ApplyStructuredUpdate
)


@dataclass(frozen=True, slots=True)
class _SpaceStateCommand:
    operation_id: str
    generation: int
    operation: _SpaceStateOperation
    effect_operation_id: str | None
    effect_generation: int


@dataclass(frozen=True, slots=True)
class _SpaceStateFailure:
    kind: str
    code: str
    message: str
    details: tuple[tuple[str, object], ...]


@dataclass(frozen=True, slots=True)
class _SpaceStateOutcome:
    value: object = None
    error: _SpaceStateFailure | None = None


@dataclass(slots=True)
class _SpaceStateWaiter:
    done: threading.Event
    value: object = None
    error: _SpaceStateFailure | None = None


@dataclass(slots=True)
class _SpaceStateOwner:
    runtime: ActorRuntime
    actor: ActorHandle
    effects: EffectLane[_SpaceStateCommand, _SpaceStateOutcome]
    generation: int = 1
    commands: dict[str, _SpaceStateCommand] = field(default_factory=dict)
    waiters: dict[str, _SpaceStateWaiter] = field(default_factory=dict)
    submitted: set[str] = field(default_factory=set)
    deferred: list[_SpaceStateCommand] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _facade_locked(method: Callable[..., Any]) -> Callable[..., Any]:
    """Serialize every facade mutation with inbound envelope application."""

    @wraps(method)
    def locked(self: "LocalOrgFs", *args: Any, **kwargs: Any) -> Any:
        if getattr(self._space_state_context, "running", False):
            with self._space_lock(str(args[0])):
                return method(self, *args, **kwargs)
        if method.__name__ in _SPACE_STATE_METHODS:
            return self._submit_space_state(method, args, kwargs)
        outermost = not hasattr(self._effect_context, "pending")
        if outermost:
            self._effect_context.pending = []
        result: Any = None
        wait_for_completion = False
        operation_id: str | None = None
        authority_lock = (
            self._lock
            if method.__name__ in {"create_space", "spaces"}
            else (
                nullcontext()
                if method.__name__ == "join"
                else self._space_lock(str(args[0]))
            )
        )
        try:
            with authority_lock:
                if (
                    outermost
                    and method.__name__ in _MUTATING_FACADE_METHODS
                    and self._effect_closed
                ):
                    raise OrgFsError("unavailable", {"message": "orgfs is closing"})
                generation = self._effect_generation
                reserved = False
                if (
                    outermost
                    and method.__name__ in _MUTATING_FACADE_METHODS
                    and self._needs_post_commit_effects()
                ):
                    self._ensure_effect_lane()
                    operation_id = uuid.uuid4().hex
                    admission = self._effect_lane.reserve(operation_id, generation)
                    if admission is not AdmissionResult.ACCEPTED:
                        raise OrgFsError(
                            "resource-exhausted",
                            {
                                "message": "orgfs post-commit effect lane is "
                                + admission.value,
                                "operationId": operation_id,
                            },
                        )
                    reserved = True
                    with self._lock:
                        self._effect_waiters[operation_id] = threading.Event()
                try:
                    result = method(self, *args, **kwargs)
                finally:
                    if outermost:
                        effects = tuple(self._effect_context.pending)
                        if operation_id is not None and reserved:
                            if effects:
                                with self._lock:
                                    self._effect_sequence += 1
                                    sequence = self._effect_sequence
                                request = EffectRequest(
                                    operation_id,
                                    generation,
                                    _FacadeEffectBatch(sequence, effects),
                                )
                                self._effect_lane.submit_reserved(request)
                                wait_for_completion = True
                            else:
                                self._effect_lane.cancel_reservation(
                                    operation_id, generation
                                )
                                with self._lock:
                                    self._effect_waiters.pop(operation_id, None)
        finally:
            if outermost:
                del self._effect_context.pending
        if (
            wait_for_completion
            and not getattr(self._effect_context, "in_effect_worker", False)
        ):
            self._wait_for_effect(operation_id)
        return result

    return locked


def _version(tree_frontier: int, content_frontier: int) -> str:
    """Encode a version as an opaque, URL-safe JSON value."""

    raw = json.dumps(
        {"treeFrontier": tree_frontier, "contentFrontier": content_frontier},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _version_number(value: str) -> int:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded))
        return int(decoded["treeFrontier"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return -1


def _encode_content_frontier(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode_content_frontier(value: object) -> bytes | None:
    if not isinstance(value, str):
        return None
    try:
        return base64.b64decode(value, validate=True)
    except ValueError:
        return None


@dataclass
class _Node:
    node_id: str
    parent: str | None
    name: str
    kind: NodeKind
    doc_id: str | None = None
    blob_hash: str | None = None
    size: int | None = None
    required_content_frontier: bytes | None = None
    ref_size: int | None = None
    ref_sha256: str | None = None
    deleted: bool = False
    version: str = ""
    modified_by: str = ""
    modified_via: str | None = None
    content_frontier: int = 0


@dataclass(frozen=True)
class _NodeSnapshot:
    node_id: str
    parent: str | None
    name: str
    kind: NodeKind
    doc_id: str | None
    blob_hash: str | None
    size: int | None
    required_content_frontier: bytes | None
    ref_size: int | None
    ref_sha256: str | None
    deleted: bool
    version: str
    modified_by: str
    modified_via: str | None
    content: bytes


#: A version layer that drops a node present in the layer below it.
_ABSENT_NODE: Any = object()
#: Layers stacked before a version is flattened back to one full map.  Bounds
#: lookup depth; flattening copies pointers only, never content.
_SNAPSHOT_FLATTEN_DEPTH = 64


class _SpaceSnapshot(collections.abc.Mapping):
    """One version of a space: the nodes that changed, over the previous one.

    Every write records a version.  Copying the whole space per version made
    each write cost O(space) and kept that copy forever; a layer holds only
    what changed and shares every unchanged ``_NodeSnapshot`` with the layer
    below.  Readers see an ordinary read-only ``node_id -> _NodeSnapshot`` map.
    """

    __slots__ = ("_base", "_changes", "_depth")

    def __init__(
        self,
        base: "_SpaceSnapshot | None",
        changes: dict[str, Any],
    ) -> None:
        if base is not None and base._depth + 1 >= _SNAPSHOT_FLATTEN_DEPTH:
            flat = base._materialize()
            for node_id, entry in changes.items():
                if entry is _ABSENT_NODE:
                    flat.pop(node_id, None)
                else:
                    flat[node_id] = entry
            base, changes = None, flat
        self._base = base
        self._changes = changes
        self._depth = 0 if base is None else base._depth + 1

    def _materialize(self) -> dict[str, _NodeSnapshot]:
        layers: list[_SpaceSnapshot] = []
        layer: _SpaceSnapshot | None = self
        while layer is not None:
            layers.append(layer)
            layer = layer._base
        result: dict[str, _NodeSnapshot] = {}
        for layer in reversed(layers):
            for node_id, entry in layer._changes.items():
                if entry is _ABSENT_NODE:
                    result.pop(node_id, None)
                else:
                    result[node_id] = entry
        return result

    def __getitem__(self, node_id: str) -> _NodeSnapshot:
        layer: _SpaceSnapshot | None = self
        while layer is not None:
            if node_id in layer._changes:
                entry = layer._changes[node_id]
                if entry is _ABSENT_NODE:
                    raise KeyError(node_id)
                return entry
            layer = layer._base
        raise KeyError(node_id)

    def __iter__(self) -> collections.abc.Iterator[str]:
        return iter(self._materialize())

    def __len__(self) -> int:
        return len(self._materialize())


def _content_fingerprint(document: "_ContentDocument") -> tuple[bytes, bytes]:
    """Identify a content state without encoding its text.

    The state vector alone misses delete-only edits; the update relative to
    that state vector carries exactly the delete set, so the pair identifies
    the document state.
    """

    state = document.doc.get_state()
    return state, document.doc.get_update(state)


@dataclass
class _History:
    node: _NodeSnapshot
    changed: str
    author: str
    actor: str | None
    at: str


@dataclass
class _Watch:
    glob: str
    callback: Callable[[ChangeEvent], None]
    watch_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    events: list[ChangeEvent] = field(default_factory=list)
    closed: bool = False

    def close(self) -> None:
        self.closed = True


@dataclass
class _MetaWatch:
    callback: Callable[[], None]
    watch_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    closed: bool = False

    def close(self) -> None:
        self.closed = True


class TreeDocument:
    """A pycrdt-backed tree log with deterministic move replay.

    Values in the Y.Map are JSON strings rather than nested Python objects so
    the update stream stays stable across pycrdt releases.  ``moves`` is a
    Y.Array of JSON move records; the projection is pure and therefore safe to
    rebuild after receiving an update from another replica.
    """

    def __init__(
        self, *, client_id: int | None = None, update: bytes | None = None
    ) -> None:
        self.doc = Doc(client_id=client_id)
        if update is not None:
            self.doc.apply_update(update)
        self.nodes = self.doc.get("nodes", type=Map)
        self.moves = self.doc.get("moves", type=Array)
        if "root" not in self.nodes:
            self._write_node(
                {
                    "node_id": "root",
                    "parent": None,
                    "name": "",
                    "kind": "dir",
                    "doc_id": None,
                    "blob_hash": None,
                    "deleted": False,
                }
            )

    @staticmethod
    def _wire_node(node: Mapping[str, Any]) -> str:
        return json.dumps(dict(node), sort_keys=True, separators=(",", ":"))

    def _write_node(self, node: Mapping[str, Any]) -> None:
        self.nodes[str(node["node_id"])] = self._wire_node(node)

    def record_node(self, node: _Node) -> None:
        value = {
                "node_id": node.node_id,
                "parent": node.parent,
                "name": node.name,
                "kind": getattr(node, "kind", "dir"),
                "doc_id": getattr(node, "doc_id", None),
                "blob_hash": getattr(node, "blob_hash", None),
                "deleted": getattr(node, "deleted", False),
        }
        if node.kind == "blob" and node.size is not None:
            value["size"] = node.size
        if node.kind == "doc":
            if node.required_content_frontier is not None:
                value["contentFrontier"] = _encode_content_frontier(
                    node.required_content_frontier
                )
            if node.ref_size is not None:
                value["refSize"] = node.ref_size
            if node.ref_sha256 is not None:
                value["refSha256"] = node.ref_sha256
        self._write_node(value)

    def record_move(
        self,
        node: _Node,
        *,
        timestamp: int,
        peer: str,
        old_parent: str | None,
        new_parent: str | None,
        name: str,
    ) -> None:
        with self.doc.transaction():
            self.moves.append(
                json.dumps(
                    {
                        "ts": timestamp,
                        "peer": peer,
                        "node": node.node_id,
                        "oldParent": old_parent,
                        "newParent": new_parent,
                        "name": name,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )

    def record(self, node: _Node) -> None:
        with self.doc.transaction():
            self.record_node(node)

    def get_update(self, state: bytes | None = None) -> bytes:
        return self.doc.get_update(state)

    def apply_update(self, update: bytes) -> None:
        self.doc.apply_update(update)

    def _raw_nodes(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for node_id, raw in (self.nodes.to_py() or {}).items():
            if isinstance(raw, str):
                try:
                    result[node_id] = json.loads(raw)
                except json.JSONDecodeError:
                    continue
        return result

    def materialize(self) -> dict[str, dict[str, Any]]:
        nodes = self._raw_nodes()
        moves: list[dict[str, Any]] = []
        for raw in self.moves.to_py() or []:
            if isinstance(raw, str):
                try:
                    moves.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        parents = {key: value.get("parent") for key, value in nodes.items()}
        names = {key: value.get("name", "") for key, value in nodes.items()}

        def cycle(node_id: str, parent: str | None) -> bool:
            seen: set[str] = set()
            cursor = parent
            while cursor is not None:
                if cursor == node_id:
                    return True
                if cursor in seen:
                    return True
                seen.add(cursor)
                cursor = parents.get(cursor)
            return False

        ordered = sorted(
            moves,
            key=lambda item: (
                int(item.get("ts", 0)),
                str(item.get("peer", "")),
                str(item.get("node", "")),
            ),
        )
        for move in ordered:
            node_id = str(move.get("node", ""))
            parent = move.get("newParent")
            if node_id not in nodes or parent is not None and parent not in nodes:
                continue
            if cycle(node_id, parent):
                # The important safety invariant: never materialize a cycle.
                continue
            parents[node_id] = parent
            if "name" in move:
                names[node_id] = str(move["name"])
        for node_id, node in nodes.items():
            node["parent"] = parents.get(node_id)
            node["name"] = names.get(node_id, node.get("name", ""))
        return nodes

    def has_cycle(self) -> bool:
        nodes = self.materialize()
        for node_id, node in nodes.items():
            seen: set[str] = set()
            cursor = node.get("parent")
            while cursor is not None:
                if cursor == node_id or cursor in seen:
                    return True
                seen.add(cursor)
                cursor = nodes.get(cursor, {}).get("parent")
        return False


class _ContentDocument:
    def __init__(
        self,
        text: str = "",
        *,
        client_id: int | None = None,
        update: bytes | None = None,
    ) -> None:
        self.doc = Doc(client_id=client_id)
        if update is not None:
            self.doc.apply_update(update)
        self.text = self.doc.get("text", type=Text)
        if text:
            with self.doc.transaction():
                self.text.insert(0, text)

    def value(self) -> str:
        return self.text.to_py() or ""

    def set(self, value: str) -> None:
        current = self.value()
        if value == current:
            return
        shared_prefix = 0
        prefix_limit = min(len(current), len(value))
        while (
            shared_prefix < prefix_limit
            and current[shared_prefix] == value[shared_prefix]
        ):
            shared_prefix += 1
        shared_suffix = 0
        suffix_limit = min(len(current), len(value)) - shared_prefix
        while (
            shared_suffix < suffix_limit
            and current[len(current) - shared_suffix - 1]
            == value[len(value) - shared_suffix - 1]
        ):
            shared_suffix += 1
        old_stop = len(current) - shared_suffix
        new_stop = len(value) - shared_suffix
        matcher = difflib.SequenceMatcher(
            a=current[shared_prefix:old_stop],
            b=value[shared_prefix:new_stop],
            autojunk=False,
        )
        edits = [
            (
                tag,
                i1 + shared_prefix,
                i2 + shared_prefix,
                j1 + shared_prefix,
                j2 + shared_prefix,
            )
            for tag, i1, i2, j1, j2 in matcher.get_opcodes()
            if tag != "equal"
        ]
        # pycrdt.Text/Yrs positions count UTF-8 bytes, while SequenceMatcher
        # positions count Python Unicode code points.  Resolve only the edit
        # boundaries, walking the old string once even when there are many edits.
        boundaries = sorted(
            {index for _, i1, i2, _, _ in edits for index in (i1, i2)}
        )
        byte_offsets: dict[int, int] = {}
        previous_index = 0
        previous_offset = 0
        for index in boundaries:
            previous_offset += len(current[previous_index:index].encode())
            byte_offsets[index] = previous_offset
            previous_index = index

        with self.doc.transaction():
            # Later ranges cannot disturb the positions of earlier ranges.
            for _tag, i1, i2, j1, j2 in reversed(edits):
                start = byte_offsets[i1]
                stop = byte_offsets[i2]
                if stop > start:
                    del self.text[start:stop]
                if j2 > j1:
                    self.text.insert(start, value[j1:j2])

    def update(self, state: bytes) -> None:
        self.doc.apply_update(state)

    def export(self) -> bytes:
        return self.doc.get_update()


@dataclass
class _Space:
    info: SpaceInfo
    tree_doc_id: str
    tree: TreeDocument
    meta: Doc
    nodes: dict[str, _Node]
    contents: dict[str, _ContentDocument]
    members: dict[str, MemberInfo]
    removed_members: set[str]
    history: dict[str, list[_History]]
    snapshots: dict[str, Mapping[str, _NodeSnapshot]]
    revision: int = 0
    watches: list[_Watch] = field(default_factory=list)
    meta_watches: list[_MetaWatch] = field(default_factory=list)
    #: node_id -> (entry in the latest version, content object, fingerprint);
    #: lets the next version reuse an entry whose node and content are unchanged.
    snapshot_marks: dict[str, tuple[_NodeSnapshot, Any, Any]] = field(
        default_factory=dict
    )


class _OrgDoc:
    def __init__(self, document: _ContentDocument, doc_id: str) -> None:
        self._document = document
        self._doc_id = doc_id

    def doc_id(self) -> str:
        return self._doc_id

    def snapshot_json(self) -> str:
        return json.dumps(
            {"text": self._document.value()}, ensure_ascii=False, sort_keys=True
        )


def _node_uri(info: SpaceInfo, node_id: str) -> str:
    """The one NodeInfo.uri producer (design docs/notes/orgfs-uri/design.md §3).

    The owner comes from the authoritative SpaceInfo only, with the
    ``user:`` prefix stripped (the URI carries the bare identity — a
    segment may never contain ``:``).  A non-user owner fails loud with a
    typed error instead of minting a malformed URI.
    """

    owner = parse_user_uri(info.owner)
    if owner is None:
        raise OrgFsError(
            "invalid-argument",
            {"message": "space owner is not a user: identity"},
        )
    return canonical_orgfs_uri(owner, info.space_id, node_id)



def _raise_invalid(message: str) -> Any:
    raise OrgFsError("invalid-argument", {"message": message})
