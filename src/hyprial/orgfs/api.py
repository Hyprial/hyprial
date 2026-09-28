"""Public API for the org shared-folder document store.

The protocol in this module is deliberately dependency free.  The concrete
implementation lives in :mod:`hyprial.orgfs.docs`; this keeps the daemon and
future store implementations from having to import the tree engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence, TypeAlias

from hyprial.contracts import ipc_errors

from .purge import PurgePlan, PurgeResult, PurgeStatus

NodeRef = str
NodeKind = Literal["dir", "doc", "blob"]
MemberMode = Literal["ro", "rw"]
ChangeKind = Literal["created", "modified", "moved", "removed", "restored"]
PurgeTarget: TypeAlias = dict[str, Any]


class OrgFsError(Exception):
    """A stable, wire-compatible orgfs error."""

    code: str
    details: dict[str, object]

    def __init__(self, code: str, details: dict[str, object] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        message = self.details.pop("message", None) or code
        super().__init__(f"{code}: {message}")


#: Every OrgFsError code a producer may raise.  orgfs codes were inline
#: string literals with no enumeration; this registry (design
#: notes/orgfs-uri/design.md §5) retro-covers them and adds the URI codes.
#: A gate test parses src/hyprial/orgfs for every OrgFsError(<code>, ...) call
#: (literal or ipc_errors constant) and asserts membership, so a new code must
#: be registered here or CI is red.  Proto-facing codes come from ipc_errors.
ORGFS_ERROR_CODES = frozenset(
    {
        "ambiguous-path",
        "blob-unavailable",
        ipc_errors.ORGFS_CONTENT_PENDING,
        "cross-space-uri",
        "invalid-argument",
        "invalid-uri",
        "log-key-conflict",
        "no-holder-online",
        "not-a-member",
        "not-owner",
        "out-of-range",
        "purged",
        "snapshot-barrier",
        "stale-plan",
        "stale-write",
        "too-large",
        "unknown-blob",
        "unknown-doc",
        "unknown-space",
    }
)


@dataclass(frozen=True, slots=True)
class SpaceInfo:
    space_id: str
    name: str
    owner: str
    created_at: str


@dataclass(frozen=True, slots=True)
class MemberInfo:
    space_id: str
    user: str
    mode: MemberMode
    added_by: str
    added_at: str


@dataclass(frozen=True, slots=True)
class NodeInfo:
    space_id: str
    node_id: str
    kind: NodeKind
    name: str
    path: str
    size: int | None
    blob_hash: str | None
    doc_id: str | None
    version: str
    modified_by: str
    modified_via: str | None
    name_conflict: bool
    deleted: bool
    content_state: Literal["arrived", "pending", "unverifiable"] | None = None
    # Canonical ``orgfs:<owner>:<spaceId>:<nodeId>`` — always populated by
    # the two NodeInfo constructors (docs.py ``_node_info``/``_snapshot_info``)
    # through the single ``_node_uri`` producer.  Keyword-only so it appends
    # after the defaulted D4 ``content_state`` without a default of its own
    # (no-fallback rule: an optional field is a compat shim in disguise).
    uri: str = field(kw_only=True)


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    node: NodeInfo
    changed: Literal["position", "content", "both", "created", "removed", "restored"]
    author: str
    actor: str | None
    at: str


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    space_id: str
    kind: ChangeKind
    node: NodeInfo
    old_path: str | None


@dataclass(frozen=True, slots=True)
class SpaceStatus:
    space_id: str
    unconfirmed_commits: int
    durable_holders_seen: tuple[str, ...]
    holders_online: tuple[str, ...]


class OrgDoc(Protocol):
    def doc_id(self) -> str: ...

    def snapshot_json(self) -> str: ...

    def version(self) -> str: ...

    def root(self) -> "DocMap": ...

    def transact(self, mutate: Callable[["DocMap"], None]) -> NodeInfo: ...


class DocMap(Protocol):
    def get(self, key: str, default: object | None = None) -> object: ...

    def keys(self) -> tuple[str, ...]: ...

    def set(self, key: str, value: object) -> None: ...

    def delete(self, key: str) -> None: ...

    def ensure_map(self, key: str) -> "DocMap": ...

    def ensure_list(self, key: str) -> "DocList": ...

    def ensure_text(self, key: str) -> "DocText": ...


class DocList(Protocol):
    def get(self, index: int) -> object: ...

    def len(self) -> int: ...

    def insert(self, index: int, value: object) -> None: ...

    def delete(self, index: int) -> None: ...


class DocText(Protocol):
    def str(self) -> str: ...

    def insert(self, index: int, value: str) -> None: ...

    def delete(self, index: int, length: int) -> None: ...


class Registration(Protocol):
    def close(self) -> None: ...


class OrgFs(Protocol):
    def create_space(self, name: str) -> SpaceInfo: ...

    def spaces(self) -> tuple[SpaceInfo, ...]: ...

    def invite(
        self, space_id: str, user: str, mode: MemberMode = "rw"
    ) -> MemberInfo: ...

    def remove_member(self, space_id: str, user: str) -> None: ...

    def members(self, space_id: str) -> tuple[MemberInfo, ...]: ...

    def join(self, space_id: str) -> SpaceInfo: ...

    def resolve(self, space_id: str, path: str) -> tuple[NodeInfo, ...]: ...

    def stat(self, space_id: str, node: NodeRef) -> NodeInfo: ...

    def listdir(self, space_id: str, node: NodeRef) -> tuple[NodeInfo, ...]: ...

    def read_text(self, space_id: str, node: NodeRef) -> tuple[str, str]: ...

    def write_text(
        self,
        space_id: str,
        node: NodeRef,
        content: str,
        *,
        base_version: str | None = None,
        expect_version: str | None = None,
    ) -> NodeInfo: ...

    def read_bytes(self, space_id: str, node: NodeRef) -> bytes: ...

    def write_bytes(
        self,
        space_id: str,
        node: NodeRef,
        content: bytes,
        *,
        expect_version: str | None = None,
    ) -> NodeInfo: ...

    def export_to(
        self, space_id: str, node: NodeRef, destination: Path
    ) -> NodeInfo: ...

    def import_from(self, space_id: str, node: NodeRef, source: Path) -> NodeInfo: ...

    def mkdir(self, space_id: str, path: str) -> NodeInfo: ...

    def move(
        self, space_id: str, source: NodeRef, destination: NodeRef
    ) -> NodeInfo: ...

    def remove(self, space_id: str, node: NodeRef) -> None: ...

    def watch(
        self, space_id: str, glob: str, callback: Callable[[ChangeEvent], None]
    ) -> Registration: ...

    def open_doc(self, space_id: str, node: NodeRef) -> OrgDoc: ...

    def history(
        self, space_id: str, node: NodeRef, limit: int = 50, before: str | None = None
    ) -> tuple[HistoryEntry, ...]: ...

    def read_at(self, space_id: str, node: NodeRef, version: str) -> bytes: ...

    def stat_at(self, space_id: str, node: NodeRef, version: str) -> NodeInfo: ...

    def trash(self, space_id: str, limit: int = 100) -> tuple[NodeInfo, ...]: ...

    def restore(
        self, space_id: str, node: NodeRef, version: str, *, recursive: bool = True
    ) -> NodeInfo: ...

    def status(self, space_id: str) -> SpaceStatus: ...

    def purge_plan(
        self, space_id: str, targets: Sequence[PurgeTarget]
    ) -> "PurgePlan": ...

    def purge(self, space_id: str, plan_id: str) -> "PurgeResult": ...

    def purge_status(self, space_id: str, plan_id: str) -> "PurgeStatus": ...

    def unban(self, space_id: str, sha: str) -> None: ...

    def serve(
        self, space_id: str, backend: Literal["fs", "memory"] = "fs"
    ) -> Mapping[str, object]: ...

    def checkout(self, space_id: str, enabled: bool) -> Mapping[str, object]: ...
