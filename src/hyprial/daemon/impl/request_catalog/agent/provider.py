"""``agent.provider.*`` declarations over the existing provider handler.

GRS integration decision: H-03 (#2200, a03a2ef75) implements write admission
but is not included in this slice. Login/logout HTTP exposure is disabled by
this integration configuration; the next H-03 integration can enable it.

Decision record (M-02, 2026-10-31): the three provider methods stay one wire
method each.  ``agent.provider.default`` keeps its read, legacy provider-write
and ``clear`` branches in the same method because the CLI and the captured
Claude default already share that handler; splitting a ``.get``/``.clear`` pair
would invent a second business API.  Every branch can write, so the method is
declared ``mutates=True`` and ``retry="never"`` even when a GUI only reads it.

``agent.provider.login`` is the existing credential installation surface, not
a new provider OAuth flow.  The CLI may read a host login file or a setup token
before calling it; the daemon method receives only the already-collected
documents or token and never returns them.  ``agent.provider.logout`` removes
only the current agent's provider files and keeps the upstream-token semantics
owned by the provider CLI.

The shared Agent registry aggregation is deliberately not modified here.
M-01 owns the partial-registration hook and M-03 combines the accepted slices;
this module exports a complete binding tuple that those commits can import.

The setup-token branch can re-surface the identity fence's existing
``STALE_AGENT_SECRET_GRANT``.  That code is declared from the same wire literal
because it has not yet been moved into the shared ``ipc_errors`` registry.

Compatibility decision after the 2026-10-31 independent pre-review: the old
handler accepts ``provider: null``, ``clear: null``/``"yes"``, unknown keys,
any ``force`` value, and any ``makeDefault`` value up to the entry's truthiness
check.  The default/login parameter schemas therefore declare the legacy value
domain instead of pre-empting the handler.  For ``default``, exact ``true``
clears the legacy provider-name file; a truthy non-boolean ``clear`` still
reaches the captured-Claude-default layer, which can clear that reference for
the operator or refuse a non-operator.  The handler remains the authority for
requiredness, coercion and validation order.  Logout keeps ``actor``
structural and leaves ``provider`` requiredness to the handler; this preserves
the legacy actor-first error for combinations such as ``{"actor": 5}`` with no
provider.  Unknown keys remain ignored.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from typing import Any

from hyprial.identity import AgentError
from hyprial.kernel import (
    HttpBinding,
    IdentityBinding,
    IdempotencySpec,
    MethodError,
    MethodSpec,
    ipc_errors,
)

_STALE_AGENT_SECRET_GRANT = "STALE_AGENT_SECRET_GRANT"

AGENT_PROVIDER_DEFAULT = "agent.provider.default"
AGENT_PROVIDER_LOGIN = "agent.provider.login"
AGENT_PROVIDER_LOGOUT = "agent.provider.logout"

_STRING = {"type": "string", "minLength": 1}
_NULLABLE_STRING = {"type": ["string", "null"], "minLength": 1}
_COMPAT_VALUE = {
    "description": (
        "Legacy value domain: the existing handler owns the real coercion, "
        "requiredness and validation order; this declaration must not pre-empt it."
    ),
    "type": ["string", "number", "boolean", "null", "array", "object"],
}

_DEFAULT_REFERENCE = {
    "type": "object",
    "properties": {
        "ownerActor": _STRING,
        "ownerEntityToken": _STRING,
        "entryId": _STRING,
        "fieldName": _STRING,
        "kind": _STRING,
    },
    "required": ["ownerActor", "ownerEntityToken", "entryId", "fieldName", "kind"],
    "additionalProperties": False,
}

_DEFAULT_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "provider": _NULLABLE_STRING,
        "previous": _NULLABLE_STRING,
        "credentialSource": _NULLABLE_STRING,
        "defaultCredential": {
            **_DEFAULT_REFERENCE,
            "type": ["object", "null"],
        },
    },
    "required": ["ok", "provider", "previous"],
    "additionalProperties": False,
}

_DEFAULT_PARAMS = {
    "type": "object",
    "properties": {
        "provider": {
            **_COMPAT_VALUE,
            "description": (
                "Legacy provider target: a present non-null value is validated "
                "by the handler, null reads the current default."
            ),
        },
        "clear": {
            **_COMPAT_VALUE,
            "description": (
                "Exact boolean true clears the legacy provider-name file. A "
                "truthy non-boolean value leaves that file but still enters the "
                "captured-Claude-default branch: the operator can clear that "
                "reference, while a non-operator is refused. Falsey values read."
            ),
        },
    },
    "additionalProperties": _COMPAT_VALUE,
}

_FILE_RECEIPT = {
    "type": "object",
    "properties": {
        "path": _STRING,
        "bytes": {"type": "integer", "minimum": 0},
        "replaced": {"type": "boolean"},
        "unchanged": {"type": "boolean"},
    },
    "required": ["path", "bytes", "replaced", "unchanged"],
    "additionalProperties": False,
}

_LOGIN_PARAMS = {
    "type": "object",
    "properties": {
        "actor": {
            **_COMPAT_VALUE,
            "description": "Target agent; the handler validates it first.",
        },
        "provider": {
            **_COMPAT_VALUE,
            "description": "Harness name; the handler validates it after actor.",
        },
        "files": {
            **_COMPAT_VALUE,
            "description": (
                "Legacy native-file document list; the handler checks its "
                "shape after the target and makeDefault checks."
            ),
        },
        "force": {
            **_COMPAT_VALUE,
            "description": (
                "Legacy bool() coercion; any value remains accepted, as the "
                "existing handler does."
            ),
        },
        "setupToken": {
            **_COMPAT_VALUE,
            "description": (
                "Sensitive setup token; the handler enforces string, emptiness, "
                "whitespace, NUL and length after the target check."
            ),
        },
        "makeDefault": {
            **_COMPAT_VALUE,
            "description": (
                "The entry checks truthiness before the handler enforces the "
                "boolean type; the legacy order must remain observable."
            ),
        },
    },
    "additionalProperties": _COMPAT_VALUE,
}

_LOGIN_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "actor": _STRING,
        "provider": _STRING,
        "credentialSource": _STRING,
        "files": {"type": "array", "items": _FILE_RECEIPT},
        "default": _DEFAULT_RESULT,
    },
    "required": ["ok", "actor", "provider"],
    "additionalProperties": False,
}

_LOGOUT_PARAMS = {
    "type": "object",
    "properties": {
        "actor": _STRING,
        "provider": {
            **_STRING,
            "description": (
                "The handler requires this after actor; keeping only actor "
                "structurally preserves the legacy actor-first error order."
            ),
        },
    },
    "required": ["actor"],
    "additionalProperties": _COMPAT_VALUE,
}

_LOGOUT_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "actor": _STRING,
        "provider": _STRING,
        "removed": {"type": "array", "items": _STRING},
        "absent": {"type": "array", "items": _STRING},
    },
    "required": ["ok", "actor", "provider", "removed", "absent"],
    "additionalProperties": False,
}

def _error(code: str) -> MethodError:
    return MethodError(code, description="No data: this error carries no data payload.")


_WRITE_ERRORS = (
    _error(ipc_errors.INVALID_ARGUMENT),
    _error(ipc_errors.CALLER_NOT_AUTHORIZED),
    _error(ipc_errors.TARGET_NOT_AUTHORIZED),
    _error(ipc_errors.AGENT_NOT_FOUND),
    _error(AgentError.code),
    _error(ipc_errors.DAEMON_ERROR),
)

_DEFAULT_ERRORS = _WRITE_ERRORS + (_error(ipc_errors.PROVIDER_UNSUPPORTED),)

_LOGIN_ERRORS = _WRITE_ERRORS + (
    _error(ipc_errors.PROVIDER_UNSUPPORTED),
    _error(ipc_errors.PROVIDER_CREDENTIAL_EXISTS),
    _error(ipc_errors.PROVIDER_UNSAFE_PATH),
    _error(ipc_errors.UNSUPPORTED),
    _error(_STALE_AGENT_SECRET_GRANT),
)

_LOGOUT_ERRORS = _WRITE_ERRORS + (
    _error(ipc_errors.PROVIDER_UNSUPPORTED),
    _error(ipc_errors.PROVIDER_UNSAFE_PATH),
    _error(ipc_errors.UNSUPPORTED),
)

AGENT_PROVIDER_DEFAULT_SPEC = MethodSpec(
    name=AGENT_PROVIDER_DEFAULT,
    owner="daemon.agents",
    params_schema=_DEFAULT_PARAMS,
    result_schema=_DEFAULT_RESULT,
    errors=_DEFAULT_ERRORS,
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator", "bound-agent"),
    http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none"), read_only_params_schema={
        "type": "object", "properties": {}, "additionalProperties": False,
        "description": "Read-only HTTP projection; default writes require integration of H-03 (#2200).",
    }),
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="provider-default-read-write"),
)

AGENT_PROVIDER_LOGIN_SPEC = MethodSpec(
    name=AGENT_PROVIDER_LOGIN,
    owner="daemon.agents",
    params_schema=_LOGIN_PARAMS,
    result_schema=_LOGIN_RESULT,
    errors=_LOGIN_ERRORS,
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator", "bound-agent"),
    http_exposed=False,
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="provider-credential-install-H-03-not-integrated"),
)

AGENT_PROVIDER_LOGOUT_SPEC = MethodSpec(
    name=AGENT_PROVIDER_LOGOUT,
    owner="daemon.agents",
    params_schema=_LOGOUT_PARAMS,
    result_schema=_LOGOUT_RESULT,
    errors=_LOGOUT_ERRORS,
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator", "bound-agent"),
    http_exposed=False,
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="provider-credential-revoke-H-03-not-integrated"),
)

AGENT_PROVIDER_SPECS = (
    AGENT_PROVIDER_DEFAULT_SPEC,
    AGENT_PROVIDER_LOGIN_SPEC,
    AGENT_PROVIDER_LOGOUT_SPEC,
)


def provider_default(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> Any:
    return app._handle_agent_provider_request(
        AGENT_PROVIDER_DEFAULT,
        dict(params),
        trusted_message_origin=trusted_message_origin,
    )


def provider_login(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> Any:
    return app._handle_agent_provider_request(
        AGENT_PROVIDER_LOGIN,
        dict(params),
        trusted_message_origin=trusted_message_origin,
    )


def provider_logout(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> Any:
    return app._handle_agent_provider_request(
        AGENT_PROVIDER_LOGOUT,
        dict(params),
        trusted_message_origin=trusted_message_origin,
    )


def _param_error(method: str, error: Any, params: Mapping[str, Any]) -> str:
    """Keep the legacy field messages for schema violations the handler shares."""

    validator = getattr(error, "validator", None)
    path = list(getattr(error, "absolute_path", ()))
    field = str(path[-1]) if path else ""
    if validator == "required":
        missing = [
            name
            for name in getattr(error, "validator_value", ())
            if name not in params
        ]
        field = missing[0] if missing else field
    if field in {"actor", "provider", "filename", "contentBase64"} and validator in {
        "required",
        "type",
        "minLength",
    }:
        return f"{field} must be a non-empty string"
    if field in {"force", "makeDefault", "clear"} and validator == "type":
        return f"{field} must be boolean"
    if field == "setupToken" and validator in {"type", "minLength", "maxLength"}:
        return "setupToken requires one non-empty Claude token and no files"
    if field == "files" and validator in {"type", "minItems"}:
        return "files must be a non-empty array"
    if path and path[0] == "files" and isinstance(path[-1], int):
        return "each file must be an object"
    path_text = "/".join(str(part) for part in path) or "<root>"
    return f"{method} params do not match the declared schema at {path_text}"


AGENT_PROVIDER_BINDINGS = (
    (
        AGENT_PROVIDER_DEFAULT_SPEC,
        provider_default,
        partial(_param_error, AGENT_PROVIDER_DEFAULT),
    ),
    (
        AGENT_PROVIDER_LOGIN_SPEC,
        provider_login,
        partial(_param_error, AGENT_PROVIDER_LOGIN),
    ),
    (
        AGENT_PROVIDER_LOGOUT_SPEC,
        provider_logout,
        partial(_param_error, AGENT_PROVIDER_LOGOUT),
    ),
)

__all__ = [
    "AGENT_PROVIDER_BINDINGS",
    "AGENT_PROVIDER_DEFAULT",
    "AGENT_PROVIDER_DEFAULT_SPEC",
    "AGENT_PROVIDER_LOGIN",
    "AGENT_PROVIDER_LOGIN_SPEC",
    "AGENT_PROVIDER_LOGOUT",
    "AGENT_PROVIDER_LOGOUT_SPEC",
    "AGENT_PROVIDER_SPECS",
    "provider_default",
    "provider_login",
    "provider_logout",
]
