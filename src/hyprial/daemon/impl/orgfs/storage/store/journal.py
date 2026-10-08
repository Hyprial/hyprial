from __future__ import annotations


import hashlib

import json





import sqlite3






from typing import Any, Iterable, Mapping

from pycrdt import Doc, Map


from hyprial.daemon.impl.orgfs.storage.store.vocabulary import CommitRecord, DocMutator, ORGFS_ENVELOPE_BYTES, ORGFS_INLINE_UPDATE_BYTES, StoreError, _EMPTY_UPDATE, _b64, _decode_state_vector, _doc_roots, _empty_meta, _encode_state_vector, _json_bytes, _now, _state_covers, _unb64, _wire_doc_id, _writer_is_safe

class StoreJournal:
    """Responsibility methods on the sole LocalSpaceStore state host.

    This class never constructs, copies, or persists an independent host.
    """

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
                meta_frontier=meta_frontier_before,
            ):
                raise StoreError("not-owner", "metadata update is owner-only")
            if doc_id != "meta" and not self._org_device_update_allowed(
                doc_id,
                update,
                author=author,
                space_owner=self._owner(),
                meta_baseline=self._docs["meta"],
            ):
                raise StoreError(
                    "not-a-member", "device row belongs to another member"
                )
            version = working.get_state()
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
            member_add_candidates = (
                self._member_add_candidates(
                    self._decode_envelope(envelope), baseline_update=before_update
                )
                if doc_id == "meta"
                else ()
            )
            self._fault("before_journal")
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "INSERT INTO commits(writer, seq, doc_id, envelope_bytes, version, committed_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (writer, seq, doc_id, envelope, version, _now()),
                )
                self._write_member_add_candidates(member_add_candidates)
            except BaseException:
                self._db.rollback()
                raise
            else:
                self._db.commit()
            self._fault("after_journal")
            doc.apply_update(update)
            self._fault("before_state")
            self._mark_persist((doc_id,))
            if doc_id == "meta":
                self._publish_blob_purge_projection()
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


    def commit_with_outbox(
        self, doc_id: str, mutate: DocMutator, *, author: str, actor: str | None
    ) -> tuple[CommitRecord, tuple[CommitRecord, ...]]:
        """Return this transaction's exact newly journaled publications."""

        with self._lock:
            before = int(
                self._db.execute(
                    "SELECT COALESCE(MAX(rowid), 0) FROM commits"
                ).fetchone()[0]
            )
            record = self.commit(doc_id, mutate, author=author, actor=actor)
            return record, self._unbroadcast_after_locked(before)


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
                    if not self._org_device_update_allowed(
                        doc_id,
                        update,
                        author=author,
                        space_owner=owner,
                        meta_baseline=self._docs["meta"],
                    ):
                        raise StoreError(
                            "not-a-member", "device row belongs to another member"
                        )
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
            self._mark_persist(doc_id for doc_id, *_rest in prepared)
            self._fault("after_state")
            return tuple(records)


    def commit_many_with_outbox(
        self,
        operations: Iterable[tuple[str, DocMutator]],
        *,
        author: str,
        actor: str | None,
    ) -> tuple[tuple[CommitRecord, ...], tuple[CommitRecord, ...]]:
        """Return the exact newly journaled publications for a batch."""

        with self._lock:
            before = int(
                self._db.execute(
                    "SELECT COALESCE(MAX(rowid), 0) FROM commits"
                ).fetchone()[0]
            )
            records = self.commit_many(operations, author=author, actor=actor)
            return records, self._unbroadcast_after_locked(before)


    def _unbroadcast_after_locked(self, rowid: int) -> tuple[CommitRecord, ...]:
        rows = self._db.execute(
            "SELECT * FROM commits "
            "WHERE rowid > ? AND broadcast_at IS NULL ORDER BY rowid",
            (rowid,),
        ).fetchall()
        return tuple(self._record_from_row(row) for row in rows)


    def committed_update(self, doc_id: str, since: bytes) -> bytes:
        """Return this node's committed ops for ``doc_id`` past ``since``.

        The per-space authority folds this back into the caller's content doc
        after a commit, so the writer's own facade carries the reserved
        coverage-clock op that ``_advance_commit_clock`` writes under the
        store's writer client.  Every outgoing envelope carries that op;
        without it the originating writer can never cover a content frontier
        a peer records after receiving it.
        """

        with self._lock:
            self._raise_if_retired(doc_id)
            return self._doc(doc_id).get_update(since)


    def frontier(self, doc_id: str) -> bytes:
        with self._lock:
            self._raise_if_retired(doc_id)
            return _encode_state_vector(
                _decode_state_vector(self._doc(doc_id).get_state())
            )



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
