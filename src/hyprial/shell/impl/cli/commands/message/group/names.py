"""Group ids and member names as the daemon reports them.

A value the user types for a member (an id or a member's name) is mapped to
the member id ``conversations.get`` lists; a value that matches no member,
or the names of several, is kept as typed so the daemon answers it.  This
module imports no command module, so ``message send`` and ``message
history`` can use it without an import cycle.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

GROUP_ID = re.compile(r"g\.[a-z2-7]{26}")
MEMBER_ID = re.compile(r"(?:a\.[a-z2-7]{26}|user:\S+)")


def is_group(conversation_id: str | None) -> bool:
    """Whether *conversation_id* is a group's ``g.`` id."""

    return conversation_id is not None and GROUP_ID.fullmatch(conversation_id) is not None


def is_member_id(value: str) -> bool:
    """Whether *value* is already a group member id (``a.…`` or ``user:…``)."""

    return MEMBER_ID.fullmatch(value) is not None


def _agent_member_id(row: Mapping[str, Any]) -> str | None:
    if row.get("userAgent") is True:
        user_actor = row.get("userActor")
        return user_actor if isinstance(user_actor, str) and user_actor else None
    agent_id = row.get("agentId")
    return agent_id if isinstance(agent_id, str) and agent_id else None


def resolve_reply_mentions(services: Any, values: Sequence[str]) -> list[str]:
    """Map CLI ``--mention`` values to member ids with at most one ``agent.list`` lookup."""

    if not values or all(is_member_id(value) for value in values):
        return list(values)
    answer = services.runtime.request("agent.list", {"includeUserAgent": True})
    rows = answer.get("agents") if isinstance(answer, Mapping) else None
    candidates = [row for row in rows or [] if isinstance(row, Mapping)]
    resolved: list[str] = []
    for value in values:
        if is_member_id(value):
            resolved.append(value)
            continue
        matched: list[str] = []
        for row in candidates:
            target_id = _agent_member_id(row)
            if target_id is None:
                continue
            if (
                row.get("displayName") == value
                or row.get("actor") == value
                or row.get("uri") == value
                or row.get("userActor") == value
            ):
                matched.append(target_id)
        unique = list(dict.fromkeys(matched))
        resolved.append(unique[0] if len(unique) == 1 else value)
    return resolved


def members_of(data: object) -> list[Mapping[str, Any]]:
    """The member records of an answer, in the daemon's order."""

    rows = data.get("members") if isinstance(data, Mapping) else None
    return [row for row in rows or [] if isinstance(row, Mapping)]


def read_members(services: Any, caller: Mapping[str, Any], conversation_id: str) -> list:
    """The current members of the group, read once with ``conversations.get``."""

    answer = services.runtime.request(
        "conversations.get", {**caller, "conversationId": conversation_id}
    )
    return members_of(answer)


def member_id(members: Sequence[Mapping[str, Any]], value: str) -> str:
    """The member id *value* names: by id, else by a unique name, else as typed."""

    if any(row.get("memberId") == value for row in members):
        return value
    named = [row.get("memberId") for row in members if row.get("displayName") == value]
    if len(named) == 1 and isinstance(named[0], str):
        return named[0]
    return value


def display_names(members: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Member id to the name it was added with."""

    return {
        str(row["memberId"]): str(row["displayName"])
        for row in members
        if isinstance(row.get("memberId"), str)
        and isinstance(row.get("displayName"), str)
        and row["displayName"]
    }


__all__ = [
    "GROUP_ID",
    "MEMBER_ID",
    "display_names",
    "is_group",
    "is_member_id",
    "member_id",
    "members_of",
    "read_members",
    "resolve_reply_mentions",
]
