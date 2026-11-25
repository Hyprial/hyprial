"""Which members a group entry wakes (slice GC1; updated 2026-11-21).

Decision notes (owner ruling 2026-11-21, superseding the first two clauses
of plan §18.1 R28 / group chat draft §4.4 / owner decisions §10):

- A group message wakes ONLY the agent members named in ``entry.mentions``,
  whether the author is the human (``kind == "user"``) or an agent.
  Messages with no mentions wake nobody (they are still appended to the
  group document for all members to read in history).
- Replying (``replyTo`` / ``message.reply``) no longer implies waking the
  replied entry's author: to wake someone, mention them explicitly.
- Owner principle (2026-11-21): if an agent should respond to unmentioned
  group messages automatically, that agent adds a ``routine`` in runtime
  data to check group history periodically — anything that can be done as
  runtime data must not be hardcoded in code.
- Unchanged invariants: system entries wake nobody; the author never wakes
  itself; an author that is not a current member wakes nobody; only agent
  members are ever woken; no hop cap (rejected, §10 8.5).
- Pure: the result follows the order of *members*.
"""

from __future__ import annotations

from collections.abc import Sequence

from hyprial.daemon.impl.conversations.groups.model import (
    MEMBER_AGENT,
    GroupEntry,
    GroupMember,
)


def wake_members(entry: GroupEntry, members: Sequence[GroupMember]) -> tuple[str, ...]:
    """The member ids of the agent members *entry* wakes."""

    if entry.system is not None:
        return ()
    author = next((member for member in members if member.member_id == entry.sender), None)
    if author is None:
        return ()
    named = set(entry.mentions)
    return tuple(
        member.member_id
        for member in members
        if member.kind == MEMBER_AGENT
        and member.member_id != entry.sender
        and member.member_id in named
    )


__all__ = ["wake_members"]
