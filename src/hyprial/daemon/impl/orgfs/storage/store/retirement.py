from __future__ import annotations


import hashlib

import json










from pathlib import Path

from typing import Any, Iterable, Mapping



from hyprial.daemon.impl.orgfs.storage.store.vocabulary import RetirementRecord, StoreError, _decode_state_vector, _doc_roots, _now

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
            # Drop the live copy too, so no later checkpoint can rewrite the
            # retired document's state file from memory.
            self._docs.pop(old_doc_id, None)
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
