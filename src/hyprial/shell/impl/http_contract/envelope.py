"""Canonical HTTP transport contract: envelope, authentication and dispatch.

One source of truth for the parts the canonical OpenAPI document and the live
request-http transport must agree on.  This module reads no home, identity,
clock, socket or environment: the offline exporter and the served schema call
the same functions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from hyprial.kernel import ipc_errors

HTTP_CONTRACT_VERSION: Final = 1
ENVELOPE_SCHEMA_VERSION: Final = 1
OPENAPI_VERSION: Final = "3.1.0"
JSON_SCHEMA_DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"
BEARER_SCHEME: Final = "bearerAuth"
DISPATCH_STATES: Final = ("not-dispatched", "dispatched", "unknown")
READ_RETRY: Final = "safe"
WRITE_RETRY: Final = "never"
MAX_MESSAGE_CHARS: Final = 512

TITLE: Final = "Hyprial HTTP request entry"
DESCRIPTION: Final = (
    "Local-operator HTTP request entry. Every operation carries the same typed "
    "transport envelope and requires the server-configured bearer token. The "
    "bearer authenticates the caller to the transport; ordinary business target "
    "fields remain declared params, and daemon authorization remains authoritative. "
    "Per-method membership and business JSON Schemas come from the daemon "
    "catalog, never from this document's hand-maintained list."
)


@dataclass(frozen=True, slots=True)
class TransportError:
    """One stable HTTP error code, its status and the stage that raises it."""

    code: str
    status: int
    description: str


# Mirrors the transport vocabulary in the H-01 entry; H-03 reuses it so the
# canonical OpenAPI and the served document cannot drift by accident.
_TRANSPORT_ERRORS: tuple[TransportError, ...] = (
    TransportError(ipc_errors.INVALID_ARGUMENT, 400, "params do not match the declared schema"),
    TransportError(ipc_errors.INVALID_REQUEST, 400, "the request framing is invalid"),
    TransportError(ipc_errors.INVALID_PARAMS, 400, "the daemon rejected the declared params"),
    TransportError(
        "HTTP_IDEMPOTENCY_KEY_UNSUPPORTED",
        400,
        "this entry does not implement idempotency keys",
    ),
    TransportError("HTTP_UNAUTHORIZED", 401, "the bearer token is missing or wrong"),
    TransportError("HTTP_HOST_DENIED", 403, "the Host header is not allowed"),
    TransportError("HTTP_ORIGIN_DENIED", 403, "the Origin is not allowed"),
    TransportError("PRINCIPAL_MISMATCH", 403, "a caller selector does not match"),
    TransportError(ipc_errors.CALLER_NOT_AUTHORIZED, 403, "the daemon refused the caller"),
    TransportError(ipc_errors.SENDER_NOT_AUTHENTICATED, 403, "the sender is not authenticated"),
    TransportError(ipc_errors.SENDER_UNRESOLVED, 403, "the sender cannot be resolved"),
    TransportError(ipc_errors.TARGET_NOT_AUTHORIZED, 403, "the daemon refused the target"),
    TransportError(
        ipc_errors.ORG_CONTEXT_NOT_AUTHORIZED, 403, "the daemon refused the org context"
    ),
    TransportError(ipc_errors.STALE_SESSION, 403, "the session is stale"),
    TransportError(ipc_errors.SESSION_SUPERSEDED, 403, "the session was superseded"),
    TransportError(ipc_errors.INVALID_SESSION_SOURCE, 403, "the session source is invalid"),
    TransportError(ipc_errors.METHOD_NOT_FOUND, 404, "the method is unknown or unexposed"),
    TransportError("HTTP_METHOD_NOT_ALLOWED", 405, "the HTTP method is not allowed"),
    TransportError("HTTP_TIMEOUT", 408, "the request budget expired"),
    TransportError("CATALOG_MISMATCH", 409, "the catalog or a digest does not match"),
    TransportError(ipc_errors.VERSION_MISMATCH, 409, "the daemon version does not match"),
    TransportError("HTTP_LENGTH_REQUIRED", 411, "Content-Length is required"),
    TransportError("HTTP_BODY_TOO_LARGE", 413, "the request body exceeds the budget"),
    TransportError("HTTP_URI_TOO_LONG", 414, "the request target is too long"),
    TransportError(
        "HTTP_UNSUPPORTED_MEDIA_TYPE", 415, "the media type is not supported"
    ),
    TransportError("HTTP_BUSY", 429, "the concurrency budget is exhausted"),
    TransportError("HTTP_HEADERS_TOO_LARGE", 431, "the headers exceed the budget"),
    TransportError("HTTP_INTERNAL", 500, "the transport failed internally"),
    TransportError(ipc_errors.DAEMON_ERROR, 500, "the daemon reported an undeclared failure"),
    TransportError("INVALID_RESPONSE", 502, "the daemon response is invalid"),
    TransportError("IPC_RESPONSE_TOO_LARGE", 502, "the daemon response is too large"),
    TransportError("HTTP_NOT_READY", 503, "the method is not ready"),
    TransportError("CATALOG_STALE", 503, "the served catalog is stale"),
    TransportError(ipc_errors.DAEMON_UNAVAILABLE, 503, "the daemon is unavailable"),
    TransportError(ipc_errors.DAEMON_RESTORING, 503, "the daemon is restoring"),
    TransportError(ipc_errors.DAEMON_NOT_READY, 503, "the daemon is not ready"),
    TransportError(ipc_errors.DAEMON_DISCONNECTED, 503, "the daemon disconnected"),
    TransportError(ipc_errors.IPC_TIMEOUT, 504, "the daemon did not answer in time"),
)

TRANSPORT_ERRORS: Final = tuple(
    sorted(_TRANSPORT_ERRORS, key=lambda item: (item.status, item.code))
)
_ERRORS_BY_STATUS: Final = MappingProxyType(
    {
        status: tuple(item for item in TRANSPORT_ERRORS if item.status == status)
        for status in sorted({item.status for item in TRANSPORT_ERRORS})
    }
)

STATUS_DESCRIPTIONS: Final = MappingProxyType(
    {
        200: "Successful invocation: the declared result in the success envelope.",
        400: (
            "Local schema/framing refusal (not dispatched) or a declared daemon "
            "argument error after dispatch; read dispatch from the envelope."
        ),
        401: "Missing or wrong server-configured bearer token; refused before dispatch.",
        403: (
            "Host/Origin/caller-selector refusal (not dispatched) or a declared "
            "daemon authorization error after dispatch; read dispatch from the envelope."
        ),
        404: "Unknown or unexposed method; refused before dispatch.",
        405: "Wrong HTTP method for this path; refused before dispatch.",
        408: (
            "Local request budget expired before dispatch, or a daemon timeout; "
            "read dispatch from the envelope."
        ),
        409: "Catalog or version mismatch; refused before dispatch.",
        411: "Content-Length is required; refused before dispatch.",
        413: "The request body exceeds the configured budget; refused before dispatch.",
        414: "The request target is too long; refused before dispatch.",
        415: "The media type is not supported; refused before dispatch.",
        429: "The concurrency budget is exhausted; refused before dispatch.",
        431: "The headers exceed the configured budget; refused before dispatch.",
        500: (
            "Internal or undeclared daemon failure; read dispatch from the envelope."
        ),
        502: "The daemon response is invalid; dispatch may be unknown.",
        503: (
            "Method or daemon not ready; local readiness is not dispatched and a "
            "daemon failure may be unknown; read dispatch from the envelope."
        ),
        504: (
            "The daemon did not answer in time; read dispatch from the envelope, "
            "which may be unknown."
        ),
    }
)

ERROR_STATUSES: Final = tuple(status for status in _ERRORS_BY_STATUS if status != 200)


def codes_for_status(status: int) -> list[str]:
    """Stable code list for one transport status, without exposing the table."""

    return [item.code for item in _ERRORS_BY_STATUS.get(status, ())]


def transport_error_documents() -> list[dict[str, Any]]:
    """Return the transport error table as fresh JSON-safe dictionaries."""

    return [
        {"code": error.code, "status": error.status, "description": error.description}
        for error in TRANSPORT_ERRORS
    ]


def security_scheme() -> dict[str, Any]:
    return {
        "type": "http",
        "scheme": "bearer",
        "description": (
            "Bearer token of the server-configured local operator. The token is "
            "read from the protected server capability file; it is not accepted "
            "from a request body and does not become the business target."
        ),
    }


def _envelope_properties() -> dict[str, Any]:
    return {
        "schemaVersion": {"const": ENVELOPE_SCHEMA_VERSION},
        "ok": {"const": False},
        "requestId": {"type": "string", "minLength": 1},
        "code": {"type": "string", "minLength": 1},
        "message": {"type": "string", "maxLength": MAX_MESSAGE_CHARS},
        "dispatch": {"enum": list(DISPATCH_STATES)},
        "method": {"type": "string", "minLength": 1},
        "data": {},
    }


def error_envelope_schema() -> dict[str, Any]:
    """The shared failure envelope; ``method`` and ``data`` are optional."""

    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schemaVersion", "ok", "requestId", "code", "message", "dispatch"],
        "properties": _envelope_properties(),
    }


def declared_error_envelope_schema(error: Mapping[str, Any]) -> dict[str, Any]:
    """A failure envelope narrowed to one declared domain error code."""

    schema = error_envelope_schema()
    properties = schema["properties"]
    properties["code"] = {"const": error["code"]}
    properties["data"] = thaw_json(error.get("dataSchema")) or {}
    return schema


def success_envelope_schema(method: str, result_component: str) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schemaVersion",
            "ok",
            "method",
            "requestId",
            "dispatch",
            "result",
        ],
        "properties": {
            "schemaVersion": {"const": ENVELOPE_SCHEMA_VERSION},
            "ok": {"const": True},
            "method": {"const": method},
            "requestId": {"type": "string", "minLength": 1},
            "dispatch": {"const": "dispatched"},
            "result": {"$ref": f"#/components/schemas/{result_component}"},
        },
    }


def thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_json(item) for item in value]
    return value


__all__ = [
    "BEARER_SCHEME",
    "DESCRIPTION",
    "DISPATCH_STATES",
    "ENVELOPE_SCHEMA_VERSION",
    "ERROR_STATUSES",
    "HTTP_CONTRACT_VERSION",
    "JSON_SCHEMA_DIALECT",
    "MAX_MESSAGE_CHARS",
    "OPENAPI_VERSION",
    "READ_RETRY",
    "STATUS_DESCRIPTIONS",
    "TITLE",
    "TRANSPORT_ERRORS",
    "TransportError",
    "WRITE_RETRY",
    "codes_for_status",
    "declared_error_envelope_schema",
    "error_envelope_schema",
    "security_scheme",
    "success_envelope_schema",
    "thaw_json",
    "transport_error_documents",
]
