"""Strict schemas for the M2 onboarding and local-auth declaration package.

Decision: gui-m2-onboarding-contracts-20261105.  These schemas describe the
public browser/CLI contract only.  Caller identity, home, view, generation and
control-session ownership are never accepted as business params; they come from
the daemon's trusted control context and are enforced by the state module.

Decision: daemon-first-z5-first-login-20261118.  ``COMPLETION`` is the daemon's
own projection of a login it drives (``auth.login.begin`` with ``until:
serving``).  It is not journaled (the three receipts stay the durable truth), is a
snapshot taken at the end of the login (``onboarding.status`` is the source of
truth for the device afterwards), and never carries a device code, a token, key
material, exception text, a pid or an epoch (``ping`` has those).  It is an
optional property added IN PLACE to the begin result and to the login operation.
It appears only on logins started with ``until: serving`` (a request parameter
that did not exist before) and is visible to every reader of that operation, for
example a GUI polling a login the CLI started.  A client that only starts logins
without ``until`` and never reads another client's login never receives it; the
compatibility judgement rests on the repository's additive rule for optional
response properties, not on a claim that no older client can ever see it.  The
contract-diff tool calls the in-place change to the ``oneOf`` variant BREAKING
(it treats any changed variant as such); the PR declares that finding rather than
reshaping the schema around it.  ``waitSeconds`` is honoured only on the daemon's
own wire: it is ``x-hyprial-http-forbidden``, so the HTTP projection (and the GUI
forwarding host, which holds its own lock while it forwards) cannot send it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from hyprial.identity import (
    CREDENTIAL_ARTIFACT_LABEL,
    DIFFERENT_ACCOUNT_REASON,
    LEGACY_UNBOUND_REASON,
)

AUTH_STATES = (
    "pending",
    "authenticated",
    "denied",
    "expired",
    "cancelled",
    "failed",
    "unknown",
)
OPERATION_STATES = (
    "pending",
    "authenticated",
    "denied",
    "expired",
    "cancelled",
    "accepted",
    "promoting",
    "completed",
    "ready",
    "failed",
    "unknown",
)
CANCEL_STATES = AUTH_STATES + (
    "accepted",
    "promoting",
    "completed",
)
INITIALIZE_STATES = (
    "accepted",
    "promoting",
    "cancelled",
    "completed",
    "failed",
    "unknown",
)
OPERATION_KINDS = ("login", "register", "device", "initialize", "logout")
LOGOUT_STATES = ("accepted", "completed", "failed", "unknown")
# The only artifact labels logout may claim to have removed.  They mirror the
# login-owned state a local logout already owns (see the shell login session
# removal primitive); device material, agent secrets and grants are absent by
# construction, so a receipt cannot claim to have removed them.
LOGOUT_REMOVED_LABELS = (
    # The identity domain owns the credential artifact name; the daemon tree
    # never spells it (T11) and only mirrors the receipt label here.
    CREDENTIAL_ARTIFACT_LABEL,
    "settings.identityIssuer",
    "settings.identityMode",
    "settings.owner",
    "settings.userId",
    "settings.username",
)
LOGOUT_NEXT_ACTIONS = ("poll", "login", "inspect")
LOGOUT_FAILURE_REASONS = (
    "removal-failed",
    "identity-changed",
    "ownership-lost",
)
LOGOUT_UNKNOWN_REASONS = (
    "removal-unconfirmed",
    "reply-lost",
    "supervisor-lost",
)
NEXT_ACTIONS = ("login", "register", "device.ensure", "initialize")
FIRST_RUN_STATES = (
    "none",
    "recorded",
    "corrupt",
    "unsupported-version",
    "unavailable",
)
FIRST_RUN_CONFIRMED_STEPS = ("configuration", "reply")
CANCEL_REASONS = (
    "cancelled",
    "already-cancelled",
    "identity-committed",
    "not-cancellable",
    "outcome-unknown",
)

NONEMPTY = {
    "type": "string",
    "minLength": 1,
    "maxLength": 512,
    "pattern": r"\S",
}
OPAQUE_ID = {
    "type": "string",
    "minLength": 8,
    "maxLength": 200,
    "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
}
OPERATION_KEY = {
    "type": "string",
    "minLength": 16,
    "maxLength": 128,
    "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
}
OWNER = {
    "type": "string",
    "minLength": 3,
    "maxLength": 320,
    "pattern": r"\S",
}
USERNAME = {
    "type": "string",
    "minLength": 1,
    "maxLength": 320,
    "pattern": r"\S",
}
DEVICE_ID = {
    "type": "string",
    "minLength": 8,
    "maxLength": 200,
    "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
}


def nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {**schema, "type": [schema["type"], "null"]}


NULL_OWNER = nullable(OWNER)
NULL_USERNAME = nullable(USERNAME)
NULL_OPAQUE_ID = nullable(OPAQUE_ID)


def record(
    properties: dict[str, Any], required: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _if(prop: str, value: Any, then: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "if": {
            "type": "object",
            "properties": {prop: {"const": value}},
            "required": [prop],
        },
        "then": {"type": "object", "properties": then},
    }


VERIFICATION = record(
    {
        "verificationUri": {
            "type": "string",
            "format": "uri",
            "minLength": 8,
            "maxLength": 2048,
        },
        "userCode": {
            "type": "string",
            "minLength": 4,
            "maxLength": 64,
            "pattern": r"\S",
        },
        "expiresAt": {"type": "string", "format": "date-time"},
        "intervalSeconds": {
            "type": "integer",
            "minimum": 1,
            "maximum": 3600,
        },
    },
    ("verificationUri", "userCode", "expiresAt", "intervalSeconds"),
)
NULLABLE_VERIFICATION = {
    "type": ["object", "null"],
    "properties": VERIFICATION["properties"],
    "required": VERIFICATION["required"],
    "additionalProperties": False,
}

REMOVED_LABELS = {
    "type": "array",
    "items": {"type": "string", "enum": list(LOGOUT_REMOVED_LABELS)},
    "uniqueItems": True,
    "maxItems": len(LOGOUT_REMOVED_LABELS),
}

LOGOUT_FAILURE = record(
    {"reason": {"type": "string", "enum": list(LOGOUT_FAILURE_REASONS)}},
    ("reason",),
)
NULLABLE_LOGOUT_FAILURE = {
    "type": ["object", "null"],
    "properties": LOGOUT_FAILURE["properties"],
    "required": LOGOUT_FAILURE["required"],
    "additionalProperties": False,
}

COMPLETION_STAGES = ("authorizing", "device", "starting", "serving", "stopped")
COMPLETION_RETRY = "serving-start"
_SHORT = {"type": "string", "minLength": 1, "maxLength": 64}
_MESSAGE = {"type": "string", "minLength": 1, "maxLength": 1000}
COMPLETION_FAILURE = record(
    {"code": _SHORT, "message": _MESSAGE}, ("code", "message")
)
OPERATION_ERROR_DATA = record(
    {
        "reason": {
            "type": "string",
            "enum": [DIFFERENT_ACCOUNT_REASON, LEGACY_UNBOUND_REASON],
        },
        "guide": NONEMPTY,
        "loginUserId": NONEMPTY,
        "issuer": NONEMPTY,
    },
    ("reason", "guide", "loginUserId", "issuer"),
)
OPERATION_ERROR = record(
    {
        "code": _SHORT,
        "message": _MESSAGE,
        "data": OPERATION_ERROR_DATA,
    },
    ("code", "message"),
)
NULLABLE_OPERATION_ERROR = {
    "type": ["object", "null"],
    "properties": OPERATION_ERROR["properties"],
    "required": OPERATION_ERROR["required"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class OperationError:
    """Structured error projected onto a terminal failed auth operation."""

    code: str
    message: str
    data: Mapping[str, str] | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"code": self.code, "message": self.message}
        if self.data:
            payload["data"] = dict(self.data)
        return payload

COMPLETION = record(
    {
        "stage": {"type": "string", "enum": list(COMPLETION_STAGES)},
        "identity": {
            "type": ["object", "null"],
            "properties": {
                "mode": {"type": ["string", "null"], "maxLength": 64},
                "issuer": {"type": ["string", "null"], "maxLength": 2048},
                "source": {"type": ["string", "null"], "maxLength": 4096},
            },
            "required": ["mode", "issuer", "source"],
            "additionalProperties": False,
        },
        "device": {
            "type": ["object", "null"],
            "properties": {
                "ready": {"type": "boolean"},
                "deviceId": nullable(DEVICE_ID),
                "keyGeneration": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "maximum": 2**31 - 1,
                },
                "userKey": record(
                    {"state": _SHORT, "code": nullable(_SHORT)}, ("state", "code")
                ),
                "error": {
                    "type": ["object", "null"],
                    "properties": COMPLETION_FAILURE["properties"],
                    "required": COMPLETION_FAILURE["required"],
                    "additionalProperties": False,
                },
                "retry": {
                    "type": ["string", "null"],
                    "enum": [COMPLETION_RETRY, None],
                },
            },
            "required": [
                "ready",
                "deviceId",
                "keyGeneration",
                "userKey",
                "error",
                "retry",
            ],
            "additionalProperties": False,
        },
        "failure": {
            "type": ["object", "null"],
            "properties": COMPLETION_FAILURE["properties"],
            "required": COMPLETION_FAILURE["required"],
            "additionalProperties": False,
        },
    },
    ("stage", "identity", "device", "failure"),
)

AUTH_LOGIN_OPERATION = record(
    {
        "operationId": OPAQUE_ID,
        "kind": {"type": "string", "enum": ["login", "register"]},
        "state": {"type": "string", "enum": list(AUTH_STATES)},
        "owner": NULL_OWNER,
        "receiptId": NULL_OPAQUE_ID,
        "verification": NULLABLE_VERIFICATION,
        "completion": COMPLETION,
        "error": NULLABLE_OPERATION_ERROR,
    },
    ("operationId", "kind", "state", "owner", "receiptId", "verification"),
)

# Polling variant: accepted logout is not cancellable, already-logged-out homes
# complete with an empty ``removed`` list, and an unknown outcome is neither a
# rollback nor a success.  ``failure`` is the strict, secret-free definite
# failure projection a supervisor may publish; it never carries issuer data.
LOGOUT_OPERATION = record(
    {
        "operationId": OPAQUE_ID,
        "kind": {"const": "logout"},
        "state": {"type": "string", "enum": list(LOGOUT_STATES)},
        "owner": NULL_OWNER,
        "receiptId": OPAQUE_ID,
        "loggedOut": {"type": "boolean"},
        "removed": REMOVED_LABELS,
        "failure": NULLABLE_LOGOUT_FAILURE,
    },
    (
        "operationId",
        "kind",
        "state",
        "owner",
        "receiptId",
        "loggedOut",
        "removed",
    ),
)

AUTH_OPERATION = {"oneOf": [AUTH_LOGIN_OPERATION, LOGOUT_OPERATION]}

STATUS_OPERATION = record(
    {
        "operationId": OPAQUE_ID,
        "kind": {"type": "string", "enum": list(OPERATION_KINDS)},
        "state": {"type": "string", "enum": list(OPERATION_STATES)},
        "receiptId": NULL_OPAQUE_ID,
    },
    ("operationId", "kind", "state", "receiptId"),
)

FIRST_RUN_STATUS = record(
    {
        "state": {"type": "string", "enum": list(FIRST_RUN_STATES)},
        "revision": {
            "type": "integer",
            "minimum": 0,
            "maximum": 2**31 - 1,
        },
        "confirmed": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": list(FIRST_RUN_CONFIRMED_STEPS),
            },
            "uniqueItems": True,
            "maxItems": len(FIRST_RUN_CONFIRMED_STEPS),
        },
        "updatedAt": {
            "type": ["string", "null"],
            "format": "date-time",
        },
        "skipped": {"type": "boolean"},
    },
    ("state", "revision", "confirmed", "updatedAt"),
)

FIRST_RUN_ATTEMPT_STATES = ("pending", "bound", "confirmed", "cancelled", "superseded")
FIRST_RUN_CREDENTIAL_KINDS = ("key", "native")
FIRST_RUN_ATTEMPT_STATUS = record(
    {
        "attemptId": OPAQUE_ID,
        "revision": {"type": "integer", "minimum": 1, "maximum": 2**31 - 1},
        "state": {"type": "string", "enum": list(FIRST_RUN_ATTEMPT_STATES)},
        "operationKey": OPERATION_KEY,
        "sendKey": OPERATION_KEY,
        "receiptRevision": {"type": "integer", "minimum": 0, "maximum": 2**31 - 1},
        "loginEpoch": {"type": "integer", "minimum": 1, "maximum": 2**53 - 1},
        "agentUri": NONEMPTY,
        "agentCreatedAtMs": {"type": "integer", "minimum": 0, "maximum": 2**53 - 1},
        "provider": nullable(NONEMPTY),
        "model": nullable(NONEMPTY),
        "harness": NONEMPTY,
        "configRevision": nullable(NONEMPTY),
        "credentialKind": {"type": "string", "enum": list(FIRST_RUN_CREDENTIAL_KINDS)},
        "requestMessageId": NULL_OPAQUE_ID,
        "conversationId": NULL_OPAQUE_ID,
        "replyMessageId": NULL_OPAQUE_ID,
    },
    (
        "attemptId",
        "revision",
        "state",
        "operationKey",
        "sendKey",
        "receiptRevision",
        "loginEpoch",
        "agentUri",
        "agentCreatedAtMs",
        "provider",
        "model",
        "harness",
        "configRevision",
        "credentialKind",
        "requestMessageId",
        "conversationId",
        "replyMessageId",
    ),
)

NULL_FIRST_RUN_ATTEMPT = {
    "oneOf": [FIRST_RUN_ATTEMPT_STATUS, {"type": "null"}]
}

FIRST_RUN_BEGIN_PARAMS = record(
    {
        "operationKey": OPERATION_KEY,
        "agentUri": NONEMPTY,
    },
    ("operationKey", "agentUri"),
)

FIRST_RUN_BIND_PARAMS = record(
    {
        "attemptId": OPAQUE_ID,
        "expectedRevision": {"type": "integer", "minimum": 1, "maximum": 2**31 - 1},
        "operationKey": OPERATION_KEY,
        "conversationId": OPAQUE_ID,
        "requestMessageId": OPAQUE_ID,
    },
    (
        "attemptId",
        "expectedRevision",
        "operationKey",
        "conversationId",
        "requestMessageId",
    ),
)

FIRST_RUN_CONFIRM_PARAMS = record(
    {
        "attemptId": OPAQUE_ID,
        "expectedAttemptRevision": {
            "type": "integer",
            "minimum": 1,
            "maximum": 2**31 - 1,
        },
        "expectedReceiptRevision": {
            "type": "integer",
            "minimum": 0,
            "maximum": 2**31 - 1,
        },
        "operationKey": OPERATION_KEY,
    },
    ("attemptId", "expectedAttemptRevision", "expectedReceiptRevision", "operationKey"),
)

FIRST_RUN_CANCEL_PARAMS = record(
    {
        "attemptId": OPAQUE_ID,
        "expectedRevision": {"type": "integer", "minimum": 1, "maximum": 2**31 - 1},
        "operationKey": OPERATION_KEY,
    },
    ("attemptId", "expectedRevision", "operationKey"),
)

FIRST_RUN_SKIP_PARAMS = record(
    {
        "expectedRevision": {"type": "integer", "minimum": 0, "maximum": 2**31 - 1},
        "operationKey": OPERATION_KEY,
    },
    ("expectedRevision", "operationKey"),
)

FIRST_RUN_ATTEMPT_RESULT = record(
    {"ok": {"const": True}, "attempt": FIRST_RUN_ATTEMPT_STATUS},
    ("ok", "attempt"),
)

FIRST_RUN_CONFIRM_RESULT = record(
    {
        "ok": {"const": True},
        "attempt": FIRST_RUN_ATTEMPT_STATUS,
        "firstRun": FIRST_RUN_STATUS,
        "requestMessageId": OPAQUE_ID,
        "replyMessageId": OPAQUE_ID,
    },
    ("ok", "attempt", "firstRun", "requestMessageId", "replyMessageId"),
)

FIRST_RUN_SKIP_RESULT = record(
    {"ok": {"const": True}, "firstRun": FIRST_RUN_STATUS},
    ("ok", "firstRun"),
)

STATUS_RESULT = {
    **record(
        {
            "ok": {"const": True},
            "phase": {"type": "string", "enum": ["bootstrap", "serving"]},
            "authenticated": {"type": "boolean"},
            "owner": NULL_OWNER,
            "username": NULL_USERNAME,
            "deviceReady": {"type": "boolean"},
            "initialized": {"type": "boolean"},
            "operations": {
                "type": "array",
                "items": STATUS_OPERATION,
                "maxItems": 64,
            },
            "nextActions": {
                "type": "array",
                "items": {"type": "string", "enum": list(NEXT_ACTIONS)},
                "uniqueItems": True,
                "maxItems": 4,
            },
            "firstRun": FIRST_RUN_STATUS,
            "firstRunAttempt": NULL_FIRST_RUN_ATTEMPT,
        },
        (
            "ok",
            "phase",
            "authenticated",
            "owner",
            "deviceReady",
            "initialized",
            "operations",
            "nextActions",
        ),
    ),
    "allOf": [
        _if("authenticated", False, {"owner": {"type": "null"}}),
        _if(
            "initialized",
            True,
            {"nextActions": {"type": "array", "maxItems": 0}},
        ),
    ],
}

_LOGIN_BEGIN_PROPERTIES = {
    "ok": {"const": True},
    "operationId": OPAQUE_ID,
    "intent": {"type": "string", "enum": ["login", "register"]},
    "state": {"type": "string", "enum": list(AUTH_STATES)},
    "verification": NULLABLE_VERIFICATION,
    "owner": NULL_OWNER,
}
_LOGIN_BEGIN_REQUIRED = (
    "ok",
    "operationId",
    "intent",
    "state",
    "verification",
    "owner",
)
LOGIN_BEGIN_RESULT = record(
    {**_LOGIN_BEGIN_PROPERTIES, "completion": COMPLETION}, _LOGIN_BEGIN_REQUIRED
)

# Same-owner reauthentication is a login-begin with one fixed intent.  The
# daemon binds the owner; the wire can never select ``register`` here.
RELOGIN_RESULT = {
    **record(
        {**_LOGIN_BEGIN_PROPERTIES, "completion": COMPLETION}, _LOGIN_BEGIN_REQUIRED
    ),
    "allOf": [
        {
            "type": "object",
            "properties": {"intent": {"const": "login"}},
            "required": ["intent"],
        }
    ],
}

LOGOUT_RESULT = {
    **record(
        {
            "ok": {"const": True},
            "operationId": OPAQUE_ID,
            "receiptId": OPAQUE_ID,
            "state": {"type": "string", "enum": list(LOGOUT_STATES)},
            "loggedOut": {"type": "boolean"},
            "removed": REMOVED_LABELS,
            # Account logout deliberately does not claim issuer-wide revocation.
            "issuerRevocation": {"const": "not-attempted"},
            "nextAction": {"type": "string", "enum": list(LOGOUT_NEXT_ACTIONS)},
        },
        (
            "ok",
            "operationId",
            "receiptId",
            "state",
            "loggedOut",
            "removed",
            "issuerRevocation",
            "nextAction",
        ),
    ),
    "allOf": [
        _if(
            "state",
            "accepted",
            {
                "loggedOut": {"const": False},
                "removed": {"type": "array", "maxItems": 0},
                "nextAction": {"const": "poll"},
            },
        ),
        _if(
            "state",
            "completed",
            {
                "loggedOut": {"const": True},
                "nextAction": {"const": "login"},
            },
        ),
        _if(
            "state",
            "failed",
            {"loggedOut": {"const": False}, "nextAction": {"const": "inspect"}},
        ),
        _if(
            "state",
            "unknown",
            {"loggedOut": {"const": False}, "nextAction": {"const": "inspect"}},
        ),
    ],
}

# Strict data for a definite or unknown logout outcome.  When an identifier is
# not known it is null rather than omitted, so a consumer cannot mistake a
# missing field for "no side effect happened".
LOGOUT_ERROR_DATA = record(
    {
        "operationId": NULL_OPAQUE_ID,
        "receiptId": NULL_OPAQUE_ID,
        "identityRemoved": {"type": "string", "enum": ["yes", "no", "unknown"]},
        "daemonStopped": {"type": "string", "enum": ["yes", "no", "unknown"]},
        "reason": {"type": "string", "enum": list(LOGOUT_UNKNOWN_REASONS)},
    },
    (
        "operationId",
        "receiptId",
        "identityRemoved",
        "daemonStopped",
        "reason",
    ),
)

AUTH_GET_RESULT = record(
    {"ok": {"const": True}, "operation": AUTH_OPERATION},
    ("ok", "operation"),
)

CANCEL_RESULT = {
    **record(
        {
            "ok": {"const": True},
            "operationId": OPAQUE_ID,
            "state": {"type": "string", "enum": list(CANCEL_STATES)},
            "cancelled": {"type": "boolean"},
            "reason": {"type": "string", "enum": list(CANCEL_REASONS)},
        },
        ("ok", "operationId", "state", "cancelled", "reason"),
    ),
    "allOf": [
        _if(
            "cancelled",
            True,
            {
                "state": {"const": "cancelled"},
                "reason": {
                    "type": "string",
                    "enum": ["cancelled", "already-cancelled"],
                },
            },
        ),
        _if(
            "cancelled",
            False,
            {
                "state": {"type": "string", "not": {"const": "cancelled"}},
                "reason": {
                    "type": "string",
                    "enum": [
                        "identity-committed",
                        "not-cancellable",
                        "outcome-unknown",
                    ],
                },
            },
        ),
    ],
}

DEVICE_ERROR_DATA = record(
    {
        "operationId": OPAQUE_ID,
        "receiptId": NULL_OPAQUE_ID,
        "identityCommitted": {"const": True},
        "deviceReady": {"type": "boolean"},
        "retryable": {"type": "boolean"},
        "reason": {
            "type": "string",
            "enum": ["device-unavailable", "outcome-unknown"],
        },
    },
    (
        "operationId",
        "receiptId",
        "identityCommitted",
        "deviceReady",
        "retryable",
        "reason",
    ),
)

DEVICE_RESULT = record(
    {
        "ok": {"const": True},
        "operationId": OPAQUE_ID,
        "receiptId": OPAQUE_ID,
        "identityCommitted": {"const": True},
        "deviceReady": {"const": True},
        "deviceId": DEVICE_ID,
        "retryable": {"const": False},
    },
    (
        "ok",
        "operationId",
        "receiptId",
        "identityCommitted",
        "deviceReady",
        "deviceId",
        "retryable",
    ),
)

INITIALIZE_RESULT = {
    **record(
        {
            "ok": {"const": True},
            "operationId": OPAQUE_ID,
            "receiptId": OPAQUE_ID,
            "state": {
                "type": "string",
                "enum": list(INITIALIZE_STATES),
            },
            "initialized": {"type": "boolean"},
            "cancelable": {"type": "boolean"},
        },
        (
            "ok",
            "operationId",
            "receiptId",
            "state",
            "initialized",
            "cancelable",
        ),
    ),
    "allOf": [
        _if(
            "state",
            "accepted",
            {
                "initialized": {"const": False},
                "cancelable": {"const": True},
            },
        ),
        _if(
            "state",
            "completed",
            {
                "initialized": {"const": True},
                "cancelable": {"const": False},
            },
        ),
        _if(
            "state",
            "promoting",
            {
                "initialized": {"const": False},
                "cancelable": {"const": False},
            },
        ),
        _if(
            "state",
            "cancelled",
            {
                "initialized": {"const": False},
                "cancelable": {"const": False},
            },
        ),
        _if(
            "state",
            "failed",
            {
                "initialized": {"const": False},
                "cancelable": {"const": False},
            },
        ),
        _if(
            "state",
            "unknown",
            {
                "initialized": {"const": False},
                "cancelable": {"const": False},
            },
        ),
        _if(
            "cancelable",
            True,
            {"state": {"const": "accepted"}},
        ),
        _if(
            "initialized",
            True,
            {"state": {"const": "completed"}},
        ),
    ],
}

STATUS_PARAMS = record({})
LOGIN_BEGIN_PARAMS = record(
    {
        "intent": {"type": "string", "enum": ["login", "register"]},
        "operationKey": OPERATION_KEY,
        "until": {"type": "string", "enum": ["authenticated", "serving"]},
    },
    ("intent", "operationKey"),
)
OPERATION_PARAMS = record({"operationId": OPAQUE_ID}, ("operationId",))
AUTH_GET_PARAMS = record(
    {
        "operationId": OPAQUE_ID,
        "waitSeconds": {
            "type": "number",
            "minimum": 0,
            "maximum": 900,
            "x-hyprial-http-forbidden": True,
        },
    },
    ("operationId",),
)
OPERATION_KEY_PARAMS = record({"operationKey": OPERATION_KEY}, ("operationKey",))
RELOGIN_PARAMS = record(
    {
        "operationKey": OPERATION_KEY,
        "until": {"type": "string", "enum": ["authenticated", "serving"]},
    },
    ("operationKey",),
)
# Account logout is confirmed by a literal, not by the generic preview flow.
#: ``expectedEpoch`` is optional: a caller that believes it is talking to a
#: specific daemon generation can require it, and a mismatch is refused before
#: any identity work.
LOGOUT_PARAMS = record(
    {
        "operationKey": OPERATION_KEY,
        "confirm": {"const": True},
        "expectedEpoch": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "pattern": r"\S",
        },
    },
    ("operationKey", "confirm"),
)
