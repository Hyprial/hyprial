from __future__ import annotations


import hashlib

import json











from typing import Any, Iterable, Mapping



from hyprial.daemon.impl.orgfs.storage.store.vocabulary import CommitRecord, ExportPage, SnapshotManifest, StoreError, _LOG, _b64, _decode_state_vector, _doc_roots, _encode_state_vector, _now, _state_covers, _unb64, _wire_doc_id, _writer_is_safe

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


    def _reset_history_indexes(self) -> None:
        self._history_rowids: dict[str, list[int]] = {}
        self._history_last_versions: dict[str, bytes] = {}
        self._history_nonmonotone: set[str] = set()
        if not hasattr(self, "_history_nonmonotone_logged"):
            self._history_nonmonotone_logged: set[str] = set()
        self._history_authors: dict[
            str, tuple[int, dict[str, int], dict[str, Any] | None]
        ] = {}
        self._history_member_add: dict[tuple[str, str], tuple[int, bytes, bool]] = {}


    def _remember_history(self, index: dict[Any, Any], key: Any, value: Any) -> None:
        if not self._db.in_transaction:
            index[key] = value


    def _doc_rowids(self, doc_id: str) -> tuple[list[int], bool]:
        cached = (
            None if self._db.in_transaction else self._history_rowids.get(doc_id)
        )
        rowids = list(cached) if cached is not None else []
        previous = (
            self._history_last_versions.get(doc_id) if cached is not None else None
        )
        monotone = cached is None or doc_id not in self._history_nonmonotone
        for rowid, raw_version in self._db.execute(
            "SELECT rowid, version FROM commits WHERE doc_id = ? AND rowid > ? "
            "ORDER BY rowid",
            (doc_id, rowids[-1] if rowids else 0),
        ):
            version = bytes(raw_version)
            if previous is not None and not _state_covers(version, previous):
                monotone = False
            rowids.append(int(rowid))
            previous = version
        if not self._db.in_transaction:
            self._history_rowids[doc_id] = rowids
            if previous is not None:
                self._history_last_versions[doc_id] = previous
            if not monotone:
                self._history_nonmonotone.add(doc_id)
                if doc_id not in self._history_nonmonotone_logged:
                    _LOG.warning(
                        "orgfs document history is non-monotone; export uses full scan: "
                        "space=%s doc=%s",
                        self.space_id,
                        doc_id,
                    )
                    self._history_nonmonotone_logged.add(doc_id)
        return rowids, monotone


    def _history_attribution(
        self, doc_id: str
    ) -> tuple[dict[str, int], dict[str, Any] | None]:
        after, counts, latest = self._history_authors.get(doc_id, (0, {}, None))
        if self._db.in_transaction:
            after, counts, latest = 0, {}, None
        counts = dict(counts)
        for rowid, raw in self._db.execute(
            "SELECT rowid, envelope_bytes FROM commits "
            "WHERE doc_id = ? AND rowid > ? ORDER BY rowid",
            (doc_id, after),
        ):
            origin = self._decode_envelope(bytes(raw))["origin"]
            author = str(origin["author"])
            counts[author] = counts.get(author, 0) + 1
            latest = origin
            after = int(rowid)
        self._remember_history(self._history_authors, doc_id, (after, counts, latest))
        return counts, latest


    def _first_uncovered(self, rowids: list[int], vv: bytes) -> int:
        if not vv or not rowids:
            return 0
        have = _decode_state_vector(vv)

        def covered(position: int) -> bool:
            row = self._db.execute(
                "SELECT version FROM commits WHERE rowid = ?", (rowids[position],)
            ).fetchone()
            return all(
                have.get(client, 0) >= clock
                for client, clock in _decode_state_vector(bytes(row[0])).items()
            )

        if covered(len(rowids) - 1):
            return len(rowids)
        low, high = 0, len(rowids) - 1
        while low < high:
            middle = (low + high) // 2
            if covered(middle):
                low = middle + 1
            else:
                high = middle
        return low


    def _export_since_full_scan(
        self, doc_id: str, vv: bytes, *, offset: int, max_bytes: int
    ) -> ExportPage:
        rows = self._db.execute(
            "SELECT version, envelope_bytes FROM commits "
            "WHERE doc_id = ? ORDER BY rowid",
            (doc_id,),
        ).fetchall()
        candidates = [
            bytes(row["envelope_bytes"])
            for row in rows
            if not (vv and _state_covers(vv, bytes(row["version"])))
        ]
        index = offset
        selected: list[bytes] = []
        size = len(b'{"envelopes":[]}')
        while index < len(candidates):
            envelope = candidates[index]
            grown = size + 4 * ((len(envelope) + 2) // 3) + 2 + bool(selected)
            if grown > max_bytes:
                if not selected:
                    selected.append(envelope)
                    index += 1
                break
            selected.append(envelope)
            size = grown
            index += 1
        next_cursor = (
            self._cursor(doc_id, index, vv) if index < len(candidates) else None
        )
        return ExportPage(tuple(selected), next_cursor, ())


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
            rowids, monotone = self._doc_rowids(doc_id)
            if not monotone:
                return self._export_since_full_scan(
                    doc_id, vv, offset=offset, max_bytes=max_bytes
                )
            start = self._first_uncovered(rowids, vv)
            index = start + offset
            selected: list[bytes] = []
            size = len(b'{"envelopes":[]}')
            while index < len(rowids):
                rows = self._db.execute(
                    "SELECT envelope_bytes FROM commits WHERE doc_id = ? "
                    "AND rowid >= ? ORDER BY rowid LIMIT ?",
                    (doc_id, rowids[index], 64),
                ).fetchall()
                full = False
                for row in rows:
                    envelope = bytes(row[0])
                    grown = size + 4 * ((len(envelope) + 2) // 3) + 2 + bool(selected)
                    if grown > max_bytes:
                        if not selected:
                            selected.append(envelope)
                            index += 1
                        full = True
                        break
                    selected.append(envelope)
                    size = grown
                    index += 1
                if full or not rows:
                    break
            next_cursor = (
                self._cursor(doc_id, index - start, vv) if index < len(rowids) else None
            )
            return ExportPage(tuple(selected), next_cursor, ())


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
            counts, latest = self._history_attribution(doc_id)
            authors = dict(counts)
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
            counts, _latest = self._history_attribution(doc_id)
        return tuple(sorted(counts))


    def document_covers_member_add(self, doc_id: str, user: str) -> bool:
        """Whether any durable document update covers the latest member add."""

        _wire_doc_id(doc_id)
        required = self.member_latest_add_frontier(user)
        if required is None:
            return False
        with self._lock:
            key = (doc_id, user)
            cached = self._history_member_add.get(key)
            after = 0
            if cached is not None and cached[1] == required:
                if cached[2]:
                    return True
                after = cached[0]
            wanted = _decode_state_vector(required)
            for rowid, raw in self._db.execute(
                "SELECT rowid, envelope_bytes FROM commits "
                "WHERE doc_id = ? AND rowid > ? ORDER BY rowid",
                (doc_id, after),
            ):
                try:
                    envelope = self._decode_envelope(bytes(raw))
                    frontier = _unb64(str(envelope["origin"]["metaFrontier"]))
                except (KeyError, StoreError, TypeError):
                    return False
                actual = _decode_state_vector(frontier)
                after = int(rowid)
                if all(actual.get(client, 0) >= clock for client, clock in wanted.items()):
                    self._remember_history(self._history_member_add, key, (after, required, True))
                    return True
            self._remember_history(self._history_member_add, key, (after, required, False))
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
