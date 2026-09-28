"""Local journal and admission boundary for an orgfs space.

The store deliberately keeps the journal as the durable commit point.  The
``.loro`` files retain their frozen names from the orgfs design even though
the selected G1 document engine is pycrdt/Yjs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, Protocol, TypeAlias

from pycrdt import Doc, Map


ORGFS_ENVELOPE_BYTES = 2 * 1024 * 1024
ORGFS_PAGE_BYTES = 1_048_576
ORGFS_INLINE_UPDATE_BYTES = 512_000
PYCRDT_CLIENT_ID_BITS = 53
PYCRDT_CLIENT_ID_MAX = (1 << PYCRDT_CLIENT_ID_BITS) - 1

_SAFE_WRITER = re.compile(r"^[A-Za-z0-9._:-]+$")
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
        "ok", "not-a-member", "pending-meta", "snapshot-barrier", "unknown-doc"
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
    for prefix in ("doc-", "tree-"):
        if doc_id.startswith(prefix):
            try:
                parsed = uuid.UUID(doc_id.removeprefix(prefix))
            except ValueError:
                break
            if str(parsed) == doc_id.removeprefix(prefix):
                return doc_id
    raise StoreError("invalid-argument", "invalid document id", doc_id=doc_id)


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


class LocalSpaceStore:
    """A file-backed :class:`SpaceStore` for one space.

    ``state_dir`` is the already-selected harness state root.  The class does
    not inspect or rewrite ``HYPRIAL_HOME``; callers use the repository's
    existing state-root selector before constructing it.
    """

    def __init__(
        self,
        state_dir: Path | str,
        space_id: str = "default",
        *,
        node_id: str = "node",
        blob_store: Any | None = None,
        fault_hook: FaultHook | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.space_id = str(space_id)
        self.node_id = str(node_id)
        self.blob_store = blob_store
        self.fault_hook = fault_hook
        self._lock = threading.RLock()
        self._incarnation = secrets.token_hex(16)
        self._lanes: dict[tuple[str, str | None], str] = {}
        self._docs: dict[str, Doc] = {}
        self._holders: dict[str, tuple[bool, dict[str, bytes]]] = {}
        self._last_drained: tuple[bytes, ...] = ()
        self._local_registration_ids: dict[str, set[int]] = {}
        self._root = self.state_dir / "orgfs" / "spaces" / self.space_id
        (self._root / "docs").mkdir(parents=True, exist_ok=True)
        self._journal_path = self._root / "journal.sqlite3"
        self._db = sqlite3.connect(self._journal_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA secure_delete=ON")
        # fsync of the journal is the design's local orgfs commit point (§3.4).
        self._db.execute("PRAGMA synchronous=FULL")  # durability: FULL -- commit point
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS commits (
                writer TEXT NOT NULL,
                seq INTEGER NOT NULL,
                doc_id TEXT NOT NULL,
                envelope_bytes BLOB NOT NULL,
                version BLOB NOT NULL,
                committed_at TEXT NOT NULL,
                broadcast_at TEXT,
                PRIMARY KEY (writer, seq, doc_id)
            );
            CREATE INDEX IF NOT EXISTS commits_doc ON commits(doc_id);
            CREATE TABLE IF NOT EXISTS pending (
                writer TEXT NOT NULL,
                seq INTEGER NOT NULL,
                doc_id TEXT NOT NULL,
                envelope_bytes BLOB NOT NULL,
                supplier TEXT NOT NULL,
                PRIMARY KEY (writer, seq, doc_id)
            );
            CREATE TABLE IF NOT EXISTS meta_frontiers (
                token BLOB PRIMARY KEY,
                owner TEXT,
                membership_json TEXT NOT NULL,
                meta_update BLOB
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                snapshot_id TEXT NOT NULL,
                doc_id TEXT NOT NULL,
                frontier BLOB NOT NULL,
                snapshot_bytes BLOB NOT NULL,
                manifest_json TEXT NOT NULL,
                PRIMARY KEY (doc_id, snapshot_id)
            );
            CREATE TABLE IF NOT EXISTS purge_plans (
                plan_id TEXT PRIMARY KEY,
                plan_json TEXT NOT NULL,
                targets_json TEXT NOT NULL
            );
            """
        )
        if "meta_update" not in {
            str(row[1]) for row in self._db.execute("PRAGMA table_info(meta_frontiers)")
        }:
            # n2 / pre-release m12: no orgfs journal containing this schema has
            # shipped.  A NULL from a development database is therefore
            # replayed lazily; there is no production migration to backfill.
            self._db.execute("ALTER TABLE meta_frontiers ADD COLUMN meta_update BLOB")
        self._db.commit()
        self._load_documents()
        self._rebuild_meta_frontiers()
        self._replay_journal()
        self._rebuild_meta_frontiers()
        if self.blob_store is not None and hasattr(
            self.blob_store, "register_purge_checker"
        ):
            self.blob_store.register_purge_checker(self.space_id, self.purge_listed)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def incarnation(self) -> str:
        return self._incarnation

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None  # type: ignore[assignment]

    def __enter__(self) -> "LocalSpaceStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _fault(self, point: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(point)

    def _doc_path(self, doc_id: str) -> Path:
        _wire_doc_id(doc_id)
        if doc_id == "meta":
            return self._root / "meta.loro"
        if doc_id.startswith("tree-"):
            return self._root / "tree.loro"
        return self._root / "docs" / f"{doc_id}.loro"

    @staticmethod
    def _client_id(writer: str) -> int:
        digest = hashlib.sha256(writer.encode()).digest()
        return int.from_bytes(digest[:7], "big") >> (56 - PYCRDT_CLIENT_ID_BITS)

    def _new_doc(self, doc_id: str, *, client_id: int | None = None) -> Doc:
        if client_id is None:
            client_id = self._client_id(f"{self.node_id}.{self._incarnation}.store")
        return Doc(client_id=client_id)

    def _load_documents(self) -> None:
        meta_path = self._doc_path("meta")
        meta = self._new_doc("meta")
        if meta_path.exists():
            try:
                meta.apply_update(meta_path.read_bytes())
            except (OSError, ValueError) as exc:
                raise StoreError(
                    "invalid-argument", f"cannot load {meta_path}"
                ) from exc
        self._docs["meta"] = meta
        active_tree = self.active_tree_doc_id()
        if active_tree is not None:
            tree = self._new_doc(active_tree)
            tree_path = self._doc_path(active_tree)
            if tree_path.exists():
                try:
                    tree.apply_update(tree_path.read_bytes())
                except (OSError, ValueError) as exc:
                    raise StoreError(
                        "invalid-argument", f"cannot load {tree_path}"
                    ) from exc
            self._docs[active_tree] = tree
        for path in sorted((self._root / "docs").glob("*.loro")):
            doc_id = _wire_doc_id(path.stem)
            if not doc_id.startswith("doc-"):
                raise StoreError("invalid-argument", f"invalid document path {path}")
            if self.retired(doc_id) is not None:
                path.unlink()
                continue
            self._docs[doc_id] = self._new_doc(doc_id)
            self._docs[doc_id].apply_update(path.read_bytes())

    def _replay_journal(self) -> None:
        rows = self._db.execute(
            "SELECT envelope_bytes FROM commits ORDER BY rowid"
        ).fetchall()
        for row in rows:
            envelope = self._decode_envelope(bytes(row[0]))
            update = self._update_bytes(envelope)
            doc = self._doc(envelope["docId"])
            try:
                doc.apply_update(update)
            except (ValueError, TypeError) as exc:
                raise StoreError(
                    "invalid-argument", "journal update cannot be applied"
                ) from exc
        self._persist_all_documents()

    def _persist_all_documents(self) -> None:
        for doc_id, doc in self._docs.items():
            path = self._doc_path(doc_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            temporary.write_bytes(doc.get_update())
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, path)

    def _doc(self, doc_id: str) -> Doc:
        _wire_doc_id(doc_id)
        if doc_id not in self._docs:
            self._docs[doc_id] = self._new_doc(doc_id)
        return self._docs[doc_id]

    def active_tree_doc_id(self) -> str | None:
        meta = self._docs.get("meta")
        if meta is None:
            return None
        docs = _doc_roots(meta).get("docs")
        tree = docs.get("tree") if isinstance(docs, dict) else None
        active = tree.get("active") if isinstance(tree, dict) else None
        if isinstance(active, str) and active.startswith("tree-"):
            try:
                return _wire_doc_id(active)
            except StoreError:
                return None
        return None

    def retired(self, doc_id: str) -> RetirementRecord | None:
        meta = self._docs.get("meta")
        if meta is None:
            return None
        records = _doc_roots(meta).get("retirements")
        raw = records.get(doc_id) if isinstance(records, dict) else None
        if not isinstance(raw, dict):
            return None
        replacement = raw.get("replacementDocId")
        plan_id = raw.get("planId")
        snapshot_id = raw.get("snapshotId")
        retired_at = raw.get("retiredAt")
        values = (replacement, plan_id, snapshot_id, retired_at)
        if not all(isinstance(item, str) for item in values):
            return None
        return RetirementRecord(doc_id, replacement, plan_id, snapshot_id, retired_at)

    def _raise_if_retired(self, doc_id: str) -> None:
        record = self.retired(doc_id)
        if record is not None:
            raise StoreError(
                "snapshot-barrier", "physical document is retired", **record.details()
            )

    def _raise_if_replacement_pending(self, doc_id: str) -> None:
        for record in self.retirement_records():
            if record.replacement_doc_id != doc_id:
                continue
            document = self._docs.get(doc_id)
            if (
                document is None
                or hashlib.sha256(document.get_update()).hexdigest()
                != record.snapshot_id
            ):
                raise StoreError(
                    "snapshot-barrier",
                    "replacement snapshot is not installed",
                    **record.details(),
                )

    def purge_listed(self, sha: str) -> bool:
        values = _doc_roots(self._docs["meta"]).get("purgeList")
        raw = values.get(sha) if isinstance(values, dict) else None
        return isinstance(raw, dict) and raw.get("unbannedAt") is None

    def snapshot_point(self, doc_id: str) -> bytes | None:
        values = _doc_roots(self._docs["meta"]).get("snapshotPoints")
        raw = values.get(doc_id) if isinstance(values, dict) else None
        frontier = raw.get("frontier") if isinstance(raw, dict) else None
        return _unb64(frontier) if isinstance(frontier, str) else None

    def writer_seq_watermarks(self, doc_id: str) -> dict[str, int]:
        """Return the immutable-log cutoff encoded into snapshotPoints."""

        _wire_doc_id(doc_id)
        with self._lock:
            return {
                str(row["writer"]): int(row["sequence"])
                for row in self._db.execute(
                    "SELECT writer, MAX(seq) AS sequence FROM commits "
                    "WHERE doc_id = ? GROUP BY writer ORDER BY writer",
                    (doc_id,),
                )
            }

    def commit_version(self, doc_id: str, writer: str, seq: int) -> bytes | None:
        """Return the CRDT frontier recorded beside one immutable log key."""

        with self._lock:
            row = self._db.execute(
                "SELECT version FROM commits WHERE doc_id = ? AND writer = ? AND seq = ?",
                (doc_id, writer, seq),
            ).fetchone()
            return None if row is None else bytes(row["version"])

    def _writer_for(self, author: str, actor: str | None) -> str:
        key = (author, actor)
        if key not in self._lanes:
            lane = f"lane-{len(self._lanes)}"
            writer = f"{self.node_id}.{self._incarnation}.{lane}"
            if not _writer_is_safe(writer):
                raise StoreError("invalid-argument", "writer contains unsafe segments")
            self._lanes[key] = writer
        return self._lanes[key]

    def _owner(self) -> str | None:
        meta = _doc_roots(self._docs["meta"])
        space = meta.get("space") or {}
        owner = space.get("owner") if isinstance(space, dict) else None
        if owner:
            return str(owner)
        row = self._db.execute(
            "SELECT owner FROM meta_frontiers WHERE owner IS NOT NULL ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return str(row[0]) if row is not None else None

    def _membership(self) -> dict[str, dict[str, Any]]:
        roots = _doc_roots(self._docs["meta"])
        members = roots.get("members")
        return members if isinstance(members, dict) else {}

    def _owner_client_ids(self, owner: str) -> set[int]:
        ids: set[int] = set()
        writers = _doc_roots(self._docs["meta"]).get("writers")
        if isinstance(writers, dict):
            for writer, details in writers.items():
                if isinstance(details, dict) and str(details.get("author")) == owner:
                    ids.update(self._row_client_ids(details))
                    # Keep old journals readable while new rows record the
                    # actual client id introduced by the facade mutation.
                    ids.add(self._client_id(str(writer)))
        for row in self._db.execute(
            "SELECT envelope_bytes FROM commits WHERE doc_id = 'meta' ORDER BY rowid"
        ):
            try:
                envelope = self._decode_envelope(bytes(row[0]))
            except StoreError:
                continue
            origin = envelope["origin"]
            if str(origin["author"]) == owner:
                ids.add(self._client_id(str(origin["writer"])))
        return ids

    def _project_frontier(self, doc: Doc, owner: str | None) -> bytes:
        if owner is None:
            return b""
        state = _decode_state_vector(doc.get_state())
        owner_ids = self._owner_client_ids(owner)
        return _encode_state_vector(
            {client: clock for client, clock in state.items() if client in owner_ids}
        )

    def _genesis_owner(self) -> str | None:
        row = self._db.execute(
            "SELECT envelope_bytes FROM commits WHERE doc_id = 'meta' ORDER BY rowid LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        try:
            envelope = self._decode_envelope(bytes(row[0]))
            if _unb64(str(envelope["origin"]["metaFrontier"])):
                return None
            probe = Doc()
            probe.apply_update(self._update_bytes(envelope))
        except (StoreError, TypeError, ValueError):
            return None
        roots = _doc_roots(probe)
        space = roots.get("space")
        owner = space.get("owner") if isinstance(space, dict) else None
        author = str(envelope["origin"]["author"])
        return author if owner and str(owner) == author else None

    def _membership_at(
        self, token: bytes
    ) -> tuple[str | None, dict[str, dict[str, Any]], Doc] | None:
        if not token:
            return None
        row = self._db.execute(
            "SELECT owner, membership_json, meta_update FROM meta_frontiers WHERE token = ?",
            (token,),
        ).fetchone()
        if row is not None and row["meta_update"] is not None:
            try:
                cached = Doc()
                cached.apply_update(bytes(row["meta_update"]))
                membership = json.loads(str(row["membership_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            else:
                if isinstance(membership, dict):
                    return (
                        str(row["owner"]) if row["owner"] is not None else None,
                        membership,
                        cached,
                    )
        owner = self._genesis_owner()
        if owner is None:
            return None
        current = self._project_frontier(self._docs["meta"], owner)
        if not _state_covers(current, token):
            return None
        probe = Doc()
        remaining: list[dict[str, Any]] = []
        for commit in self._db.execute(
            "SELECT envelope_bytes FROM commits WHERE doc_id = 'meta' ORDER BY rowid"
        ):
            try:
                envelope = self._decode_envelope(bytes(commit["envelope_bytes"]))
                remaining.append(envelope)
            except StoreError:
                return None
        while remaining:
            projected = self._project_frontier(probe, owner)
            progressed = False
            for envelope in tuple(remaining):
                before = _unb64(str(envelope["origin"]["metaFrontier"]))
                if not _state_covers(token, before):
                    remaining.remove(envelope)
                    continue
                if not _state_covers(projected, before):
                    continue
                trial = Doc()
                trial.apply_update(probe.get_update())
                try:
                    trial.apply_update(self._update_bytes(envelope))
                except (StoreError, TypeError, ValueError):
                    return None
                trial_frontier = self._project_frontier(trial, owner)
                if _state_covers(token, trial_frontier):
                    probe = trial
                    remaining.remove(envelope)
                    progressed = True
                    break
                remaining.remove(envelope)
            if not progressed:
                if projected == token:
                    break
                return None
        projected = self._project_frontier(probe, owner)
        if projected != token:
            return None
        roots = _doc_roots(probe)
        members = roots.get("members")
        membership = members if isinstance(members, dict) else {}
        if row is None:
            self._db.execute(
                "INSERT OR REPLACE INTO meta_frontiers(token, owner, membership_json, meta_update) VALUES (?, ?, ?, ?)",
                (
                    token,
                    owner,
                    json.dumps(membership, sort_keys=True, separators=(",", ":")),
                    probe.get_update(),
                ),
            )
            self._db.commit()
        return owner, membership, probe

    def _record_meta_frontier(self, token: bytes) -> None:
        # A member-authored suffix can cite an older owner frontier without
        # advancing the current owner projection.  Every cached baseline that
        # covers that suffix is therefore potentially stale.  Invalidate all
        # rows and eagerly rematerialize the current token; older tokens stay
        # lazy and are rebuilt from the immutable journal on their next read.
        with self._db:
            self._db.execute("DELETE FROM meta_frontiers")
        if self._membership_at(token) is None:
            raise StoreError("pending-meta", "cannot materialize metadata frontier")

    def _rebuild_meta_frontiers(self) -> None:
        owner = self._owner()
        token = self._project_frontier(self._docs["meta"], owner)
        if token:
            self._record_meta_frontier(token)

    def _meta_frontier_for_commit(self) -> bytes:
        return self._project_frontier(self._docs["meta"], self._owner())

    def _writer_registered(self, writer: str) -> bool:
        writers = _doc_roots(self._docs["meta"]).get("writers")
        return isinstance(writers, dict) and writer in writers

    @staticmethod
    def _changed_client_ids(before: bytes, after: bytes) -> set[int]:
        before_state = _decode_state_vector(before)
        return {
            client
            for client, clock in _decode_state_vector(after).items()
            if clock > before_state.get(client, 0)
        }

    def _mutation_client_ids(
        self, doc_id: str, mutate: DocMutator, *, writer: str
    ) -> set[int]:
        probe = self._new_doc(doc_id, client_id=self._client_id(writer))
        probe.apply_update(self._doc(doc_id).get_update())
        before = probe.get_state()
        mutate(probe)
        return self._changed_client_ids(before, probe.get_state()) | {
            self._client_id(writer)
        }

    @staticmethod
    def _row_client_ids(details: Any) -> set[int]:
        if not isinstance(details, dict):
            return set()
        values = [details.get("peerId")]
        peer_ids = details.get("peerIds")
        if isinstance(peer_ids, list):
            values.extend(peer_ids)
        return {
            int(value)
            for value in values
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and float(value).is_integer()
            and 0 <= int(value) <= PYCRDT_CLIENT_ID_MAX
        }

    def _registered_client_ids(self, writer: str) -> set[int]:
        writers = _doc_roots(self._docs["meta"]).get("writers")
        if not isinstance(writers, dict):
            return set()
        return self._row_client_ids(writers.get(writer))

    def writer_client_id(self, author: str, actor: str | None) -> int:
        """Return the stable client id used by this incarnation's writer lane."""

        with self._lock:
            return self._client_id(self._writer_for(author, actor))

    def writer_attributions(self) -> dict[str, tuple[str, ...]]:
        """Project the replicated writer table as node -> author URIs."""

        with self._lock:
            writers = _doc_roots(self._docs["meta"]).get("writers")
            if not isinstance(writers, dict):
                return {}
            by_node: dict[str, set[str]] = {}
            for row in writers.values():
                if not isinstance(row, dict):
                    continue
                node = row.get("node")
                author = row.get("author")
                if (
                    isinstance(node, str)
                    and node
                    and isinstance(author, str)
                    and author.startswith("user:")
                    and len(author) > len("user:")
                ):
                    by_node.setdefault(node, set()).add(author)
            return {
                node: tuple(sorted(authors))
                for node, authors in sorted(by_node.items())
            }

    def _register_writer(
        self,
        doc: Doc,
        *,
        writer: str,
        author: str,
        actor: str | None,
        peer_ids: Iterable[int] = (),
    ) -> None:
        _empty_meta(doc)
        writers = doc.get("writers", type=Map)
        assert writers is not None
        existing = (writers.to_py() or {}).get(writer)
        known = self._row_client_ids(existing)
        known.update(int(peer_id) for peer_id in peer_ids)
        known.add(self._client_id(writer))
        self._local_registration_ids[writer] = set(known)
        writers[writer] = Map(
            {
                "node": self.node_id,
                "peerId": self._client_id(writer),
                "peerIds": sorted(known),
                "author": author,
                "actor": actor,
                "startedAt": _now(),
            }
        )

    def _local_meta_exception_allowed(
        self, *, author: str, writer: str, update: bytes, baseline: bytes
    ) -> bool:
        owner = self._owner()
        member = self._membership().get(author, {})
        if (
            owner is not None
            and author != owner
            and (not isinstance(member, dict) or member.get("removedAt"))
        ):
            return False
        probe = Doc()
        try:
            probe.apply_update(baseline)
            before = _doc_roots(probe)
            probe.apply_update(update)
        except (TypeError, ValueError):
            return False
        after = _doc_roots(probe)
        touched = _changed_meta_entries(before, after)
        allowed = {
            ("writers", writer),
            ("purgeAcks", self.node_id),
            ("__orgfs__", "c"),
        }
        if owner is None or author == owner:
            allowed |= {
                (root, key)
                for root, value in _doc_roots(probe).items()
                if root != "writers"
                for key in ([None] if not isinstance(value, dict) else value)
            }
        changed_ids = self._changed_client_ids(
            Doc().get_state(), self._doc_from_update(update).get_state()
        ) | self._local_registration_ids.get(writer, set())
        return (
            bool(touched)
            and (owner is None or author == owner or touched <= allowed)
            and self._meta_shape_valid(before, after, origin_node=self.node_id)
            and self._writer_row_valid(
                before,
                after,
                writer=writer,
                author=author,
                node=self.node_id,
                changed_ids=changed_ids,
                observed_ids=set(_decode_state_vector(probe.get_state())),
            )
        )

    @staticmethod
    def _doc_from_update(update: bytes) -> Doc:
        probe = Doc()
        probe.apply_update(update)
        return probe

    def _writer_row_valid(
        self,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        *,
        writer: str,
        author: str,
        node: str,
        changed_ids: set[int],
        observed_ids: set[int],
    ) -> bool:
        before_writers = before.get("writers")
        after_writers = after.get("writers")
        old = before_writers.get(writer) if isinstance(before_writers, dict) else None
        new = after_writers.get(writer) if isinstance(after_writers, dict) else None
        if not isinstance(new, dict):
            return False
        old_ids = self._row_client_ids(old)
        new_ids = self._row_client_ids(new)
        identity_valid = (
            str(new.get("author")) == author
            and str(new.get("node")) == node
            and bool(new_ids)
            and changed_ids <= new_ids
        )
        if old == new:
            return identity_valid
        # A writer row may pre-register client ids used by a simultaneous
        # tree/content batch. Those ids do not exist in the meta document yet,
        # so the receiver cannot observe them in this update. They are safe to
        # carry without affecting the owner-projected frontier; an already
        # observed id, however, may only be newly claimed if this update itself
        # advances it.
        unattested_ids = new_ids - old_ids - changed_ids
        return (
            identity_valid
            and old_ids <= new_ids
            and unattested_ids.isdisjoint(observed_ids)
        )

    @staticmethod
    def _advance_commit_clock(doc: Doc, *, writer: str, seq: int) -> None:
        marker = doc.get("__orgfs__", type=Map)
        marker["c"] = seq

    @staticmethod
    def _known_plan_ids(roots: Mapping[str, Any]) -> set[str]:
        plans = roots.get("purgePlans")
        known = set(plans) if isinstance(plans, dict) else set()
        retirements = roots.get("retirements")
        if isinstance(retirements, dict):
            known.update(
                str(value.get("planId"))
                for value in retirements.values()
                if isinstance(value, dict) and value.get("planId")
            )
        return known

    def _meta_shape_valid(
        self,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        *,
        origin_node: str,
    ) -> bool:
        marker = after.get("__orgfs__")
        clock = marker.get("c") if isinstance(marker, dict) else None
        if (
            isinstance(clock, bool)
            or not isinstance(clock, (int, float))
            or not float(clock).is_integer()
        ):
            return False
        old_retirements = before.get("retirements")
        new_retirements = after.get("retirements")
        if isinstance(old_retirements, dict):
            if not isinstance(new_retirements, dict):
                return False
            if any(
                new_retirements.get(key) != value
                for key, value in old_retirements.items()
            ):
                return False
        old_acks = before.get("purgeAcks")
        new_acks = after.get("purgeAcks")
        if isinstance(new_acks, dict):
            old_acks = old_acks if isinstance(old_acks, dict) else {}
            changed = {
                key
                for key in set(new_acks) | set(old_acks)
                if new_acks.get(key) != old_acks.get(key)
            }
            if changed and changed != {origin_node}:
                return False
            known = self._known_plan_ids(before) | self._known_plan_ids(after)
            for key in changed:
                value = new_acks.get(key)
                if not isinstance(value, dict) or any(
                    plan_id not in known for plan_id in value
                ):
                    return False
        return True

    def _update_bytes(self, envelope: Mapping[str, Any]) -> bytes:
        if ("update" in envelope) == ("updateBlob" in envelope):
            raise StoreError(
                "invalid-argument", "update and updateBlob are mutually exclusive"
            )
        if "update" in envelope:
            return _unb64(str(envelope["update"]))
        digest = str(envelope["updateBlob"])
        if self.blob_store is None:
            raise StoreError(
                "unknown-blob", "updateBlob requires a blob store", digest=digest
            )
        try:
            return bytes(self.blob_store.get(self.space_id, digest))
        except Exception as exc:
            raise StoreError(
                "unknown-blob", "update blob is unavailable", digest=digest
            ) from exc

    def _decode_envelope(self, raw: bytes) -> dict[str, Any]:
        if len(raw) > ORGFS_ENVELOPE_BYTES:
            raise StoreError("too-large", "envelope exceeds the frozen limit")
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise StoreError("invalid-argument", "invalid envelope JSON") from exc
        if (
            not isinstance(value, dict)
            or value.get("schemaVersion") != 1
            or value.get("type") != "orgfs-update"
        ):
            raise StoreError("invalid-argument", "invalid orgfs-update envelope")
        _wire_doc_id(str(value.get("docId", "")))
        if str(value.get("spaceId")) != self.space_id:
            raise StoreError("invalid-argument", "envelope belongs to another space")
        if not isinstance(value.get("seq"), int) or value["seq"] < 0:
            raise StoreError("invalid-argument", "invalid sequence")
        origin = value.get("origin")
        if not isinstance(origin, dict):
            raise StoreError("invalid-argument", "missing origin")
        required = ("writer", "node", "author", "actor", "committedAt", "metaFrontier")
        if any(key not in origin for key in required):
            raise StoreError("invalid-argument", "incomplete origin")
        writer = str(origin["writer"])
        if not _writer_is_safe(writer):
            raise StoreError("invalid-argument", "unsafe writer")
        if ("update" in value) == ("updateBlob" in value):
            raise StoreError(
                "invalid-argument", "update and updateBlob are mutually exclusive"
            )
        if "update" in value:
            if len(_unb64(str(value["update"]))) > ORGFS_ENVELOPE_BYTES:
                raise StoreError("too-large", "update exceeds the envelope limit")
        else:
            digest = value.get("updateBlob")
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise StoreError("invalid-argument", "invalid updateBlob digest")
        return value

    def _make_envelope(
        self,
        doc_id: str,
        writer: str,
        seq: int,
        update: bytes,
        *,
        author: str,
        actor: str | None,
        meta_frontier: bytes,
    ) -> bytes:
        origin = {
            "writer": writer,
            "node": self.node_id,
            "author": author,
            "actor": actor,
            "committedAt": _now(),
            "metaFrontier": _b64(meta_frontier),
        }
        envelope: dict[str, Any] = {
            "schemaVersion": 1,
            "type": "orgfs-update",
            "spaceId": self.space_id,
            "docId": doc_id,
            "seq": seq,
            "origin": origin,
        }
        if len(update) > ORGFS_INLINE_UPDATE_BYTES:
            if self.blob_store is None:
                raise StoreError("too-large", "large update requires a blob store")
            digest = str(
                self.blob_store.put(self.space_id, update, reason="updateBlob")
            )
            envelope["updateBlob"] = digest
        else:
            envelope["update"] = _b64(update)
        raw = _json_bytes(envelope)
        if len(raw) > ORGFS_ENVELOPE_BYTES:
            raise StoreError("too-large", "encoded envelope exceeds the frozen limit")
        return raw

    def _record_from_row(self, row: sqlite3.Row) -> CommitRecord:
        return CommitRecord(
            writer=str(row["writer"]),
            seq=int(row["seq"]),
            envelope_bytes=bytes(row["envelope_bytes"]),
            version=_b64(bytes(row["version"])),
        )

    def _commit_one(
        self,
        doc_id: str,
        mutate: DocMutator,
        *,
        author: str,
        actor: str | None,
        writer: str,
    ) -> CommitRecord:
        """Commit one row and discard attempt-local registration evidence."""

        try:
            return self._commit_one_inner(
                doc_id, mutate, author=author, actor=actor, writer=writer
            )
        finally:
            if doc_id == "meta":
                self._local_registration_ids.pop(writer, None)

    def _commit_one_inner(
        self,
        doc_id: str,
        mutate: DocMutator,
        *,
        author: str,
        actor: str | None,
        writer: str,
    ) -> CommitRecord:
        _wire_doc_id(doc_id)
        with self._lock:
            self._raise_if_retired(doc_id)
            self._raise_if_replacement_pending(doc_id)
            if doc_id != "meta" and self._owner() is None:
                raise StoreError("pending-meta", "space metadata has no owner yet")
            if doc_id != "meta" and self._owner() is not None:
                member = self._membership().get(author, {})
                if author != self._owner() and (
                    member.get("mode") != "rw" or member.get("removedAt")
                ):
                    raise StoreError(
                        "not-a-member",
                        "local writer is not an rw member",
                        author=author,
                    )
            creates_document = (
                doc_id.startswith("doc-")
                and self._db.execute(
                    "SELECT 1 FROM commits WHERE doc_id = ? LIMIT 1", (doc_id,)
                ).fetchone()
                is None
            )
            doc = self._doc(doc_id)
            before_update = doc.get_update()
            working = self._new_doc(doc_id, client_id=self._client_id(writer))
            working.apply_update(before_update)
            before_state = working.get_state()
            seq_row = self._db.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 AS next_seq FROM commits WHERE writer = ? AND doc_id = ?",
                (writer, doc_id),
            ).fetchone()
            seq = int(seq_row["next_seq"])
            meta_frontier_before = self._meta_frontier_for_commit()
            if doc_id == "meta":
                _empty_meta(working)
                for root in (
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
                    working.get(root, type=Map)
            mutate(working)
            # n3: the reserved coverage clock is not itself a user mutation.
            # Existing-document no-ops are rejected without a journal row;
            # creating a new (including empty) document still needs one row so
            # replicas can observe that document's initial state.
            user_update = working.get_update(before_state)
            if (
                not user_update or user_update == _EMPTY_UPDATE
            ) and not creates_document:
                raise StoreError("invalid-argument", "mutation produced no update")
            self._advance_commit_clock(working, writer=writer, seq=seq)
            update = working.get_update(before_state)
            if doc_id == "meta":
                roots = _doc_roots(working)
                space = roots.get("space")
                proposed_owner = space.get("owner") if isinstance(space, dict) else None
                current_owner = self._owner()
                if current_owner is None and str(proposed_owner or "") != author:
                    raise StoreError(
                        "not-owner", "metadata genesis must pin its author as owner"
                    )
                if (
                    current_owner is not None
                    and str(proposed_owner or "") != current_owner
                ):
                    raise StoreError("not-owner", "P1 does not support owner changes")
            if doc_id == "meta" and not self._local_meta_exception_allowed(
                author=author,
                writer=writer,
                update=update,
                baseline=before_update,
            ):
                raise StoreError("not-owner", "metadata update is owner-only")
            version = working.get_state()
            self._fault("before_journal")
            self._db.execute("BEGIN IMMEDIATE")
            try:
                is_meta_genesis = (
                    doc_id == "meta"
                    and self._db.execute(
                        "SELECT 1 FROM commits WHERE doc_id = 'meta' LIMIT 1"
                    ).fetchone()
                    is None
                )
                envelope = self._make_envelope(
                    doc_id,
                    writer,
                    seq,
                    update,
                    author=author,
                    actor=actor,
                    meta_frontier=b"" if is_meta_genesis else meta_frontier_before,
                )
                self._db.execute(
                    "INSERT INTO commits(writer, seq, doc_id, envelope_bytes, version, committed_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (writer, seq, doc_id, envelope, version, _now()),
                )
            except BaseException:
                self._db.rollback()
                raise
            else:
                self._db.commit()
            self._fault("after_journal")
            doc.apply_update(update)
            self._fault("before_state")
            self._persist_all_documents()
            if doc_id == "meta":
                token = self._meta_frontier_for_commit()
                if token:
                    self._record_meta_frontier(token)
            self._fault("after_state")
            if doc_id == "meta":
                self._last_drained += self._drain_pending()
            return CommitRecord(writer, seq, envelope, _b64(version))

    def commit(
        self, doc_id: str, mutate: DocMutator, *, author: str, actor: str | None
    ) -> CommitRecord:
        _wire_doc_id(doc_id)
        with self._lock:
            writer = self._writer_for(author, actor)
            if doc_id != "meta" and self._owner() is not None:
                member = self._membership().get(author, {})
                if author != self._owner() and (
                    member.get("mode") != "rw" or member.get("removedAt")
                ):
                    raise StoreError(
                        "not-a-member",
                        "local writer is not an rw member",
                        author=author,
                    )
            mutation_ids = (
                set()
                if doc_id == "meta"
                else self._mutation_client_ids(doc_id, mutate, writer=writer)
            )
            missing_ids = mutation_ids - self._registered_client_ids(writer)
            if doc_id == "meta":
                original = mutate

                def with_registration(doc: Doc) -> None:
                    before = doc.get_state()
                    original(doc)
                    changed = self._changed_client_ids(before, doc.get_state())
                    if changed - self._registered_client_ids(
                        writer
                    ) or not self._writer_registered(writer):
                        self._register_writer(
                            doc,
                            writer=writer,
                            author=author,
                            actor=actor,
                            peer_ids=changed,
                        )

                mutate = with_registration
            elif missing_ids or not self._writer_registered(writer):
                self._commit_one(
                    "meta",
                    lambda doc: self._register_writer(
                        doc,
                        writer=writer,
                        author=author,
                        actor=actor,
                        peer_ids=mutation_ids,
                    ),
                    author=author,
                    actor=actor,
                    writer=writer,
                )
            return self._commit_one(
                doc_id, mutate, author=author, actor=actor, writer=writer
            )

    def commit_many(
        self,
        operations: Iterable[tuple[str, DocMutator]],
        *,
        author: str,
        actor: str | None,
    ) -> tuple[CommitRecord, ...]:
        """Commit several document mutations in one SQLite transaction."""

        items = tuple(operations)
        if not items:
            return ()
        with self._lock:
            writer = self._writer_for(author, actor)
            mutation_ids = {
                client_id
                for doc_id, mutate in items
                for client_id in self._mutation_client_ids(
                    doc_id, mutate, writer=writer
                )
            }
            missing_ids = mutation_ids - self._registered_client_ids(writer)
            if missing_ids or not self._writer_registered(writer):
                self._commit_one(
                    "meta",
                    lambda doc: self._register_writer(
                        doc,
                        writer=writer,
                        author=author,
                        actor=actor,
                        peer_ids=mutation_ids,
                    ),
                    author=author,
                    actor=actor,
                    writer=writer,
                )

            prepared: list[tuple[str, Doc, bytes, bytes, int, bytes]] = []
            records: list[CommitRecord] = []
            self._fault("before_journal")
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for doc_id, mutate in items:
                    _wire_doc_id(doc_id)
                    self._raise_if_retired(doc_id)
                    self._raise_if_replacement_pending(doc_id)
                    if doc_id == "meta":
                        raise StoreError(
                            "invalid-argument", "commit_many does not accept metadata"
                        )
                    owner = self._owner()
                    member = self._membership().get(author, {})
                    if owner is None:
                        raise StoreError(
                            "pending-meta", "space metadata has no owner yet"
                        )
                    if author != owner and (
                        member.get("mode") != "rw" or member.get("removedAt")
                    ):
                        raise StoreError(
                            "not-a-member", "local writer is not an rw member"
                        )
                    live = self._doc(doc_id)
                    working = self._new_doc(doc_id, client_id=self._client_id(writer))
                    working.apply_update(live.get_update())
                    before_state = working.get_state()
                    mutate(working)
                    user_update = working.get_update(before_state)
                    if not user_update or user_update == _EMPTY_UPDATE:
                        continue
                    row = self._db.execute(
                        "SELECT COALESCE(MAX(seq), -1) + 1 AS next_seq FROM commits WHERE writer = ? AND doc_id = ?",
                        (writer, doc_id),
                    ).fetchone()
                    seq = int(row["next_seq"])
                    self._advance_commit_clock(working, writer=writer, seq=seq)
                    update = working.get_update(before_state)
                    version = working.get_state()
                    envelope = self._make_envelope(
                        doc_id,
                        writer,
                        seq,
                        update,
                        author=author,
                        actor=actor,
                        meta_frontier=self._meta_frontier_for_commit(),
                    )
                    self._db.execute(
                        "INSERT INTO commits(writer, seq, doc_id, envelope_bytes, version, committed_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (writer, seq, doc_id, envelope, version, _now()),
                    )
                    prepared.append((doc_id, live, update, version, seq, envelope))
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise
            self._fault("after_journal")
            for doc_id, live, update, version, seq, envelope in prepared:
                live.apply_update(update)
                records.append(CommitRecord(writer, seq, envelope, _b64(version)))
            self._fault("before_state")
            self._persist_all_documents()
            self._fault("after_state")
            return tuple(records)

    def frontier(self, doc_id: str) -> bytes:
        with self._lock:
            self._raise_if_retired(doc_id)
            return _encode_state_vector(
                _decode_state_vector(self._doc(doc_id).get_state())
            )

    def committed_update(self, doc_id: str, since: bytes) -> bytes:
        """Return this node's committed ops for ``doc_id`` past ``since``.

        The facade folds this back into its content doc after a commit, so the
        writer's own facade carries the reserved coverage-clock op that
        ``_advance_commit_clock`` writes under the store's writer client.  Every
        outgoing envelope carries that op; without it the originating writer can
        never cover a content frontier a peer records after receiving it.
        """

        with self._lock:
            self._raise_if_retired(doc_id)
            return self._doc(doc_id).get_update(since)

    @staticmethod
    def _covered(version: bytes, vv: bytes) -> bool:
        if not vv:
            return False
        return _state_covers(vv, version)

    @staticmethod
    def _cursor(doc_id: str, offset: int, vv: bytes) -> str:
        return _b64(
            _json_bytes(
                {
                    "docId": doc_id,
                    "offset": offset,
                    "vvHash": hashlib.sha256(vv).hexdigest(),
                }
            )
        )

    @staticmethod
    def _decode_cursor(cursor: str, doc_id: str, vv: bytes) -> int:
        try:
            value = json.loads(_unb64(cursor))
            if (
                value.get("docId") != doc_id
                or value.get("vvHash") != hashlib.sha256(vv).hexdigest()
                or not isinstance(value.get("offset"), int)
                or value["offset"] < 0
            ):
                raise ValueError
            return int(value["offset"])
        except (
            StoreError,
            ValueError,
            TypeError,
            AttributeError,
            json.JSONDecodeError,
        ) as exc:
            raise StoreError(
                "bad-cursor", "cursor does not belong to this export"
            ) from exc

    def export_since(
        self, doc_id: str, vv: bytes, *, cursor: str | None, max_bytes: int
    ) -> ExportPage:
        _wire_doc_id(doc_id)
        if max_bytes <= 0:
            raise StoreError("invalid-argument", "max_bytes must be positive")
        with self._lock:
            self._raise_if_retired(doc_id)
            roots = _doc_roots(self._docs["meta"])
            snapshot_points = roots.get("snapshotPoints")
            point = (
                snapshot_points.get(doc_id)
                if isinstance(snapshot_points, dict)
                else None
            )
            snapshot_id = point.get("snapshotId") if isinstance(point, dict) else None
            if isinstance(snapshot_id, str):
                snapshot_row = self._db.execute(
                    "SELECT frontier, manifest_json FROM snapshots "
                    "WHERE doc_id = ? AND snapshot_id = ?",
                    (doc_id, snapshot_id),
                ).fetchone()
                if snapshot_row is not None and not self._covered(
                    bytes(snapshot_row["frontier"]), vv
                ):
                    try:
                        manifest = json.loads(str(snapshot_row["manifest_json"]))
                    except json.JSONDecodeError as exc:
                        raise StoreError(
                            "invalid-argument", "stored snapshot manifest is invalid"
                        ) from exc
                    if not isinstance(manifest, dict):
                        raise StoreError(
                            "invalid-argument", "stored snapshot manifest is invalid"
                        )
                    return ExportPage((), None, (manifest,))
            offset = self._decode_cursor(cursor, doc_id, vv) if cursor else 0
            rows = self._db.execute(
                "SELECT * FROM commits WHERE doc_id = ? ORDER BY rowid", (doc_id,)
            ).fetchall()
            candidates: list[bytes] = []
            for row in rows:
                if not self._covered(bytes(row["version"]), vv):
                    candidates.append(bytes(row["envelope_bytes"]))
            selected: list[bytes] = []
            index = offset
            while index < len(candidates):
                trial = selected + [candidates[index]]
                encoded = _json_bytes({"envelopes": [_b64(item) for item in trial]})
                if len(encoded) > max_bytes:
                    if not selected:
                        selected = trial
                        index += 1
                    break
                selected = trial
                index += 1
            next_cursor = (
                self._cursor(doc_id, index, vv) if index < len(candidates) else None
            )
            return ExportPage(tuple(selected), next_cursor, ())

    def _meta_exception_allowed(
        self,
        envelope: Mapping[str, Any],
        update: bytes,
        owner: str | None,
        members: Mapping[str, Any],
        baseline: Doc,
    ) -> bool:
        origin = envelope["origin"]
        author = str(origin["author"])
        probe = Doc()
        try:
            probe.apply_update(baseline.get_update())
            before = _doc_roots(probe)
            probe.apply_update(update)
        except (TypeError, ValueError):
            return False
        after = _doc_roots(probe)
        after_space = after.get("space")
        if owner and (
            not isinstance(after_space, dict) or str(after_space.get("owner")) != owner
        ):
            return False
        member = members.get(author)
        if (
            owner
            and author != owner
            and (not isinstance(member, dict) or member.get("removedAt"))
        ):
            return False
        writer = str(origin["writer"])
        touched = _changed_meta_entries(before, after)
        allowed = {
            ("writers", writer),
            ("purgeAcks", str(origin["node"])),
            ("__orgfs__", "c"),
        }
        changed_ids = self._changed_client_ids(baseline.get_state(), probe.get_state())
        return (
            bool(touched)
            and (author == owner or touched <= allowed)
            and self._meta_shape_valid(before, after, origin_node=str(origin["node"]))
            and self._writer_row_valid(
                before,
                after,
                writer=writer,
                author=author,
                node=str(origin["node"]),
                changed_ids=changed_ids,
                observed_ids=set(_decode_state_vector(probe.get_state())),
            )
        )

    def admission(
        self, envelope: bytes
    ) -> Literal[
        "ok", "not-a-member", "pending-meta", "snapshot-barrier", "unknown-doc"
    ]:
        with self._lock:
            return self._admission_locked(envelope)

    def _admission_locked(
        self, envelope: bytes
    ) -> Literal[
        "ok", "not-a-member", "pending-meta", "snapshot-barrier", "unknown-doc"
    ]:
        try:
            value = self._decode_envelope(envelope)
        except StoreError:
            return "not-a-member"
        if self.retired(str(value["docId"])) is not None:
            return "snapshot-barrier"
        origin = value["origin"]
        token = _unb64(str(origin["metaFrontier"]))
        if str(value["docId"]) == "meta":
            if not token:
                if (
                    self._db.execute(
                        "SELECT 1 FROM commits WHERE doc_id = 'meta' LIMIT 1"
                    ).fetchone()
                    is not None
                ):
                    return "not-a-member"
                try:
                    probe = Doc()
                    probe.apply_update(self._update_bytes(value))
                except (StoreError, TypeError, ValueError):
                    return "not-a-member"
                roots = _doc_roots(probe)
                space = roots.get("space")
                genesis_owner = space.get("owner") if isinstance(space, dict) else None
                return (
                    "ok"
                    if genesis_owner and str(genesis_owner) == str(origin["author"])
                    else "not-a-member"
                )
        snapshot = self._membership_at(token)
        if snapshot is None:
            return "pending-meta"
        doc_id = str(value["docId"])
        if doc_id.startswith("tree-"):
            active_tree = self.active_tree_doc_id()
            replacement_trees = {
                record.replacement_doc_id
                for record in self.retirement_records()
                if record.replacement_doc_id.startswith("tree-")
            }
            if doc_id != active_tree and doc_id not in replacement_trees:
                return "unknown-doc"
        owner, members, baseline = snapshot
        author = str(origin["author"])
        member = members.get(author, {})
        if str(value["docId"]) == "meta":
            try:
                allowed = self._meta_exception_allowed(
                    value, self._update_bytes(value), owner, members, baseline
                )
            except StoreError:
                return "not-a-member"
            return "ok" if allowed else "not-a-member"
        if author == owner or (
            member.get("mode") == "rw" and not member.get("removedAt")
        ):
            return "ok"
        return "not-a-member"

    def _pending(self, envelope: bytes, supplier: str) -> None:
        value = self._decode_envelope(envelope)
        self._db.execute(
            "INSERT OR REPLACE INTO pending(writer, seq, doc_id, envelope_bytes, supplier) VALUES (?, ?, ?, ?, ?)",
            (
                str(value["origin"]["writer"]),
                int(value["seq"]),
                str(value["docId"]),
                envelope,
                supplier,
            ),
        )
        self._db.commit()

    def _apply_import(
        self, value: Mapping[str, Any], envelope: bytes, supplier: str
    ) -> ImportResult:
        writer = str(value["origin"]["writer"])
        seq = int(value["seq"])
        found = self._db.execute(
            "SELECT * FROM commits WHERE writer = ? AND seq = ? AND doc_id = ?",
            (writer, seq, str(value["docId"])),
        ).fetchone()
        if found is not None:
            if bytes(found["envelope_bytes"]) == envelope:
                return ImportResult(
                    "applied", "duplicate", self._record_from_row(found)
                )
            _LOG.warning(
                "orgfs rejected same journal key with different bytes: space=%s doc=%s writer=%s seq=%s supplier=%s",
                self.space_id,
                value["docId"],
                writer,
                seq,
                supplier,
            )
            return ImportResult("rejected", "log-key-conflict")
        doc_id = str(value["docId"])
        update = self._update_bytes(value)
        doc = self._doc(doc_id)
        working = self._new_doc(doc_id)
        working.apply_update(doc.get_update())
        before_state = working.get_state()
        try:
            working.apply_update(update)
        except (TypeError, ValueError):
            return ImportResult("rejected", "invalid-argument")
        version = working.get_state()
        next_seq = int(
            self._db.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM commits "
                "WHERE writer = ? AND doc_id = ?",
                (writer, doc_id),
            ).fetchone()[0]
        )
        if seq > next_seq:
            return ImportResult("pending", "pending-dependency")
        if version == before_state:
            # Yjs accepts an update whose structural predecessor is missing,
            # but keeps it only in the receiving Doc's in-memory pending set.
            # Journaling that row now and rebuilding a fresh working Doc on the
            # next import would discard the pending structs while the durable
            # duplicate key prevents a retry. Park the original bytes until a
            # predecessor advances this document instead.
            return ImportResult("pending", "pending-dependency")
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO commits(writer, seq, doc_id, envelope_bytes, version, committed_at, broadcast_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (writer, seq, doc_id, envelope, version, _now(), _now()),
                )
        except sqlite3.IntegrityError:
            # A concurrent importer won the key.  Re-enter the idempotency path.
            return self._apply_import(value, envelope, supplier)
        doc.apply_update(update)
        self._persist_all_documents()
        if doc_id == "meta":
            token = self._meta_frontier_for_commit()
            if token:
                self._record_meta_frontier(token)
        return ImportResult(
            "applied", record=CommitRecord(writer, seq, envelope, _b64(version))
        )

    def _drain_pending(self) -> tuple[bytes, ...]:
        applied: list[bytes] = []
        while True:
            rows = self._db.execute("SELECT * FROM pending ORDER BY rowid").fetchall()
            progressed = False
            for row in rows:
                envelope = bytes(row["envelope_bytes"])
                try:
                    try:
                        value = self._decode_envelope(envelope)
                        self._update_bytes(value)
                        decision = self.admission(envelope)
                    except StoreError as exc:
                        if exc.code == "unknown-blob":
                            continue
                        decision = (
                            "snapshot-barrier"
                            if exc.code == "snapshot-barrier"
                            else "not-a-member"
                        )
                    if decision == "pending-meta":
                        continue
                    if decision == "ok":
                        result = self._apply_import(
                            value, envelope, str(row["supplier"])
                        )
                        if result.status == "applied" and result.code != "duplicate":
                            applied.append(envelope)
                        if result.status not in {"applied", "rejected"}:
                            continue
                    elif decision == "snapshot-barrier":
                        _LOG.warning(
                            "orgfs discarded pending retired document update: "
                            "space=%s doc=%s code=snapshot-barrier",
                            self.space_id,
                            row["doc_id"],
                        )
                    self._db.execute(
                        "DELETE FROM pending WHERE writer = ? AND seq = ? AND doc_id = ?",
                        (
                            str(row["writer"]),
                            int(row["seq"]),
                            str(row["doc_id"]),
                        ),
                    )
                    self._db.commit()
                    progressed = True
                except Exception:
                    self._db.rollback()
                    _LOG.exception(
                        "orgfs pending drain deferred after durable-trigger failure"
                    )
            if not progressed:
                return tuple(applied)

    def import_envelope(self, envelope: bytes, *, supplier: str) -> ImportResult:
        with self._lock:
            try:
                value = self._decode_envelope(bytes(envelope))
            except StoreError as exc:
                return ImportResult("rejected", exc.code, details=exc.details)
            try:
                self._raise_if_retired(str(value["docId"]))
            except StoreError as exc:
                return ImportResult("rejected", exc.code, details=exc.details)
            writer = str(value["origin"]["writer"])
            seq = int(value["seq"])
            found = self._db.execute(
                "SELECT * FROM commits WHERE writer = ? AND seq = ? AND doc_id = ?",
                (writer, seq, str(value["docId"])),
            ).fetchone()
            if found is not None:
                if bytes(found["envelope_bytes"]) == bytes(envelope):
                    return ImportResult(
                        "applied", "duplicate", self._record_from_row(found)
                    )
                _LOG.warning(
                    "orgfs rejected same journal key with different bytes: space=%s doc=%s writer=%s seq=%s supplier=%s",
                    self.space_id,
                    value["docId"],
                    writer,
                    seq,
                    supplier,
                )
                return ImportResult("rejected", "log-key-conflict")
            try:
                self._update_bytes(value)
            except StoreError as exc:
                return ImportResult("rejected", exc.code, details=exc.details)
            decision = self.admission(bytes(envelope))
            if decision == "pending-meta":
                self._pending(bytes(envelope), supplier)
                return ImportResult("pending", "pending-meta")
            if decision != "ok":
                return ImportResult("rejected", decision)
            result = self._apply_import(value, bytes(envelope), supplier)
            if result.status == "pending":
                self._pending(bytes(envelope), supplier)
                return result
            if result.status == "applied":
                drained = self._drain_pending()
                if drained:
                    return ImportResult(
                        result.status,
                        result.code,
                        result.record,
                        details={"drained": drained},
                    )
            return result

    def take_drained(self) -> tuple[bytes, ...]:
        """Return envelopes applied while the latest local meta commit drained pending."""

        with self._lock:
            drained = self._last_drained
            self._last_drained = ()
            return drained

    def unbroadcast(self) -> Iterable[CommitRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM commits WHERE broadcast_at IS NULL ORDER BY rowid"
            ).fetchall()
            return tuple(self._record_from_row(row) for row in rows)

    def has_unbroadcast_local_doc(self, doc_id: str) -> bool:
        """Return whether this node has unpublished work on one physical doc."""

        with self._lock:
            for row in self._db.execute(
                "SELECT envelope_bytes FROM commits "
                "WHERE doc_id = ? AND broadcast_at IS NULL",
                (doc_id,),
            ):
                envelope = self._decode_envelope(bytes(row[0]))
                if str(envelope["origin"]["node"]) == self.node_id:
                    return True
            return False

    def mark_broadcast(self, writer: str, seq: int, *, doc_id: str) -> None:
        _wire_doc_id(doc_id)
        with self._lock:
            self._fault("before_broadcast")
            with self._db:
                self._db.execute(
                    "UPDATE commits SET broadcast_at = COALESCE(broadcast_at, ?) "
                    "WHERE writer = ? AND seq = ? AND doc_id = ?",
                    (_now(), writer, seq, doc_id),
                )
            self._fault("after_broadcast")

    def snapshot(self, doc_id: str, *, shallow_since: bytes | None) -> SnapshotManifest:
        with self._lock:
            self._raise_if_retired(doc_id)
            doc = self._doc(doc_id)
            body = doc.get_update()
            digest = hashlib.sha256(body).hexdigest()
            authors: dict[str, int] = {}
            latest: dict[str, Any] | None = None
            for row in self._db.execute(
                "SELECT envelope_bytes FROM commits WHERE doc_id = ? ORDER BY rowid",
                (doc_id,),
            ):
                envelope = self._decode_envelope(bytes(row[0]))
                origin = envelope["origin"]
                author = str(origin["author"])
                authors[author] = authors.get(author, 0) + 1
                latest = origin
            manifest = SnapshotManifest(
                space_id=self.space_id,
                doc_id=doc_id,
                snapshot_id=digest,
                frontier=_b64(
                    _encode_state_vector(_decode_state_vector(doc.get_state()))
                ),
                shallow_since=_b64(shallow_since)
                if shallow_since is not None
                else None,
                snapshot_bytes=body,
                kind="shallow" if shallow_since is not None else "full",
                authors_before=authors,
                created_by={
                    "writer": str(latest["writer"]) if latest else "",
                    "author": str(latest["author"]) if latest else "",
                    "actor": latest.get("actor") if latest else None,
                },
                created_at=_now(),
            )
            self._db.execute(
                "INSERT OR REPLACE INTO snapshots"
                "(snapshot_id, doc_id, frontier, snapshot_bytes, manifest_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    digest,
                    doc_id,
                    doc.get_state(),
                    body,
                    json.dumps(manifest.to_dict(), sort_keys=True),
                ),
            )
            self._db.commit()
            return manifest

    def record_holder_frontier(
        self, node: str, durable: bool, frontiers: Mapping[str, bytes]
    ) -> None:
        with self._lock:
            self._holders[str(node)] = (
                bool(durable),
                {str(k): bytes(v) for k, v in frontiers.items()},
            )

    def holders_seen(self) -> tuple[tuple[str, bool], ...]:
        with self._lock:
            return tuple(
                sorted((node, durable) for node, (durable, _) in self._holders.items())
            )

    def holder_frontiers(self) -> dict[str, dict[str, bytes]]:
        """Return an isolated copy of the in-memory holder frontier hints."""

        with self._lock:
            return {
                node: dict(frontiers)
                for node, (_durable, frontiers) in self._holders.items()
            }

    def document_ids(self) -> tuple[str, ...]:
        """Return known document ids in protocol order (metadata first)."""

        with self._lock:
            rows = self._db.execute(
                "SELECT DISTINCT doc_id FROM commits ORDER BY doc_id"
            ).fetchall()
            known = {str(row[0]) for row in rows} | set(self._docs)
            active_tree = self.active_tree_doc_id()
            first = ("meta", active_tree) if active_tree is not None else ("meta",)
            return tuple(
                item
                for item in (*first, *sorted(known - set(first)))
                if item in known and self.retired(item) is None
            )

    def log_range(
        self, doc_id: str, writer: str, *, after: int | None, limit: int
    ) -> tuple[CommitRecord, ...]:
        """Read immutable journal rows for the frozen log-range protocol."""

        _wire_doc_id(doc_id)
        if not _writer_is_safe(writer):
            raise StoreError("invalid-argument", "invalid writer")
        if after is not None and (
            isinstance(after, bool) or not isinstance(after, int) or after < 0
        ):
            raise StoreError(
                "invalid-argument", "after must be a non-negative integer or null"
            )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 0 < limit <= 256
        ):
            raise StoreError("invalid-argument", "limit must be between 1 and 256")
        with self._lock:
            self._raise_if_retired(doc_id)
            rows = self._db.execute(
                "SELECT * FROM commits WHERE doc_id = ? AND writer = ? AND seq > ? "
                "ORDER BY seq LIMIT ?",
                (doc_id, writer, -1 if after is None else after, limit),
            ).fetchall()
            return tuple(self._record_from_row(row) for row in rows)

    def member_mode(self, author: str) -> str | None:
        """Return the current active member mode used by sync admission."""

        with self._lock:
            if author == self._owner():
                return "rw"
            member = self._membership().get(author)
            if not isinstance(member, dict) or member.get("removedAt"):
                return None
            mode = member.get("mode")
            return str(mode) if mode in {"ro", "rw"} else None

    def _inventory_rows(self, doc_id: str) -> dict[str, Any]:
        rows = self._db.execute(
            "SELECT writer, seq, envelope_bytes FROM commits WHERE doc_id = ? ORDER BY rowid",
            (doc_id,),
        ).fetchall()
        logs: list[str] = []
        update_blobs: set[str] = set()
        writers: set[str] = set()
        authors: dict[str, int] = {}
        for row in rows:
            envelope = self._decode_envelope(bytes(row["envelope_bytes"]))
            writer = str(row["writer"])
            logs.append(f"{doc_id}/{writer}/{int(row['seq'])}")
            writers.add(writer)
            author = str(envelope["origin"]["author"])
            authors[author] = authors.get(author, 0) + 1
            if "updateBlob" in envelope:
                update_blobs.add(str(envelope["updateBlob"]))
        snapshots = [
            {"snapshotId": str(row["snapshot_id"]), "docId": doc_id}
            for row in self._db.execute(
                "SELECT snapshot_id FROM snapshots WHERE doc_id = ? ORDER BY snapshot_id",
                (doc_id,),
            )
        ]
        return {
            "logKeys": logs,
            "updateBlobs": sorted(update_blobs),
            "snapshots": snapshots,
            "writersAffected": sorted(writers),
            "authorsBefore": authors,
        }

    def purge_inventory(self, doc_id: str) -> dict[str, Any]:
        """Return the exact local object set bound by an S5 plan."""

        with self._lock:
            self._raise_if_retired(doc_id)
            return self._inventory_rows(doc_id)

    def install_replacement(
        self,
        old_doc_id: str,
        new_doc_id: str,
        snapshot_bytes: bytes,
        *,
        author: str | None = None,
        actor: str | None = None,
    ) -> None:
        """Install a semantically rebuilt document after retirement is durable."""

        with self._lock:
            record = self.retired(old_doc_id)
            if record is None or record.replacement_doc_id != new_doc_id:
                raise StoreError("snapshot-barrier", "retirement is not committed")
            if hashlib.sha256(snapshot_bytes).hexdigest() != record.snapshot_id:
                raise StoreError(
                    "invalid-argument", "replacement bytes do not match snapshotId"
                )
            replacement = self._new_doc(new_doc_id)
            replacement.apply_update(snapshot_bytes)
            self._docs[new_doc_id] = replacement
            if author is not None:
                writer = self._writer_for(author, actor)
                peer_ids = set(_decode_state_vector(replacement.get_state()))
                missing_ids = peer_ids - self._registered_client_ids(writer)
                if missing_ids or not self._writer_registered(writer):
                    self._commit_one(
                        "meta",
                        lambda doc: self._register_writer(
                            doc,
                            writer=writer,
                            author=author,
                            actor=actor,
                            peer_ids=peer_ids,
                        ),
                        author=author,
                        actor=actor,
                        writer=writer,
                    )
                seq = int(
                    self._db.execute(
                        "SELECT COALESCE(MAX(seq), -1) + 1 FROM commits "
                        "WHERE writer = ? AND doc_id = ?",
                        (writer, new_doc_id),
                    ).fetchone()[0]
                )
                envelope = self._make_envelope(
                    new_doc_id,
                    writer,
                    seq,
                    snapshot_bytes,
                    author=author,
                    actor=actor,
                    meta_frontier=self._meta_frontier_for_commit(),
                )
                with self._db:
                    self._db.execute(
                        "INSERT INTO commits"
                        "(writer, seq, doc_id, envelope_bytes, version, committed_at, broadcast_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            writer,
                            seq,
                            new_doc_id,
                            envelope,
                            replacement.get_state(),
                            _now(),
                            _now(),
                        ),
                    )
            self._docs.pop(old_doc_id, None)
            self._persist_all_documents()
            if old_doc_id.startswith("doc-"):
                self._doc_path(old_doc_id).unlink(missing_ok=True)
            manifest = self.snapshot(new_doc_id, shallow_since=None)
            if manifest.snapshot_id != record.snapshot_id:
                raise StoreError(
                    "invalid-argument", "installed replacement snapshot changed bytes"
                )
            if self.blob_store is not None:
                digest = self.blob_store.put(
                    self.space_id, snapshot_bytes, reason="replica"
                )
                if digest != record.snapshot_id:
                    raise StoreError(
                        "invalid-argument", "replacement snapshot hash mismatch"
                    )
            self._fault("replacement_advertised")

    def retirement_records(self) -> tuple[RetirementRecord, ...]:
        """Return immutable retirement metadata in old-doc order."""

        with self._lock:
            roots = _doc_roots(self._docs["meta"])
            values = roots.get("retirements")
            records: list[RetirementRecord] = []
            for old_doc_id in sorted(values if isinstance(values, dict) else ()):
                record = self.retired(str(old_doc_id))
                if record is not None:
                    records.append(record)
            return tuple(records)

    def pending_replacements(self) -> tuple[RetirementRecord, ...]:
        """Return replacements whose full snapshot is not installed locally."""

        with self._lock:
            return tuple(
                record
                for record in self.retirement_records()
                if record.replacement_doc_id not in self._docs
                or hashlib.sha256(
                    self._docs[record.replacement_doc_id].get_update()
                ).hexdigest()
                != record.snapshot_id
            )

    def purge_list_entries(self) -> dict[str, str]:
        """Return active purge-list digest to plan-id mappings."""

        with self._lock:
            roots = _doc_roots(self._docs["meta"])
            values = roots.get("purgeList")
            return {
                str(digest): str(raw["planId"])
                for digest, raw in (values.items() if isinstance(values, dict) else ())
                if isinstance(raw, dict)
                and isinstance(raw.get("planId"), str)
                and raw.get("unbannedAt") is None
            }

    def delete_retired_objects(self, old_doc_id: str) -> tuple[str, ...]:
        """Delete every local journal/snapshot/updateBlob object for a retired id."""

        with self._lock:
            if self.retired(old_doc_id) is None:
                raise StoreError("invalid-argument", "document is not retired")
            inventory = self._inventory_rows(old_doc_id)
            with self._db:
                self._db.execute("DELETE FROM pending WHERE doc_id = ?", (old_doc_id,))
                self._db.execute("DELETE FROM commits WHERE doc_id = ?", (old_doc_id,))
                self._db.execute(
                    "DELETE FROM snapshots WHERE doc_id = ?", (old_doc_id,)
                )
            if old_doc_id.startswith("doc-"):
                self._doc_path(old_doc_id).unlink(missing_ok=True)
            if self.blob_store is not None:
                for digest in inventory["updateBlobs"]:
                    if hasattr(self.blob_store, "release"):
                        self.blob_store.release(self.space_id, digest)
                    if hasattr(self.blob_store, "delete_if_unreferenced"):
                        self.blob_store.delete_if_unreferenced(digest)
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._db.execute("VACUUM")
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return tuple(inventory["updateBlobs"])

    def retired_residue(self, old_doc_id: str) -> tuple[str, ...]:
        with self._lock:
            residue: list[str] = []
            if self._db.execute(
                "SELECT 1 FROM commits WHERE doc_id = ? LIMIT 1", (old_doc_id,)
            ).fetchone():
                residue.append("journal")
            if self._db.execute(
                "SELECT 1 FROM snapshots WHERE doc_id = ? LIMIT 1", (old_doc_id,)
            ).fetchone():
                residue.append("snapshot")
            if old_doc_id in self._docs:
                residue.append("document")
            if old_doc_id.startswith("doc-") and self._doc_path(old_doc_id).exists():
                residue.append("document-file")
            return tuple(residue)

    def save_purge_plan(
        self,
        plan_id: str,
        plan: Mapping[str, Any],
        targets: Iterable[Mapping[str, str]],
    ) -> None:
        """Persist an owner-local S5 plan without advancing replicated metadata."""

        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO purge_plans(plan_id, plan_json, targets_json) "
                "VALUES (?, ?, ?)",
                (
                    plan_id,
                    json.dumps(plan, sort_keys=True, separators=(",", ":")),
                    json.dumps(tuple(targets), sort_keys=True, separators=(",", ":")),
                ),
            )

    def load_purge_plan(
        self, plan_id: str
    ) -> tuple[dict[str, Any], tuple[dict[str, str], ...]] | None:
        """Load a durable owner-local S5 plan after a facade restart."""

        with self._lock:
            row = self._db.execute(
                "SELECT plan_json, targets_json FROM purge_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            if row is None:
                return None
            plan = json.loads(str(row["plan_json"]))
            targets = json.loads(str(row["targets_json"]))
            if not isinstance(plan, dict) or not isinstance(targets, list):
                raise StoreError("stale-plan", "persisted purge plan is malformed")
            return plan, tuple(dict(target) for target in targets)

    def scan_forbidden_bytes(
        self, needles: Iterable[bytes]
    ) -> dict[str, tuple[str, ...]]:
        """Scan journal, all document state, and blob objects for forbidden bytes."""

        forbidden = tuple(bytes(needle) for needle in needles if needle)
        if not forbidden:
            raise ValueError("at least one non-empty byte sequence is required")
        journal_paths = tuple(
            path
            for path in (
                self._journal_path,
                self._journal_path.with_name(f"{self._journal_path.name}-wal"),
                self._journal_path.with_name(f"{self._journal_path.name}-shm"),
            )
            if path.is_file()
        )
        document_paths = tuple(sorted(self._root.glob("*.loro"))) + tuple(
            sorted((self._root / "docs").glob("*.loro"))
        )
        blob_root = getattr(self.blob_store, "blob_root", None)
        blob_paths = (
            tuple(
                path
                for path in sorted(Path(blob_root).rglob("*"))
                if path.is_file() and len(path.name) == 64
            )
            if blob_root is not None
            else ()
        )
        result: dict[str, tuple[str, ...]] = {}
        for scope, paths in (
            ("journal", journal_paths),
            ("documents", document_paths),
            ("blobs", blob_paths),
        ):
            hits: list[str] = []
            for path in paths:
                raw = path.read_bytes()
                if any(needle in raw for needle in forbidden):
                    hits.append(str(path))
            if scope == "journal":
                for table in ("commits", "pending"):
                    for row in self._db.execute(
                        f"SELECT rowid, envelope_bytes FROM {table}"  # noqa: S608
                    ):
                        envelope = bytes(row["envelope_bytes"])
                        candidates = [envelope]
                        try:
                            candidates.append(
                                self._update_bytes(self._decode_envelope(envelope))
                            )
                        except StoreError:
                            pass
                        if any(
                            needle in candidate
                            for needle in forbidden
                            for candidate in candidates
                        ):
                            hits.append(f"{self._journal_path}#{table}:{row['rowid']}")
            result[scope] = tuple(hits)
        return result

    def unconfirmed_commits(self) -> int:
        with self._lock:
            holders = [
                frontiers for durable, frontiers in self._holders.values() if durable
            ]
            local_rows = []
            for row in self._db.execute("SELECT * FROM commits ORDER BY rowid"):
                try:
                    origin = self._decode_envelope(bytes(row["envelope_bytes"]))[
                        "origin"
                    ]
                except StoreError:
                    continue
                if str(origin.get("node")) == self.node_id:
                    local_rows.append(row)
            if not holders:
                return len(local_rows)
            count = 0
            for row in local_rows:
                doc_id = str(row["doc_id"])
                if not any(
                    self._covered(bytes(row["version"]), fronts.get(doc_id, b""))
                    for fronts in holders
                ):
                    count += 1
            return count


def assert_client_ids_registered(store: LocalSpaceStore) -> None:
    """Assert the eventual D.2 invariant after registration has converged."""

    with store._lock:
        document_ids = {
            client_id
            for doc in store._docs.values()
            for client_id in _decode_state_vector(doc.get_state())
        }
        roots = _doc_roots(store._docs["meta"])
        writers = roots.get("writers")
        registered = {
            client_id
            for details in (writers.values() if isinstance(writers, dict) else ())
            for client_id in store._row_client_ids(details)
        }
    missing = document_ids - registered
    assert not missing, (
        f"orgfs client ids missing writer attribution: {sorted(missing)}"
    )


# Names used by downstream integration and by early P1 examples.
SQLiteSpaceStore = LocalSpaceStore
FileSpaceStore = LocalSpaceStore
PersistentSpaceStore = LocalSpaceStore


__all__ = [
    "CommitRecord",
    "ExportPage",
    "FileSpaceStore",
    "ImportResult",
    "LocalSpaceStore",
    "PersistentSpaceStore",
    "RetirementRecord",
    "SQLiteSpaceStore",
    "SnapshotManifest",
    "SpaceStore",
    "StoreError",
    "assert_client_ids_registered",
]
