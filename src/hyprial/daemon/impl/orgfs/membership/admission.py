# Decision record
# - 2026-11-25 (C2, design §3.6): admission is "baseline plus delta": the
#   staged copy equals the authoritative instance, the update is applied to
#   it, and the before / after materialized values are compared.  An empty
#   probe cannot see replaced or deleted Map keys (review must-change 1).
# - Missing predecessor: decided only from the frame's ``expected_vector``
#   (decoded).  pycrdt 0.14.9 ``get_state(update)`` reports 0 for an update
#   that does not start at clock 0 and ``merge_updates`` stops at the same
#   gap, so neither detects pending structs independently.
# - Tree: a row counts as changed when its stored value, deleted flag or
#   materialized path changed (moves of an ancestor change descendants'
#   paths).  A protected change with no row rewrite carries no author
#   evidence and is rejected; so is a deleted protected key.
# - Content (§3.6, revised after C2): when the text or the author set
#   changes, the decoded state vector must strictly advance.  The value of
#   ``__orgfs__.c`` is never compared: concurrent honest writes leave it at
#   the same value, or even a smaller one.
# - Meta: ``owner`` is ``view.owner`` (genesis included), falling back to the
#   staged ``space.owner`` when the view has none.  Any change to the
#   ``space`` root needs ``modifiedBy == owner`` afterwards; owner, kind,
#   spaceId and createdAt keep existing values.
# - Member rows (phase D P0, design §9 R8): removal marks the row with
#   ``removedAt`` and never deletes its key.  Marking removed and re-adding
#   are row changes like any other (``addedBy == owner``); the owner's row may
#   not carry ``removedAt``; deleting any member key is ``not-owner``.  The
#   strictly later ``addedAt`` on re-add is the helper's guarantee and is not
#   judged here.
# - Space id and name come from the view (§3.6 ``AdmissionView``).
# - ``require_verified`` is the §3.8 switch for after U2; off by default.
# - 2026-11-25 (F2, design §9 "准入规则的补充", T1 report D-1/D-2): the root
#   row, once it exists, stays a directory that is never deleted (soft or key
#   removal), renamed or moved (raw row or move replay); any other change
#   needs ``author == owner``.  Every space, not only org directory spaces.
#   A tree state without a root row (a joiner's first tree) is not judged by
#   this rule.
# - In org directory spaces a changed row sitting at a protected path must use
#   that path's deterministic node id, and a doc row that path's deterministic
#   doc id; a protected node id or linked doc id must name exactly the rooted
#   path the row sits at (``ProtectionPolicy.identity_error``).  The path is
#   the rooted one: a row whose parents do not reach the root has none, and a
#   protected id there is refused.  Checked before the author rules, so
#   protected roots cannot be renamed or moved by anyone.
# - Rejection details carry the row's path (tree) or the document's path
#   (content), falling back to the rule's id only when no path is known.
"""Result-state admission of remote updates."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Mapping

from hyprial.daemon.impl.orgfs.membership.authorship import row_author
from hyprial.daemon.impl.orgfs.membership.contract import (
    Accepted,
    AdmissionResult,
    AdmissionView,
    Pending,
    RecordVerifier,
    Rejected,
    SafetyRecord,
    UnavailableRecordVerifier,
)
from hyprial.daemon.impl.orgfs.membership.policy import (
    ProtectedRule,
    ProtectionPolicy,
    parse_member_row,
)
from hyprial.daemon.impl.orgfs.membership.staged import (
    ContentValue,
    MetaValue,
    StagedDoc,
    TreeValue,
    decode_vector,
    doc_kind,
    vector_advanced,
    vector_covers,
)

ORGFS_TEXT_MAX = 4 * 1024 * 1024
IMMUTABLE_SPACE_KEYS = ("owner", "kind", "spaceId", "createdAt")
SPACE_MODIFIED_BY = "modifiedBy"
ROOT_NODE = "root"
_MISSING = object()


def _rooted_path(value: TreeValue, node_id: str) -> str | None:
    cursor: object = node_id
    seen: set[str] = set()
    while isinstance(cursor, str) and cursor != ROOT_NODE:
        if cursor in seen or cursor not in value.placed:
            return None
        seen.add(cursor)
        cursor = value.placed[cursor].get("parent")
    if cursor != ROOT_NODE:
        return None
    return value.paths.get(node_id)


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ResultStateAdmission:
    """The C2 ``Admission``: judge one remote update by its resulting state."""

    def __init__(
        self,
        *,
        verifier: RecordVerifier | None = None,
        require_verified: bool = False,
    ) -> None:
        self.verifier: RecordVerifier = verifier or UnavailableRecordVerifier()
        self.require_verified = require_verified

    def admit(
        self,
        doc_id: str,
        update: bytes,
        expected_vector: bytes,
        staged: StagedDoc,
        view: AdmissionView,
    ) -> AdmissionResult:
        """Judge ``update`` on ``staged``; the caller rebuilds ``staged`` unless accepted."""

        kind = doc_kind(doc_id)
        if staged.kind != kind:
            raise ValueError(f"staged copy is {staged.kind}, document {doc_id} is {kind}")
        try:
            decode_vector(expected_vector)
        except ValueError:
            return Rejected("invalid-argument", {"docId": doc_id, "reason": "invalid-vector"})
        if kind == "content" and view.node_for_doc(doc_id) is None:
            return Pending("unknown-doc")
        before = staged.materialize()
        before_vector = staged.vector()
        try:
            staged.apply(update)
        except ValueError:
            return Rejected("invalid-argument", {"docId": doc_id, "reason": "undecodable"})
        if not vector_covers(staged.vector(), expected_vector):
            return Pending("dependency")
        after = staged.materialize()
        advanced = vector_advanced(before_vector, staged.vector())
        policy = ProtectionPolicy.from_view(view, owner=self._owner(view, before))
        if isinstance(before, MetaValue) and isinstance(after, MetaValue):
            verdict = self._meta(doc_id, before, after, view)
        elif isinstance(before, TreeValue) and isinstance(after, TreeValue):
            verdict = self._tree(doc_id, before, after, policy)
        else:
            assert isinstance(before, ContentValue) and isinstance(after, ContentValue)
            verdict = self._content(doc_id, before, after, view, policy, advanced)
        return verdict or Accepted()

    @staticmethod
    def _owner(view: AdmissionView, before: object) -> str:
        if view.owner:
            return view.owner
        if isinstance(before, MetaValue):
            return str(before.space.get("owner") or "")
        return ""

    def _verify(
        self,
        space_id: str,
        kind: Literal["member", "document"],
        key: str,
        payload: Mapping[str, Any],
        digest: str,
    ) -> str | None:
        record = SafetyRecord(space_id, kind, key, payload, digest)
        verdict = self.verifier.verify(record)
        if self.require_verified and verdict != "verified":
            return verdict
        return None

    def _meta(
        self, doc_id: str, before: MetaValue, after: MetaValue, view: AdmissionView
    ) -> Rejected | None:
        for key in IMMUTABLE_SPACE_KEYS:
            old = before.space.get(key)
            if old is not None and after.space.get(key) != old:
                return Rejected("not-owner", {"docId": doc_id, "field": f"space.{key}"})
        owner = view.owner or str(after.space.get("owner") or "")
        claimed = after.space.get("owner")
        if view.owner and claimed is not None and claimed != view.owner:
            return Rejected("not-owner", {"docId": doc_id, "field": "space.owner"})
        if before.space != after.space and (
            not owner or after.space.get(SPACE_MODIFIED_BY) != owner
        ):
            return Rejected("not-owner", {"docId": doc_id, "field": f"space.{SPACE_MODIFIED_BY}"})
        for user in sorted(set(before.members) - set(after.members)):
            return Rejected("not-owner", {"docId": doc_id, "user": user, "reason": "member-row-deleted"})
        for user in sorted(after.members):
            value = after.members[user]
            if before.members.get(user, _MISSING) == value:
                continue
            row = parse_member_row(value)
            if row is None or not owner or row.added_by != owner:
                return Rejected("not-owner", {"docId": doc_id, "user": user})
            if user == owner and not row.is_member:
                return Rejected("not-owner", {"docId": doc_id, "user": user, "reason": "owner-row-removed"})
            payload = dict(value) if isinstance(value, Mapping) else {}
            refused = self._verify(view.space_id, "member", user, payload, _canonical_sha256(payload))
            if refused is not None:
                return Rejected("not-owner", {"docId": doc_id, "user": user, "verdict": refused})
        return None

    @staticmethod
    def _tree_rules(
        node_id: str,
        paths: tuple[str | None, ...],
        rows: tuple[Mapping[str, Any] | None, ...],
        policy: ProtectionPolicy,
    ) -> list[ProtectedRule]:
        rules = [policy.rule_for_path(path) for path in paths]
        rules.append(policy.rule_for_node(node_id))
        for row in rows:
            linked = row.get("doc_id") if row is not None else None
            if isinstance(linked, str):
                rules.append(policy.rule_for_doc(linked))
        return [rule for rule in rules if rule is not None]

    def _tree(
        self, doc_id: str, before: TreeValue, after: TreeValue, policy: ProtectionPolicy
    ) -> Rejected | None:
        node_ids = set(before.raw) | set(after.raw) | set(before.paths) | set(after.paths)
        for node_id in sorted(node_ids):
            old_raw = before.raw.get(node_id, _MISSING)
            new_raw = after.raw.get(node_id, _MISSING)
            raw_changed = old_raw != new_raw
            old_row = before.placed.get(node_id)
            new_row = after.placed.get(node_id)
            old_path = before.paths.get(node_id)
            new_path = after.paths.get(node_id)
            placed_changed = (
                old_path != new_path
                or (old_row or {}).get("deleted") != (new_row or {}).get("deleted")
                or (node_id == ROOT_NODE and old_row != new_row)
            )
            if not raw_changed and not placed_changed:
                continue
            detail = {"docId": doc_id, "nodeId": node_id}
            row = after.rows.get(node_id)
            if raw_changed and new_raw is not _MISSING:
                if row is None or row.get("node_id") != node_id:
                    return Rejected("invalid-argument", {**detail, "reason": "row-format"})
                if row_author(row) is None:
                    return Rejected("invalid-argument", {**detail, "reason": "row-author"})
            if node_id == ROOT_NODE:
                refused = self._root(before, after, policy)
                if refused is not None:
                    return Rejected("protected-path", {**detail, "path": "", "reason": refused})
                continue
            rooted = _rooted_path(after, node_id)
            where = rooted if rooted is not None else (new_path if new_path is not None else old_path)
            if new_raw is not _MISSING and new_row is not None:
                refused = policy.identity_error(node_id, rooted, new_row)
                if refused is not None:
                    return Rejected(
                        "protected-path", {**detail, "path": where, "reason": refused}
                    )
            rules = self._tree_rules(
                node_id, (old_path, new_path), (before.rows.get(node_id), row), policy
            )
            if not rules:
                continue
            if new_raw is _MISSING:
                return Rejected("protected-path", {**detail, "reason": "protected-key-deleted"})
            if not raw_changed:
                return Rejected("protected-path", {**detail, "reason": "unauthored-move"})
            author = row_author(row or {})
            for rule in rules:
                if not policy.allows(rule, author):
                    path = where if where is not None else rule.where
                    return Rejected("protected-path", {**detail, "path": path, "author": author})
        return None

    @staticmethod
    def _root(before: TreeValue, after: TreeValue, policy: ProtectionPolicy) -> str | None:
        if ROOT_NODE not in before.raw:
            return None
        if ROOT_NODE not in after.raw:
            return "root-deleted"
        old_row = before.rows.get(ROOT_NODE) or {}
        row = after.rows.get(ROOT_NODE) or {}
        placed_before = before.placed.get(ROOT_NODE) or {}
        placed = after.placed.get(ROOT_NODE) or {}
        if row.get("kind") != "dir" or bool(row.get("deleted")) or bool(placed.get("deleted")):
            return "root-kind-or-deleted"
        for key in ("name", "parent"):
            if row.get(key) != old_row.get(key) or placed.get(key) != placed_before.get(key):
                return "root-moved"
        if before.raw.get(ROOT_NODE) != after.raw.get(ROOT_NODE) and (
            not policy.owner or row_author(row) != policy.owner
        ):
            return "root-author"
        return None

    def _content(
        self,
        doc_id: str,
        before: ContentValue,
        after: ContentValue,
        view: AdmissionView,
        policy: ProtectionPolicy,
        advanced: bool,
    ) -> Rejected | None:
        detail: dict[str, object] = {"docId": doc_id}
        if not after.authors_valid:
            return Rejected("invalid-argument", {**detail, "reason": "authors-format"})
        if not before.authors <= after.authors:
            return Rejected("protected-path", {**detail, "reason": "authors-removed"})
        node = view.node_for_doc(doc_id) or {}
        node_id = node.get("node_id")
        path = view.path_of(node_id) if isinstance(node_id, str) else None
        rules = [rule for rule in (policy.rule_for_doc(doc_id), policy.rule_for_path(path)) if rule]
        added = sorted(after.authors - before.authors)
        for rule in rules:
            for author in added:
                if not policy.allows(rule, author):
                    return Rejected(
                        "protected-path",
                        {**detail, "path": path or rule.where, "author": author},
                    )
        changed = before.text != after.text or before.authors != after.authors
        if changed and not advanced:
            return Rejected("invalid-argument", {**detail, "reason": "vector-not-advanced"})
        if len(after.text.encode("utf-8")) > ORGFS_TEXT_MAX:
            return Rejected("too-large", {**detail, "maxBytes": ORGFS_TEXT_MAX})
        if rules and changed:
            try:
                parsed = json.loads(after.text)
            except json.JSONDecodeError:
                parsed = None
            payload = parsed if isinstance(parsed, dict) else {}
            digest = hashlib.sha256(after.text.encode("utf-8")).hexdigest()
            refused = self._verify(view.space_id, "document", doc_id, payload, digest)
            if refused is not None:
                return Rejected("protected-path", {**detail, "verdict": refused})
        return None
