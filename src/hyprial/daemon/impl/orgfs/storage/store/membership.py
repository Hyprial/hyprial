from __future__ import annotations

import hashlib
import json

from typing import Any, Iterable, Mapping

from pycrdt import Doc, Map
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import DocMutator, PYCRDT_CLIENT_ID_MAX, RetirementRecord, StoreError, _LOG, _changed_meta_entries, _decode_state_vector, _doc_roots, _empty_meta, _encode_state_vector, _now, _state_covers, _unb64, _wire_doc_id, _writer_is_safe
_MEMBER_ADD_FRONTIER_SCHEMA_VERSION = 1

class StoreMembership:
    """Responsibility methods on the sole LocalSpaceStore state host.

    This class never constructs, copies, or persists an independent host.
    """

    def active_tree_doc_id(self) -> str | None:
        with self._lock:
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
        with self._lock:
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
        # Blob reads call this from transport threads.  pycrdt hands a caller
        # the doc's open transaction, which is bound to the thread that opened
        # it, so every read of a store doc must hold the lock writers hold.
        with self._lock:
            values = _doc_roots(self._docs["meta"]).get("purgeList")
        raw = values.get(sha) if isinstance(values, dict) else None
        return isinstance(raw, dict) and raw.get("unbannedAt") is None

    def purge_projection(self) -> frozenset[str]:
        """Return the current immutable set of active replicated blob bans."""

        values = _doc_roots(self._docs["meta"]).get("purgeList")
        return frozenset(
            sha
            for sha, entry in (values.items() if isinstance(values, dict) else ())
            if isinstance(entry, dict) and entry.get("unbannedAt") is None
        )

    def _publish_blob_purge_projection(self) -> None:
        if self.blob_store is not None and hasattr(
            self.blob_store, "register_purge_projection"
        ):
            self.blob_store.register_purge_projection(
                self.space_id, self.purge_projection()
            )

    def snapshot_point(self, doc_id: str) -> bytes | None:
        with self._lock:
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

    def member_latest_add_frontier(self, user: str) -> bytes | None:
        """Return the indexed causal owner frontier of the latest active add."""

        with self._lock:
            members = self._docs["meta"].get("members", type=Map)
            value = members.get(user)
            final = value.to_py() if isinstance(value, Map) else value
            if not isinstance(final, dict) or final.get("removedAt"):
                return None
            row = self._db.execute(
                "SELECT frontier FROM member_add_frontiers "
                "WHERE user = ? AND member_json = ?",
                (user, self._member_index_key(final)),
            ).fetchone()
            return None if row is None else bytes(row[0])

    def _member_latest_add_frontier_scan(self, user: str) -> bytes | None:
        """ROUND 13 test oracle: retain the pre-index journal scan verbatim."""

        with self._lock:
            owner = self._owner()
            if owner is None:
                return None
            final = self._membership().get(user)
            if not isinstance(final, dict) or final.get("removedAt"):
                return None
            latest: bytes | None = None
            for row in self._db.execute(
                "SELECT envelope_bytes FROM commits "
                "WHERE doc_id = 'meta' ORDER BY rowid"
            ):
                try:
                    envelope = self._decode_envelope(bytes(row[0]))
                    token = _unb64(str(envelope["origin"]["metaFrontier"]))
                    snapshot = self._membership_at(token)
                    if snapshot is None:
                        continue
                    probe = Doc()
                    probe.apply_update(snapshot[2].get_update())
                    before = _doc_roots(probe).get("members")
                    prior = before.get(user) if isinstance(before, dict) else None
                    probe.apply_update(self._update_bytes(envelope))
                except (KeyError, StoreError, TypeError, ValueError):
                    continue
                after = _doc_roots(probe).get("members")
                current = after.get(user) if isinstance(after, dict) else None
                active = isinstance(current, dict) and not current.get("removedAt")
                prior_active = isinstance(prior, dict) and not prior.get("removedAt")
                if (
                    active
                    and (
                        not prior_active
                        or prior.get("addedAt") != current.get("addedAt")
                    )
                    and current == final
                ):
                    candidate = self._project_frontier(probe, owner)
                    if latest is None or _state_covers(candidate, latest):
                        latest = candidate
            return latest

    @staticmethod
    def _member_index_key(member: Mapping[str, Any]) -> str:
        return json.dumps(member, sort_keys=True, separators=(",", ":"))

    def _candidate_owner_frontier(self, doc: Doc, owner: str) -> bytes:
        """Project one causal probe without depending on the live meta doc."""

        owner_ids = self._journal_client_ids(owner)
        writers = _doc_roots(doc).get("writers")
        if isinstance(writers, dict):
            for writer, details in writers.items():
                if isinstance(details, dict) and str(details.get("author")) == owner:
                    owner_ids.update(self._row_client_ids(details))
                    owner_ids.add(self._client_id(str(writer)))
        state = _decode_state_vector(doc.get_state())
        return _encode_state_vector(
            {client: clock for client, clock in state.items() if client in owner_ids}
        )

    def _member_add_candidates(
        self,
        envelope: Mapping[str, Any],
        *,
        baseline_update: bytes | None = None,
    ) -> tuple[tuple[str, str, bytes], ...]:
        """Derive add rows from an envelope's own causal membership baseline."""

        if str(envelope.get("docId")) != "meta":
            return ()
        origin = envelope.get("origin")
        if not isinstance(origin, Mapping):
            return ()
        try:
            token = _unb64(str(origin["metaFrontier"]))
        except (KeyError, StoreError, TypeError):
            return ()
        if not token:
            # Genesis has no causal frontier and cannot be used by a leave row.
            return ()
        owner: str | None
        if baseline_update is None:
            snapshot = self._membership_at(token)
            if snapshot is None:
                return ()
            owner, _membership, baseline = snapshot
            baseline_update = baseline.get_update()
        else:
            baseline = Doc()
            baseline.apply_update(baseline_update)
            roots = _doc_roots(baseline)
            space = roots.get("space")
            owner = (
                str(space.get("owner"))
                if isinstance(space, dict) and space.get("owner")
                else None
            )
        if owner is None:
            return ()
        probe = Doc()
        try:
            probe.apply_update(baseline_update)
            before = _doc_roots(probe).get("members")
            probe.apply_update(self._update_bytes(envelope))
        except (StoreError, TypeError, ValueError):
            return ()
        before = before if isinstance(before, dict) else {}
        after = _doc_roots(probe).get("members")
        after = after if isinstance(after, dict) else {}
        changed: list[tuple[str, str, bytes]] = []
        candidate: bytes | None = None
        for user in sorted(set(before) | set(after)):
            prior = before.get(user)
            current = after.get(user)
            active = isinstance(current, dict) and not current.get("removedAt")
            prior_active = isinstance(prior, dict) and not prior.get("removedAt")
            if active and (
                not prior_active or prior.get("addedAt") != current.get("addedAt")
            ):
                if candidate is None:
                    candidate = self._candidate_owner_frontier(probe, owner)
                changed.append((user, self._member_index_key(current), candidate))
        return tuple(changed)

    def _write_member_add_candidates(
        self, candidates: Iterable[tuple[str, str, bytes]]
    ) -> None:
        """Merge derived rows inside the caller's meta-journal transaction."""

        for user, member_json, frontier in candidates:
            row = self._db.execute(
                "SELECT frontier FROM member_add_frontiers "
                "WHERE user = ? AND member_json = ?",
                (user, member_json),
            ).fetchone()
            if row is not None and not _state_covers(frontier, bytes(row[0])):
                continue
            self._db.execute(
                "INSERT INTO member_add_frontiers(user, member_json, frontier) "
                "VALUES (?, ?, ?) ON CONFLICT(user, member_json) DO UPDATE SET "
                "frontier = excluded.frontier",
                (user, member_json, frontier),
            )

    def _rebuild_member_add_frontiers(self) -> None:
        """Rebuild by sharing causal probes across owner writer restarts."""
        owner = self._owner()
        owner_ids = set(self._meta_writer_client_ids(owner)) if owner else set()
        entries: list[tuple[int, dict[int, int], bytes, int, int, int]] = []
        member_updates: list[tuple[int, bytes]] = []
        lanes: dict[int, list[tuple[int, bytes]]] = {}
        rows = self._db.execute(
            "SELECT rowid, writer, envelope_bytes, version FROM commits "
            "WHERE doc_id = 'meta' ORDER BY rowid"
        )
        for row in rows:
            try:
                envelope = self._decode_envelope(bytes(row["envelope_bytes"]))
                origin = envelope["origin"]
                target = _decode_state_vector(_unb64(str(origin["metaFrontier"])))
                update = self._update_bytes(envelope)
                version = _decode_state_vector(bytes(row["version"]))
            except (KeyError, StoreError, TypeError, ValueError):
                continue
            rowid = int(row["rowid"])
            if owner is None:
                continue
            if str(origin.get("author")) != owner:
                member_updates.append((rowid, update))
                continue
            writer = str(row["writer"])
            client_id = self._client_id(writer)
            owner_ids.add(client_id)
            clock = version.get(client_id, 0)
            entries.append((len(entries), target, update, client_id, clock, rowid))
            lanes.setdefault(client_id, []).append((clock, update))

        candidate_batches: list[list[tuple[str, str, bytes]]] = [[] for _entry in entries]
        probes: list[dict[str, Any]] = []
        for index, target, update, entry_client, entry_clock, rowid in entries:
            eligible: list[tuple[int, dict[str, Any]]] = []
            for retained in probes:
                frontier = retained["frontier"]
                if self._state_map_covers(target, frontier):
                    eligible.append((sum(frontier.values()), retained))
            if eligible:
                probe_state = max(eligible, key=lambda item: item[0])[1]
            else:
                probe_state = {
                    "doc": Doc(),
                    "members": {},
                    "positions": {},
                    "member_position": 0,
                    "frontier": {},
                }
                probes.append(probe_state)

            probe = probe_state["doc"]
            probe_members = probe_state["members"]
            member_position = int(probe_state["member_position"])
            while member_position < len(member_updates) and (
                member_updates[member_position][0] < rowid
            ):
                try:
                    self._apply_member_rebuild_update(
                        probe, probe_members, member_updates[member_position][1]
                    )
                except (TypeError, ValueError):
                    break
                member_position += 1
            probe_state["member_position"] = member_position

            current = probe_state["frontier"]
            positions = probe_state["positions"]
            targets = list(target.items())
            stalled = 0
            while targets and stalled < len(targets):
                client_id, target_clock = targets.pop(0)
                lane = lanes.get(client_id, ())
                position = positions.get(client_id, 0)
                while position < len(lane) and lane[position][0] <= current.get(client_id, 0):
                    position += 1
                while position < len(lane) and lane[position][0] <= target_clock:
                    try:
                        before_state = probe.get_state()
                        self._apply_member_rebuild_update(
                            probe, probe_members, lane[position][1]
                        )
                        if not self._rebuild_update_integrated(
                            probe, before_state, client_id, lane[position][0]
                        ):
                            break
                    except (TypeError, ValueError):
                        break
                    current[client_id] = lane[position][0]
                    position += 1
                positions[client_id] = position
                if current.get(client_id, 0) < target_clock:
                    targets.append((client_id, target_clock))
                    stalled += 1
                else:
                    stalled = 0
            if current != target:
                _LOG.warning(
                    "orgfs member-add rebuild skipped an entry whose frontier is "
                    "unreachable: space=%s rowid=%s",
                    self.space_id,
                    rowid,
                )
                continue
            try:
                changes = self._apply_member_rebuild_update(
                    probe, probe_members, update
                )
            except (TypeError, ValueError):
                _LOG.warning(
                    "orgfs member-add rebuild skipped an undecodable entry: "
                    "space=%s rowid=%s",
                    self.space_id,
                    rowid,
                )
                continue
            current[entry_client] = entry_clock
            exact_frontier: bytes | None = None
            candidates: list[tuple[str, str, bytes]] = []
            if target:
                for user, prior, current in changes:
                    active = isinstance(current, dict) and not current.get("removedAt")
                    prior_active = isinstance(prior, dict) and not prior.get(
                        "removedAt"
                    )
                    if active and (
                        not prior_active
                        or prior.get("addedAt") != current.get("addedAt")
                    ):
                        if exact_frontier is None:
                            exact_frontier = _encode_state_vector(
                                probe_state["frontier"]
                            )
                        candidates.append(
                            (
                                user,
                                self._member_index_key(current),
                                exact_frontier,
                            )
                        )
                candidate_batches[index] = candidates

        with self._db:
            self._db.execute("DELETE FROM member_add_frontiers")
            for candidates in candidate_batches:
                self._write_member_add_candidates(candidates)
            self._db.execute(
                "INSERT OR REPLACE INTO member_add_frontier_state"
                "(singleton, schema_version) VALUES (1, ?)",
                (_MEMBER_ADD_FRONTIER_SCHEMA_VERSION,),
            )

    @staticmethod
    def _apply_member_rebuild_update(
        doc: Doc,
        membership: dict[str, dict[str, Any]],
        update: bytes,
    ) -> tuple[tuple[str, Any, Any], ...]:
        members = doc.get("members", type=Map)
        changed: set[str] = set()

        def observe(events: Iterable[Any]) -> None:
            for event in events:
                if event.path:
                    changed.add(str(event.path[0]))
                else:
                    changed.update(event.keys)

        subscription = (
            members.observe_deep(observe) if isinstance(members, Map) else None
        )
        try:
            doc.apply_update(update)
        finally:
            if subscription is not None:
                subscription.drop()
        current_members = doc.get("members", type=Map)
        if not isinstance(members, Map) and isinstance(current_members, Map):
            changed.update(current_members)
        changes: list[tuple[str, Any, Any]] = []
        for user in sorted(changed):
            prior = membership.get(user)
            value = (
                current_members.get(user) if isinstance(current_members, Map) else None
            )
            current = value.to_py() if isinstance(value, Map) else value
            if isinstance(current, dict):
                membership[user] = current
            else:
                membership.pop(user, None)
            changes.append((user, prior, current))
        return tuple(changes)

    @staticmethod
    def _state_map_covers(actual: Mapping[int, int], required: Mapping[int, int]) -> bool:
        return all(actual.get(client, 0) >= clock for client, clock in required.items())
    @staticmethod
    def _rebuild_update_integrated(doc: Doc, before: bytes, client: int, clock: int) -> bool:
        return doc.get_state() != before or _decode_state_vector(before).get(client, 0) >= clock

    @staticmethod
    def _frontier_for_client_ids(doc: Doc, client_ids: set[int]) -> bytes:
        state = _decode_state_vector(doc.get_state())
        return _encode_state_vector(
            {client: clock for client, clock in state.items() if client in client_ids}
        )

    def _owner_client_ids(self, owner: str) -> set[int]:
        # Called once per candidate while a metadata frontier is replayed, so
        # it must not rescan the meta journal or re-read the meta document each
        # time: both parts are cached and only extended by what changed.
        return set(self._meta_writer_client_ids(owner)) | self._journal_client_ids(
            owner
        )

    def _meta_writer_client_ids(self, owner: str) -> frozenset[int]:
        meta = self._docs["meta"]
        state = meta.get_state()
        key = (owner, state, meta.get_update(state))
        cached = getattr(self, "_meta_writer_ids_cache", None)
        if cached is not None and cached[0] == key:
            return cached[1]
        ids: set[int] = set()
        writers = _doc_roots(meta).get("writers")
        if isinstance(writers, dict):
            for writer, details in writers.items():
                if isinstance(details, dict) and str(details.get("author")) == owner:
                    ids.update(self._row_client_ids(details))
                    # Keep old journals readable while new rows record the
                    # actual client id introduced by the facade mutation.
                    ids.add(self._client_id(str(writer)))
        result = frozenset(ids)
        self._meta_writer_ids_cache = (key, result)
        return result

    def _journal_client_ids(self, owner: str) -> set[int]:
        # Meta journal rows are append-only (retirement never deletes meta),
        # so scan only the rows added since the previous call.  Inside an open
        # transaction a row may still roll back and its rowid be reused, so
        # scan everything uncached there.
        if self._db.in_transaction:
            scan = {"rowid": 0, "by_author": {}}
        else:
            scan = getattr(self, "_meta_journal_scan", None)
            if scan is None:
                scan = self._meta_journal_scan = {"rowid": 0, "by_author": {}}
        by_author: dict[str, set[int]] = scan["by_author"]
        for row in self._db.execute(
            "SELECT rowid, envelope_bytes FROM commits "
            "WHERE doc_id = 'meta' AND rowid > ? ORDER BY rowid",
            (scan["rowid"],),
        ):
            scan["rowid"] = int(row[0])
            try:
                envelope = self._decode_envelope(bytes(row[1]))
            except StoreError:
                continue
            origin = envelope["origin"]
            by_author.setdefault(str(origin["author"]), set()).add(
                self._client_id(str(origin["writer"]))
            )
        return set(by_author.get(owner, ()))

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
        owner_ids = self._owner_client_ids(owner)
        target = _decode_state_vector(token)
        selected: list[bytes] = []
        remaining: list[tuple[dict[int, int], bytes]] = []
        for commit in self._db.execute(
            "SELECT envelope_bytes FROM commits WHERE doc_id = 'meta' ORDER BY rowid"
        ):
            try:
                envelope = self._decode_envelope(bytes(commit["envelope_bytes"]))
                update = self._update_bytes(envelope)
                if str(envelope["origin"].get("author")) == owner:
                    remaining.append(
                        (
                            _decode_state_vector(
                                _unb64(str(envelope["origin"]["metaFrontier"]))
                            ),
                            update,
                        )
                    )
                else:
                    selected.append(update)
            except (KeyError, StoreError, TypeError, ValueError):
                return None
        probe = Doc()
        for update in selected:
            probe.apply_update(update)
        while remaining:
            progressed = False
            for before, update in tuple(remaining):
                if not self._state_map_covers(target, before):
                    remaining.remove((before, update))
                    continue
                trial = Doc()
                try:
                    for applied in (*selected, update):
                        trial.apply_update(applied)
                except (TypeError, ValueError):
                    return None
                trial_frontier = {
                    client: clock
                    for client, clock in _decode_state_vector(
                        trial.get_state()
                    ).items()
                    if client in owner_ids
                }
                remaining.remove((before, update))
                if self._state_map_covers(target, trial_frontier):
                    probe = trial
                    selected.append(update)
                    progressed = True
                    break
            if not progressed:
                break
        projected = {
            client: clock
            for client, clock in _decode_state_vector(probe.get_state()).items()
            if client in owner_ids
        }
        if projected != target:
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
            # The journal is authoritative.  A later admission read derives
            # this row lazily, and the next meta commit retries the live head.
            _LOG.warning(
                "orgfs deferred metadata frontier materialization: space=%s",
                self.space_id,
            )

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
        self, *, author: str, writer: str, update: bytes, baseline: bytes, meta_frontier: bytes
    ) -> bool:
        if meta_frontier:
            snapshot = self._membership_at(meta_frontier)
            if snapshot is None:
                # Not a permission answer: the frontier this commit builds on
                # has no materialized membership, for the owner too.
                raise StoreError(
                    "unavailable",
                    "the metadata frontier this update builds on is not materialized",
                )
            owner, members, frontier_doc = snapshot
            envelope = {"origin": {"author": author, "writer": writer, "node": self.node_id}}
            return self._meta_exception_allowed(envelope, update, owner, members, frontier_doc)
        owner = self._owner()
        member = self._membership().get(author)
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
