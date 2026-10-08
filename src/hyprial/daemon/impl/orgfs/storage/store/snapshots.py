from __future__ import annotations


import hashlib

import json











from typing import Any, Iterable, Mapping



from hyprial.daemon.impl.orgfs.storage.store.vocabulary import CommitRecord, SnapshotManifest, StoreError, _b64, _decode_state_vector, _encode_state_vector, _now, _state_covers, _unb64, _wire_doc_id, _writer_is_safe

class StoreSnapshots:
    """Responsibility methods on the sole LocalSpaceStore state host.

    This class never constructs, copies, or persists an independent host.
    """

    def unbroadcast(self) -> Iterable[CommitRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM commits WHERE broadcast_at IS NULL ORDER BY rowid"
            ).fetchall()
            return tuple(self._record_from_row(row) for row in rows)


    def unbroadcast_through(
        self, keys: tuple[tuple[str, int, str], ...]
    ) -> tuple[CommitRecord, ...]:
        """Return unpublished durable rows through a transaction's journal frontier."""

        if not keys:
            return ()
        with self._lock:
            rowids = []
            for writer, seq, doc_id in keys:
                row = self._db.execute(
                    "SELECT rowid FROM commits WHERE writer = ? AND seq = ? AND doc_id = ?",
                    (writer, seq, doc_id),
                ).fetchone()
                if row is not None:
                    rowids.append(int(row[0]))
            if not rowids:
                return ()
            rows = self._db.execute(
                "SELECT * FROM commits WHERE broadcast_at IS NULL AND rowid <= ? "
                "ORDER BY rowid",
                (max(rowids),),
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

    def document_authors(self, doc_id: str) -> tuple[str, ...]:
        """Return every durable author attributed to the physical document."""

        _wire_doc_id(doc_id)
        with self._lock:
            authors = {
                str(self._decode_envelope(bytes(row[0]))["origin"]["author"])
                for row in self._db.execute(
                    "SELECT envelope_bytes FROM commits WHERE doc_id = ? ORDER BY rowid",
                    (doc_id,),
                )
            }
        return tuple(sorted(authors))


    def document_covers_member_add(self, doc_id: str, user: str) -> bool:
        """Whether any durable document update covers the latest member add."""

        _wire_doc_id(doc_id)
        required = self.member_latest_add_frontier(user)
        if required is None:
            return False
        with self._lock:
            for row in self._db.execute(
                "SELECT envelope_bytes FROM commits WHERE doc_id = ? ORDER BY rowid",
                (doc_id,),
            ):
                try:
                    envelope = self._decode_envelope(bytes(row[0]))
                    frontier = _unb64(str(envelope["origin"]["metaFrontier"]))
                except (KeyError, StoreError, TypeError):
                    return False
                if _state_covers(frontier, required):
                    return True
        return False


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
