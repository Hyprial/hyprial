# Decision record
# - 2026-11-24 (S3a): the legacy receivers write guard and its decision record
#   live in ``admission_support/legacy_receivers.py``; the gate stays in
#   ``_org_device_update_allowed``.
"""OrgFS update admission, including organization device-row ownership.

Protected org-directory ownership is enforced independently by every replica.
This boundary assumes ``origin.author`` is genuine; signatures are a separate
prerequisite and are not provided by this module.  Protected content uses a
deterministic id encoding its path principal, so admission depends only on
``(doc_id, author)`` and cannot vary with content/link arrival order.  Aged
pending rows are re-evaluated under the current admission policy before
removal; every expiry batch emits its space, organization, oldest hold time,
and admitted/discarded counts so expiry is never a silent loss boundary.
"""

from __future__ import annotations

import json
from collections import Counter
import sqlite3
from typing import Any, Mapping

from pycrdt import Array, Doc, Map

from hyprial.identity import (
    directory_owner_principal,
    org_from_space_name,
    parse_protected_directory_node_id,
    parse_protected_directory_doc_id,
    protected_directory_author_allowed,
    protected_section_for_path,
    protected_directory_node_id,
    protected_directory_doc_id,
)

from hyprial.daemon.impl.orgfs.storage.store.admission_support import StoreLegacyReceivers, StorePending
from hyprial.daemon.impl.orgfs.storage.store.admission_support.legacy_receivers import _LEGACY_RECEIVERS_SECTION
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import AdmissionDecision, CommitRecord, ImportResult, StoreError, _LOG, _b64, _changed_meta_entries, _decode_state_vector, _doc_roots, _now, _tree_node_operation_allowed, _unb64

class StoreAdmission(StorePending, StoreLegacyReceivers):
    """Responsibility methods on the sole LocalSpaceStore state host.

    This class never constructs, copies, or persists an independent host.
    The pending-hold queue lives in :class:`StorePending`; the admission
    decision below re-judges a parked row before it drains.
    """

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
            before_state = probe.get_state()
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
        changed_ids = self._changed_client_ids(before_state, probe.get_state())
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

    @staticmethod
    def _is_org_directory_space(meta: Doc) -> bool:
        """The one naming seam item 2 can change when org spaces are renamed."""

        space = _doc_roots(meta).get("space")
        name = space.get("name") if isinstance(space, dict) else None
        return isinstance(name, str) and org_from_space_name(name) is not None

    @staticmethod
    def _deletion_only(
        before: Mapping[str, Any] | None, after: Mapping[str, Any] | None
    ) -> bool:
        if before is None or after is None:
            return False
        prior = dict(before)
        current = dict(after)
        was_deleted = bool(prior.pop("deleted", False))
        is_deleted = bool(current.pop("deleted", False))
        return not was_deleted and is_deleted and prior == current

    def _device_tree_update_allowed(
        self,
        baseline: Doc,
        update: bytes,
        *,
        author: str,
        space_owner: str | None,
        space_id: str,
        space_name: str | None = None,
    ) -> bool | None:
        probe = self._new_doc("tree-probe")
        try:
            probe.apply_update(baseline.get_update())
            before = self._tree_rows(probe)
            before_moves = self._tree_move_wires(probe)
            probe.apply_update(update)
            after = self._tree_rows(probe)
            after_moves = self._tree_move_wires(probe)
        except (TypeError, ValueError):
            return False
        try:
            principal = directory_owner_principal(author)
            owner_principal = (
                directory_owner_principal(space_owner) if space_owner else None
            )
        except ValueError:
            return False
        if owner_principal is None:
            return False
        new_moves = Counter(after_moves) - Counter(before_moves)
        for raw in new_moves.elements():
            try:
                move = json.loads(raw)
            except json.JSONDecodeError:
                return False
            if not isinstance(move, dict) or "oldParent" not in move:
                return False
            moved = move.get("node")
            old_parent = move.get("oldParent")
            new_parent = move.get("newParent")
            name = move.get("name")
            if not isinstance(moved, str) or not isinstance(old_parent, str):
                return False
            if new_parent is not None and not isinstance(new_parent, str):
                return False
            if not isinstance(name, str):
                return False
            try:
                if any(
                    parse_protected_directory_node_id(node_id) is not None
                    for node_id in (moved, old_parent, new_parent)
                    if node_id is not None
                ):
                    return False
                new_parent_identity = (
                    parse_protected_directory_node_id(new_parent)
                    if new_parent is not None
                    else None
                )
                result_path = (
                    name
                    if new_parent == "root"
                    else (
                        f"{new_parent_identity[2]}/{name}"
                        if new_parent_identity is not None
                        else None
                    )
                )
                expected_result_id = (
                    protected_directory_node_id(
                        space_id,
                        result_path,
                        space_owner=space_owner,
                    )
                    if result_path is not None
                    else None
                )
            except ValueError:
                return False
            if expected_result_id is not None and moved != expected_result_id:
                return False

        authored = self._new_doc("tree-authored-probe")
        try:
            authored.apply_update(update)
            authored_rows = self._tree_rows(authored, strict=True)
        except (TypeError, ValueError):
            return False
        for node_id, node in authored_rows.items():
            if not _tree_node_operation_allowed(
                node_id,
                node,
                principal=principal,
                space_owner=space_owner,
                space_id=space_id,
                space_name=space_name,
            ):
                return False

        changed = {
            node_id
            for node_id in before.keys() | after.keys()
            if before.get(node_id) != after.get(node_id)
        }
        for node_id in changed:
            prior = before.get(node_id)
            after_node = after.get(node_id)
            if after_node is not None and after_node.get("node_id") != node_id:
                return False
            if node_id == "root":
                if principal != owner_principal:
                    return False
                continue
            try:
                identity = parse_protected_directory_node_id(node_id)
            except ValueError:
                return False
            if identity is not None and identity[0] != space_id:
                return False
            if identity is not None and (identity[2] == "directory/user-keys"
                                         or identity[2].startswith("directory/user-keys/")):
                return False
            section = (
                protected_section_for_path(identity[2], space_owner=space_owner)
                if identity is not None
                else "devices"
            ) or "devices"
            position_changed = after_node is not None and (
                prior is None
                or prior.get("parent") != after_node.get("parent")
                or prior.get("name") != after_node.get("name")
            )
            if prior is not None and position_changed:
                assert after_node is not None
                try:
                    if any(
                        parse_protected_directory_node_id(candidate) is not None
                        for candidate in (
                            node_id,
                            prior.get("parent"),
                            after_node.get("parent"),
                        )
                        if isinstance(candidate, str)
                    ):
                        return False
                except ValueError:
                    return False
            if position_changed:
                assert after_node is not None
                parent = after_node.get("parent")
                name = after_node.get("name")
                if not isinstance(parent, str) or not isinstance(name, str):
                    return False
                try:
                    parent_identity = parse_protected_directory_node_id(parent)
                except ValueError:
                    return False
                if parent == "root":
                    child_path = name
                elif parent_identity is not None:
                    if parent_identity[0] != space_id:
                        return False
                    child_path = f"{parent_identity[2]}/{name}"
                elif prior is None and parent not in after:
                    return None
                else:
                    child_path = None
                try:
                    expected_node_id = (
                        protected_directory_node_id(
                            space_id,
                            child_path,
                            space_owner=space_owner,
                        )
                        if child_path is not None
                        else None
                    )
                except ValueError:
                    return False
                if expected_node_id is not None and node_id != expected_node_id:
                    return False
                if prior is None and identity is not None and (
                    node_id != expected_node_id
                    or not protected_directory_author_allowed(
                        section=section,
                        path_principal=identity[1],
                        author=principal,
                        space_owner=space_owner,
                        space_name=space_name,
                    )
                ):
                    return False
            if prior is not None and identity is not None:
                if after_node is None:
                    return False
                deletion = self._deletion_only(prior, after_node)
                if deletion:
                    if not protected_directory_author_allowed(
                        section=section,
                        path_principal=identity[1],
                        author=principal,
                        space_owner=space_owner,
                        space_name=space_name,
                    ):
                        return False
                elif not protected_directory_author_allowed(
                    section=section,
                    path_principal=identity[1],
                    author=principal,
                    space_owner=space_owner,
                    space_name=space_name,
                ):
                    return False
            if (
                after_node is not None
                and not after_node.get("deleted")
                and after_node.get("kind") == "doc"
            ):
                linked_doc_id = str(after_node.get("doc_id", ""))
                try:
                    linked_protected = (
                        parse_protected_directory_doc_id(linked_doc_id) is not None
                    )
                    expected_doc_id = (
                        protected_directory_doc_id(
                            space_id,
                            identity[2],
                            space_owner=space_owner,
                        )
                        if identity is not None
                        else None
                    )
                except ValueError:
                    return False
                if (linked_protected or expected_doc_id is not None) and (
                    linked_doc_id != expected_doc_id
                ):
                    return False
        return True

    @staticmethod
    def _tree_rows(
        doc: Doc, *, strict: bool = False
    ) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for node_id, raw in (doc.get("nodes", type=Map).to_py() or {}).items():
            if not isinstance(raw, str):
                if strict:
                    raise ValueError("incoming tree row must be JSON text")
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                if strict:
                    raise ValueError("incoming tree row must be valid JSON") from None
                _LOG.warning("orgfs skipped invalid tree row node_id=%s", node_id)
                continue
            if isinstance(value, dict):
                rows[str(node_id)] = value
            elif strict:
                raise ValueError("incoming tree row must be an object")
        return rows

    @staticmethod
    def _tree_move_wires(doc: Doc) -> list[str]:
        return [
            raw
            for raw in (doc.get("moves", type=Array).to_py() or [])
            if isinstance(raw, str)
        ]

    def _org_device_update_allowed(
        self,
        doc_id: str,
        update: bytes,
        *,
        author: str,
        space_owner: str | None,
        meta_baseline: Doc,
        local: bool = False,
    ) -> bool | None:
        try:
            protected = parse_protected_directory_doc_id(doc_id)
        except ValueError:
            return False
        if protected is not None and protected[1] == "user-keys":
            return False
        org_space = self._is_org_directory_space(meta_baseline)
        if (local or org_space) and (
            (protected is not None and protected[1] == _LEGACY_RECEIVERS_SECTION)
            or (doc_id.startswith("tree-")
                and (org_space or self._tree_holds_legacy_receivers(
                    doc_id, meta_baseline, space_owner))
                and self._tree_touches_legacy_receivers(self._doc(doc_id), update))
        ):
            return False
        if not self._is_org_directory_space(meta_baseline):
            return True
        space = _doc_roots(meta_baseline).get("space")
        space_id = space.get("spaceId") if isinstance(space, dict) else None
        if not isinstance(space_id, str) or not space_id:
            return False
        if doc_id.startswith("tree-"):
            return self._device_tree_update_allowed(
                self._doc(doc_id),
                update,
                author=author,
                space_owner=space_owner,
                space_id=space_id,
                space_name=space.get("name") if isinstance(space, dict) else None,
            )
        try:
            identity = parse_protected_directory_doc_id(doc_id)
        except ValueError:
            return False
        if identity is None:
            return True
        try:
            principal = directory_owner_principal(author)
            owner_principal = (
                directory_owner_principal(space_owner) if space_owner else None
            )
        except ValueError:
            return False
        encoded_space, section, path_principal, leaf = identity
        if encoded_space != space_id:
            return False
        if section in {"invites", "org"}:
            return principal == owner_principal
        space_name = None
        if isinstance(space, dict):
            name = space.get("name")
            space_name = name if isinstance(name, str) else None
        return protected_directory_author_allowed(
            section=section,
            path_principal=path_principal,
            author=principal,
            space_owner=space_owner,
            space_name=space_name,
        )
    def admission(self, envelope: bytes) -> AdmissionDecision:
        with self._lock:
            return self._admission_locked(envelope)


    def _admission_locked(self, envelope: bytes) -> AdmissionDecision:
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
        elif not token:
            # Only the unique meta genesis may be admitted without a causal
            # membership frontier.  Parking a non-meta update with an empty
            # token cannot ever become covered and makes every drain pass
            # decode the same unauthorizable bytes.
            return "not-a-member"
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
        # R2 removal wins (Allen 2026-10-09 ruling, replacing the 09-26
        # frontier-only rule): an envelope authored by X is admitted only if
        # X is a member at the envelope's frontier AND this replica holds no
        # removal of X that the envelope is concurrent with or later than.
        # Duplicates the remover already merged never re-enter here, so every
        # fresh envelope from a removed author is refused, never parked.
        # Re-adding X opens a new epoch: only frontiers covering the latest
        # (re-)add are inside it.
        if author != owner and self._author_removed_wins(author, token):
            return "removed-author"
        member = members.get(author, {})
        if str(value["docId"]) == "meta":
            try:
                allowed = self._meta_exception_allowed(
                    value, self._update_bytes(value), owner, members, baseline
                )
            except StoreError:
                return "not-a-member"
            return "ok" if allowed else "not-a-member"
        if not (
            author == owner
            or (member.get("mode") == "rw" and not member.get("removedAt"))
        ):
            return "not-a-member"
        try:
            device_allowed = self._org_device_update_allowed(
                doc_id,
                self._update_bytes(value),
                author=author,
                space_owner=owner,
                meta_baseline=baseline,
            )
        except StoreError:
            device_allowed = False
        if device_allowed is None:
            return "pending-dependency"
        if not device_allowed:
            return "not-a-member"
        return "ok"

    def _is_active_member_at_envelope_frontier(
        self, value: Mapping[str, Any]
    ) -> bool:
        origin = value.get("origin")
        if not isinstance(origin, Mapping):
            return False
        token_text = origin.get("metaFrontier")
        if not isinstance(token_text, str):
            return False
        try:
            snapshot = self._membership_at(_unb64(token_text))
        except StoreError:
            return False
        if snapshot is None:
            return False
        owner, members, _baseline = snapshot
        author = str(origin.get("author", ""))
        member = members.get(author)
        return author == owner or (
            isinstance(member, dict)
            and member.get("mode") == "rw"
            and not member.get("removedAt")
        )

    def _meta_update_waits_for_causal_prefix(
        self, value: Mapping[str, Any], update: bytes
    ) -> bool:
        """Detect a missing member-authored meta prefix without accepting it."""

        if (
            str(value.get("docId")) != "meta"
            or not self._is_active_member_at_envelope_frontier(value)
        ):
            return False
        origin = value.get("origin")
        if not isinstance(origin, Mapping):
            return False
        try:
            token = _unb64(str(origin["metaFrontier"]))
        except (KeyError, StoreError, TypeError):
            return False
        snapshot = self._membership_at(token)
        if snapshot is None:
            return False
        probe = Doc()
        try:
            probe.apply_update(snapshot[2].get_update())
            before = _doc_roots(probe).get("__orgfs__")
            prior_clock = before.get("c") if isinstance(before, dict) else None
            if (
                isinstance(prior_clock, bool)
                or not isinstance(prior_clock, (int, float))
                or not float(prior_clock).is_integer()
            ):
                return False
            probe.apply_update(update)
        except (TypeError, ValueError):
            return False
        after = _doc_roots(probe).get("__orgfs__")
        # Only a missing reserved clock can signal a causal prefix gap.
        # Explicit malformed values (bool, string, fractional number) are
        # semantic violations and must be rejected immediately by shape policy.
        return not isinstance(after, dict) or "c" not in after

    def _drain_keeps_not_a_member(
        self, value: Mapping[str, Any], update: bytes
    ) -> bool:
        """The drain's keep-pending rule for a ``not-a-member`` decision.

        Drain-only on purpose.  Direct import keeps only a causal-prefix gap
        pending and refuses everything else at the door, so a forged or
        malformed member meta update never holds a pending slot.  A row the
        drain re-judges was parked for another reason; there a meta update
        whose author is active at its stamped frontier stays parked, because
        the membership materialized for that frontier can still settle after
        its commit (deferred materialization, "settle mixed metadata
        frontiers").  Residual: such a row that never becomes admissible
        lingers until the pending age or capacity bound drops it.
        """

        return self._meta_update_waits_for_causal_prefix(value, update) or (
            str(value.get("docId")) == "meta"
            and self._is_active_member_at_envelope_frontier(value)
        )

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
                self.space_id, value["docId"], writer, seq, supplier,
            )
            return ImportResult("rejected", "log-key-conflict")
        doc_id = str(value["docId"])
        update = self._update_bytes(value)
        doc = self._doc(doc_id)
        working = self._import_doc(doc_id)
        before_state = working.get_state()
        try:
            working.apply_update(update)
        except (TypeError, ValueError):
            self._import_docs.pop(doc_id, None)
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
            self._import_docs.pop(doc_id, None)
            return ImportResult("pending", "pending-dependency")
        if version == before_state:
            # Yjs accepts an update whose structural predecessor is missing,
            # but keeps it only in the receiving Doc's in-memory pending set.
            # Journaling that row now and rebuilding a fresh working Doc on the
            # next import would discard the pending structs while the durable
            # duplicate key prevents a retry. Park the original bytes until a
            # predecessor advances this document instead.
            self._import_docs.pop(doc_id, None)
            return ImportResult("pending", "pending-dependency")
        origin_node = str(value["origin"]["node"])
        member_add_candidates = self._member_add_candidates(value)
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO commits(writer, seq, doc_id, envelope_bytes, version, origin_node, committed_at, broadcast_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (writer, seq, doc_id, envelope, version, origin_node, _now(), _now()),
                )
                if origin_node == self.node_id:
                    self._db.execute(
                        "UPDATE commit_stats SET local_total = local_total + 1 "
                        "WHERE singleton = 1"
                    )
                self._write_member_add_candidates(member_add_candidates)
        except sqlite3.IntegrityError:
            self._import_docs.pop(doc_id, None)
            return self._apply_import(value, envelope, supplier)
        except BaseException:
            self._import_docs.pop(doc_id, None)
            raise
        if origin_node == self.node_id:
            self._note_covered_insert(doc_id, version)
        doc.apply_update(update)
        self._sync_doc_caches(doc_id, update, import_doc=working)
        self._mark_persist((doc_id,))
        if doc_id == "meta":
            self._publish_blob_purge_projection()
            token = self._meta_frontier_for_commit()
            if token:
                self._record_meta_frontier(token)
        return ImportResult(
            "applied", record=CommitRecord(writer, seq, envelope, _b64(version))
        )



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
                    self.space_id, value["docId"], writer, seq, supplier,
                )
                return ImportResult("rejected", "log-key-conflict")
            try:
                self._update_bytes(value)
            except StoreError as exc:
                return ImportResult("rejected", exc.code, details=exc.details)
            decision = self.admission(bytes(envelope))
            if decision in {"pending-meta", "pending-dependency"}:
                if not self._pending(bytes(envelope), supplier):
                    return ImportResult(
                        "pending",
                        decision,
                        details={"dropped": True, "reason": "capacity"},
                    )
                return ImportResult("pending", decision)
            if decision != "ok":
                if decision == "not-a-member" and self._meta_update_waits_for_causal_prefix(
                    value, self._update_bytes(value)
                ):
                    if not self._pending(bytes(envelope), supplier):
                        return ImportResult(
                            "pending",
                            "pending-dependency",
                            details={"dropped": True, "reason": "capacity"},
                        )
                    return ImportResult("pending", "pending-dependency")
                return ImportResult("rejected", decision)
            result = self._apply_import(value, bytes(envelope), supplier)
            if result.status == "pending":
                if not self._pending(bytes(envelope), supplier):
                    return ImportResult(
                        "pending",
                        result.code,
                        details={"dropped": True, "reason": "capacity"},
                    )
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
