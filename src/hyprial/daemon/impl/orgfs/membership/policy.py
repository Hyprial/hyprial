# Decision record
# - 2026-11-25 (C2, design §3.2): member rows are nested Maps
#   ``user -> {"mode","addedBy","addedAt"}``.  A row may also carry the
#   reserved ``sig`` slot (§3.8).
# - 2026-11-25 (phase D P0, design §9 R8, superseding C2's "removal deletes
#   the row"): removal keeps the row and writes ``removedAt`` (a non-empty
#   string; ``mode`` unchanged), so the previous ``addedAt`` stays comparable.
#   A row with ``removedAt`` is not a member: ``read_members`` and
#   ``member_mode`` skip it, ``read_member_rows`` returns every row.
#   ``put_member`` re-adding a removed user drops ``removedAt`` and makes
#   ``addedAt`` strictly greater than the old value (the old value plus one
#   microsecond when the new one is not later); changing a current member's
#   mode keeps its ``addedAt``.  Every write replaces the whole row value, and
#   drops ``sig`` because it signed the previous value.
# - ``addedAt`` is the owner node's microsecond ISO-8601 UTC timestamp
#   (``...Z``); it is compared as a datetime.  An old value that does not
#   parse cannot be ordered: the new value is used unless it is equal, which
#   raises ``ValueError``.
# - Protected-path rules are not restated here.  A path's section and path
#   principal come from identity's ``protected_section_for_path`` and the
#   decoded ``protected_directory_node_id``; authorization is identity's
#   ``protected_directory_author_allowed``.  A path without a named section
#   (``directory``, ``directory/devices``) falls back to ``devices`` with the
#   owner as principal, as ``storage/store/admission.py`` does today.
# - 2026-11-25 (F2): ``identity_error`` holds the deterministic-id rule of the
#   design §9 admission supplement; it uses identity's id builders and never
#   restates their encoding.
# - Protection is on only in org directory spaces (``org_from_space_name``),
#   matching the current facade and admission.  A protected id that does not
#   decode, or that names another space, yields a rule nobody may satisfy.
# - The permanent-remove ownership refusal has no code in the design (§6);
#   ``not-owner`` is used and reported.
"""Member table helpers, protected-path policy and the local write check."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from pycrdt import Doc, Map

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.membership.contract import AdmissionView
from hyprial.daemon.impl.orgfs.membership.staged import DocKind
from hyprial.identity import (
    org_from_space_name,
    parse_protected_directory_doc_id,
    parse_protected_directory_node_id,
    protected_directory_doc_id,
    protected_directory_author_allowed,
    protected_directory_node_id,
    protected_section_for_path,
)

MEMBER_MODES = frozenset({"ro", "rw"})
_MEMBER_KEYS = frozenset({"mode", "addedBy", "addedAt"})
_MEMBER_OPTIONAL_KEYS = frozenset({"sig", "removedAt"})
SPACE_ROOT = "space"
MEMBERS_ROOT = "members"


@dataclass(frozen=True, slots=True)
class MemberRow:
    """One parsed member row."""

    mode: str
    added_by: str
    added_at: str
    removed_at: str | None = None

    @property
    def is_member(self) -> bool:
        """Whether the row names a current member (no ``removedAt``)."""

        return self.removed_at is None


def parse_member_row(value: object) -> MemberRow | None:
    """Parse a member row; ``None`` when its format is not valid."""

    if not isinstance(value, Mapping):
        return None
    keys = set(value)
    if not _MEMBER_KEYS <= keys or keys - _MEMBER_KEYS - _MEMBER_OPTIONAL_KEYS:
        return None
    mode = value.get("mode")
    added_by = value.get("addedBy")
    added_at = value.get("addedAt")
    if mode not in MEMBER_MODES:
        return None
    if not isinstance(added_by, str) or not added_by or not isinstance(added_at, str):
        return None
    removed_at = value.get("removedAt")
    if "removedAt" in keys and (not isinstance(removed_at, str) or not removed_at):
        return None
    return MemberRow(str(mode), added_by, added_at, removed_at)


def read_space(doc: Doc) -> dict[str, Any]:
    """The meta document's ``space`` root."""

    return dict(doc.get(SPACE_ROOT, type=Map).to_py() or {})


def read_member_rows(doc: Doc) -> dict[str, MemberRow]:
    """Every well-formed member row of the meta document, removed rows included."""

    rows: dict[str, MemberRow] = {}
    for user, value in (doc.get(MEMBERS_ROOT, type=Map).to_py() or {}).items():
        row = parse_member_row(value)
        if row is not None:
            rows[str(user)] = row
    return rows


def read_members(doc: Doc) -> dict[str, MemberRow]:
    """The current members: well-formed rows without ``removedAt``."""

    return {user: row for user, row in read_member_rows(doc).items() if row.is_member}


def member_mode(doc: Doc, user: str) -> str | None:
    """The mode of ``user`` when a current member, else ``None``."""

    row = parse_member_row((doc.get(MEMBERS_ROOT, type=Map).to_py() or {}).get(user))
    return row.mode if row is not None and row.is_member else None


def write_space(
    doc: Doc,
    *,
    space_id: str,
    name: str,
    owner: str,
    created_at: str,
    kind: str | None = None,
) -> None:
    """Write the ``space`` root at space creation (owner only, §3.4)."""

    values: dict[str, Any] = {
        "spaceId": space_id,
        "name": name,
        "owner": owner,
        "createdAt": created_at,
    }
    if kind is not None:
        values["kind"] = kind
    with doc.transaction():
        doc.get(SPACE_ROOT, type=Map).update(values)


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def readded_at(previous: str, proposed: str) -> str:
    """An ``addedAt`` strictly later than ``previous``: ``proposed`` or ``previous`` + 1 µs."""

    old = _parse_timestamp(previous)
    if old is None:
        if proposed == previous:
            raise ValueError(f"cannot order addedAt after {previous!r}")
        return proposed
    new = _parse_timestamp(proposed)
    if new is not None and new > old:
        return proposed
    return _format_timestamp(old + timedelta(microseconds=1))


def _write_row(doc: Doc, user: str, row: MemberRow) -> None:
    value: dict[str, Any] = {"mode": row.mode, "addedBy": row.added_by, "addedAt": row.added_at}
    if row.removed_at is not None:
        value["removedAt"] = row.removed_at
    with doc.transaction():
        doc.get(MEMBERS_ROOT, type=Map)[user] = Map(value)


def put_member(
    doc: Doc, user: str, *, mode: str, added_by: str, added_at: str
) -> MemberRow:
    """Add a member, re-add a removed one, or change a current member's mode.

    Returns the row written.  A current member keeps its ``addedAt``; a
    removed one loses ``removedAt`` and gets an ``addedAt`` strictly later
    than its previous one.
    """

    if mode not in MEMBER_MODES:
        raise ValueError(f"unknown member mode: {mode}")
    if not user or not added_by or not added_at:
        raise ValueError("member, addedBy and addedAt are required")
    current = read_member_rows(doc).get(user)
    if current is None:
        when = added_at
    elif current.is_member:
        when = current.added_at
    else:
        when = readded_at(current.added_at, added_at)
    row = MemberRow(mode, added_by, when)
    _write_row(doc, user, row)
    return row


def mark_member_removed(doc: Doc, user: str, *, removed_at: str) -> MemberRow:
    """Mark a current member removed; the row stays with ``removedAt``.

    Raises ``KeyError`` when ``user`` is not a current member.
    """

    if not removed_at:
        raise ValueError("removedAt is required")
    current = read_members(doc).get(user)
    if current is None:
        raise KeyError(user)
    row = MemberRow(current.mode, current.added_by, current.added_at, removed_at)
    _write_row(doc, user, row)
    return row


@dataclass(frozen=True, slots=True)
class ProtectedRule:
    """The protected section and path principal governing one path or id."""

    section: str
    principal: str
    space_id: str
    where: str
    valid: bool = True


class ProtectionPolicy:
    """Protected-directory rules of one space, delegated to identity."""

    def __init__(self, *, space_id: str, space_name: str | None, owner: str) -> None:
        self.space_id = space_id
        self.space_name = space_name
        self.owner = owner
        self.enabled = (
            isinstance(space_name, str) and org_from_space_name(space_name) is not None
        )

    @classmethod
    def from_view(cls, view: AdmissionView, *, owner: str | None = None) -> "ProtectionPolicy":
        """The policy of the space described by ``view``."""

        return cls(
            space_id=view.space_id,
            space_name=view.space_name,
            owner=view.owner if owner is None else owner,
        )

    def _invalid(self, where: str) -> ProtectedRule:
        return ProtectedRule("", "", self.space_id, where, valid=False)

    def rule_for_path(self, path: str | None) -> ProtectedRule | None:
        """The rule for a slash path, or ``None`` when it is not protected."""

        if not self.enabled or not path:
            return None
        try:
            node_id = protected_directory_node_id(self.space_id, path, space_owner=self.owner)
            if node_id is None:
                return None
            identity = parse_protected_directory_node_id(node_id)
        except ValueError:
            return self._invalid(path)
        if identity is None:
            return None
        section = protected_section_for_path(path, space_owner=self.owner) or "devices"
        return ProtectedRule(section, identity[1], identity[0], path)

    def rule_for_node(self, node_id: str) -> ProtectedRule | None:
        """The rule encoded in a protected tree-node id."""

        if not self.enabled:
            return None
        try:
            identity = parse_protected_directory_node_id(node_id)
        except ValueError:
            return self._invalid(node_id)
        if identity is None:
            return None
        section = protected_section_for_path(identity[2], space_owner=self.owner) or "devices"
        return ProtectedRule(section, identity[1], identity[0], identity[2])

    def rule_for_doc(self, doc_id: str) -> ProtectedRule | None:
        """The rule encoded in a protected content-document id."""

        if not self.enabled:
            return None
        try:
            identity = parse_protected_directory_doc_id(doc_id)
        except ValueError:
            return self._invalid(doc_id)
        if identity is None:
            return None
        space_id, section, principal, _leaf = identity
        return ProtectedRule(section, principal, space_id, doc_id)

    def identity_error(self, node_id: str, path: str | None, row: Mapping[str, Any]) -> str | None:
        """Why a placed row breaks the deterministic-id rules, or ``None``.

        ``path`` is the row's rooted path (``None`` when it does not reach
        the root).  A row at a protected path carries that path's node id; a
        node id or a linked doc id that is protected names exactly the path
        the row sits at.
        """

        if not self.enabled:
            return None
        try:
            node_protected = parse_protected_directory_node_id(node_id) is not None
            linked = row.get("doc_id") if row.get("kind") == "doc" else None
            doc_protected = (
                isinstance(linked, str) and parse_protected_directory_doc_id(linked) is not None
            )
            if path is None or not path:
                return "unplaced-protected-id" if node_protected or doc_protected else None
            expected_node = protected_directory_node_id(self.space_id, path, space_owner=self.owner)
            if (node_protected or expected_node is not None) and node_id != expected_node:
                return "non-deterministic-node-id"
            if row.get("kind") != "doc":
                return None
            expected_doc = protected_directory_doc_id(self.space_id, path, space_owner=self.owner)
        except ValueError:
            return "invalid-protected-id"
        if (doc_protected or expected_doc is not None) and linked != expected_doc:
            return "non-deterministic-doc-id"
        return None

    def allows(self, rule: ProtectedRule, author: str | None) -> bool:
        """Whether ``author`` may write under ``rule``."""

        if not rule.valid or rule.space_id != self.space_id or not author:
            return False
        try:
            return protected_directory_author_allowed(
                section=rule.section,
                path_principal=rule.principal,
                author=author,
                space_owner=self.owner,
                space_name=self.space_name,
            )
        except ValueError:
            return False


@dataclass(frozen=True, slots=True)
class LocalWrite:
    """What one local write touches, as the facade resolved it.

    ``paths`` lists every path involved (before and after a move);
    ``node_ids`` the tree rows written; ``doc_id`` the content document;
    ``permanent`` marks ``remove(permanent=True)`` whose target row was last
    written by ``node_author``.
    """

    kind: DocKind
    paths: tuple[str, ...] = ()
    node_ids: tuple[str, ...] = ()
    doc_id: str | None = None
    permanent: bool = False
    node_author: str | None = None


def _rules(write: LocalWrite, policy: ProtectionPolicy) -> list[ProtectedRule]:
    rules = [policy.rule_for_path(path) for path in write.paths]
    rules.extend(policy.rule_for_node(node_id) for node_id in write.node_ids)
    if write.doc_id is not None:
        rules.append(policy.rule_for_doc(write.doc_id))
    return [rule for rule in rules if rule is not None]


def local_write_error(
    caller: str, write: LocalWrite, view: AdmissionView, policy: ProtectionPolicy
) -> OrgFsError | None:
    """Judge a local write before any update is produced (design §3.4)."""

    if write.kind == "meta":
        if caller != view.owner:
            return OrgFsError("not-owner", {"user": caller})
        return None
    if view.member_mode(caller) != "rw":
        return OrgFsError("not-a-member", {"user": caller})
    for rule in _rules(write, policy):
        if not policy.allows(rule, caller):
            return OrgFsError(
                "protected-path",
                {"message": f"protected path: {rule.where}", "path": rule.where},
            )
    if write.permanent and caller != view.owner and caller != write.node_author:
        return OrgFsError("not-owner", {"user": caller})
    return None


def check_local_write(
    caller: str, write: LocalWrite, view: AdmissionView, policy: ProtectionPolicy
) -> None:
    """Raise the ``OrgFsError`` of :func:`local_write_error`, if any."""

    error = local_write_error(caller, write, view, policy)
    if error is not None:
        raise error
