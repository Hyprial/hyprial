"""Handlers map contract params to the service seam; schemas and errors stay closed."""
from __future__ import annotations

import pytest

from hyprial.daemon.impl.provider_keys import api
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.identity import ProviderKeys
from hyprial.kernel import DaemonRequestError, ipc_errors

from .conftest import AGENT_REF, KEY_ID, RESULTS, SECRET

EXPECTED = {"uri": AGENT_REF.uri, "createdAtMs": AGENT_REF.created_at_ms}


def _invoke(app, method, params):
    return METHOD_REGISTRY.invoke(app, method, params, trusted_message_origin="ipc")


def _error(app, method, params):
    with pytest.raises(DaemonRequestError) as caught:
        _invoke(app, method, params)
    assert SECRET not in str(caught.value)
    assert SECRET not in repr(caught.value.data)
    return caught.value


CALLS = [
    (api.USER_PROVIDER_LOGIN,
     {"provider": "anthropic", "kind": "api-key", "secret": SECRET},
     ("login", {"provider": "anthropic", "kind": "api-key", "secret": SECRET, "label": None,
                "environment_names": None}), "login"),
    (api.USER_PROVIDER_LOGIN,
     {"provider": "anthropic", "kind": "api-key", "secret": SECRET, "label": "work",
      "environmentNames": ["ANTHROPIC_API_KEY", "CLAUDE_KEY"]},
     ("login", {"provider": "anthropic", "kind": "api-key", "secret": SECRET, "label": "work",
                "environment_names": ["ANTHROPIC_API_KEY", "CLAUDE_KEY"]}), "login"),
    (api.USER_PROVIDER_LIST, {},
     ("list_keys", {"provider": None, "include_deleted": False}), "list_keys"),
    (api.USER_PROVIDER_LIST, {"provider": "openai", "includeDeleted": True},
     ("list_keys", {"provider": "openai", "include_deleted": True}), "list_keys"),
    (api.USER_PROVIDER_LOGOUT, {"keyId": KEY_ID}, ("logout", {"key_id": KEY_ID}), "logout"),
    (api.USER_PROVIDER_ROTATE, {"keyId": KEY_ID},
     ("rotate", {"key_id": KEY_ID, "replace_secret": None}), "rotate"),
    (api.USER_PROVIDER_ROTATE, {"keyId": KEY_ID, "replaceSecret": SECRET},
     ("rotate", {"key_id": KEY_ID, "replace_secret": SECRET}), "rotate"),
    (api.AGENT_PROVIDER_ALLOW, {"agent": "worker", "provider": "anthropic"},
     ("allow", {"agent": AGENT_REF, "provider": "anthropic", "key_id": None,
                "environment_names": None, "replace": False}), "allow"),
    (api.AGENT_PROVIDER_ALLOW,
     {"agent": "a." + "b" * 26, "provider": "anthropic", "keyId": KEY_ID,
      "environmentNames": ["ANTHROPIC_API_KEY"], "replace": True, "expectedAgent": EXPECTED},
     ("allow", {"agent": AGENT_REF, "provider": "anthropic", "key_id": KEY_ID,
                "environment_names": ["ANTHROPIC_API_KEY"], "replace": True}), "allow"),
    (api.AGENT_PROVIDER_REVOKE, {"agent": "worker", "provider": "anthropic"},
     ("revoke", {"agent": AGENT_REF, "grant_id": "anthropic"}), "revoke"),
    (api.AGENT_PROVIDER_STATUS, {"agent": "worker"},
     ("status", {"agent": AGENT_REF, "provider": None}), "status"),
    (api.AGENT_PROVIDER_STATUS, {"agent": AGENT_REF.uri, "provider": "anthropic",
                                 "expectedAgent": EXPECTED},
     ("status", {"agent": AGENT_REF, "provider": "anthropic"}), "status"),
]


@pytest.mark.parametrize(("method", "params", "call", "result"), CALLS)
def test_method_maps_params_to_service(app, service, resolver, method, params, call, result):
    assert _invoke(app, method, params) == RESULTS[result]
    assert service.calls == [call]
    assert SECRET not in repr(RESULTS[result])
    if method.startswith("agent."):
        assert resolver.calls == [(params["agent"], params.get("expectedAgent"))]
    else:
        assert resolver.calls == []


BAD_PARAMS = [
    (api.USER_PROVIDER_LOGIN, {"provider": "anthropic", "kind": "api-key", "secret": SECRET,
                               "extra": 1}),
    (api.USER_PROVIDER_LOGIN, {"provider": "anthropic", "kind": "api-key",
                               "secret": SECRET + "\nnext"}),
    (api.USER_PROVIDER_LOGIN, {"provider": "anthropic", "kind": "api-key",
                               "secret": SECRET + "\r"}),
    (api.USER_PROVIDER_LOGIN, {"provider": "anthropic", "kind": "api-key", "secret": ""}),
    (api.USER_PROVIDER_LOGIN, {"provider": "anthropic", "kind": "api-key",
                               "secret": "x" * 16385}),
    (api.USER_PROVIDER_LOGIN, {"provider": "anthropic", "kind": "oauth", "secret": SECRET}),
    (api.USER_PROVIDER_LOGIN, {"provider": "../etc", "kind": "api-key", "secret": SECRET}),
    (api.USER_PROVIDER_LOGIN, {"provider": "anthropic", "kind": "api-key", "secret": SECRET,
                               "label": "x" * 129}),
    (api.USER_PROVIDER_LOGIN, {"provider": "anthropic", "kind": "api-key", "secret": SECRET,
                               "environmentNames": []}),
    (api.USER_PROVIDER_LOGIN, {"provider": "anthropic", "kind": "api-key", "secret": SECRET,
                               "environmentNames": ["lower"]}),
    (api.USER_PROVIDER_LOGIN, {"provider": "anthropic", "kind": "api-key", "secret": SECRET,
                               "environmentNames": ["A", "A"]}),
    (api.USER_PROVIDER_LOGIN, {"provider": "anthropic", "kind": "api-key", "secret": SECRET,
                               "environmentNames": [f"N{i}" for i in range(9)]}),
    (api.USER_PROVIDER_LIST, {"includeDeleted": "yes"}),
    (api.USER_PROVIDER_LOGOUT, {}),
    (api.USER_PROVIDER_LOGOUT, {"keyId": "pk.short"}),
    (api.USER_PROVIDER_ROTATE, {"keyId": KEY_ID, "replaceSecret": SECRET + "\n"}),
    (api.USER_PROVIDER_ROTATE, {"keyId": KEY_ID, "replaceSecret": ""}),
    (api.AGENT_PROVIDER_ALLOW, {"agent": "", "provider": "anthropic"}),
    (api.AGENT_PROVIDER_ALLOW, {"agent": "worker", "provider": "anthropic", "replace": 1}),
    (api.AGENT_PROVIDER_ALLOW, {"agent": "worker", "provider": "anthropic",
                                "expectedAgent": {"uri": "x"}}),
    (api.AGENT_PROVIDER_ALLOW, {"agent": "worker", "provider": "anthropic",
                                "expectedAgent": {**EXPECTED, "entityToken": "t"}}),
    (api.AGENT_PROVIDER_REVOKE, {"agent": "worker", "provider": "anthropic",
                                 "expectedAgent": {"uri": "", "createdAtMs": 1}}),
    (api.AGENT_PROVIDER_STATUS, {"agent": "worker",
                                 "expectedAgent": {"uri": "x", "createdAtMs": -1}}),
    (api.AGENT_PROVIDER_STATUS, {"agent": "worker",
                                 "expectedAgent": {"uri": "x", "createdAtMs": True}}),
    (api.AGENT_PROVIDER_STATUS, {"agent": "worker", "grantId": "anthropic"}),
]


@pytest.mark.parametrize(("method", "params"), BAD_PARAMS)
def test_schema_rejections_are_invalid_argument_without_values(app, service, resolver,
                                                               method, params):
    error = _error(app, method, params)
    assert error.code == ipc_errors.INVALID_ARGUMENT
    assert service.calls == [] and resolver.calls == []


@pytest.mark.parametrize("method", [api.AGENT_PROVIDER_ALLOW, api.AGENT_PROVIDER_REVOKE,
                                    api.AGENT_PROVIDER_STATUS])
def test_incarnation_mismatch_and_missing_agent_pass_through(app, service, method):
    params = {"agent": "worker", "provider": "anthropic",
              "expectedAgent": {"uri": AGENT_REF.uri, "createdAtMs": 1}}
    assert _error(app, method, params).code == ipc_errors.AGENT_INCARNATION_MISMATCH
    assert _error(app, method, {"agent": "missing", "provider": "anthropic"}).code \
        == ipc_errors.AGENT_NOT_FOUND
    assert service.calls == []


DECLARED = [
    (api.USER_PROVIDER_LOGIN, ipc_errors.USER_KEY_UNBOUND),
    (api.USER_PROVIDER_LOGIN, ipc_errors.PROVIDER_STORE_CONFLICT),
    (api.USER_PROVIDER_LIST, ipc_errors.PROVIDER_STORE_UNAVAILABLE),
    (api.USER_PROVIDER_LOGOUT, ipc_errors.PROVIDER_KEY_NOT_FOUND),
    (api.USER_PROVIDER_ROTATE, ipc_errors.PROVIDER_KEY_UNSEAL_FAILED),
    (api.AGENT_PROVIDER_ALLOW, ipc_errors.PROVIDER_CREDENTIAL_EXISTS),
    (api.AGENT_PROVIDER_ALLOW, ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING),
    (api.AGENT_PROVIDER_REVOKE, ipc_errors.PROVIDER_GRANT_MISSING),
    (api.AGENT_PROVIDER_STATUS, ipc_errors.PROVIDER_STORE_UNAVAILABLE),
]
UNDECLARED = [
    (api.USER_PROVIDER_LIST, ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID),
    (api.AGENT_PROVIDER_STATUS, ipc_errors.PROVIDER_GRANT_REVOKED),
    (api.USER_PROVIDER_LOGIN, ipc_errors.PROVIDER_KEY_NOT_FOUND),
]
PARAMS = {
    api.USER_PROVIDER_LOGIN: {"provider": "anthropic", "kind": "api-key", "secret": SECRET},
    api.USER_PROVIDER_LIST: {},
    api.USER_PROVIDER_LOGOUT: {"keyId": KEY_ID},
    api.USER_PROVIDER_ROTATE: {"keyId": KEY_ID, "replaceSecret": SECRET},
    api.AGENT_PROVIDER_ALLOW: {"agent": "worker", "provider": "anthropic"},
    api.AGENT_PROVIDER_REVOKE: {"agent": "worker", "provider": "anthropic"},
    api.AGENT_PROVIDER_STATUS: {"agent": "worker"},
}


@pytest.mark.parametrize(("method", "code"), DECLARED)
def test_declared_service_errors_pass_through(app, service, method, code):
    service.error = ProviderKeys.Error(code)
    assert _error(app, method, dict(PARAMS[method])).code == code


@pytest.mark.parametrize(("method", "code"), UNDECLARED)
def test_undeclared_service_errors_become_daemon_error(app, service, method, code):
    service.error = ProviderKeys.Error(code)
    assert _error(app, method, dict(PARAMS[method])).code == ipc_errors.DAEMON_ERROR


@pytest.mark.parametrize("method", sorted(PARAMS))
def test_unexpected_exception_never_echoes_the_secret(app, service, method):
    service.error = ValueError(f"backend broke near {SECRET}")
    assert _error(app, method, dict(PARAMS[method])).code == ipc_errors.DAEMON_ERROR


@pytest.mark.parametrize("method", [api.USER_PROVIDER_LOGIN, api.USER_PROVIDER_ROTATE])
def test_declared_error_message_is_scrubbed_of_the_secret(app, service, method):
    service.error = DaemonRequestError(ipc_errors.PROVIDER_STORE_CONFLICT, f"conflict {SECRET}")
    assert _error(app, method, dict(PARAMS[method])).code == ipc_errors.PROVIDER_STORE_CONFLICT


@pytest.mark.parametrize("method", sorted(PARAMS))
def test_gate_closed_without_service_is_store_unavailable(app, service, method):
    del app._provider_key_service
    assert _error(app, method, dict(PARAMS[method])).code == ipc_errors.PROVIDER_STORE_UNAVAILABLE


@pytest.mark.parametrize(("method", "result"), [
    (api.USER_PROVIDER_LOGIN, "login"), (api.USER_PROVIDER_LIST, "list_keys"),
    (api.AGENT_PROVIDER_ALLOW, "allow"), (api.AGENT_PROVIDER_STATUS, "status"),
])
def test_results_are_validated_and_a_leaked_secret_is_refused(app, service, method, result):
    service.results[result] = {**RESULTS[result], "secret": SECRET}
    assert _error(app, method, dict(PARAMS[method])).code == ipc_errors.DAEMON_ERROR


def test_nested_result_fields_are_closed(app, service):
    service.results["login"] = {"key": {**RESULTS["login"]["key"], "value": SECRET}}
    assert _error(app, api.USER_PROVIDER_LOGIN, dict(PARAMS[api.USER_PROVIDER_LOGIN])).code \
        == ipc_errors.DAEMON_ERROR
    service.results["status"] = {**RESULTS["status"], "grants": [
        {**RESULTS["status"]["grants"][0], "state": "maybe"}]}
    assert _error(app, api.AGENT_PROVIDER_STATUS, {"agent": "worker"}).code \
        == ipc_errors.DAEMON_ERROR


def test_undeclared_user_method_is_not_found(app):
    with pytest.raises(DaemonRequestError) as caught:
        app.handle("user.provider.import", {}, _trusted_message_origin="ipc")
    assert caught.value.code == ipc_errors.METHOD_NOT_FOUND


def test_router_serves_declared_agent_provider_methods_from_the_registry(app, service):
    assert app.handle(api.AGENT_PROVIDER_STATUS, {"agent": "worker"},
                      _trusted_message_origin="ipc") == RESULTS["status"]


WRITE_ONLY = [(api.USER_PROVIDER_LOGIN, "secret"), (api.USER_PROVIDER_ROTATE, "replaceSecret")]


def _with_value(method, field, value):
    return {**PARAMS[method], field: value}


@pytest.mark.parametrize(("method", "field"), WRITE_ONLY)
@pytest.mark.parametrize("value", ["é" * 16384, "\ud800", "\ud800abc", "\udfff" * 3],
                         ids=["over-16384-bytes", "lone-surrogate", "surrogate-prefix",
                              "low-surrogates"])
def test_write_only_value_outside_utf8_byte_bounds_is_invalid_argument(app, service, method,
                                                                        field, value):
    with pytest.raises(DaemonRequestError) as caught:
        _invoke(app, method, _with_value(method, field, value))
    assert caught.value.code == ipc_errors.INVALID_ARGUMENT
    assert value not in str(caught.value) and value not in repr(caught.value.data)
    assert service.calls == []


@pytest.mark.parametrize(("method", "field"), WRITE_ONLY)
@pytest.mark.parametrize("value", ["é" * 8192, "\U0001f600" * 4096, "x" * 16384, "é"],
                         ids=["2-byte-16384", "4-byte-16384", "ascii-16384", "one-char"])
def test_write_only_value_inside_utf8_byte_bounds_reaches_the_service(app, service, method,
                                                                       field, value):
    assert len(value.encode("utf-8")) <= 16384
    _invoke(app, method, _with_value(method, field, value))
    assert len(service.calls) == 1
    assert value in service.calls[0][1].values()


@pytest.mark.parametrize("char", ["​", "‮", "", "\ud800", "\u0085", "͸"],
                         ids=["Cf-zero-width", "Cf-bidi-override", "Co-private-use",
                              "Cs-surrogate", "Cc-next-line", "Cn-unassigned"])
def test_label_with_any_unicode_other_category_is_invalid_argument(app, service, char):
    params = {**PARAMS[api.USER_PROVIDER_LOGIN], "label": f"a{char}b"}
    with pytest.raises(DaemonRequestError) as caught:
        _invoke(app, api.USER_PROVIDER_LOGIN, params)
    assert caught.value.code == ipc_errors.INVALID_ARGUMENT
    assert char not in str(caught.value)
    assert service.calls == []


def test_label_with_printable_unicode_reaches_the_service(app, service):
    params = {**PARAMS[api.USER_PROVIDER_LOGIN], "label": "工作 key é \U0001f600"}
    _invoke(app, api.USER_PROVIDER_LOGIN, params)
    assert service.calls[0][1]["label"] == params["label"]
