"""Identity read contracts expose the name-only user store and fail closed."""

from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.request_catalog.identity import (
    IDENTITY_BINDINGS,
    PARTIAL_MIGRATED_METHODS,
)
from hyprial.daemon.impl.request_catalog.identity.examples import IDENTITY_EXAMPLES
from hyprial.daemon.impl.request_catalog.identity.write_examples import (
    IDENTITY_WRITE_EXAMPLES,
)
from hyprial.daemon.impl.request_catalog.registry import MethodRegistry
from hyprial.identity import UserStore
from hyprial.kernel import DaemonRequestError, export_openapi

READ_METHODS = ("identity.users.list",)
WRITE_METHODS = ("identity.users.add",)
REGISTRY = MethodRegistry.from_bindings(IDENTITY_BINDINGS)


def invoke(app, method, params, *, origin="ipc"):
    return REGISTRY.invoke(app, method, params, trusted_message_origin=origin)


@pytest.fixture
def real_app(tmp_path):
    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    return SimpleNamespace(state_dir=state, hyprial_home=home)


def test_identity_bindings_cover_exactly_the_two_declared_methods():
    assert set(REGISTRY.names) == {*READ_METHODS, *WRITE_METHODS}
    assert len(IDENTITY_BINDINGS) == 2
    assert PARTIAL_MIGRATED_METHODS == frozenset(REGISTRY.names)


def test_read_methods_are_safe_read_only_local_operator_http():
    for name in READ_METHODS:
        binding = REGISTRY.get(name)
        assert binding is not None
        spec = binding.spec
        assert (spec.mutates, spec.retry, spec.http_exposed) == (False, "safe", True)
        assert spec.audience == ("local-operator",)
        assert spec.http_binding is not None
        assert spec.http_binding.identity.mode == "none"


def test_http_projection_omits_host_injected_confirmation_but_daemon_requires_it():
    document = export_openapi(
        REGISTRY.specs,
        example_sets=(*IDENTITY_EXAMPLES, *IDENTITY_WRITE_EXAMPLES),
    )
    for name in WRITE_METHODS:
        assert f"/daemon/request/{name}" in document["paths"]
    spec = REGISTRY.get("identity.users.add").spec
    assert "confirmedBy" in spec.params_schema["required"]
    assert spec.params_schema["properties"]["confirmedBy"]["x-hyprial-http-forbidden"] is True
    schema = document["paths"]["/daemon/request/identity.users.add"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]
    assert "confirmedBy" not in schema["properties"]
    assert "confirmedBy" not in schema.get("required", [])


def test_declared_examples_cover_both_methods():
    assert {
        example.method for example in (*IDENTITY_EXAMPLES, *IDENTITY_WRITE_EXAMPLES)
    } == set(REGISTRY.names)


def test_the_user_store_is_the_only_read_authority(real_app):
    created = invoke(
        real_app,
        "identity.users.add",
        {"kind": "guest", "displayName": "Visitor", "confirmedBy": "isolated-test"},
    )
    assert invoke(real_app, "identity.users.list", {"kind": "guest"}) == {
        "users": [created["user"]]
    }
    assert invoke(real_app, "identity.users.list", {"user": "missing"}) == {"users": []}
    users = UserStore(real_app.state_dir / "users.sqlite3")
    try:
        assert [user.to_json() for user in users.list_users()] == [created["user"]]
    finally:
        users.close()


def test_listing_without_a_store_answers_empty_and_creates_nothing(real_app):
    assert invoke(real_app, "identity.users.list", {}) == {"users": []}
    assert not (real_app.state_dir / "users.sqlite3").exists()


@pytest.mark.parametrize("params", [{"owner": "forged"}, {"kind": 3}])
def test_invalid_params_never_read_the_store(params):
    with pytest.raises(DaemonRequestError) as error:
        REGISTRY.invoke(SimpleNamespace(), "identity.users.list", params)
    assert error.value.code == "INVALID_ARGUMENT"


def test_source_failure_is_not_an_empty_list(real_app):
    (real_app.state_dir / "users.sqlite3").write_bytes(b"not a database" * 64)
    with pytest.raises(DaemonRequestError) as error:
        invoke(real_app, "identity.users.list", {})
    assert error.value.code == "IDENTITY_SOURCE_UNAVAILABLE"


def test_result_schema_refuses_extra_sensitive_user_fields(real_app, monkeypatch):
    invoke(
        real_app,
        "identity.users.add",
        {"kind": "guest", "displayName": "Guest", "confirmedBy": "isolated-test"},
    )
    leaked = SimpleNamespace(
        kind="guest",
        user_key="guest",
        to_json=lambda: {
            "userKey": "guest", "kind": "guest", "owner": None, "nickname": None,
            "realName": None, "displayName": "Guest", "createdAtMs": 1,
            "updatedAtMs": 1, "entityToken": "not-public",
        },
    )
    monkeypatch.setattr(UserStore, "list_users", lambda self: (leaked,))
    with pytest.raises(DaemonRequestError) as error:
        invoke(real_app, "identity.users.list", {})
    assert error.value.code == "DAEMON_ERROR"
