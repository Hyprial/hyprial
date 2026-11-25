"""Human rendering for ``hyprial message group``.

Pure presentation of the daemon's answers: names, member ids, counts and
times are the daemon's; a missing field renders as ``-``.  Nothing here
decides membership or who may manage it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.shell.impl.cli.commands.message.group.names import members_of
from hyprial.shell.impl.cli.commands.message.threads.conversation import timestamp
from hyprial.shell.impl.cli.commands.message.threads.reader import text
from hyprial.shell.impl.cli.output import tables


def _roster(data: Mapping[str, Any]) -> str:
    members = members_of(data)
    names = ", ".join(text(row.get("displayName")) for row in members)
    noun = "member" if len(members) == 1 else "members"
    return f"{len(members)} {noun} ({names})"


def render_group(data: Mapping[str, Any]) -> str:
    """``message group create``: the group id, its title and its members in one line."""

    return f"{text(data.get('conversationId'))}  {text(data.get('title'))}  {_roster(data)}"


def render_group_detail(data: Mapping[str, Any]) -> str:
    """``message group show``: the summary line, the caller's standing, one row per member."""

    may = "can" if data.get("canManageMembers") is True else "cannot"
    cells = [
        [
            text(row.get("displayName")),
            text(row.get("memberId")),
            "yes" if row.get("manager") is True else "no",
            timestamp(row.get("joinedAtMs")),
        ]
        for row in members_of(data)
    ]
    return "\n".join(
        [
            render_group(data),
            f"you are {text(data.get('self'))} and {may} manage members",
            tables.render_table(None, ["NAME", "MEMBER", "MANAGES", "JOINED"], cells),
        ]
    )


def render_group_list(data: Mapping[str, Any]) -> str:
    """``message group list``: one row per group, then the next page's cursor."""

    rows = [row for row in data.get("conversations") or [] if isinstance(row, Mapping)]
    if not rows:
        return "no groups"
    cells = [
        [
            text(row.get("conversationId")),
            text(row.get("title")),
            str(row.get("memberCount", "-")),
            str(row.get("messages", "-")),
            timestamp(row.get("lastMessageAtMs")),
        ]
        for row in rows
    ]
    lines = [
        tables.render_table("groups", ["GROUP", "TITLE", "MEMBERS", "MESSAGES", "LAST"], cells)
    ]
    cursor = data.get("nextCursor")
    if isinstance(cursor, str) and cursor:
        lines.append(f"more: --cursor {cursor}")
    return "\n".join(lines)


def render_added(named: str, data: Mapping[str, Any]) -> str:
    """``message group members add``: what changed, then the member list."""

    group = text(data.get("conversationId"))
    if data.get("entryId") is None:
        return f"{named} is already a member of {group}: {_roster(data)}"
    return f"added {named} to {group}: {_roster(data)}"


def render_removed(named: str, data: Mapping[str, Any]) -> str:
    """``message group members remove``: what changed, then the member list."""

    group = text(data.get("conversationId"))
    if data.get("entryId") is None:
        return f"{named} is not a member of {group}; nothing changed: {_roster(data)}"
    return f"removed {named} from {group}: {_roster(data)}"


def render_group_history(
    data: Mapping[str, Any], conversation: str, names: Mapping[str, str]
) -> str:
    """``message history --conversation <g.>``: one block per group entry.

    A membership change is its sentence; a message is its author's name and
    id, the message it answers, the members it names, then its text.  A
    member id with no current name (a former member) is shown as the id.
    """

    def name(value: object) -> str:
        return names.get(value, text(value)) if isinstance(value, str) else text(value)

    entries = [row for row in data.get("entries") or [] if isinstance(row, Mapping)]
    if not entries:
        return "no messages"
    lines: list[str] = []
    for entry in entries:
        at = timestamp(entry.get("createdAtMs"))
        body = entry.get("message")
        if isinstance(entry.get("system"), Mapping):
            lines.append(f"{at}  {text(body)}")
            continue
        lines.append(f"{at}  {name(entry.get('from'))}  {text(entry.get('messageId'))}")
        replied = entry.get("replyTo")
        if isinstance(replied, Mapping):
            lines.append(
                f"  in reply to {name(replied.get('from'))} {text(replied.get('messageId'))}: "
                f"\"{replied.get('excerpt') or ''}\""
            )
        mentions = entry.get("mentions")
        if isinstance(mentions, list) and mentions:
            lines.append(f"  mentions: {', '.join(name(member) for member in mentions)}")
        for line in (body if isinstance(body, str) else "").splitlines() or [""]:
            lines.append(f"  {line}")
    older = data.get("nextOlderCursor")
    if isinstance(older, str) and older:
        lines.append(f"older: --conversation {conversation} --mode older --cursor {older}")
    return "\n".join(lines)


__all__ = [
    "render_added",
    "render_group",
    "render_group_detail",
    "render_group_history",
    "render_group_list",
    "render_removed",
]
