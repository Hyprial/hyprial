"""The group conversation methods: declarations and their bindings (slice GC2).

Decision notes (group chat design draft §3.1-§3.3, owner decisions §10,
2026-11-18):

- Four methods: ``conversations.create``, ``conversations.get``,
  ``conversations.members.add`` and ``conversations.members.remove`` (slice
  GH adds ``conversations.post``, below).  There is no
  ``conversations.members.list`` (``get`` answers the members) and no
  rename (the title is set at creation).
- They share the conversation reads' caller rule, aliases and HTTP binding
  (``local-owner-user-selector`` on ``from``): over HTTP the caller is
  always the owner.  IPC callers are the owner (``from=user:<owner>``) or an
  agent with its session; with ``onBehalfOf`` the caller is that agent.
- Who may call what: only the owner creates and manages members; a member
  agent may ``get`` and may remove itself (leave); the owner cannot leave;
  anyone else is ``CONVERSATION_NOT_MEMBER``; an unknown ``g.`` id is
  ``CONVERSATION_NOT_FOUND``.  A document that cannot be read or written
  now is ``DAEMON_UNAVAILABLE``: retry with the same key.
- Mutations take an ``idempotencyKey``: ``create`` derives the group id
  from it, and an add or remove derives its entry id from it, so a replay
  answers the same group or the current member list and writes nothing.
  Like ``message.send`` they declare ``retry="never"``: the key, not the
  transport, makes a retry safe.
- ``entryId`` of an add or remove is the system entry the call wrote, or
  null when the call changed nothing (adding a current member, removing a
  non-member).
- The handlers live in ``application.messaging.inbox_surface.groups``.
- ``GROUP_BINDINGS`` is served from the registry next to
  ``CONVERSATION_BINDINGS``.  The four names in
  ``migration.MIGRATED_METHODS`` and the two error codes
  ``CONVERSATION_NOT_FOUND`` / ``CONVERSATION_NOT_MEMBER`` were approved by
  the repository owner on 2026-11-19.

Decision notes (slice GH, 2026-11-19): ``conversations.post`` is the HTTP
door to a group send.

- ``message.send`` to a group is IPC-only: its HTTP profile requires ``to``
  and hides ``mentions`` and ``replyTo``, and the HTTP constraint
  projection takes only ``type``, ``properties`` and ``required``.  Making
  ``to`` optional there (required unless the id is a group, checked in the
  handler) was rejected: it changes the published HTTP schema of every
  direct send, turns today's listener refusal of a body without ``to``
  (not dispatched) into a daemon refusal (dispatched, another message), and
  would expose ``replyTo`` to direct sends too.  A separate method leaves
  the direct send's HTTP contract byte for byte as it was and gives the
  generated client one plain operation with its own required fields.
- No logic of its own: the handler is ``message.send``'s handler under the
  name ``message.send``, so the caller rule, the summary check and GC3's
  ``send_to_group`` (one document entry, GC4's rows) are the ones an IPC
  group send runs, and the answer is the same bytes.  The params are
  ``message.send``'s group subset: ``conversationId`` must be a ``g.`` id,
  ``to`` and the 1:1-only fields are not declared, and ``summary`` /
  ``idempotencyKey`` carry the direct send's HTTP constraints.
- Same caller aliases and HTTP binding as the other group methods, so over
  HTTP the caller is the owner.  ``message.send`` with a group id stays the
  IPC and CLI way to the same handler.

Decision notes (2026-11-21, Contract Mock key echo): ``conversations.post``
declares ``mock_echo={"idempotencyKey": "operationId"}`` so the Contract Mock
echoes caller-minted keys on success examples.  ``conversations.create``,
``conversations.members.add`` and ``conversations.members.remove`` return no
operation key and rely on selector matching for ``title``, ``members`` and
``conversationId``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.kernel import IdempotencySpec, MethodError, MethodSpec, ipc_errors

from . import _HTTP, _IDENTITY, CALLER_ERRORS
from .group_schema import GROUP_ID, GROUP_PROPERTIES, GROUP_REQUIRED, MEMBERS

JsonObject = dict[str, Any]

_STRING = {"type": "string", "minLength": 1}
_KEY = {**_STRING, "description": "Makes a retry of this call answer the first call."}
_MEMBER_REF = {
    **_STRING,
    "description": "An agent of this owner: its name, its a.<id>, or its address.",
}


def _params(required: list[str], **properties: Any) -> JsonObject:
    return {
        "type": "object",
        "properties": {**_IDENTITY, **properties},
        "required": required,
        "additionalProperties": False,
    }


def _data(**properties: Any) -> JsonObject:
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(properties),
        "additionalProperties": False,
    }


_CREATE_PARAMS = _params(
    ["title", "members", "idempotencyKey"],
    title={**_STRING, "maxLength": 200},
    members={"type": "array", "items": _MEMBER_REF},
    idempotencyKey=_KEY,
)
_GET_PARAMS = _params(["conversationId"], conversationId=GROUP_ID)
_ADD_PARAMS = _params(
    ["conversationId", "member", "idempotencyKey"],
    conversationId=GROUP_ID,
    member=_MEMBER_REF,
    idempotencyKey=_KEY,
)
_REMOVE_PARAMS = _params(
    ["conversationId", "memberId", "idempotencyKey"],
    conversationId=GROUP_ID,
    memberId={**_STRING, "description": "A member id as conversations.get names it."},
    idempotencyKey=_KEY,
)

_GROUP_RESULT = {
    "type": "object",
    "properties": GROUP_PROPERTIES,
    "required": GROUP_REQUIRED,
    "additionalProperties": False,
}
_GET_RESULT = {
    "type": "object",
    "properties": {
        **GROUP_PROPERTIES,
        "self": {**_STRING, "description": "The caller's member id."},
        "canManageMembers": {"type": "boolean"},
    },
    "required": [*GROUP_REQUIRED, "self", "canManageMembers"],
    "additionalProperties": False,
}
_MEMBERS_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "conversationId": GROUP_ID,
        "members": MEMBERS,
        "entryId": {
            "type": ["string", "null"],
            "minLength": 1,
            "description": "The system entry this call wrote; null when nothing changed.",
        },
    },
    "required": ["ok", "conversationId", "members", "entryId"],
    "additionalProperties": False,
}

_NOT_FOUND = MethodError(
    ipc_errors.CONVERSATION_NOT_FOUND,
    _data(conversationId=_STRING),
    description="no readable chat space holds this group",
)
_NOT_MEMBER = MethodError(
    ipc_errors.CONVERSATION_NOT_MEMBER,
    _data(conversationId=_STRING),
    description="the caller is not a current member of this group",
)
_NOT_AUTHORIZED = MethodError(
    ipc_errors.CALLER_NOT_AUTHORIZED,
    description="only the owner creates groups and manages their members",
)
_AGENT_NOT_FOUND = MethodError(
    ipc_errors.AGENT_NOT_FOUND,
    _data(member=_STRING),
    description="the member names no agent of this owner on this machine",
)
_UNSUPPORTED = MethodError(
    ipc_errors.UNSUPPORTED_TARGET,
    _data(member=_STRING),
    description="the member belongs to another owner",
)
_UNAVAILABLE = MethodError(
    ipc_errors.DAEMON_UNAVAILABLE,
    description="the group document cannot be read or written now; retry the same call",
)
_SUMMARY = MethodError(
    ipc_errors.SUMMARY_REQUIRED,
    {
        "type": "object",
        "properties": {
            "field": {"const": "summary"}, "reason": {"type": "string"},
            "maxBytes": {"type": "integer"}, "summaryBytes": {"type": "integer"},
        },
        "required": ["maxBytes"],
        "additionalProperties": False,
    },
    description="the summary is missing, blank or longer than its byte limit",
)

_POST_PARAMS = _params(
    ["conversationId", "message", "summary", "idempotencyKey"],
    conversationId=GROUP_ID,
    message=_STRING,
    summary={"type": "string", "minLength": 1, "pattern": r"\S",
             "description": "What a member sees first (at most 512 UTF-8 bytes)."},
    idempotencyKey={
        "type": "string", "minLength": 22, "maxLength": 128, "pattern": r"^[A-Za-z0-9_-]+$",
        "description": "Makes a retry of this call answer the first call.",
    },
    mentions={"type": "array", "items": {
        **_STRING, "description": "A current member's id as conversations.get names it.",
    }},
    replyTo={**_STRING, "description": "The messageId of the group message this one answers."},
)
_POST_DELIVERY = {
    "type": "object",
    "properties": {
        "target": GROUP_ID,
        "messageId": _STRING,
        "conversationId": GROUP_ID,
        "accepted": {"const": True},
        "queued": {"const": False},
        "duplicate": {"const": True},
    },
    "required": ["target", "messageId", "conversationId", "accepted", "queued"],
    "additionalProperties": False,
}
_POST_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "actingPrincipal": _STRING,
        "operationId": _STRING,
        "conversationId": GROUP_ID,
        "messageId": _STRING,
        "deliveries": {"type": "array", "minItems": 1, "maxItems": 1, "items": _POST_DELIVERY},
    },
    "required": ["ok", "actingPrincipal", "operationId", "conversationId", "messageId",
                 "deliveries"],
    "additionalProperties": False,
}


def _spec(name: str, params: JsonObject, result: JsonObject, errors: tuple[MethodError, ...],
          *, mutates: bool) -> MethodSpec:
    return MethodSpec(
        name=name,
        owner="daemon.conversations",
        params_schema=params,
        result_schema=result,
        errors=(*CALLER_ERRORS, *errors),
        mode="unary",
        mutates=mutates,
        retry="never" if mutates else "safe",
        audience=("local-operator", "bound-agent"),
        http_exposed=True,
        http_binding=_HTTP,
        confirmation="none",
        idempotency=(
            IdempotencySpec(mode="never", scope="group-conversation-key")
            if mutates
            else IdempotencySpec(mode="safe", scope="read-only-query")
        ),
    )


CONVERSATIONS_CREATE_SPEC = _spec(
    "conversations.create", _CREATE_PARAMS, _GROUP_RESULT,
    (_NOT_AUTHORIZED, _AGENT_NOT_FOUND, _UNSUPPORTED, _UNAVAILABLE), mutates=True,
)
CONVERSATIONS_GET_SPEC = _spec(
    "conversations.get", _GET_PARAMS, _GET_RESULT,
    (_NOT_FOUND, _NOT_MEMBER, _UNAVAILABLE), mutates=False,
)
CONVERSATIONS_MEMBERS_ADD_SPEC = _spec(
    "conversations.members.add", _ADD_PARAMS, _MEMBERS_RESULT,
    (_NOT_FOUND, _NOT_MEMBER, _NOT_AUTHORIZED, _AGENT_NOT_FOUND, _UNSUPPORTED, _UNAVAILABLE),
    mutates=True,
)
CONVERSATIONS_MEMBERS_REMOVE_SPEC = _spec(
    "conversations.members.remove", _REMOVE_PARAMS, _MEMBERS_RESULT,
    (_NOT_FOUND, _NOT_MEMBER, _NOT_AUTHORIZED, _UNAVAILABLE), mutates=True,
)
CONVERSATIONS_POST_SPEC = MethodSpec(
    name="conversations.post",
    owner="daemon.conversations",
    params_schema=_POST_PARAMS,
    result_schema=_POST_RESULT,
    errors=(*CALLER_ERRORS, _SUMMARY, _NOT_FOUND, _NOT_MEMBER, _UNAVAILABLE),
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator", "bound-agent"),
    http_exposed=True,
    http_binding=_HTTP,
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="group-conversation-key"),
    mock_echo={"idempotencyKey": "operationId"},
)


def conversations_create(
    app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None
) -> JsonObject:
    from hyprial.daemon.impl.application.messaging.inbox_surface.groups import create_group

    return create_group(app, dict(params), trusted_origin=trusted_message_origin)


def conversations_get(
    app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None
) -> JsonObject:
    from hyprial.daemon.impl.application.messaging.inbox_surface.groups import get_group

    return get_group(app, dict(params), trusted_origin=trusted_message_origin)


def conversations_members_add(
    app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None
) -> JsonObject:
    from hyprial.daemon.impl.application.messaging.inbox_surface.groups import add_member

    return add_member(app, dict(params), trusted_origin=trusted_message_origin)


def conversations_members_remove(
    app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None
) -> JsonObject:
    from hyprial.daemon.impl.application.messaging.inbox_surface.groups import remove_member

    return remove_member(app, dict(params), trusted_origin=trusted_message_origin)


def conversations_post(
    app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None
) -> JsonObject:
    return app._ipc_message_send(
        "message.send", dict(params), _trusted_message_origin=trusted_message_origin,
    )


GROUP_BINDINGS = (
    (CONVERSATIONS_CREATE_SPEC, conversations_create),
    (CONVERSATIONS_GET_SPEC, conversations_get),
    (CONVERSATIONS_MEMBERS_ADD_SPEC, conversations_members_add),
    (CONVERSATIONS_MEMBERS_REMOVE_SPEC, conversations_members_remove),
    (CONVERSATIONS_POST_SPEC, conversations_post),
)

__all__ = [
    "CONVERSATIONS_CREATE_SPEC",
    "CONVERSATIONS_GET_SPEC",
    "CONVERSATIONS_MEMBERS_ADD_SPEC",
    "CONVERSATIONS_MEMBERS_REMOVE_SPEC",
    "CONVERSATIONS_POST_SPEC",
    "GROUP_BINDINGS",
    "conversations_create",
    "conversations_get",
    "conversations_members_add",
    "conversations_members_remove",
    "conversations_post",
]
