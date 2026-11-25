"""Schemas of the group conversation answers (slice GC2).

Decision notes (group chat design draft §3, owner decisions §10, 2026-11-18):

- A group is named by its ``g.`` id everywhere: the ``conversationId`` of
  every group method and of a group head, and its ``legacyConversationId``
  (the id ``message.send`` will take for it).
- A member is the record the group document stores: ``memberId`` is
  ``user:<owner>`` for the owner or ``a.<id>`` for an agent, never a
  machine-bound address; ``displayName`` is the name the local registry
  gave when the member was added; ``address`` (agents only) is the address
  at that time, a routing hint and never an identity.
- A group head is listed by ``conversations.list`` only when the caller asks
  for ``kinds`` containing ``group``; its ``kind`` is ``group``, so a client
  tells it from a direct head (whose ``kind`` is a thread kind or null) and
  from the direct head's ``c.`` id pattern.  ``lastMessageAtMs`` is the time
  of the newest entry of any class, so a new group sorts by its creation;
  ``lastMessageId`` and ``messages`` count interaction messages only.
  ``messageClass`` is always ``interaction`` (coordinator, 2026-11-19), so
  a client that lists only interaction conversations shows a new group.
- ``KINDS`` defaults to ``["direct"]``: the list answer is then exactly the
  answer it was before groups existed.

Decision notes (slice GC3): ``conversations.history`` takes a ``c.`` or a
``g.`` id (``DOCUMENT_ID_PATTERN``).  A group entry has the direct entry's
fields (``from`` a member id, ``to`` the group id) plus the optional
``GROUP_ENTRY_PROPERTIES``, which no direct entry carries.

Decision notes (slice B1): a group ``conversations.history`` result carries
``memberNames`` (``GROUP_HISTORY_PROPERTIES``), mapping every member id that
has joined the group (including former members) to its display name; 1:1
history results omit it.
"""

from __future__ import annotations

GROUP_ID_PATTERN = r"^g\.[a-z2-7]{26}$"
KIND_DIRECT = "direct"
KIND_GROUP = "group"

_STRING = {"type": "string", "minLength": 1}
_MS = {"type": "integer", "minimum": 0}

GROUP_ID = {
    "type": "string",
    "pattern": GROUP_ID_PATTERN,
    "description": "The group conversation: its g. id, also the id message.send takes.",
}

KINDS = {
    "type": "array",
    "items": {"enum": [KIND_DIRECT, KIND_GROUP]},
    "minItems": 1,
    "uniqueItems": True,
    "description": (
        "Which conversations to list: direct (the default, alone) and/or group."
    ),
}

MEMBER = {
    "type": "object",
    "properties": {
        "memberId": {
            **_STRING,
            "description": "user:<owner> for the owner, a.<id> for an agent.",
        },
        "kind": {"enum": ["user", "agent"]},
        "displayName": {"type": "string"},
        "manager": {
            "type": "boolean",
            "description": "Whether this member may add and remove members.",
        },
        "joinedAtMs": _MS,
        "addedBy": _STRING,
        "address": {
            **_STRING,
            "description": "The agent's address when it was added: a routing hint only.",
        },
    },
    "required": ["memberId", "kind", "displayName", "manager", "joinedAtMs", "addedBy"],
    "additionalProperties": False,
}

MEMBERS = {"type": "array", "items": MEMBER}

GROUP_PROPERTIES = {
    "ok": {"const": True},
    "conversationId": GROUP_ID,
    "kind": {"const": KIND_GROUP},
    "title": _STRING,
    "createdBy": _STRING,
    "createdAtMs": _MS,
    "members": MEMBERS,
}
GROUP_REQUIRED = ["ok", "conversationId", "kind", "title", "createdBy", "createdAtMs", "members"]

GROUP_HEAD = {
    "type": "object",
    "properties": {
        "kind": {"const": KIND_GROUP},
        "conversationId": GROUP_ID,
        "legacyConversationId": GROUP_ID,
        "title": _STRING,
        "memberCount": {"type": "integer", "minimum": 1},
        "lastMessageAtMs": _MS,
        "lastMessageId": {"type": ["string", "null"], "minLength": 1},
        "messages": {"type": "integer", "minimum": 0},
        "messageClass": {"const": "interaction"},
    },
    "required": [
        "kind", "conversationId", "legacyConversationId", "title", "memberCount",
        "lastMessageAtMs", "lastMessageId", "messages", "messageClass",
    ],
    "additionalProperties": False,
}

#: A ``c.`` cell id or a ``g.`` group id: what ``conversations.history`` reads.
DOCUMENT_ID_PATTERN = r"^[cg]\.[a-z2-7]{26}$"

#: The properties only a group history entry carries (slice GC3).
GROUP_ENTRY_PROPERTIES = {
    "replyTo": {
        "type": "object",
        "properties": {
            "messageId": _STRING,
            "from": {**_STRING, "description": "The member id of the replied message's author."},
            "excerpt": {
                "type": "string",
                "description": "The first 120 characters of the replied message.",
            },
        },
        "required": ["messageId", "from", "excerpt"],
        "additionalProperties": False,
        "description": "Group entries only: the group message this one answers.",
    },
    "mentions": {
        "type": "array",
        "items": _STRING,
        "description": "Group entries only: the member ids this message names.",
    },
    "system": {
        "type": "object",
        "properties": {
            "event": {"enum": ["created", "member-added", "member-removed", "member-left"]},
            "memberId": _STRING,
        },
        "required": ["event", "memberId"],
        "additionalProperties": False,
        "description": "Group system entries only: the membership change.",
    },
}

GROUP_HISTORY_PROPERTIES = {
    "memberNames": {
        "type": "object",
        "additionalProperties": {"type": "string"},
        "description": (
            "Group history only: memberId to displayName for members that "
            "have joined the group, including former members."
        ),
    },
}

__all__ = [
    "DOCUMENT_ID_PATTERN",
    "GROUP_ENTRY_PROPERTIES",
    "GROUP_HEAD",
    "GROUP_HISTORY_PROPERTIES",
    "GROUP_ID",
    "GROUP_ID_PATTERN",
    "GROUP_PROPERTIES",
    "GROUP_REQUIRED",
    "KINDS",
    "KIND_DIRECT",
    "KIND_GROUP",
    "MEMBER",
    "MEMBERS",
]
