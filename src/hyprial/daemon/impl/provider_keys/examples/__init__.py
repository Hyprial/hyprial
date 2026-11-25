"""Synthetic request examples for the local operator's provider keys and grants."""

from hyprial.identity import agent_id_for_entity_token
from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet, ipc_errors

from ..api import (
    AGENT_PROVIDER_ALLOW,
    AGENT_PROVIDER_REVOKE,
    AGENT_PROVIDER_STATUS,
    GRANT_TTL_MS,
    USER_PROVIDER_LIST,
    USER_PROVIDER_LOGIN,
    USER_PROVIDER_LOGOUT,
    USER_PROVIDER_ROTATE,
)

_PROVIDER = "example"
_OWNER = "owner@example.test"
_KEY_ID = "pk." + "a" * 26
_RECIPIENT = "x25519:" + "0" * 32
_SENTINEL = "example-not-a-credential"
_AGENT_UUID = "11111111-1111-4111-8111-111111111111"
_AGENT_ID = agent_id_for_entity_token(_AGENT_UUID.replace("-", ""))
_AGENT = {"agent": _AGENT_UUID, "agentId": _AGENT_ID, "actor": "example-worker"}
_CREATED = 1_700_000_000_000
_ISSUED = _CREATED + 1000
_KEY = {
    "keyId": _KEY_ID, "provider": _PROVIDER, "kind": "api-key", "label": "",
    "owner": _OWNER, "state": "active", "createdAt": _CREATED, "updatedAt": _CREATED,
    "dekVersion": 1, "environmentNames": ["EXAMPLE_API_KEY"],
    "recipients": [{"recipientKeyId": _RECIPIENT, "role": "owner-device", "deviceUid": "example-node"}],
    "lastUsedAt": None, "grants": [],
}
_GRANT = {
    **_AGENT, "grantId": _PROVIDER, "provider": _PROVIDER, "owner": _OWNER, "keyId": _KEY_ID,
    "state": "active", "seq": 1, "dekVersion": 1, "issuedAt": _ISSUED,
    "expiresAt": _ISSUED + GRANT_TTL_MS, "environmentNames": _KEY["environmentNames"],
    "recipientKeyId": _RECIPIENT, "legacyRef": None,
}
_GRANT_STATUS = {
    "grantId": _PROVIDER, "provider": _PROVIDER, "state": "ok", "code": None,
    "owner": _OWNER, "keyId": _KEY_ID, "seq": 1, "dekVersion": 1,
    "expiresAt": _GRANT["expiresAt"], "unsealable": True,
}
_SLOT = {"agent": _AGENT_ID, "provider": _PROVIDER}
_LOGIN = {"provider": _PROVIDER, "kind": "api-key", "secret": _SENTINEL}
_KEY_REF = {"keyId": _KEY_ID}
_ROTATE = {**_KEY_REF, "replaceSecret": _SENTINEL}


def _denied(params):
    return MethodExample(
        name="denied", kind="domain-error", description="A bound agent calls this local-operator method.",
        params=params, error=MethodExampleError(
            ipc_errors.CALLER_NOT_AUTHORIZED, "provider key methods accept only a local operator over ipc",
        ),
    )


def _invalid(name, params, message):
    return MethodExample(name, "invalid-params", message, params,
                         error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, message))


def _failure(name, params, code, message):
    return MethodExample(name, "domain-error", message, params, error=MethodExampleError(code, message))


_KEY_MISSING = _failure("key-not-found", _KEY_REF, ipc_errors.PROVIDER_KEY_NOT_FOUND, "provider key not found")
_BAD_KEY_ID = _invalid("invalid-key-id", {"keyId": "unknown"}, "keyId must be a provider key identifier")
_NO_AGENT = _invalid("missing-agent", {"provider": _PROVIDER}, "agent is required")

EXAMPLES = (
    MethodExampleSet(
        USER_PROVIDER_LOGIN, empty_not_applicable="Login returns the created key.", cases=(
            MethodExample("created", "success", "Create a provider key.", _LOGIN, result={"key": _KEY}),
            _invalid("missing-secret", {"provider": _PROVIDER, "kind": "api-key"}, "secret is required"),
            _failure("no-signing-key", _LOGIN, ipc_errors.USER_KEY_ABSENT, "user key absent; log in on this device"),
            _denied(_LOGIN),
        ),
    ),
    MethodExampleSet(USER_PROVIDER_LIST, cases=(
        MethodExample("one-key", "success", "List the owner's active key.", {},
                      result={"keys": [_KEY], "invalid": []}),
        MethodExample("no-keys", "empty", "The owner has no provider keys.", {},
                      result={"keys": [], "invalid": []}, reason="An owner may have no keys."),
        _invalid("invalid-filter", {"includeDeleted": "yes"}, "includeDeleted must be a boolean"),
        _failure("store-unavailable", {}, ipc_errors.PROVIDER_STORE_UNAVAILABLE,
                 "provider-key owner store unavailable"),
        _denied({}),
    )),
    MethodExampleSet(
        USER_PROVIDER_LOGOUT, empty_not_applicable="Logout returns a deletion receipt.", cases=(
            MethodExample("deleted", "success", "Delete a key with no grants.", _KEY_REF,
                          result={**_KEY_REF, "state": "deleted", "revokedGrants": [], "alreadyDeleted": False}),
            _BAD_KEY_ID, _KEY_MISSING, _denied(_KEY_REF),
        ),
    ),
    MethodExampleSet(
        USER_PROVIDER_ROTATE, empty_not_applicable="Rotation returns the updated key.", cases=(
            MethodExample("rotated", "success", "Rotate a key with no active grants.", _ROTATE,
                          result={"key": {**_KEY, "dekVersion": 2, "updatedAt": _ISSUED}, "reissuedGrants": 0}),
            _BAD_KEY_ID, _KEY_MISSING, _denied(_ROTATE),
        ),
    ),
    MethodExampleSet(
        AGENT_PROVIDER_ALLOW, empty_not_applicable="Allow returns the active grant.", cases=(
            MethodExample("allowed", "success", "Grant the agent access to the provider key.", _SLOT,
                          result={"changed": True, "grant": _GRANT}),
            _NO_AGENT,
            _failure("no-provider-key", _SLOT, ipc_errors.PROVIDER_KEY_NOT_FOUND, "provider key not found"),
            _denied(_SLOT),
        ),
    ),
    MethodExampleSet(
        AGENT_PROVIDER_REVOKE, empty_not_applicable="Revoke returns the revoked grant.", cases=(
            MethodExample("revoked", "success", "Revoke a grant for a stopped agent.", _SLOT,
                          result={"changed": True, "restart": {"state": "not-running"}, "grant": {
                              **_GRANT, "state": "revoked", "seq": 2, "dekVersion": None,
                              "issuedAt": None, "expiresAt": None, "recipientKeyId": None,
                          }}),
            _NO_AGENT,
            _failure("no-grant", _SLOT, ipc_errors.PROVIDER_GRANT_MISSING, "provider grant missing"),
            _denied(_SLOT),
        ),
    ),
    MethodExampleSet(AGENT_PROVIDER_STATUS, cases=(
        MethodExample("active-grant", "success", "Inspect an agent's active grant.", _SLOT,
                      result={**_AGENT, "grants": [_GRANT_STATUS]}),
        MethodExample("no-grants", "empty", "Inspect an agent with no grants.", {"agent": _AGENT_ID},
                      result={**_AGENT, "grants": []}, reason="An unfiltered agent can have no grants."),
        _NO_AGENT,
        _failure("unknown-agent", {"agent": "missing-worker"}, ipc_errors.AGENT_NOT_FOUND,
                 "no local agent matches the reference"),
        _denied(_SLOT),
    )),
)
