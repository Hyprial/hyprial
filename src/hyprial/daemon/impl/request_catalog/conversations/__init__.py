"""The ``conversations.*`` read methods: declarations plus real handlers (S6-read).

Decision notes (cell model phase B, S6-read; design §9.3, §9.5, §10.0):

- A conversation is addressed by its cell id (``c.``): the cell the
  recorder writes the conversation's interaction records into, keyed by the
  local owner, the thread and the legacy ``conversationId``.  The answers
  also name the legacy id, so an old client can map it.
- ``conversations.history`` is the conversation-cell read of the GUI's
  ``sessions.history`` (whose daemon call is ``message.history``): both run
  the one ``history_answer`` engine (modes, cursors, limits, entry shape)
  over the delivery facade's hydrating read, so a body the queue dropped
  after ack and retention is restored from its record (S5 semantics) and an
  unreadable record leaves the same residue.  The queue rows are the legacy
  conversation's rows restricted to the cell's thread; since RM1 the record
  is read first (see below), and for a legacy conversation with one
  counterpart whose record matches the queue the entries equal
  ``message.history``'s plus ``messageClass``.
- Identity is ``message.history``'s: the authenticated message caller under
  the consumer-plane rule, the operator form ``user:<owner>`` with
  ``onBehalfOf``, and an IPC request with no session, operator or adapter
  identity refused.  The participant is never a selector.
- The HTTP entry binds the ``local-owner-user-selector`` identity: the
  host injects ``from=user:<owner>`` and pins it to the configured local
  operator.  The caller aliases (``from``, ``actor``, ``sessionRef``,
  ``onBehalfOf``) are daemon/MCP-supplied, so the HTTP projection omits them
  and the listener refuses any the browser sends before dispatch.  IPC keeps
  every field with unchanged meaning; the read semantics below are shared by
  both entries.
- A row's thread attribution is its stored ``thread_id`` or, without one, the
  thread its two ends infer: the recorder's rule, so a listed ``c.`` id is
  the cell the recorder writes.  ``kind`` is null for a stored thread id no
  row of the caller infers (a stamp from another node); ``peer`` is then the
  row's other end.
- The read never composes the cell runtime and never needs OrgFS to answer:
  when the record cannot be read it answers from the local delivery queue
  alone.  ``DAEMON_NOT_READY`` means the delivery facade in place has no
  conversation read.
- The module is a package of its own so ``request_catalog`` keeps room
  under the seven-file directory limit for the lines that add catalog
  modules in parallel.

Decision notes (plan slice S1, rulings R4, R6):

- Conversations between two addressed actors that have no send-link thread
  (reserved actors, internal agents, the three-segment adapter sender) are
  listed and readable.  They are filed under a pair key that is not a
  thread, so their ``kind`` is null; their ``conversationId`` is a ``c.`` id
  like any other.
- ``messageClass`` on a list head is the class of the conversation's newest
  message (``system`` for mechanism traffic such as a PAC dispatch and its
  reply, else ``interaction``), so a display can filter.  Hook exchanges
  and coordinator relays are ``interaction`` under the class rulebook.
  History entries carry ``messageClass`` (in every mode since RM1).

Decision notes (plan slice RM1, R13, R15):

- The conversation record (the cell's ``rec-*`` segments in the owner-home
  space) is the read model.  ``conversations.history`` answers the record's
  messages, one per message id, plus the cell's queue rows whose message id
  the record does not hold, in ``(createdAtMs, messageId)`` order with the
  same cursors and modes.  ``conversations.list`` lists the cells of the
  record and of the queue, once each.  Entries are those the caller is the
  sender or recipient of, as before.
- Two ids, named plainly: a head's and a history answer's
  ``conversationId`` is the conversation document, the ``c.`` cell id;
  ``legacyConversationId`` (head and answer) and every history entry's
  ``conversationId`` is the id ``message.send`` takes (``conversationId``
  there).
- ``recordAvailable`` (both results, optional so older daemons stay valid)
  is false when the record could not be read (OrgFS not up or failing, no
  recorder composition) and the answer is the local queue alone.
- ``legacyConversationId`` of a history answer comes from the queue head,
  else from the record: a conversation only the record holds now names it
  where the queue-only read answered null.
- A redacted record entry shows the queue's original body while the local
  queue still holds that message (same payload digest), so a reader keeps
  seeing what it saw before RM1; once the queue no longer holds it the
  redacted body shows.  No entry field marks a redacted body.
- ``messages`` on a head counts the conversation's messages; with both
  sources it is the larger of the record's and the queue's counts, exact
  whenever one side holds the other.
- History entries carry ``messageClass`` in every mode, including the page
  returned without a mode.

Decision notes (plan slice S2, owner ruling 2026-11-17):

- A message that was delivered but whose body the recorder may not write
  (redaction refused it, or it is too large) is a placeholder in the
  record: it is listed, counted and placed like any message.  While the
  local queue holds its body the entry shows that body, as before.  Once
  the queue no longer holds it, the entry has an empty ``message`` and
  ``bodyOmitted`` names the reason.  ``bodyOmitted`` is optional and
  absent on every other entry, so older clients stay valid; its value is
  a free string because another device may record a reason this one does
  not know.

Decision notes (slice GC2, group chat design draft §3, owner decisions §10):

- ``conversations.list`` takes an optional ``kinds``; without it, or with
  ``["direct"]`` alone, the answer is the one above, byte for byte.  With
  ``group`` it also lists the caller's group heads (``group_schema``), in
  the same newest-first order, and the list cursor binds ``kinds``.
- The group methods (``conversations.create``, ``conversations.get``,
  ``conversations.members.add`` and ``conversations.members.remove``) are
  declared in ``groups`` with their handlers and bound in the registry
  next to these two reads.

Decision notes (slice GC3, group chat design draft §3.5):

- ``conversations.history`` also takes a group's ``g.`` id and then reads the
  group document (members only; ``CONVERSATION_NOT_FOUND``,
  ``CONVERSATION_NOT_MEMBER``, ``DAEMON_UNAVAILABLE`` when the document
  cannot be read).  A group entry has the direct entry's fields, with
  ``from`` a member id and ``to`` the group id, plus ``replyTo``,
  ``mentions`` and, on a system entry, ``system``; no direct entry carries
  those three.  ``conversations.list`` declares none of the group codes.

Decision notes (slice B1): group ``conversations.history`` results carry the
optional ``memberNames`` map (``GROUP_HISTORY_PROPERTIES``); direct
``conversations.history`` results omit it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.identity import InvalidAgentNameError
from hyprial.kernel import (
    HttpBinding,
    IdempotencySpec,
    IdentityBinding,
    MethodError,
    MethodSpec,
    ipc_errors,
)

from ..address_errors import AGENT_ADDRESS_RETIRED_ERROR
from .group_schema import (
    DOCUMENT_ID_PATTERN,
    GROUP_ENTRY_PROPERTIES,
    GROUP_HEAD,
    GROUP_HISTORY_PROPERTIES,
    KINDS,
)

JsonObject = dict[str, Any]

CELL_ID_PATTERN = r"^c\.[a-z2-7]{26}$"
PAGE_MAX = 200

_STRING = {"type": "string", "minLength": 1}
_NULLABLE = {"type": ["string", "null"]}
# Release sync (HTTP boundary): the daemon/MCP-injected caller identity is
# never self-reported by a browser.  The four aliases stay in the IPC schema
# unchanged; the HTTP projection drops them and the listener refuses them
# before dispatch.  ``actor`` is host-only too because it takes precedence
# over ``from`` in the caller rule and would otherwise let a browser claim an
# agent identity.
_HOST_ONLY = {**_STRING, "x-hyprial-http-forbidden": True}
_IDENTITY = {
    name: dict(_HOST_ONLY) for name in ("from", "actor", "sessionRef", "onBehalfOf")
}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": PAGE_MAX}

_LIST_PARAMS = {
    "type": "object",
    "properties": {
        **_IDENTITY,
        "cursor": _STRING,
        "limit": _LIMIT,
        "kinds": KINDS,
    },
    "additionalProperties": False,
}

_HISTORY_PARAMS = {
    "type": "object",
    "properties": {
        **_IDENTITY,
        "conversationId": {"type": "string", "pattern": DOCUMENT_ID_PATTERN},
        "cursor": _STRING,
        "limit": _LIMIT,
        "mode": {"enum": ["tail", "older", "after"]},
    },
    "required": ["conversationId"],
    "additionalProperties": False,
}

_RECORD_AVAILABLE = {
    "type": "boolean",
    "description": (
        "False when the conversation record could not be read and the answer "
        "comes from the local delivery queue alone."
    ),
}
_DOCUMENT_ID = {
    "type": "string",
    "pattern": CELL_ID_PATTERN,
    "description": "The conversation document: the c. id of its conversation cell.",
}
_READ_ID = {
    "type": "string",
    "pattern": DOCUMENT_ID_PATTERN,
    "description": (
        "The conversation document: the c. id of its conversation cell, or a group's g. id."
    ),
}
_SEND_ID = {
    **_STRING,
    "description": "The conversation id message.send takes (its conversationId).",
}

_HEAD = {
    "type": "object",
    "properties": {
        "conversationId": _DOCUMENT_ID,
        "kind": {"oneOf": [{"enum": ["agent", "user", "adapter"]}, {"type": "null"}]},
        "peer": _STRING,
        "legacyConversationId": _SEND_ID,
        "lastMessageAtMs": {"type": "integer", "minimum": 0},
        "lastMessageId": _STRING,
        "messages": {"type": "integer", "minimum": 1},
        "messageClass": {"oneOf": [
            {"enum": ["system", "interaction"]}, {"type": "null"},
        ]},
    },
    "required": [
        "conversationId", "kind", "peer", "legacyConversationId",
        "lastMessageAtMs", "lastMessageId", "messages",
    ],
    "additionalProperties": False,
}

_LIST_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "participant": _STRING,
        "conversations": {"type": "array", "items": {"oneOf": [_HEAD, GROUP_HEAD]}},
        "nextCursor": _NULLABLE,
        "recordAvailable": _RECORD_AVAILABLE,
    },
    "required": ["ok", "participant", "conversations", "nextCursor"],
    "additionalProperties": False,
}

_JSON_VALUE = {"type": ["string", "number", "boolean", "null", "object", "array"]}

_ENTRY = {
    "type": "object",
    "properties": {
        "kind": {"const": "message"},
        "messageId": _STRING,
        "conversationId": {
            "type": "string",
            "description": "The conversation id message.send took for this message.",
        },
        "from": {"type": "string"},
        "to": {"type": "string"},
        "intent": {"enum": ["request", "reply"]},
        "createdAtMs": {"type": "integer"},
        "message": {"type": "string"},
        "origin": {"type": "object", "additionalProperties": _JSON_VALUE},
        "messageClass": {"enum": ["system", "interaction"]},
        "bodyOmitted": {
            **_STRING,
            "description": (
                "Present only when the conversation record holds this message "
                "without its body and the local queue no longer holds the body: "
                "message is then empty and this names why the body was not "
                "recorded (redacted, redaction-invalid, too-large; other "
                "devices may write other reasons)."
            ),
        },
        **GROUP_ENTRY_PROPERTIES,
    },
    "required": [
        "kind", "messageId", "conversationId", "from", "to", "intent",
        "createdAtMs", "message", "origin",
    ],
    "additionalProperties": False,
}

_HISTORY_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "conversationId": _READ_ID,
        "mode": {"enum": ["tail", "older", "after"]},
        "entries": {"type": "array", "items": _ENTRY},
        "headCursor": _NULLABLE,
        "nextCursor": _NULLABLE,
        "nextOlderCursor": _NULLABLE,
        "legacyConversationId": {
            **_NULLABLE,
            "description": (
                "The conversation id message.send takes, null when the caller "
                "has no message in the conversation."
            ),
        },
        "recordAvailable": _RECORD_AVAILABLE,
        **GROUP_HISTORY_PROPERTIES,
    },
    "required": [
        "ok", "conversationId", "entries", "nextCursor",
        "legacyConversationId",
    ],
    "additionalProperties": False,
}


def _data(**properties: Any) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "additionalProperties": False}


CALLER_ERRORS = (
    MethodError(
        ipc_errors.INVALID_ARGUMENT,
        _data(sender=_STRING, unexpectedFields={"type": "array", "items": _STRING}),
        description="params, cursor or caller selector rejected",
    ),
    MethodError(ipc_errors.SENDER_UNRESOLVED, _data(sender=_STRING)),
    # Release (UUID addressing §2.3): the caller rule refuses a retired name
    # URI with the id address in data, as message.history declares it.
    AGENT_ADDRESS_RETIRED_ERROR,
    MethodError(ipc_errors.SENDER_NOT_AUTHENTICATED, _data(claimedIdentity=_STRING)),
    MethodError(ipc_errors.AMBIGUOUS_TARGET, _data(
        target=_STRING, candidates={"type": "array", "items": _STRING},
    )),
    MethodError(InvalidAgentNameError.code),
    MethodError(ipc_errors.STALE_SESSION),
    MethodError(ipc_errors.SESSION_SUPERSEDED),
    MethodError(ipc_errors.INVALID_SESSION_SOURCE),
)
"""The refusals of ``message.history``'s caller rule."""

_ERRORS = (
    *CALLER_ERRORS,
    MethodError(
        ipc_errors.DAEMON_NOT_READY,
        description="the delivery facade in place has no conversation read",
    ),
)

_HISTORY_ERRORS = (
    *_ERRORS,
    MethodError(
        ipc_errors.CONVERSATION_NOT_FOUND, _data(conversationId=_STRING),
        description="no readable chat space holds this group",
    ),
    MethodError(
        ipc_errors.CONVERSATION_NOT_MEMBER, _data(conversationId=_STRING),
        description="the caller is not a current member of this group",
    ),
    MethodError(
        ipc_errors.DAEMON_UNAVAILABLE,
        description="the group document cannot be read now; retry",
    ),
)

_HTTP = HttpBinding(IdentityBinding("local-owner-user-selector", "from"))

CONVERSATIONS_LIST_SPEC = MethodSpec(
    name="conversations.list",
    owner="daemon.conversations",
    params_schema=_LIST_PARAMS,
    result_schema=_LIST_RESULT,
    errors=_ERRORS,
    mode="unary",
    mutates=False,
    retry="safe",
    audience=("local-operator", "bound-agent"),
    http_exposed=True,
    http_binding=_HTTP,
    confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="read-only-query"),
)

CONVERSATIONS_HISTORY_SPEC = MethodSpec(
    name="conversations.history",
    owner="daemon.conversations",
    params_schema=_HISTORY_PARAMS,
    result_schema=_HISTORY_RESULT,
    errors=_HISTORY_ERRORS,
    mode="unary",
    mutates=False,
    retry="safe",
    audience=("local-operator", "bound-agent"),
    http_exposed=True,
    http_binding=_HTTP,
    confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="read-only-query"),
)


def conversations_list(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    return app._ipc_conversations_list(
        CONVERSATIONS_LIST_SPEC.name,
        dict(params),
        _trusted_message_origin=trusted_message_origin,
    )


def conversations_history(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    return app._ipc_conversations_history(
        CONVERSATIONS_HISTORY_SPEC.name,
        dict(params),
        _trusted_message_origin=trusted_message_origin,
    )


CONVERSATION_BINDINGS = (
    (CONVERSATIONS_LIST_SPEC, conversations_list),
    (CONVERSATIONS_HISTORY_SPEC, conversations_history),
)

__all__ = [
    "CALLER_ERRORS",
    "CONVERSATIONS_HISTORY_SPEC",
    "CONVERSATIONS_LIST_SPEC",
    "CONVERSATION_BINDINGS",
    "conversations_history",
    "conversations_list",
]
