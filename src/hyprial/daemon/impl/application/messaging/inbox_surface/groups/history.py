"""``conversations.history`` for a group: one page of its document (slice GC3).

Decision notes (group chat design draft §3.5; owner decisions §10; brief
GC3):

- The rows are the group document's entries, every writer and every home,
  in ``(createdAtMs, entryId)`` order; there is no queue merge.  The page
  engine is direct history's ``history_answer`` (modes, limits, cursors
  bound to the reader and the ``g.`` id), with the group entry projection.
- Access is membership: the reader (the conversation reads' caller, so
  with ``onBehalfOf`` the agent acted for) must be a current member; the
  owner always is.  A member reads the whole document, including what was
  written before it joined; a removed member reads nothing.  Unknown group
  ``CONVERSATION_NOT_FOUND``, other reader ``CONVERSATION_NOT_MEMBER``, a
  document that cannot be read ``DAEMON_UNAVAILABLE``: there is no other
  source to fall back to, so ``recordAvailable`` is always true.
- An entry: ``from``, ``mentions`` and ``replyTo.from`` are member ids (for
  an agent this differs from 1:1 history, where ``from`` is an address);
  ``to`` is the group id, not an address; ``origin`` is empty.  A system
  entry (membership change) keeps ``kind: "message"`` with ``messageClass:
  "system"``, a plain sentence as ``message``, ``system: {event, memberId}``
  and ``intent: "request"`` (the entry schema requires an intent; nothing
  is owed on a system entry).  Names in the sentence are the display names
  the members were added with.
- ``threadId`` is null and ``legacyConversationId`` is the group id.
- Slice B1: group ``conversations.history`` answers ``memberNames``, the
  ``memberId -> displayName`` map computed by ``_names(entries)`` across the
  group document so every author, reply target, mention and membership-change
  subject on the page (including former members) has its display name.  1:1
  ``conversations.history`` never carries ``memberNames``.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import Any

from hyprial.daemon.impl.conversations.groups import (
    EVENT_CREATED,
    EVENT_MEMBER_ADDED,
    EVENT_MEMBER_LEFT,
    EVENT_MEMBER_REMOVED,
    GroupEntry,
    SystemEvent,
)

from ..history import history_answer
from .members import member_id_of
from .requests import _existing, _not_member, group_log


@dataclass(frozen=True)
class _Row:
    """A group entry as the page engine walks it."""

    entry: GroupEntry

    @property
    def created_at_ms(self) -> int:
        return self.entry.created_at_ms

    @property
    def message_id(self) -> str:
        return self.entry.entry_id


def _read(rows: tuple[_Row, ...], *, after: tuple[int, str] | None = None,
          before: tuple[int, str] | None = None, limit: int = 50,
          newest_first: bool = False) -> tuple[_Row, ...]:
    def position(row: _Row) -> tuple[int, str]:
        return row.created_at_ms, row.message_id

    picked = [
        row for row in rows
        if (after is None or position(row) > after)
        and (before is None or position(row) < before)
    ]
    if newest_first:
        picked.reverse()
    return tuple(picked[:limit])


def _names(entries: tuple[GroupEntry, ...]) -> dict[str, str]:
    names: dict[str, str] = {}
    for entry in entries:
        member = entry.system.member if entry.system is not None else None
        if member is not None:
            names.setdefault(member.member_id, member.display_name or member.member_id)
    return names


def _sentence(entry: GroupEntry, event: SystemEvent, names: dict[str, str]) -> str:
    author = names.get(entry.sender, entry.sender)
    member = names.get(event.member_id, event.member_id)
    return {
        EVENT_CREATED: f'{author} created the group "{event.title or ""}"',
        EVENT_MEMBER_ADDED: f"{author} added {member}",
        EVENT_MEMBER_REMOVED: f"{author} removed {member}",
        EVENT_MEMBER_LEFT: f"{member} left",
    }.get(event.event, "")


def _entry_json(row: _Row, *, names: dict[str, str]) -> dict[str, Any]:
    entry = row.entry
    document: dict[str, Any] = {
        "kind": "message",
        "messageId": entry.entry_id,
        "conversationId": entry.conversation_id,
        "from": entry.sender,
        "to": entry.conversation_id,
        "intent": entry.intent or "request",
        "createdAtMs": entry.created_at_ms,
        "message": "",
        "origin": {},
        "messageClass": entry.message_class,
    }
    if entry.system is not None:
        document["message"] = _sentence(entry, entry.system, names)
        document["system"] = {"event": entry.system.event, "memberId": entry.system.member_id}
        return document
    if entry.body_omitted is not None:
        document["bodyOmitted"] = entry.body_omitted
    else:
        document["message"] = entry.body.decode("utf-8", errors="replace")
    if entry.reply_to is not None:
        document["replyTo"] = entry.reply_to.to_json()
    if entry.mentions:
        document["mentions"] = list(entry.mentions)
    return document


def group_history(app: Any, method: str, params: dict[str, Any],
                  trusted_origin: str | None) -> dict[str, Any]:
    """``conversations.history`` for the group *params* names."""

    caller = app._history_caller(method, params, trusted_origin)
    group_id = str(params["conversationId"])
    entries, state = _existing(group_log(app), group_id)
    reader = member_id_of(app, str(caller.subject))
    if reader is None or state.member(reader) is None:
        raise _not_member(group_id)
    rows = tuple(_Row(entry) for entry in entries)
    names = _names(entries)
    page = history_answer(
        params,
        read=partial(_read, rows),
        participant=caller.subject,
        conversation_id=group_id,
        classed=True,
        project=partial(_entry_json, names=names),
    )
    return {**page, "legacyConversationId": group_id,
            "recordAvailable": True, "memberNames": names}


__all__ = ["group_history"]
