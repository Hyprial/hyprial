# Decision record (2026-11-23): unconfirmed_commits is a materialized count.
# - commit_stats.local_total moves in the same SQLite transaction as every
#   local-origin journal insert and every retired-document delete.  Rows written
#   before origin_node existed are classified once, by the node segment of the
#   writer they were inserted under (see storage/store/__init__.py).
# - Covered local rows are tracked in memory per document and change only when
#   a holder frontier or its durability changes.  Status reads one stats row.
from __future__ import annotations


import hashlib

import json










from pathlib import Path

from typing import Any, Iterable, Mapping



from hyprial.daemon.impl.orgfs.storage.store.vocabulary import RetirementRecord, StoreError, _decode_state_vector, _doc_roots, _now

_HEX_DIGITS = frozenset("0123456789abcdef")


def _writer_belongs_to_node(writer: str, node_id: str) -> bool:
    """Return whether ``writer`` is a lane writer created by ``node_id``.

    Local writers are ``<node>.<32 hex incarnation>.lane-<n>``.  Splitting from
    the right keeps a node id that contains dots intact and rejects another
    node whose id merely extends this one.
    """

    prefix = node_id + "."
    if not writer.startswith(prefix):
        return False
    incarnation, separator, lane = writer[len(prefix) :].partition(".")
    if not separator or not lane.startswith("lane-"):
        return False
    if len(incarnation) != 32 or any(
        character not in _HEX_DIGITS for character in incarnation
    ):
        return False
    suffix = lane[len("lane-") :]
    return suffix.isascii() and suffix.isdigit()


class StoreRetirement:
    """Responsibility methods on the sole LocalSpaceStore state host.

    This class never constructs, copies, or persists an independent host.
    """

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
            if self._db.execute(
                "SELECT 1 FROM commits WHERE doc_id = ? LIMIT 1", (new_doc_id,)
            ).fetchone() is not None:
                raise StoreError(
                    "invalid-argument",
                    "replacement target already has history",
                    doc_id=new_doc_id,
                )
            replacement = self._new_doc(new_doc_id)
            replacement.apply_update(snapshot_bytes)
            self._invalidate_doc_caches(new_doc_id)
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
                        "(writer, seq, doc_id, envelope_bytes, version, origin_node, committed_at, broadcast_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            writer,
                            seq,
                            new_doc_id,
                            envelope,
                            replacement.get_state(),
                            self.node_id,
                            _now(),
                            _now(),
                        ),
                    )
                    self._db.execute(
                        "UPDATE commit_stats SET local_total = local_total + 1 "
                        "WHERE singleton = 1"
                    )
                self._note_covered_insert(new_doc_id, replacement.get_state())
            self._docs.pop(old_doc_id, None)
            self._invalidate_doc_caches(old_doc_id)
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
                retired_local = sum(
                    1
                    for row in self._db.execute(
                        "SELECT writer, origin_node FROM commits WHERE doc_id = ?",
                        (old_doc_id,),
                    )
                    if self._row_is_local_origin(row["writer"], row["origin_node"])
                )
                self._db.execute("DELETE FROM pending WHERE doc_id = ?", (old_doc_id,))
                self._db.execute("DELETE FROM commits WHERE doc_id = ?", (old_doc_id,))
                self._db.execute(
                    "DELETE FROM snapshots WHERE doc_id = ?", (old_doc_id,)
                )
                if retired_local:
                    self._db.execute(
                        "UPDATE commit_stats SET local_total = MAX(0, local_total - ?) "
                        "WHERE singleton = 1",
                        (retired_local,),
                    )
            covered = self._covered_by_doc.pop(old_doc_id, 0)
            if covered:
                self._covered_local = max(0, self._covered_local - covered)
            # Drop the live copy too, so no later checkpoint can rewrite the
            # retired document's state file from memory.
            self._docs.pop(old_doc_id, None)
            self._invalidate_doc_caches(old_doc_id)
            self._dirty_docs.discard(old_doc_id)
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
            # VACUUM may renumber rowids of a table without an INTEGER PRIMARY
            # KEY, so the incremental meta-journal cursor is no longer valid.
            self._meta_journal_scan = None
            self._reset_history_indexes()
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


    def parked_commits(self) -> tuple[tuple[str, int], ...]:
        """(author, count) of commits held in ``pending`` behind a missing predecessor."""
        with self._lock:
            rows = self._db.execute(
                "SELECT author, COUNT(*) AS n FROM pending GROUP BY author ORDER BY author"
            ).fetchall()
        return tuple((str(row["author"]), int(row["n"])) for row in rows)


    def _init_commit_stats(self) -> None:
        """Classify pre-origin_node rows once and persist the local row count."""

        with self._lock:
            row = self._db.execute(
                "SELECT initialized FROM commit_stats WHERE singleton = 1"
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO commit_stats(singleton, local_total, initialized) "
                    "VALUES (1, 0, 0)"
                )
            elif int(row["initialized"]):
                return
            total = 0
            for counted in self._db.execute(
                "SELECT writer, COUNT(*) AS n FROM commits GROUP BY writer"
            ):
                if _writer_belongs_to_node(str(counted["writer"]), self.node_id):
                    total += int(counted["n"])
            with self._db:
                self._db.execute(
                    "UPDATE commit_stats SET local_total = ?, initialized = 1 "
                    "WHERE singleton = 1",
                    (total,),
                )


    def _row_is_local_origin(self, writer: object, origin_node: object) -> bool:
        if origin_node is not None:
            return str(origin_node) == self.node_id
        return _writer_belongs_to_node(str(writer), self.node_id)


    def unconfirmed_commits(self) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT local_total FROM commit_stats WHERE singleton = 1"
            ).fetchone()
            total = 0 if row is None else int(row[0])
            return max(0, total - self._covered_local)
