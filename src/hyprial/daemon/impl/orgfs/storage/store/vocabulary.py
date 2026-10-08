"""Local journal and admission boundary for an orgfs space.

The store deliberately keeps the journal as the durable commit point.  The
``.loro`` files retain their frozen names from the orgfs design even though
the selected G1 document engine is pycrdt/Yjs.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Iterable, Literal, Mapping, Protocol, TypeAlias

from pycrdt import Doc, Map

from hyprial.identity import (
    directory_owner_principal,
    parse_protected_directory_node_id,
    parse_protected_directory_doc_id,
    protected_directory_node_id,
    protected_directory_doc_id,
)


ORGFS_ENVELOPE_BYTES = 2 * 1024 * 1024
ORGFS_PAGE_BYTES = 1_048_576
ORGFS_INLINE_UPDATE_BYTES = 512_000
PYCRDT_CLIENT_ID_BITS = 53
PYCRDT_CLIENT_ID_MAX = (1 << PYCRDT_CLIENT_ID_BITS) - 1
_MAX_PENDING_IMPORTS = 512
_MAX_PENDING_IMPORT_AGE_SECONDS = 60 * 60
#: Replica key components stay 15 bytes below the usual 255-byte filesystem
#: component limit.  This covers the longest ``.tmp-XXXXXXXX`` suffix plus a
#: byte of margin and is shared by ingress writer validation.
MAX_REPLICA_SEGMENT_LENGTH = 240

_SAFE_WRITER = re.compile(
    rf"^[A-Za-z0-9._:-]{{1,{MAX_REPLICA_SEGMENT_LENGTH}}}$"
)
_EMPTY_UPDATE = b"\x00\x00"
_DOC_IDS = {"meta"}
_LOG = logging.getLogger(__name__)

DocMutator: TypeAlias = Callable[[Doc], None]
FaultHook: TypeAlias = Callable[[str], None]


class StoreError(RuntimeError):
    """A durable store operation was rejected."""

    def __init__(self, code: str, message: str | None = None, **details: Any) -> None:
        self.code = code
        self.details = details
        super().__init__(f"{code}: {message or code}")


@dataclass(frozen=True, slots=True)
class CommitRecord:
    writer: str
    seq: int
    envelope_bytes: bytes
    version: str

    @property
    def envelope(self) -> bytes:
        return self.envelope_bytes


@dataclass(frozen=True, slots=True)
class ExportPage:
    envelopes: tuple[bytes, ...]
    next: str | None
    snapshots: tuple[Mapping[str, Any], ...] = ()

    @property
    def next_cursor(self) -> str | None:
        return self.next


@dataclass(frozen=True, slots=True)
class ImportResult:
    status: Literal["applied", "pending", "rejected"]
    code: str | None = None
    record: CommitRecord | None = None
    details: Mapping[str, Any] | None = None

    @property
    def outcome(self) -> str:
        return self.status

    @property
    def applied(self) -> bool:
        return self.status == "applied"


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    space_id: str
    doc_id: str
    snapshot_id: str
    frontier: str
    shallow_since: str | None
    snapshot_bytes: bytes
    kind: Literal["full", "shallow"] = "full"
    redactions: tuple[str, ...] = ()
    supersedes: str | None = None
    authors_before: Mapping[str, int] = field(default_factory=dict)
    created_by: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "type": "orgfs-snapshot",
            "spaceId": self.space_id,
            "docId": self.doc_id,
            "snapshotId": self.snapshot_id,
            "kind": self.kind,
            "frontier": self.frontier,
            "shallowSince": self.shallow_since,
            "redactions": list(self.redactions),
            "supersedes": self.supersedes,
            "authorsBefore": dict(self.authors_before),
            "sizeBytes": len(self.snapshot_bytes),
            "createdBy": dict(self.created_by),
            "createdAt": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class RetirementRecord:
    old_physical_doc_id: str
    replacement_doc_id: str
    plan_id: str
    snapshot_id: str
    retired_at: str

    def details(self) -> dict[str, str]:
        return {
            "retiredDocId": self.old_physical_doc_id,
            "replacementDocId": self.replacement_doc_id,
            "planId": self.plan_id,
        }


class SpaceStore(Protocol):
    def commit(
        self, doc_id: str, mutate: DocMutator, *, author: str, actor: str | None
    ) -> CommitRecord: ...

    def frontier(self, doc_id: str) -> bytes: ...

    def committed_update(self, doc_id: str, since: bytes) -> bytes: ...

    def export_since(
        self, doc_id: str, vv: bytes, *, cursor: str | None, max_bytes: int
    ) -> ExportPage: ...

    def import_envelope(self, envelope: bytes, *, supplier: str) -> ImportResult: ...

    def unbroadcast(self) -> Iterable[CommitRecord]: ...

    def mark_broadcast(self, writer: str, seq: int, *, doc_id: str) -> None: ...

    def snapshot(
        self, doc_id: str, *, shallow_since: bytes | None
    ) -> SnapshotManifest: ...

    def admission(
        self, envelope: bytes
    ) -> Literal[
        "ok",
        "not-a-member",
        "pending-dependency",
        "pending-meta",
        "snapshot-barrier",
        "unknown-doc",
    ]: ...


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeError) as exc:
        raise StoreError("invalid-argument", "invalid base64 value") from exc


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _plain(value: Any) -> Any:
    if isinstance(value, Map):
        return {str(k): _plain(v) for k, v in (value.to_py() or {}).items()}
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _doc_roots(doc: Doc) -> dict[str, Any]:
    roots: dict[str, Any] = {}
    # ``Doc.items()`` exposes ``None`` placeholders for typed shared values
    # after an update is imported; ``get(..., type=Map)`` materializes the
    # integrated value correctly.
    for key in doc.keys():
        value = doc.get(str(key), type=Map)
        roots[str(key)] = _plain(value)
    return roots


def _empty_meta(doc: Doc) -> None:
    # These roots are part of the frozen P2 meta layout. Creating them in the
    # first meta transaction makes the genesis update self-contained.
    for name in (
        "space",
        "members",
        "writers",
        "purgeAcks",
        "docs",
        "retirements",
        "purgeList",
        "purgePlans",
        "snapshotPoints",
        "purgeAudit",
    ):
        if name not in doc:
            doc[name] = Map()


def _wire_doc_id(doc_id: str) -> str:
    if doc_id in _DOC_IDS:
        return doc_id
    try:
        protected = parse_protected_directory_doc_id(doc_id)
    except ValueError:
        protected = None
    if protected is not None:
        return doc_id
    for prefix in ("doc-", "tree-"):
        if doc_id.startswith(prefix):
            try:
                parsed = uuid.UUID(doc_id.removeprefix(prefix))
            except ValueError:
                break
            if str(parsed) == doc_id.removeprefix(prefix):
                return doc_id
    raise StoreError("invalid-argument", "invalid document id", doc_id=doc_id)


def _tree_node_operation_allowed(
    node_id: str,
    node: Mapping[str, Any],
    *,
    principal: str,
    space_owner: str | None,
    space_id: str,
) -> bool:
    """Judge one authored tree row from ids, not its CRDT winning state."""

    parent = node.get("parent")
    name = node.get("name")
    if node.get("node_id") != node_id:
        return False
    if node_id == "root":
        try:
            return bool(
                space_owner
                and principal == directory_owner_principal(space_owner)
            )
        except ValueError:
            return False
    if not isinstance(parent, str) or not isinstance(name, str):
        return False
    try:
        identity = parse_protected_directory_node_id(node_id)
        parent_identity = parse_protected_directory_node_id(parent)
        if identity is not None:
            if identity[0] != space_id or principal != identity[1]:
                return False
            path = identity[2]
            parent_path, separator, expected_name = path.rpartition("/")
            expected_parent = (
                protected_directory_node_id(
                    space_id,
                    parent_path,
                    space_owner=space_owner,
                )
                if separator
                else "root"
            )
            if parent != expected_parent or name != expected_name:
                return False
        else:
            result_path = (
                name
                if parent == "root"
                else (
                    f"{parent_identity[2]}/{name}"
                    if parent_identity is not None
                    else None
                )
            )
            expected = (
                protected_directory_node_id(
                    space_id,
                    result_path,
                    space_owner=space_owner,
                )
                if result_path is not None
                else None
            )
            if expected is not None:
                return False
        linked_doc_id = str(node.get("doc_id", ""))
        linked_identity = parse_protected_directory_doc_id(linked_doc_id)
        if linked_identity is not None and identity is None:
            return False
        expected_doc_id = (
            protected_directory_doc_id(
                space_id,
                identity[2],
                space_owner=space_owner,
            )
            if identity is not None
            else None
        )
        if (
            node.get("kind") == "doc"
            and not node.get("deleted")
            and (linked_identity is not None or expected_doc_id is not None)
            and linked_doc_id != expected_doc_id
        ):
            return False
    except ValueError:
        return False
    return True


def _writer_is_safe(writer: str) -> bool:
    return bool(_SAFE_WRITER.fullmatch(writer))


def _read_var_uint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > 63:
            break
    raise StoreError("invalid-argument", "invalid Yjs state vector")


def _write_var_uint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varuint cannot be negative")
    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _decode_state_vector(value: bytes) -> dict[int, int]:
    if not value:
        return {}
    count, offset = _read_var_uint(value, 0)
    result: dict[int, int] = {}
    for _ in range(count):
        client, offset = _read_var_uint(value, offset)
        clock, offset = _read_var_uint(value, offset)
        result[client] = clock
    if offset != len(value):
        raise StoreError("invalid-argument", "invalid Yjs state vector")
    return result


def _encode_state_vector(value: Mapping[int, int]) -> bytes:
    items = sorted(
        (int(client), int(clock)) for client, clock in value.items() if clock > 0
    )
    return b"".join(
        (_write_var_uint(len(items)),)
        + tuple(
            part
            for client, clock in items
            for part in (_write_var_uint(client), _write_var_uint(clock))
        )
    )


def _state_covers(actual: bytes, required: bytes) -> bool:
    actual_state = _decode_state_vector(actual)
    return all(
        actual_state.get(client, 0) >= clock
        for client, clock in _decode_state_vector(required).items()
    )


# Public verification helpers used by the orgfs facade.  Keep the private
# spellings above as compatibility aliases for the admission tests and the
# store's older internal call sites.
decode_state_vector = _decode_state_vector
state_covers = _state_covers


def _changed_meta_entries(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> set[tuple[str, str | None]]:
    changed: set[tuple[str, str | None]] = set()
    for root in set(before) | set(after):
        old = before.get(root)
        new = after.get(root)
        if old == new:
            continue
        if isinstance(old, dict) and isinstance(new, dict):
            for key in set(old) | set(new):
                if old.get(key) != new.get(key):
                    changed.add((root, str(key)))
        else:
            changed.add((root, None))
    return changed
