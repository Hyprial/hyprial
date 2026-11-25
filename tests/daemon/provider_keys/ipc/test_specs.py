"""The seven provider-key MethodSpecs match contract freeze r3 §5 and §7.2."""
from __future__ import annotations

from collections.abc import Mapping

import pytest

from hyprial.daemon.impl.provider_keys import api
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY, MIGRATED_FAMILIES
from hyprial.kernel import ipc_errors, validate_method_spec

READS = {api.USER_PROVIDER_LIST, api.AGENT_PROVIDER_STATUS}
AGENT_METHODS = {api.AGENT_PROVIDER_ALLOW, api.AGENT_PROVIDER_REVOKE, api.AGENT_PROVIDER_STATUS}
SIGNING = {
    ipc_errors.USER_KEY_ABSENT, ipc_errors.USER_KEY_UNBOUND,
    ipc_errors.USER_KEY_BINDING_EXPIRED, ipc_errors.USER_KEY_QUARANTINED,
}
COMMON = {
    ipc_errors.INVALID_ARGUMENT, ipc_errors.CALLER_NOT_AUTHORIZED,
    ipc_errors.PROVIDER_STORE_UNAVAILABLE, ipc_errors.PROVIDER_STORE_CONFLICT,
}
AGENT = {ipc_errors.AGENT_NOT_FOUND, ipc_errors.AGENT_INCARNATION_MISMATCH}
EXPECTED_ERRORS = {
    api.USER_PROVIDER_LOGIN: COMMON | SIGNING | {
        ipc_errors.PROVIDER_KEY_UNSEAL_FAILED, ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID},
    api.USER_PROVIDER_LIST: COMMON,
    api.USER_PROVIDER_LOGOUT: COMMON | SIGNING | {
        ipc_errors.PROVIDER_KEY_NOT_FOUND, ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID},
    api.USER_PROVIDER_ROTATE: COMMON | SIGNING | {
        ipc_errors.PROVIDER_KEY_NOT_FOUND, ipc_errors.PROVIDER_KEY_UNSEAL_FAILED,
        ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID},
    api.AGENT_PROVIDER_ALLOW: COMMON | SIGNING | AGENT | {
        ipc_errors.PROVIDER_KEY_NOT_FOUND, ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING,
        ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID, ipc_errors.PROVIDER_CREDENTIAL_EXISTS,
        ipc_errors.USER_AGENT_IMMUTABLE},
    api.AGENT_PROVIDER_REVOKE: COMMON | SIGNING | AGENT | {
        ipc_errors.PROVIDER_GRANT_MISSING, ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID},
    api.AGENT_PROVIDER_STATUS: COMMON | AGENT,
}
PARAMS = {
    api.USER_PROVIDER_LOGIN: ({"provider", "kind", "secret"}, {"label", "environmentNames"}),
    api.USER_PROVIDER_LIST: (set(), {"provider", "includeDeleted"}),
    api.USER_PROVIDER_LOGOUT: ({"keyId"}, set()),
    api.USER_PROVIDER_ROTATE: ({"keyId"}, {"replaceSecret"}),
    api.AGENT_PROVIDER_ALLOW: ({"agent", "provider"},
                               {"keyId", "environmentNames", "replace", "expectedAgent"}),
    api.AGENT_PROVIDER_REVOKE: ({"agent", "provider"}, {"expectedAgent"}),
    api.AGENT_PROVIDER_STATUS: ({"agent"}, {"provider", "expectedAgent"}),
}


def _spec(name):
    binding = METHOD_REGISTRY.get(name)
    assert binding is not None, name
    return binding.spec


def test_all_seven_methods_are_registered_with_preauthorize():
    assert api.PROVIDER_KEY_METHODS == set(EXPECTED_ERRORS)
    for name in api.PROVIDER_KEY_METHODS:
        binding = METHOD_REGISTRY.get(name)
        assert binding is not None, name
        assert binding.preauthorize is not None, name


def test_user_family_is_migrated():
    assert "user" in MIGRATED_FAMILIES
    assert "agent" not in MIGRATED_FAMILIES


@pytest.mark.parametrize("name", sorted(EXPECTED_ERRORS))
def test_common_shape(name):
    spec = _spec(name)
    validate_method_spec(spec)
    assert spec.owner == "daemon.provider-keys"
    assert spec.mode == "unary"
    assert spec.audience == ("local-operator",)
    assert spec.http_exposed is True
    assert spec.http_binding is not None
    assert spec.http_binding.identity.mode == "none"
    assert spec.confirmation == "none"
    read = name in READS
    assert spec.mutates is (not read)
    assert spec.retry == ("safe" if read else "never")
    assert spec.idempotency is not None and spec.idempotency.mode == spec.retry


@pytest.mark.parametrize("name", sorted(EXPECTED_ERRORS))
def test_declared_errors_follow_contract(name):
    assert {error.code for error in _spec(name).errors} == EXPECTED_ERRORS[name]
    assert all(error.description for error in _spec(name).errors)


@pytest.mark.parametrize("name", sorted(PARAMS))
def test_params_are_closed_with_exact_fields(name):
    schema = _spec(name).params_schema
    required, optional = PARAMS[name]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema.get("required", ())) == required
    assert set(schema["properties"]) == required | optional
    assert "actor" not in schema["properties"]
    assert "sessionRef" not in schema["properties"]


@pytest.mark.parametrize(("name", "field"), [
    (api.USER_PROVIDER_LOGIN, "secret"), (api.USER_PROVIDER_ROTATE, "replaceSecret"),
])
def test_secret_fields_are_write_only_single_line(name, field):
    schema = _spec(name).params_schema["properties"][field]
    assert schema["type"] == "string"
    assert schema["writeOnly"] is True
    assert schema["minLength"] == 1
    assert schema["maxLength"] == 16384
    assert dict(schema["not"]) == {"type": "string", "pattern": "[\\r\\n]"}


@pytest.mark.parametrize("name", sorted(AGENT_METHODS))
def test_expected_agent_is_closed(name):
    schema = _spec(name).params_schema["properties"]["expectedAgent"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"uri", "createdAtMs"}
    assert dict(schema["properties"]["uri"]) == {"type": "string", "minLength": 1}
    assert dict(schema["properties"]["createdAtMs"]) == {"type": "integer", "minimum": 0}


def _closed_everywhere(schema):
    if isinstance(schema, Mapping):
        kind = schema.get("type")
        if kind == "object" or (isinstance(kind, tuple) and "object" in kind):
            assert schema.get("additionalProperties") is False, schema
        for value in schema.values():
            _closed_everywhere(value)
    elif isinstance(schema, tuple):
        for value in schema:
            _closed_everywhere(value)


@pytest.mark.parametrize("name", sorted(EXPECTED_ERRORS))
def test_result_schemas_are_closed_and_never_carry_secrets(name):
    spec = _spec(name)
    _closed_everywhere(spec.result_schema)
    _closed_everywhere(spec.params_schema)
    text = repr(spec.result_schema)
    assert "secret" not in text.lower()
