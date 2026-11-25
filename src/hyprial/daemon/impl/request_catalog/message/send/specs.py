"""Message send declaration; HTTP narrows the stable key without changing IPC.

Decision (2026-11-05): the HTTP write profile admits only ``retry=never`` so a
lost response stays ``dispatch=unknown`` and is never retried automatically.
Legacy IPC keeps its handler-owned parameter admission.  The required
``idempotencyKey`` remains the durable business key used by the daemon and by
``message.send.key_lookup``; it is not an automatic HTTP retry permission.

Decision (UA2, 2026-11-18): a delivery to ``user:<owner>`` queued to this
node's user agent adds ``pushed`` (did the owner notification go out) and,
when it did not, ``pushCode``; ``accepted``/``queued`` stay the queue's
answer.  UA2b: only a push with nothing configured to push to answers
``pushed: false``; a transient or unknown push outcome raises its already
declared code (``USER_DELIVERY_TIMEOUT``, ``USER_DELIVERY_FAILED``,
``USER_DELIVERY_UNAVAILABLE``, ``SUBMIT_OUTCOME_UNKNOWN``) with the
per-target ``{target, messageId}`` after the message was queued.  UA2d:
an unclaimed push (``USER_DELIVERY_TIMEOUT``) while the daemon has no
evidence of any push destination also answers ``pushed: false`` with that
code (``send_targets/user.py``).  I3 and I7a (owner item A-7) superseded
the raise: a queued push is best-effort and answers ``pushed: false`` with
the code instead.

Decision (I4b, owner items Q0-a and Q0-d, 2026-11-23): the ``route:`` target
is gone, so the route-only answers leave the declaration: the frozen
``ROUTE_ADAPTER_UNCONFIGURED``, ``ROUTE_SEND_FAILED``,
``ROUTE_RESOURCES_UNSUPPORTED`` and ``ROUTE_RESOURCE_REPLY_UNSUPPORTED``, the
literals ``ROUTE_NOT_CONFIGURED``, ``ROUTE_FANOUT_MEMBER_INVALID`` and the
four ``ROUTE_APP_*``/``ROUTE_OPERATOR_NOT_IN_CHAT`` refusals, and the entry
fields ``routeDelivery``, ``chatId`` and ``fanoutOf``.  Attachments to a
target other than this node's ``user:<owner>`` are ``UNSUPPORTED_TARGET``;
with ``replyTo`` they are ``INVALID_ARGUMENT``.

Decision (owner, 2026-11-24): the channel capability had no entry point left
after I4b, so the frozen ``CHANNEL_NOT_AUTHORIZED`` leaves the declaration
and the code registry.

Decision (2026-11-06): ``message.send`` declares ``SUBMIT_OUTCOME_UNKNOWN``
with the same receipt identity data (``target``/``messageId``) as the other
receiver refusals.  A receiver answer whose outcome is unknown must keep
its code on the wire; ``DaemonMethodRegistry`` replaces an undeclared handler
code with ``DAEMON_ERROR``, erasing the one code clients use to distinguish a
possibly-delivered message from a plain internal failure.

Decision (2026-11-17, conversation plan slice C1): a send without a
``conversationId`` belongs to the default conversation of its sender and each
target (a derived ``dc.`` id, the same in both directions) instead of a fresh
random one.  The conversation is therefore per target: each ``deliveries``
entry carries its ``conversationId``; the top-level ``conversationId`` is the
explicit id when one was passed and otherwise the first target's, which for a
single-target send is that target's.  The delivery field is optional in the
schema because a result relayed from a daemon built before this change does
not carry it.

Decision (2026-11-18, conversation plan slice G0): the HTTP profile no longer
requires ``conversationId``.  It was required (2026-11-05, "match id scope")
so that a GUI whose response was lost could reconcile through
``message.send.lookup``, which selects the stored send by the original
``conversationId``; when every send carried a client-made id the client always
knew it.  A send without one is now stored under the pair's default
conversation, derived from the server-injected ``user:<owner>`` sender, and the
send response returns that id.  A caller that lost the response reconciles with
``message.send.key_lookup`` (by ``idempotencyKey`` alone), which returns the
stored ``conversationId``; ``message.send.lookup`` still requires the id.

Decision (2026-11-19, R2, design ``user-im-binding-removal-tier2`` 8.3): the
owner push is the hand-off to this node's user-proxy.  A queued ``user:``
entry carries ``pushed: true`` with ``via: "user-proxy"``, or ``pushed:
false`` with ``pushCode`` (``TARGET_UNRESOLVED``: no user-proxy here; a
sender bound to an adapter follows I3 below), and never ``receiverAdapter``.  The UA2d
unclaimed-push rule is gone with the receipt wait.  The agent bot's
``adapter`` field, its ledger shape ``{ledger, errorType}`` on
``USER_DELIVERY_UNAVAILABLE`` and the identity resolver's ``IDENTITY_*``
codes no longer occur on this method.  ``receiverAdapter``,
``nativeMessageId`` and ``duplicate`` stay for another owner's ``user:``
target, which keeps the Zenoh hand-off.

Decision (2026-11-23, I3, contract i7-0 v2 sections 3.2 to 3.4, owner items
A-6 and A-7): a sender whose cell has an adapter-role member is pushed by
the daemon to the chats its own cell names, closing KP-113.  The entry keeps
its scalar fields and gains values only: ``via`` may be ``"cell"``, and
``pushCode`` may accompany ``pushed: true`` when some chat was not
reached.  That copy is best-effort: one inline attempt, the queued send never
fails because of it, ``SUBMIT_OUTCOME_UNKNOWN`` can appear as a
``pushCode``, and nothing retries it.  A sender whose cell cannot be read
gets ``USER_DELIVERY_UNAVAILABLE`` and no hand-off.  The platform's
actionable data (scope list, authorization link) goes to the
``send.user_push`` log only.

Decision (2026-11-24, I7a, contract i7-0 v2 sections 3.2, 4 and 6, owner
items A-1, A-7 and A-8): the user-proxy hand-off of R2 is gone.  An unbound
sender (an agent without adapter members, the operator) is pushed by the
daemon to the targets of this node's user agent cell, ``via:
"user-agent"``; an empty user agent cell answers ``pushed: false`` with
``TARGET_UNRESOLVED``, and with nothing queued here and no user agent the
send is refused with ``TARGET_UNRESOLVED``.  ``via: "user-proxy"`` no
longer occurs, and the literal ``PROXY_SEND_REFUSED`` leaves the declared
errors with its producer.

Decision (2026-11-19, group conversation slice GC3): a ``conversationId``
that is a group id (``g.``) with no ``to`` sends to the group: one entry in
the group document, one delivery whose target is the group id, refused with
``CONVERSATION_NOT_FOUND`` / ``CONVERSATION_NOT_MEMBER``.  ``mentions``
(member ids) is new and, like ``replyTo``, IPC-only on this method.  The
HTTP profile is unchanged: the HTTP constraint projection takes ``type``,
``properties`` and ``required`` only and cannot say "``to``, or a group
id".  Since slice GH an HTTP client posts to a group with
``conversations.post`` (``request_catalog/conversations/groups``), which
runs this method's handler; this method's HTTP contract is byte for byte
the one before groups.
Decision (2026-11-21, Contract Mock key echo): ``message.send`` declares
``mock_echo={"idempotencyKey": "operationId"}`` and
``message.send.key_lookup`` declares ``mock_echo={"operationKey": "commandId"}``
so the Contract Mock echoes caller-minted keys on success examples without
hardcoding method names in the engine.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.identity import InvalidAgentNameError
from hyprial.kernel import (
    HttpBinding,
    IdentityBinding,
    IdempotencySpec,
    MethodError,
    MethodSpec,
    ipc_errors,
)

from ...address_errors import AGENT_ADDRESS_RETIRED_ERROR, TARGET_UNRESOLVED_ERROR

_STRING = {"type": "string"}
_NONEMPTY_STRING = {"type": "string", "minLength": 1}
_INTEGER = {"type": "integer"}
_STRINGS = {"type": "array", "items": _STRING}
_JSON_VALUE = {"type": ["string", "number", "boolean", "object", "array", "null"]}
_FORBIDDEN_VALUE = {**_JSON_VALUE, "x-hyprial-http-forbidden": True}
_AGENT_SEGMENT = r"(?:[^:\s]|[^:\s][^:]*[^:\s])"
_AGENT_URI_PATTERN = rf"^agent:{_AGENT_SEGMENT}:{_AGENT_SEGMENT}:{_AGENT_SEGMENT}$"


def _data(properties: Mapping[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object", "properties": dict(properties),
        "required": list(required), "additionalProperties": False,
    }


_TARGET_DATA = _data({"target": _STRING}, ("target",))
_SENDER_DATA = _data({"sender": _STRING}, ("sender",))
_CLAIMED_DATA = _data({"claimedIdentity": _STRING}, ("claimedIdentity",))
_AMBIGUOUS_DATA = {
    **_data({
        "target": _STRING, "alias": _STRING, "kind": _STRING,
        "candidates": _STRINGS,
    }, ("candidates",)),
    "oneOf": [
        {"type": "object", "required": ["target"]},
        {"type": "object", "required": ["alias", "kind"]},
    ],
}
_SUMMARY_DATA = _data({
    "field": {"const": "summary"}, "reason": _STRING,
    "maxBytes": _INTEGER, "summaryBytes": _INTEGER,
}, ("maxBytes",))
_RESOURCE_DATA = _data({
    "index": _INTEGER, "path": _STRING, "reason": _STRING,
    "kind": _JSON_VALUE, "sizeBytes": _INTEGER, "maxBytes": _INTEGER,
    "supportedExtensions": _STRINGS,
})
_USER_DELIVERY_DATA = _data({
    "target": _STRING, "messageId": _STRING,
}, ("target", "messageId"))
_GROUP_DATA = _data({"conversationId": _STRING}, ("conversationId",))
# Re-sync with release after #2547: message.send reaches TARGET_UNRESOLVED
# both from UUID address admission (release: {target}) and from the
# user-delivery receiver (cell model S10: {target, messageId}).  A spec
# declares a code once, so the one entry carries both shapes: target is
# always present, messageId only on the user-delivery path.
_TARGET_UNRESOLVED_SEND_ERROR = MethodError(
    ipc_errors.TARGET_UNRESOLVED,
    _data({"target": _STRING, "messageId": _STRING}, ("target",)),
    description=(
        TARGET_UNRESOLVED_ERROR.description
        + "  From user delivery (the owner has no configured receiver) the "
        "data also carries the per-target messageId."
    ),
)

_PARAMS = {
    "type": "object",
    "properties": {
        "actor": _JSON_VALUE,
        "from": _FORBIDDEN_VALUE,
        "sessionRef": _FORBIDDEN_VALUE,
        "onBehalfOf": _FORBIDDEN_VALUE,
        "to": _JSON_VALUE,
        "message": _JSON_VALUE,
        "summary": _JSON_VALUE,
        "conversationId": _JSON_VALUE,
        "idempotencyKey": _JSON_VALUE,
        "replyTo": _FORBIDDEN_VALUE,
        "topic": _FORBIDDEN_VALUE,
        "resourcePaths": _FORBIDDEN_VALUE,
        "providerMetadata": _FORBIDDEN_VALUE,
        "role": _FORBIDDEN_VALUE,
        "first_output_eta": _FORBIDDEN_VALUE,
        "human_gates": _FORBIDDEN_VALUE,
        "mentions": _FORBIDDEN_VALUE,
    },
    "additionalProperties": False,
}

_RESOURCE_DELIVERY = {
    "type": "object",
    "properties": {
        "kind": _NONEMPTY_STRING,
        "fileName": _NONEMPTY_STRING,
        "mediaType": _NONEMPTY_STRING,
        "sizeBytes": {"type": "integer", "minimum": 0},
        "fileType": _NONEMPTY_STRING,
        "fileKey": _NONEMPTY_STRING,
        "imageKey": _NONEMPTY_STRING,
        "nativeMessageId": _NONEMPTY_STRING,
    },
    "additionalProperties": False,
}

_DELIVERY = {
    "type": "object",
    "properties": {
        "target": _NONEMPTY_STRING,
        "messageId": _NONEMPTY_STRING,
        "conversationId": _NONEMPTY_STRING,
        "accepted": {"type": "boolean"},
        "queued": {"type": "boolean"},
        "receiverAdapter": {"type": "boolean"},
        "nativeMessageId": _NONEMPTY_STRING,
        "duplicate": {"type": "boolean"},
        "openRequestsFrom": {"type": "array", "items": _NONEMPTY_STRING},
        "outcomeKnown": {"type": "boolean"},
        "code": _NONEMPTY_STRING,
        "reason": _NONEMPTY_STRING,
        "resolutionReason": _NONEMPTY_STRING,
        "via": _NONEMPTY_STRING,
        "resourceDeliveries": {"type": "array", "items": _RESOURCE_DELIVERY},
        "pushed": {"type": "boolean"},
        "pushCode": _NONEMPTY_STRING,
    },
    "required": ["target", "messageId", "accepted", "queued"],
    "additionalProperties": False,
}

_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "actingPrincipal": _NONEMPTY_STRING,
        "operationId": _NONEMPTY_STRING,
        "conversationId": _NONEMPTY_STRING,
        "deliveries": {"type": "array", "minItems": 1, "items": _DELIVERY},
        "messageId": _NONEMPTY_STRING,
        "openRequestsFrom": {"type": "array", "items": _NONEMPTY_STRING},
        "replyPathUnavailable": {"type": "boolean"},
        "queued": {"type": "boolean"},
    },
    "required": ["ok", "actingPrincipal", "operationId", "conversationId", "deliveries"],
    "additionalProperties": False,
}

MESSAGE_SEND_SPEC = MethodSpec(
    name="message.send",
    owner="daemon.inbox",
    params_schema=_PARAMS,
    result_schema=_RESULT,
    errors=(
        MethodError(ipc_errors.INVALID_ARGUMENT, _data({
            "sender": _STRING, "target": _STRING, "messageId": _STRING,
        })),
        MethodError(ipc_errors.DAEMON_UNAVAILABLE),
        MethodError(ipc_errors.SUMMARY_REQUIRED, _SUMMARY_DATA),
        MethodError(ipc_errors.SENDER_UNRESOLVED, _SENDER_DATA),
        AGENT_ADDRESS_RETIRED_ERROR,
        _TARGET_UNRESOLVED_SEND_ERROR,
        MethodError(ipc_errors.SENDER_NOT_AUTHENTICATED, _CLAIMED_DATA),
        MethodError(ipc_errors.CALLER_NOT_AUTHORIZED, _SENDER_DATA),
        MethodError(ipc_errors.AMBIGUOUS_TARGET, _AMBIGUOUS_DATA),
        MethodError(ipc_errors.INVALID_AGENT_NAME),
        MethodError(ipc_errors.STALE_SESSION),
        MethodError(ipc_errors.SESSION_SUPERSEDED),
        MethodError(ipc_errors.INVALID_SESSION_SOURCE),
        MethodError(ipc_errors.TARGET_IS_NODE, _TARGET_DATA),
        MethodError(ipc_errors.UNSUPPORTED_TARGET, _TARGET_DATA),
        MethodError(ipc_errors.TARGET_NOT_AUTHORIZED, _data({
            "caller": _STRING, "target": _STRING, "capability": _STRING,
            "reason": _STRING,
        }, ("caller", "target", "capability", "reason"))),
        MethodError(ipc_errors.DISPATCH_ROLE_MISMATCH, _data({
            "target": _STRING, "role": _STRING,
        }, ("target", "role"))),
        MethodError(ipc_errors.MESSAGE_REPLY_UNAVAILABLE),
        MethodError(ipc_errors.USER_DELIVERY_UNAVAILABLE, _USER_DELIVERY_DATA),
        MethodError(ipc_errors.USER_DELIVERY_FAILED, _USER_DELIVERY_DATA),
        MethodError(ipc_errors.USER_DELIVERY_TIMEOUT, _USER_DELIVERY_DATA),
        MethodError(ipc_errors.SUBMIT_OUTCOME_UNKNOWN, _USER_DELIVERY_DATA),
        MethodError("USER_DELIVERY_OVERLOADED", _USER_DELIVERY_DATA),
        MethodError(ipc_errors.ATTACHMENT_PATH_REFUSED, _RESOURCE_DATA),
        MethodError("INVALID_RESOURCE_PATHS"),
        MethodError("RESOURCE_KIND_UNSUPPORTED", _RESOURCE_DATA),
        MethodError("RESOURCE_EMPTY", _RESOURCE_DATA),
        MethodError("RESOURCE_TOO_LARGE", _RESOURCE_DATA),
        MethodError("RESOURCE_NOT_FOUND", _RESOURCE_DATA),
        MethodError("RESOURCE_UNREADABLE", _RESOURCE_DATA),
        MethodError("RESOURCE_NOT_FILE", _RESOURCE_DATA),
        MethodError("RESOURCE_FILE_NAME_INVALID", _RESOURCE_DATA),
        MethodError("RESOURCE_IMAGE_TYPE_UNSUPPORTED", _RESOURCE_DATA),
        MethodError(ipc_errors.CONVERSATION_NOT_FOUND, _GROUP_DATA,
                    description="no readable chat space holds this group"),
        MethodError(ipc_errors.CONVERSATION_NOT_MEMBER, _GROUP_DATA,
                    description="the sender is not a current member of this group"),
    ),
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator", "bound-agent"),
    http_exposed=True,
    http_binding=HttpBinding(
        IdentityBinding("local-owner-user-selector", "actor"),
        readiness="inbox-service",
        params_constraints={
            "type": "object",
            "properties": {
                "to": {"type": "array", "minItems": 1, "maxItems": 1,
                       "items": {**_NONEMPTY_STRING, "pattern": _AGENT_URI_PATTERN}},
                "message": _NONEMPTY_STRING,
                "summary": {"type": "string", "minLength": 1, "pattern": r"\S"},
                "conversationId": {"type": "string", "minLength": 1, "pattern": r"\S"},
                "idempotencyKey": {
                    "type": "string", "minLength": 22, "maxLength": 128,
                    "pattern": r"^[A-Za-z0-9_-]+$",
                },
            },
            "required": ["to", "message", "summary", "idempotencyKey"],
        },
    ),
    confirmation="none",
    ipc_param_validation="handler",
    idempotency=IdempotencySpec(mode="never", scope="target-and-index"),
    mock_echo={"idempotencyKey": "operationId"},
)


def message_send(
    app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None,
) -> Any:
    return app._ipc_message_send(
        "message.send", dict(params), _trusted_message_origin=trusted_message_origin,
    )


MESSAGE_SEND_BINDINGS = ((MESSAGE_SEND_SPEC, message_send),)

_KEY_LOOKUP_PARAMS = {
    "type": "object",
    "properties": {
        "operationKey": {
            "type": "string", "minLength": 22, "maxLength": 128,
            "pattern": r"^[A-Za-z0-9_-]+$",
        },
        "actor": _NONEMPTY_STRING,
        "from": {**_NONEMPTY_STRING, "x-hyprial-http-forbidden": True},
        "sessionRef": {**_NONEMPTY_STRING, "x-hyprial-http-forbidden": True},
    },
    "required": ["operationKey"],
    "additionalProperties": False,
}
_KEY_LOOKUP_RESULT = {
    "type": "object",
    "properties": {
        "status": {"enum": ["accepted", "unknown"]},
        "commandId": _NONEMPTY_STRING,
        "conversationId": _NONEMPTY_STRING,
        "messageId": _NONEMPTY_STRING,
        "reason": {"enum": ["not-found", "invalid-record", "unavailable"]},
    },
    "additionalProperties": False,
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "status": {"const": "accepted"},
                "commandId": _NONEMPTY_STRING,
                "conversationId": _NONEMPTY_STRING,
                "messageId": _NONEMPTY_STRING,
            },
            "required": ["status", "commandId", "conversationId", "messageId"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "status": {"const": "unknown"},
                "reason": {"enum": ["not-found", "invalid-record", "unavailable"]},
            },
            "required": ["status", "reason"],
            "additionalProperties": False,
        },
    ],
}

MESSAGE_SEND_KEY_LOOKUP_SPEC = MethodSpec(
    name="message.send.key_lookup",
    owner="daemon.inbox",
    params_schema=_KEY_LOOKUP_PARAMS,
    result_schema=_KEY_LOOKUP_RESULT,
    errors=(
        MethodError(ipc_errors.INVALID_ARGUMENT, _data({"sender": _STRING})),
        MethodError(ipc_errors.SENDER_UNRESOLVED, _SENDER_DATA),
        AGENT_ADDRESS_RETIRED_ERROR,
        MethodError(ipc_errors.SENDER_NOT_AUTHENTICATED, _CLAIMED_DATA),
        MethodError(InvalidAgentNameError.code),
        MethodError(ipc_errors.STALE_SESSION),
        MethodError(ipc_errors.SESSION_SUPERSEDED),
        MethodError(ipc_errors.INVALID_SESSION_SOURCE),
    ),
    mode="unary",
    mutates=False,
    retry="safe",
    audience=("local-operator", "bound-agent"),
    http_exposed=True,
    http_binding=HttpBinding(
        IdentityBinding("local-owner-user-selector", "actor"),
        readiness="inbox-service",
    ),
    confirmation="none",
    ipc_param_validation="handler",
    idempotency=IdempotencySpec(mode="safe", scope="read-only-send-key-lookup"),
    mock_echo={"operationKey": "commandId"},
)


def message_send_key_lookup(
    app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None,
) -> Any:
    return app._ipc_message_send_key_lookup(
        "message.send.key_lookup", dict(params),
        _trusted_message_origin=trusted_message_origin,
    )


MESSAGE_SEND_KEY_LOOKUP_BINDINGS = ((MESSAGE_SEND_KEY_LOOKUP_SPEC, message_send_key_lookup),)
