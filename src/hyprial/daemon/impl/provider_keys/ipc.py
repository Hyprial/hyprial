# Decision record (provider-key contract freeze r3 §5, §5.3, §7.2; HQ F-1, F-9)
# - Seven declared methods: user.provider.login/list/logout/rotate and
#   agent.provider.allow/revoke/status. Owner "daemon.provider-keys", unary,
#   audience local-operator, confirmation none.
# - 2026-11-10 (F-9 GUI phase): http_exposed=True with
#   HttpBinding(IdentityBinding("none")). The HTTP shell forwards over the local
#   socket, so the daemon still sees origin "ipc" and the preauthorize below is
#   unchanged. Reads retry "safe"; writes retry "never". The forwarded-origin
#   and refusal tests remain the gate for this exposure.
# - preauthorize runs before schema validation and accepts only origin "ipc":
#   worker origin, None/"daemon", an actor or sessionRef param, and an IPC peer
#   that is a registered interactive session are all CALLER_NOT_AUTHORIZED.
#   The HTTP shell forwards over the local socket, so it arrives as "ipc".
# - Bindings are (spec, handler) pairs; the registry wraps them in BoundMethod
#   with this preauthorize, which keeps this module free of a registry import
#   cycle. The service and agent resolver are imported lazily from .context.
# - Write-only values (secret, replaceSecret) never reach a result, an error
#   message or error data: results are closed schemas, a service error whose
#   text or data contains a write-only value is replaced by a fixed message,
#   and unexpected exceptions become a fixed DAEMON_ERROR.
# - JSON Schema bounds count code points, so the handler also enforces the
#   contract's 1-16384 bytes of valid UTF-8 for write-only values and refuses a
#   label with any Unicode category C* character (Cc, Cf, Cs, Co, Cn), matching
#   the A1 record validator. Both refusals are INVALID_ARGUMENT with a fixed
#   message, before the service is resolved.
"""Provider-key IPC declarations, operator-only preauthorization and handlers."""
from __future__ import annotations

import logging
import unicodedata
from functools import partial
from typing import Any, Mapping

from hyprial.kernel import (
    DaemonRequestError,
    HttpBinding,
    IdentityBinding,
    IdempotencySpec,
    MethodError,
    MethodSpec,
    ipc_errors,
)
from .api import (
    AGENT_PROVIDER_ALLOW,
    AGENT_PROVIDER_REVOKE,
    AGENT_PROVIDER_STATUS,
    PROVIDER_KEY_METHODS,
    USER_PROVIDER_LIST,
    USER_PROVIDER_LOGIN,
    USER_PROVIDER_LOGOUT,
    USER_PROVIDER_ROTATE,
)

_LOG = logging.getLogger(__name__)
_OWNER = "daemon.provider-keys"
_READS = frozenset({USER_PROVIDER_LIST, AGENT_PROVIDER_STATUS})
_AGENT_SCOPED = frozenset({AGENT_PROVIDER_ALLOW, AGENT_PROVIDER_REVOKE, AGENT_PROVIDER_STATUS})
_WRITE_ONLY = ("secret", "replaceSecret")
_IDENTITY_PARAMS = ("actor", "sessionRef")
_REFUSED = "provider key methods accept only a local operator over ipc"
_FAILED = "provider key operation failed"
_REDACTED = "provider key operation refused"
_BAD_SECRET = "write-only value must be 1-16384 bytes of valid UTF-8"
_BAD_LABEL = "label must not contain control, format, surrogate, private-use or unassigned characters"
_MAX_SECRET_BYTES = 16384

_NAME = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"}
_KEY_ID = {"type": "string", "pattern": "^pk\\.[a-z2-7]{26}$"}
_ENV_NAMES = {"type": "array", "minItems": 1, "maxItems": 8, "uniqueItems": True,
              "items": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]*$"}}
_SECRET = {"type": "string", "writeOnly": True, "minLength": 1, "maxLength": 16384,
           "not": {"type": "string", "pattern": "[\\r\\n]"}}
_LABEL = {"type": "string", "maxLength": 128,
          "not": {"type": "string", "pattern": "[\\u0000-\\u001f\\u007f-\\u009f]"}}
_AGENT_REF = {"type": "string", "minLength": 1, "maxLength": 512}
_EXPECTED_AGENT = {
    "type": "object",
    "properties": {"uri": {"type": "string", "minLength": 1},
                   "createdAtMs": {"type": "integer", "minimum": 0}},
    "required": ["uri", "createdAtMs"], "additionalProperties": False,
}
_TIME = {"type": "integer", "minimum": 0}
_TIME_OR_NULL = {"type": ["integer", "null"], "minimum": 0}
_POSITIVE_OR_NULL = {"type": ["integer", "null"], "minimum": 1}
_STRING = {"type": "string"}
_STRING_OR_NULL = {"type": ["string", "null"]}
_SIX = (
    ipc_errors.PROVIDER_GRANT_MISSING, ipc_errors.PROVIDER_GRANT_REVOKED,
    ipc_errors.PROVIDER_KEY_NOT_FOUND, ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING,
    ipc_errors.PROVIDER_KEY_UNSEAL_FAILED, ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID,
)


def _object(properties: Mapping[str, Any], required: tuple[str, ...] | None = None) -> dict:
    """A closed JSON object schema; every property is required unless stated."""
    return {"type": "object", "properties": dict(properties),
            "required": list(properties if required is None else required),
            "additionalProperties": False}


_KEY_GRANT = _object({
    "agent": _STRING, "agentId": _STRING_OR_NULL, "grantId": _STRING, "provider": _STRING,
    "state": {"enum": ["active", "revoked", "awaiting-key"]},
})
_KEY_VIEW = _object({
    "keyId": _STRING, "provider": _STRING, "kind": {"enum": ["api-key"]},
    "label": {"type": "string", "maxLength": 128}, "owner": _STRING,
    "state": {"enum": ["active", "deleted"]}, "createdAt": _TIME, "updatedAt": _TIME,
    "dekVersion": _POSITIVE_OR_NULL, "environmentNames": {"type": "array", "items": _STRING},
    "recipients": {"type": "array", "items": _object({
        "recipientKeyId": _STRING, "role": {"enum": ["owner-device", "host"]},
        "deviceUid": _STRING})},
    "lastUsedAt": _TIME_OR_NULL,
    "grants": {"type": "array", "items": _KEY_GRANT},
})
_LEGACY_REF = {
    "type": ["object", "null"],
    "properties": {"entryId": _STRING, "fieldName": _STRING,
                   "revision": {"type": "integer", "minimum": 1}},
    "required": ["entryId", "fieldName", "revision"], "additionalProperties": False,
}
_GRANT_VIEW = _object({
    "agent": _STRING, "agentId": _STRING, "actor": _STRING, "grantId": _STRING,
    "provider": _STRING, "owner": _STRING, "keyId": _STRING_OR_NULL,
    "state": {"enum": ["active", "revoked", "awaiting-key"]},
    "seq": {"type": "integer", "minimum": 1}, "dekVersion": _POSITIVE_OR_NULL,
    "issuedAt": _TIME_OR_NULL, "expiresAt": _TIME_OR_NULL,
    "environmentNames": {"type": "array", "items": _STRING},
    "recipientKeyId": _STRING_OR_NULL, "legacyRef": _LEGACY_REF,
})
_GRANT_STATUS = _object({
    "grantId": _STRING, "provider": _STRING,
    "state": {"enum": ["ok", "missing", "revoked", "awaiting-key", "recipient-pending",
                       "unseal-failed", "signature-invalid", "key-not-found", "unavailable"]},
    "code": {"enum": [None, *_SIX, ipc_errors.PROVIDER_STORE_UNAVAILABLE]},
    "owner": _STRING_OR_NULL, "keyId": _STRING_OR_NULL,
    "seq": {"type": ["integer", "null"], "minimum": 1}, "dekVersion": _POSITIVE_OR_NULL,
    "expiresAt": _TIME_OR_NULL, "unsealable": {"type": "boolean"},
})

_PARAMS_BY_METHOD = {
    USER_PROVIDER_LOGIN: _object({"provider": _NAME, "kind": {"type": "string", "enum": ["api-key"]},
                                  "secret": _SECRET, "label": _LABEL,
                                  "environmentNames": _ENV_NAMES},
                                 ("provider", "kind", "secret")),
    USER_PROVIDER_LIST: _object({"provider": _NAME, "includeDeleted": {"type": "boolean"}}, ()),
    USER_PROVIDER_LOGOUT: _object({"keyId": _KEY_ID}),
    USER_PROVIDER_ROTATE: _object({"keyId": _KEY_ID, "replaceSecret": _SECRET}, ("keyId",)),
    AGENT_PROVIDER_ALLOW: _object({"agent": _AGENT_REF, "provider": _NAME, "keyId": _KEY_ID,
                                   "environmentNames": _ENV_NAMES, "replace": {"type": "boolean"},
                                   "expectedAgent": _EXPECTED_AGENT}, ("agent", "provider")),
    AGENT_PROVIDER_REVOKE: _object({"agent": _AGENT_REF, "provider": _NAME,
                                    "expectedAgent": _EXPECTED_AGENT}, ("agent", "provider")),
    AGENT_PROVIDER_STATUS: _object({"agent": _AGENT_REF, "provider": _NAME,
                                    "expectedAgent": _EXPECTED_AGENT}, ("agent",)),
}

_RESULTS_BY_METHOD = {
    USER_PROVIDER_LOGIN: _object({"key": _KEY_VIEW}),
    USER_PROVIDER_LIST: _object({
        "keys": {"type": "array", "items": _KEY_VIEW},
        "invalid": {"type": "array", "items": _object({
            "keyId": _STRING,
            "code": {"enum": [ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID,
                              ipc_errors.PROVIDER_KEY_NOT_FOUND]}})},
    }),
    USER_PROVIDER_LOGOUT: _object({
        "keyId": _STRING, "state": {"enum": ["deleted"]},
        "revokedGrants": {"type": "array", "items": _object({
            "agent": _STRING, "agentId": _STRING_OR_NULL, "grantId": _STRING,
            "provider": _STRING})},
        "alreadyDeleted": {"type": "boolean"},
    }),
    USER_PROVIDER_ROTATE: _object({"key": _KEY_VIEW,
                                   "reissuedGrants": {"type": "integer", "minimum": 0}}),
    AGENT_PROVIDER_ALLOW: _object({"grant": _GRANT_VIEW, "changed": {"type": "boolean"}}),
    AGENT_PROVIDER_REVOKE: _object({
        "grant": _GRANT_VIEW, "changed": {"type": "boolean"},
        "restart": _object({"state": {"enum": ["not-running", "not-supported"]}}),
    }),
    AGENT_PROVIDER_STATUS: _object({"agent": _STRING, "agentId": _STRING, "actor": _STRING,
                                    "grants": {"type": "array", "items": _GRANT_STATUS}}),
}

_COMMON = (
    (ipc_errors.INVALID_ARGUMENT, "Parameters do not match the declared schema."),
    (ipc_errors.CALLER_NOT_AUTHORIZED, "Only a local operator over ipc may call this method."),
    (ipc_errors.PROVIDER_STORE_UNAVAILABLE, "Owner store, gate or trust anchor unavailable."),
    (ipc_errors.PROVIDER_STORE_CONFLICT, "Conditional write still conflicted after retries."),
)
_SIGNING = (
    (ipc_errors.USER_KEY_ABSENT, "No local user signing key."),
    (ipc_errors.USER_KEY_UNBOUND, "Owner signing key is not bound or signing is disabled."),
    (ipc_errors.USER_KEY_BINDING_EXPIRED, "Owner signing key binding expired."),
    (ipc_errors.USER_KEY_QUARANTINED, "Owner signing key is quarantined."),
)
_AGENT = (
    (ipc_errors.AGENT_NOT_FOUND, "Target agent is not in the local registry."),
    (ipc_errors.AGENT_INCARNATION_MISMATCH, "expectedAgent does not match the agent incarnation."),
)
_USER_AGENT = (ipc_errors.USER_AGENT_IMMUTABLE,
               "The agent is the owner's user agent, which holds no credential.")
_NOT_FOUND = (ipc_errors.PROVIDER_KEY_NOT_FOUND, "Key does not exist or is deleted.")
_UNSEAL = (ipc_errors.PROVIDER_KEY_UNSEAL_FAILED, "Readback or local unseal check failed.")
_INVALID = (ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID, "A signed record failed verification.")
_ERRORS_BY_METHOD = {
    USER_PROVIDER_LOGIN: (*_COMMON, *_SIGNING, _UNSEAL, _INVALID),
    USER_PROVIDER_LIST: _COMMON,
    USER_PROVIDER_LOGOUT: (*_COMMON, *_SIGNING, _NOT_FOUND, _INVALID),
    USER_PROVIDER_ROTATE: (*_COMMON, *_SIGNING, _NOT_FOUND, _UNSEAL, _INVALID),
    AGENT_PROVIDER_ALLOW: (*_COMMON, *_SIGNING, *_AGENT, _USER_AGENT, _NOT_FOUND, _INVALID,
                           (ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING,
                            "This device is not a recipient of the key."),
                           (ipc_errors.PROVIDER_CREDENTIAL_EXISTS,
                            "Another key holds the slot and replace was not given.")),
    AGENT_PROVIDER_REVOKE: (*_COMMON, *_SIGNING, *_AGENT, _INVALID,
                            (ipc_errors.PROVIDER_GRANT_MISSING, "The slot never had a grant.")),
    AGENT_PROVIDER_STATUS: (*_COMMON, *_AGENT),
}


def _spec(method: str) -> MethodSpec:
    read = method in _READS
    retry = "safe" if read else "never"
    return MethodSpec(
        name=method, owner=_OWNER,
        params_schema=_PARAMS_BY_METHOD[method], result_schema=_RESULTS_BY_METHOD[method],
        errors=tuple(MethodError(code=code, description=description)
                     for code, description in _ERRORS_BY_METHOD[method]),
        mode="unary", mutates=not read, retry=retry,
        audience=("local-operator",), http_exposed=True,
        http_binding=HttpBinding(IdentityBinding("none")), confirmation="none",
        idempotency=IdempotencySpec(mode=retry, scope="owner-provider-key"),
    )


def preauthorize(method: str, app: Any, params: Mapping[str, Any], *,
                 trusted_message_origin: str | None = None) -> None:
    """Refuse every caller except a non-interactive local operator over ipc, before schema checks."""
    refused = trusted_message_origin != "ipc" or any(key in params for key in _IDENTITY_PARAMS)
    if not refused:
        peer_pid = getattr(getattr(app, "_ipc_peer", None), "pid", None)
        if peer_pid is not None:
            interactive = getattr(app, "_interactive_identity_binding_peer", None)
            refused = interactive is None or bool(interactive(peer_pid))
    if refused:
        raise DaemonRequestError(ipc_errors.CALLER_NOT_AUTHORIZED, _REFUSED)


def handle(method: str, app: Any, params: Mapping[str, Any], *,
           trusted_message_origin: str | None = None) -> Any:
    """Re-check the caller, then run one provider-key operation on the owner service."""
    preauthorize(method, app, params, trusted_message_origin=trusted_message_origin)
    _check_values(params)
    try:
        return _dispatch(method, app, params)
    except DaemonRequestError as error:
        clean = _scrubbed(error, params)
        if clean is error:
            raise
        raise clean from None
    except (NameError, ImportError):
        raise
    except Exception as error:  # noqa: BLE001
        _LOG.warning("provider key method %s failed with %s", method, type(error).__name__)
        raise DaemonRequestError(ipc_errors.DAEMON_ERROR, _FAILED) from None


def _check_values(params: Mapping[str, Any]) -> None:
    """Refuse write-only values outside the UTF-8 byte bounds and labels with C* characters."""
    for name in _WRITE_ONLY:
        value = params.get(name)
        if isinstance(value, str) and not 1 <= _utf8_length(value) <= _MAX_SECRET_BYTES:
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, _BAD_SECRET)
    label = params.get("label")
    if isinstance(label, str) and any(unicodedata.category(char).startswith("C") for char in label):
        raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, _BAD_LABEL)


def _utf8_length(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return 0


def _dispatch(method: str, app: Any, params: Mapping[str, Any]) -> Any:
    from .context import resolve_agent, service_for

    service = service_for(app)
    if method == USER_PROVIDER_LOGIN:
        return service.login(provider=params["provider"], kind=params["kind"],
                             secret=params["secret"], label=params.get("label"),
                             environment_names=params.get("environmentNames"))
    if method == USER_PROVIDER_LIST:
        return service.list_keys(provider=params.get("provider"),
                                 include_deleted=params.get("includeDeleted", False))
    if method == USER_PROVIDER_LOGOUT:
        return service.logout(key_id=params["keyId"])
    if method == USER_PROVIDER_ROTATE:
        return service.rotate(key_id=params["keyId"], replace_secret=params.get("replaceSecret"))
    if method not in _AGENT_SCOPED:
        raise DaemonRequestError(ipc_errors.DAEMON_ERROR, _FAILED)
    agent = resolve_agent(app, params["agent"], params.get("expectedAgent"))
    if method == AGENT_PROVIDER_ALLOW:
        return service.allow(agent=agent, provider=params["provider"], key_id=params.get("keyId"),
                             environment_names=params.get("environmentNames"),
                             replace=params.get("replace", False))
    if method == AGENT_PROVIDER_REVOKE:
        return service.revoke(agent=agent, grant_id=params["provider"])
    return service.status(agent=agent, provider=params.get("provider"))


def _scrubbed(error: DaemonRequestError, params: Mapping[str, Any]) -> DaemonRequestError:
    """The same error, or a fixed-message copy when it would echo a write-only value."""
    values = [params[name] for name in _WRITE_ONLY
              if isinstance(params.get(name), str) and params[name]]
    exposed = str(error) + ("" if error.data is None else repr(error.data))
    if any(value in exposed for value in values):
        return DaemonRequestError(error.code, _REDACTED)
    return error


PROVIDER_KEY_BINDINGS = tuple(
    (_spec(name), partial(handle, name)) for name in sorted(PROVIDER_KEY_METHODS)
)
