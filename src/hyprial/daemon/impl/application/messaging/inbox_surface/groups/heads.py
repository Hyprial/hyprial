"""Group heads for ``conversations.list`` with ``kinds`` (slice GC2).

Decision notes (group chat design draft §3.4, brief GC2):

- The heads are computed from the group documents alone, never from the
  delivery queue: one listing of the group directories, then one read and
  fold per group, with no cache and no index.
- A group is listed for a caller that is a current member of it; a
  directory that does not fold to a ``created`` entry is not a group.
- ``lastMessageAtMs`` is the newest entry of any class, so a new group sorts
  by its last membership entry; ``lastMessageId`` and ``messages`` count
  interaction messages.
- A group head's ``messageClass`` is always ``interaction`` (coordinator,
  2026-11-19): a group is a place for interaction, and a client that lists
  only interaction conversations must still show a new, empty group.  The
  system entries inside it are folded by the history view.
- A head carries ``last_message_at_ms`` and ``conversation_id`` like a
  direct head, so the list's ordering, paging and cursor treat both alike.
- When the documents cannot be read the answer is no group heads and
  ``available`` false, which the list publishes as ``recordAvailable``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hyprial.daemon.impl.conversations import RecordUnavailable
from hyprial.daemon.impl.conversations.groups import CLASS_INTERACTION, fold_group
from hyprial.kernel import DaemonRequestError


@dataclass(frozen=True)
class GroupHead:
    """One group as ``conversations.list`` lists it."""

    conversation_id: str
    title: str
    member_count: int
    last_message_at_ms: int
    last_message_id: str | None
    messages: int

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": "group",
            "conversationId": self.conversation_id,
            "legacyConversationId": self.conversation_id,
            "title": self.title,
            "memberCount": self.member_count,
            "lastMessageAtMs": self.last_message_at_ms,
            "lastMessageId": self.last_message_id,
            "messages": self.messages,
            "messageClass": CLASS_INTERACTION,
        }


def group_heads(app: Any, participant: str) -> tuple[tuple[GroupHead, ...], bool]:
    """The groups *participant* is a current member of, and whether they could be read."""

    from .members import member_id_of
    from .requests import group_log

    member_id = member_id_of(app, participant)
    if member_id is None:
        return (), True
    try:
        log = group_log(app)
        heads = []
        for group_id in log.groups():
            entries = log.read(group_id)
            state = fold_group(entries)
            if state.created_by is None or state.member(member_id) is None:
                continue
            messages = [entry for entry in entries if entry.message_class == CLASS_INTERACTION]
            heads.append(GroupHead(
                conversation_id=group_id,
                title=str(state.title),
                member_count=len(state.members),
                last_message_at_ms=entries[-1].created_at_ms,
                last_message_id=messages[-1].entry_id if messages else None,
                messages=len(messages),
            ))
    except (RecordUnavailable, DaemonRequestError):
        return (), False
    return tuple(heads), True


__all__ = ["GroupHead", "group_heads"]
