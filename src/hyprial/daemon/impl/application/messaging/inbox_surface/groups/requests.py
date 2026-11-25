"""The group conversation request handlers (slice GC2).

Decision notes (group chat design draft §3.1-§3.3, owner decisions §10,
brief GC2):

- The document is the group log in the owner's chat space, through
  ``OwnerHomeGroupPlacement`` over the conversation placement the recorder
  composition leaves on the application, keyed by the same authority the
  conversation reads use; the writer is this daemon's node id.  No
  placement, or a document that cannot be read or written now, is
  ``DAEMON_UNAVAILABLE``, never ``CONVERSATION_NOT_FOUND``.
- A group exists when its document folds to a ``created`` entry; anything
  else under a ``g.`` id is ``CONVERSATION_NOT_FOUND``.
- Every answer is the fold of the document read back after the write, not
  of the entries this call built.
- Ids: ``create`` mints the group id from the owner and the
  ``idempotencyKey``, so a replay names the same group and answers it as it
  is now without writing.  An add or remove derives its entry id from the
  group, the method, the caller and the key; when the document already
  holds that entry the call is a replay and answers the current members
  with that ``entryId``, even after later changes (a replayed add never
  re-adds a member removed since).  A replay is recognised before the
  membership check, so an agent that left can replay its leave.
- Ordering: the fold orders entries by ``(createdAtMs, entryId)``, so each
  entry a call writes is stamped one millisecond after the newest entry it
  read or wrote before it.  The members of one ``create`` therefore join in
  the order given, after the ``created`` entry, even at a frozen clock.
- Who may: ``create`` and managing other members need the owner (a
  manager); an agent may remove itself (leave); a manager, the owner, never
  leaves and is never removed (``INVALID_ARGUMENT``); a caller that is not a
  current member is ``CONVERSATION_NOT_MEMBER``.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from hyprial.daemon.impl.composition.conversations.placement import OwnerHomeGroupPlacement
from hyprial.daemon.impl.conversations import RecordUnavailable
from hyprial.daemon.impl.conversations.groups import (
    CLASS_SYSTEM,
    EVENT_CREATED,
    EVENT_MEMBER_ADDED,
    EVENT_MEMBER_LEFT,
    EVENT_MEMBER_REMOVED,
    GroupEntry,
    GroupLog,
    GroupMember,
    GroupState,
    SystemEvent,
    fold_group,
)
from hyprial.kernel import DaemonRequestError, ipc_errors, mint_group_id

from ..history import _conversation_authority
from .members import Candidate, caller_member_id, owner_candidate, owner_member_id, resolve_member

_ENTRY_NAMESPACE = uuid.UUID("6b1f3c58-2f0e-5d6a-9c41-3e8d7a0b5c21")


def group_log(app: Any) -> GroupLog:
    """The group log of this daemon's owner; ``DAEMON_UNAVAILABLE`` without a placement."""

    homes = getattr(app, "_conversation_placement", None)
    if homes is None:
        raise _unavailable()
    placement = OwnerHomeGroupPlacement(homes, _conversation_authority(app))
    return GroupLog(placement=placement, writer=_writer(app))


def _writer(app: Any) -> str:
    return str(getattr(app, "node_id", "") or "local")


def _unavailable() -> DaemonRequestError:
    return ipc_errors.DaemonUnavailableError(
        "the group document cannot be read or written now; retry the same call"
    )


def _read(log: GroupLog, group_id: str) -> tuple[GroupEntry, ...]:
    try:
        return log.read(group_id)
    except RecordUnavailable:
        raise _unavailable() from None


def _write(log: GroupLog, group_id: str, entries: list[GroupEntry]) -> tuple[GroupEntry, ...]:
    try:
        log.append(group_id, entries)
        return log.read(group_id)
    except RecordUnavailable:
        raise _unavailable() from None


def _existing(log: GroupLog, group_id: str) -> tuple[tuple[GroupEntry, ...], GroupState]:
    entries = _read(log, group_id)
    state = fold_group(entries)
    if state.created_by is None:
        raise DaemonRequestError(
            ipc_errors.CONVERSATION_NOT_FOUND, "no such group conversation",
            {"conversationId": group_id},
        )
    return entries, state


def _not_member(group_id: str) -> DaemonRequestError:
    return DaemonRequestError(
        ipc_errors.CONVERSATION_NOT_MEMBER, "the caller is not a member of this group",
        {"conversationId": group_id},
    )


def _not_authorized() -> DaemonRequestError:
    return DaemonRequestError(
        ipc_errors.CALLER_NOT_AUTHORIZED,
        "only the owner creates groups and manages their members",
    )


def _invalid(message: str) -> DaemonRequestError:
    return DaemonRequestError(ipc_errors.INVALID_ARGUMENT, message)


def _entry_id(*parts: str) -> str:
    return str(uuid.uuid5(_ENTRY_NAMESPACE, "\0".join(parts)))


def _stamp(entries: tuple[GroupEntry, ...]) -> int:
    now = time.time_ns() // 1_000_000
    return max(now, max((entry.created_at_ms + 1 for entry in entries), default=now))


def _member(candidate: Candidate, *, at: int, added_by: str, manager: bool = False) -> GroupMember:
    return GroupMember(
        member_id=candidate.member_id,
        kind=candidate.kind,
        display_name=candidate.display_name,
        manager=manager,
        joined_at_ms=at,
        added_by=added_by,
        address=candidate.address,
    )


def _system(writer: str, group_id: str, entry_id: str, author: str, at: int,
            event: SystemEvent) -> GroupEntry:
    return GroupEntry(
        entry_id=entry_id,
        conversation_id=group_id,
        message_class=CLASS_SYSTEM,
        sender=author,
        created_at_ms=at,
        recorded_at_ms=at,
        writer=writer,
        system=event,
    )


def _group_json(group_id: str, state: GroupState) -> dict[str, Any]:
    return {
        "ok": True,
        "conversationId": group_id,
        "kind": "group",
        "title": state.title,
        "createdBy": state.created_by,
        "createdAtMs": state.created_at_ms,
        "members": [member.to_json() for member in state.members],
    }


def _members_json(group_id: str, state: GroupState, entry_id: str | None) -> dict[str, Any]:
    return {
        "ok": True,
        "conversationId": group_id,
        "members": [member.to_json() for member in state.members],
        "entryId": entry_id,
    }


def create_group(app: Any, params: dict[str, Any], *, trusted_origin: str | None) -> dict[str, Any]:
    """``conversations.create``: the owner's new group, or the one its key made before."""

    caller = caller_member_id(app, "conversations.create", params, trusted_origin)
    owner = owner_member_id(app)
    if caller != owner:
        raise _not_authorized()
    title = str(params["title"]).strip()
    if not title:
        raise _invalid("a group needs a non-empty title")
    log = group_log(app)
    group_id = mint_group_id(f"{owner}\0{params['idempotencyKey']}".encode())
    entries = _read(log, group_id)
    state = fold_group(entries)
    if state.created_by is not None:
        return _group_json(group_id, state)
    candidates = [owner_candidate(app)]
    for reference in params["members"]:
        candidate = resolve_member(app, str(reference))
        if any(known.member_id == candidate.member_id for known in candidates):
            raise _invalid(f"{reference!r} names a member twice (the owner is always a member)")
        candidates.append(candidate)
    at = _stamp(entries)
    written = [
        _system(_writer(app), group_id, _entry_id(group_id, "create", "0"), owner, at,
                SystemEvent(EVENT_CREATED, owner,
                            _member(candidates[0], at=at, added_by=owner, manager=True), title)),
    ]
    for index, candidate in enumerate(candidates[1:], start=1):
        joined = at + index
        written.append(_system(
            _writer(app), group_id, _entry_id(group_id, "create", str(index)), owner, joined,
            SystemEvent(EVENT_MEMBER_ADDED, candidate.member_id,
                        _member(candidate, at=joined, added_by=owner)),
        ))
    return _group_json(group_id, fold_group(_write(log, group_id, written)))


def get_group(app: Any, params: dict[str, Any], *, trusted_origin: str | None) -> dict[str, Any]:
    """``conversations.get``: the group as a current member sees it."""

    caller = caller_member_id(app, "conversations.get", params, trusted_origin)
    group_id = str(params["conversationId"])
    _entries, state = _existing(group_log(app), group_id)
    member = None if caller is None else state.member(caller)
    if member is None:
        raise _not_member(group_id)
    return {**_group_json(group_id, state), "self": member.member_id,
            "canManageMembers": member.manager}


def add_member(app: Any, params: dict[str, Any], *, trusted_origin: str | None) -> dict[str, Any]:
    """``conversations.members.add``: the owner adds an agent; a current member is a no-op."""

    caller = caller_member_id(app, "conversations.members.add", params, trusted_origin)
    group_id = str(params["conversationId"])
    log = group_log(app)
    entries, state = _existing(log, group_id)
    entry_id = _entry_id(group_id, "add", str(caller), str(params["idempotencyKey"]))
    if any(entry.entry_id == entry_id for entry in entries):
        return _members_json(group_id, state, entry_id)
    member = None if caller is None else state.member(caller)
    if member is None:
        raise _not_member(group_id)
    if not member.manager:
        raise _not_authorized()
    candidate = resolve_member(app, str(params["member"]))
    if state.member(candidate.member_id) is not None:
        return _members_json(group_id, state, None)
    at = _stamp(entries)
    added = _system(_writer(app), group_id, entry_id, member.member_id, at,
                    SystemEvent(EVENT_MEMBER_ADDED, candidate.member_id,
                                _member(candidate, at=at, added_by=member.member_id)))
    return _members_json(group_id, fold_group(_write(log, group_id, [added])), entry_id)


def remove_member(app: Any, params: dict[str, Any], *, trusted_origin: str | None) -> dict[str, Any]:
    """``conversations.members.remove``: the owner removes a member, or an agent leaves."""

    caller = caller_member_id(app, "conversations.members.remove", params, trusted_origin)
    group_id = str(params["conversationId"])
    target = str(params["memberId"])
    log = group_log(app)
    entries, state = _existing(log, group_id)
    entry_id = _entry_id(group_id, "remove", str(caller), str(params["idempotencyKey"]))
    if any(entry.entry_id == entry_id for entry in entries):
        return _members_json(group_id, state, entry_id)
    member = None if caller is None else state.member(caller)
    if member is None:
        raise _not_member(group_id)
    if target == member.member_id:
        if member.manager:
            raise _invalid("the owner cannot leave a group")
        event = SystemEvent(EVENT_MEMBER_LEFT, target)
    else:
        if not member.manager:
            raise _not_authorized()
        removed = state.member(target)
        if removed is None:
            return _members_json(group_id, state, None)
        if removed.manager:
            raise _invalid("a manager is never removed from a group")
        event = SystemEvent(EVENT_MEMBER_REMOVED, target)
    entry = _system(_writer(app), group_id, entry_id, member.member_id, _stamp(entries), event)
    return _members_json(group_id, fold_group(_write(log, group_id, [entry])), entry_id)


__all__ = ["add_member", "create_group", "get_group", "group_log", "remove_member"]
