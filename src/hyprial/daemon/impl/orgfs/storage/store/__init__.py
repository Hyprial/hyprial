from __future__ import annotations

import hashlib

import os

import secrets

import sqlite3

import tempfile

import threading

from pathlib import Path

from collections.abc import Iterable
from typing import Any

from pycrdt import Doc

from hyprial.kernel import lock_exclusive, unlock

from hyprial.daemon.impl.orgfs.storage.store.membership import (
    StoreMembership,
    _MEMBER_ADD_FRONTIER_SCHEMA_VERSION,
)
from hyprial.daemon.impl.orgfs.storage.store.journal import StoreJournal
from hyprial.daemon.impl.orgfs.storage.store.admission import StoreAdmission
from hyprial.daemon.impl.orgfs.storage.store.snapshots import StoreSnapshots
from hyprial.daemon.impl.orgfs.storage.store.retirement import StoreRetirement
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import CommitRecord as CommitRecord
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import DocMutator as DocMutator
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import ExportPage as ExportPage
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import FaultHook as FaultHook
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import ImportResult as ImportResult
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import ORGFS_ENVELOPE_BYTES as ORGFS_ENVELOPE_BYTES
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import ORGFS_INLINE_UPDATE_BYTES as ORGFS_INLINE_UPDATE_BYTES
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import ORGFS_PAGE_BYTES as ORGFS_PAGE_BYTES
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import PYCRDT_CLIENT_ID_BITS as PYCRDT_CLIENT_ID_BITS
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import PYCRDT_CLIENT_ID_MAX as PYCRDT_CLIENT_ID_MAX
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import RetirementRecord as RetirementRecord
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import SnapshotManifest as SnapshotManifest
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import SpaceStore as SpaceStore
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import StoreError as StoreError
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _DOC_IDS as _DOC_IDS
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _EMPTY_UPDATE as _EMPTY_UPDATE
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _LOG as _LOG
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _MAX_PENDING_IMPORTS as _MAX_PENDING_IMPORTS
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _MAX_PENDING_IMPORT_AGE_SECONDS as _MAX_PENDING_IMPORT_AGE_SECONDS
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _SAFE_WRITER as _SAFE_WRITER
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _b64 as _b64
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _changed_meta_entries as _changed_meta_entries
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _decode_state_vector as _decode_state_vector
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _doc_roots as _doc_roots
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _empty_meta as _empty_meta
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _encode_state_vector as _encode_state_vector
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _json_bytes as _json_bytes
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _now as _now
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _plain as _plain
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _read_var_uint as _read_var_uint
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _state_covers as _state_covers
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _unb64 as _unb64
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _wire_doc_id as _wire_doc_id
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _write_var_uint as _write_var_uint
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _writer_is_safe as _writer_is_safe
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import decode_state_vector as decode_state_vector
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import document_file_stem as document_file_stem
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import is_document_file_stem as is_document_file_stem
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import state_covers as state_covers


#: Writes between refreshes of the cached ``.loro`` state files.
_CHECKPOINT_COMMITS = 64
#: A ``docs/h-*.loro`` file is this line, the document id, a newline, then
#: the document update.  The id lives in the file because the name is a hash.
_DOC_FILE_MAGIC = b"hyprial-orgfs-doc/1\n"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class LocalSpaceStore(StoreMembership, StoreJournal, StoreAdmission, StoreSnapshots, StoreRetirement):
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
        self._pending_import_limit = _MAX_PENDING_IMPORTS
        # The supplier is the host-gated identity.  Authors are self-asserted
        # until signatures land and therefore cannot own capacity buckets.
        self._pending_import_supplier_limit = max(1, _MAX_PENDING_IMPORTS // 8)
        self._pending_import_max_age_seconds = _MAX_PENDING_IMPORT_AGE_SECONDS
        self._pending_drain_page_size = 64
        self._pending_drop_count = 0
        self._pending_drop_warning_at = float("-inf")
        self._pending_drop_warning_interval_seconds = 60.0
        self._local_registration_ids: dict[str, set[int]] = {}
        self._dirty_docs: set[str] = set()
        self._commits_since_checkpoint = 0
        self._root = self.state_dir / "orgfs" / "spaces" / self.space_id
        (self._root / "docs").mkdir(parents=True, exist_ok=True)
        self._journal_path = self._root / "journal.sqlite3"
        self._db = sqlite3.connect(self._journal_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA secure_delete=ON")
        # fsync of the journal is the design's local orgfs commit point (§3.4).
        self._db.execute("PRAGMA synchronous=FULL")  # durability: FULL -- commit point
        member_add_tables = {
            str(row[0])
            for row in self._db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
                "('member_add_frontiers', 'member_add_frontier_state')"
            )
        }
        member_add_index_complete = member_add_tables == {
            "member_add_frontiers",
            "member_add_frontier_state",
        }
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
                author TEXT NOT NULL DEFAULT '',
                meta_frontier BLOB,
                envelope_bytes BLOB NOT NULL,
                supplier TEXT NOT NULL,
                held_at TEXT NOT NULL,
                PRIMARY KEY (writer, seq, doc_id)
            );
            CREATE TABLE IF NOT EXISTS meta_frontiers (
                token BLOB PRIMARY KEY,
                owner TEXT,
                membership_json TEXT NOT NULL,
                meta_update BLOB
            );
            CREATE TABLE IF NOT EXISTS member_add_frontiers (
                user TEXT NOT NULL,
                member_json TEXT NOT NULL,
                frontier BLOB NOT NULL,
                PRIMARY KEY (user, member_json)
            );
            CREATE TABLE IF NOT EXISTS member_add_frontier_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER
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
        if "held_at" not in {
            str(row[1]) for row in self._db.execute("PRAGMA table_info(pending)")
        }:
            self._db.execute("ALTER TABLE pending ADD COLUMN held_at TEXT")
            self._db.execute(
                "UPDATE pending SET held_at = ? WHERE held_at IS NULL", (_now(),)
            )
        if "author" not in {
            str(row[1]) for row in self._db.execute("PRAGMA table_info(pending)")
        }:
            self._db.execute(
                "ALTER TABLE pending ADD COLUMN author TEXT NOT NULL DEFAULT ''"
            )
        if "meta_frontier" not in {
            str(row[1]) for row in self._db.execute("PRAGMA table_info(pending)")
        }:
            self._db.execute("ALTER TABLE pending ADD COLUMN meta_frontier BLOB")
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS pending_supplier ON pending(supplier)"
        )
        if "schema_version" not in {
            str(row[1])
            for row in self._db.execute(
                "PRAGMA table_info(member_add_frontier_state)"
            )
        }:
            self._db.execute(
                "ALTER TABLE member_add_frontier_state "
                "ADD COLUMN schema_version INTEGER"
            )
        member_add_index_complete = member_add_index_complete and (
            self._db.execute(
                "SELECT 1 FROM member_add_frontier_state "
                "WHERE singleton = 1 AND schema_version = ?",
                (_MEMBER_ADD_FRONTIER_SCHEMA_VERSION,),
            ).fetchone()
            is not None
        )
        self._db.commit()
        self._load_documents()
        self._rebuild_meta_frontiers()
        self._replay_journal()
        self._rebuild_meta_frontiers()
        if not member_add_index_complete:
            self._rebuild_member_add_frontiers()
        if self.blob_store is not None and hasattr(
            self.blob_store, "register_purge_projection"
        ):
            self.blob_store.register_purge_projection(
                self.space_id, self.purge_projection()
            )


    @property
    def root(self) -> Path:
        return self._root


    @property
    def incarnation(self) -> str:
        return self._incarnation


    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self.checkpoint()
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
        return self._root / "docs" / f"{document_file_stem(doc_id)}.loro"


    def _write_document_file(
        self, path: Path, doc_id: str, update: bytes, *, durable_name: bool = False
    ) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".orgfs-doc-", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(_DOC_FILE_MAGIC + doc_id.encode("utf-8") + b"\n" + update)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            if durable_name:
                _fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


    @staticmethod
    def _read_document_file(path: Path) -> tuple[str, bytes]:
        raw = path.read_bytes()
        newline = raw.find(b"\n", len(_DOC_FILE_MAGIC))
        if not raw.startswith(_DOC_FILE_MAGIC) or newline == -1:
            raise StoreError("invalid-argument", f"invalid document file {path}")
        try:
            doc_id = _wire_doc_id(raw[len(_DOC_FILE_MAGIC) : newline].decode("ascii"))
        except (StoreError, UnicodeDecodeError) as exc:
            raise StoreError("invalid-argument", f"invalid document file {path}") from exc
        if not doc_id.startswith("doc-") or document_file_stem(doc_id) != path.stem:
            raise StoreError("invalid-argument", f"invalid document file {path}")
        return doc_id, raw[newline + 1 :]


    def _migrate_document_files(self) -> None:
        """Rename id-named ``docs/<doc id>.loro`` files (0.5.2 and earlier) once.

        Each file is rewritten under its hashed name (temporary file, fsync,
        rename, directory fsync) before the old name is removed, so a crash
        leaves the old file only, both (the new one complete), or the new one
        only, and the next open resumes from any of them.  With both present
        the new file wins: it may already carry later writes.  The lock file
        keeps two concurrent opens of the same space from interleaving.
        There is no way back: an older build cannot read ``h-*.loro``.
        """

        docs = self._root / "docs"
        with (self._root / ".docs-migration.lock").open("a+b") as stream:
            lock_exclusive(stream.fileno())
            try:
                for legacy in sorted(docs.glob("*.loro")):
                    if is_document_file_stem(legacy.stem):
                        continue
                    doc_id = _wire_doc_id(legacy.stem)
                    if not doc_id.startswith("doc-"):
                        raise StoreError(
                            "invalid-argument", f"invalid document path {legacy}"
                        )
                    target = self._doc_path(doc_id)
                    if not target.exists():
                        self._write_document_file(
                            target, doc_id, legacy.read_bytes(), durable_name=True
                        )
                    self._fault("docs-migration:written")
                    legacy.unlink()
                    _fsync_directory(docs)
            finally:
                unlock(stream.fileno())


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
        self._migrate_document_files()
        for path in sorted((self._root / "docs").glob("*.loro")):
            if not is_document_file_stem(path.stem):
                raise StoreError("invalid-argument", f"invalid document path {path}")
            doc_id, update = self._read_document_file(path)
            if self.retired(doc_id) is not None:
                path.unlink()
                continue
            self._docs[doc_id] = self._new_doc(doc_id)
            self._docs[doc_id].apply_update(update)


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
        self._persist_documents(tuple(self._docs))
        self._dirty_docs.clear()
        self._commits_since_checkpoint = 0


    def _mark_persist(self, doc_ids: Iterable[str]) -> None:
        """Record documents a commit or import changed; checkpoint in batches.

        The journal is the commit point: its fsync makes a write durable, and
        startup replays every journal row over the ``.loro`` files.  Those
        files are a cache, and only retirement deletes journal rows (after
        persisting every document).  Rewriting them on each write cost O(space)
        bytes and one fsync per document, so a write now only marks what it
        changed and the files are refreshed every ``_CHECKPOINT_COMMITS``
        writes, on retirement and on close.
        """

        changed = tuple(dict.fromkeys(doc_ids))
        # The frozen layout promises a state file per document once it has a
        # commit; only refreshes of an existing file are deferred.
        self._persist_documents(
            doc_id for doc_id in changed if not self._doc_path(doc_id).exists()
        )
        self._dirty_docs.update(changed)
        self._commits_since_checkpoint += 1
        if self._commits_since_checkpoint >= _CHECKPOINT_COMMITS:
            self.checkpoint()


    def checkpoint(self) -> None:
        """Refresh the cached state files of every document changed since the last one."""

        with self._lock:
            dirty = tuple(
                doc_id
                for doc_id in self._dirty_docs
                if doc_id in self._docs and self.retired(doc_id) is None
            )
            self._dirty_docs.clear()
            self._commits_since_checkpoint = 0
            self._persist_documents(dirty)


    def _persist_documents(self, doc_ids: Iterable[str]) -> None:
        for doc_id in dict.fromkeys(doc_ids):
            doc = self._docs[doc_id]
            path = self._doc_path(doc_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            if doc_id.startswith("doc-"):
                self._write_document_file(path, doc_id, doc.get_update())
                continue
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".orgfs-doc-", dir=path.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(doc.get_update())
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)


    def _doc(self, doc_id: str) -> Doc:
        _wire_doc_id(doc_id)
        if doc_id not in self._docs:
            self._docs[doc_id] = self._new_doc(doc_id)
        return self._docs[doc_id]


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
