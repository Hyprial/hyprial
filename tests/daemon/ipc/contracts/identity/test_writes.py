"""Identity writes remain local-operator only over the name-only user store."""

from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.request_catalog.identity import IDENTITY_BINDINGS
from hyprial.daemon.impl.request_catalog.registry import MethodRegistry
from hyprial.identity import UserStore
from hyprial.kernel import DaemonRequestError

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


def add_guest(app, display_name="Guest"):
    return invoke(
        app,
        "identity.users.add",
        {
            "kind": "guest",
            "displayName": display_name,
            "confirmedBy": "isolated-test",
        },
    )["user"]["userKey"]


def test_write_methods_are_operator_only_never_retried_and_host_injected():
    for name in WRITE_METHODS:
        spec = REGISTRY.get(name).spec
        assert (spec.mutates, spec.retry, spec.http_exposed) == (True, "never", True)
        assert spec.idempotency is not None
        assert spec.idempotency.mode == "never"
        assert spec.audience == ("local-operator",)
        assert spec.ipc_param_validation == "handler"
    binding = REGISTRY.get("identity.users.add").spec.http_binding
    assert binding is not None
    assert binding.identity.mode == "local-owner-selector"
    assert binding.identity.param == "confirmedBy"


def test_repeated_member_create_refuses_and_guest_create_never_reuses_key(real_app):
    member = {
        "kind": "member",
        "owner": "member@invalid.test",
        "confirmedBy": "isolated-test",
    }
    invoke(real_app, "identity.users.add", member)
    with pytest.raises(DaemonRequestError) as error:
        invoke(real_app, "identity.users.add", member)
    assert error.value.code == "USER_EXISTS"
    first = add_guest(real_app)
    second = add_guest(real_app)
    assert first != second


def test_worker_cannot_mutate_even_with_valid_schema(real_app):
    params = {"kind": "guest", "displayName": "Guest", "confirmedBy": "isolated-test"}
    with pytest.raises(DaemonRequestError) as error:
        invoke(real_app, "identity.users.add", params, origin="worker")
    assert error.value.code == "IDENTITY_OVERRIDE_FORBIDDEN"
    assert not (real_app.state_dir / "users.sqlite3").exists()


def test_worker_refusal_precedes_handler_field_validation(real_app):
    with pytest.raises(DaemonRequestError) as error:
        invoke(
            real_app,
            "identity.users.add",
            {"kind": [], "unknown": {"forged": True}},
            origin="worker",
        )
    assert error.value.code == "IDENTITY_OVERRIDE_FORBIDDEN"


@pytest.mark.parametrize(
    "extra", [{"user": "guest-forged"}, {"entityToken": "forged"}]
)
def test_users_add_refuses_store_generated_or_extra_fields(real_app, extra):
    params = {
        "kind": "guest",
        "displayName": "Guest",
        "confirmedBy": "isolated-test",
        **extra,
    }
    with pytest.raises(DaemonRequestError) as error:
        invoke(real_app, "identity.users.add", params)
    assert error.value.code == "INVALID_ARGUMENT"


def test_member_requires_owner_and_guest_forbids_owner(real_app):
    with pytest.raises(DaemonRequestError) as member_error:
        invoke(
            real_app,
            "identity.users.add",
            {"kind": "member", "confirmedBy": "isolated-test"},
        )
    assert member_error.value.code == "USER_OWNER_REQUIRED"
    with pytest.raises(DaemonRequestError) as guest_error:
        invoke(
            real_app,
            "identity.users.add",
            {
                "kind": "guest",
                "displayName": "Guest",
                "owner": "someone",
                "confirmedBy": "isolated-test",
            },
        )
    assert guest_error.value.code == "USER_OWNER_FORBIDDEN"


def test_add_creates_the_store_and_mirrors_the_home(real_app):
    key = add_guest(real_app)
    assert (real_app.state_dir / "users.sqlite3").is_file()
    assert (real_app.hyprial_home / "users" / key / "profile.json").is_file()


def test_unopenable_store_is_source_unavailable(real_app):
    (real_app.state_dir / "users.sqlite3").write_bytes(b"not a database" * 64)
    with pytest.raises(DaemonRequestError) as error:
        add_guest(real_app)
    assert error.value.code == "IDENTITY_SOURCE_UNAVAILABLE"


def test_write_result_schema_refuses_extra_authority_fields(real_app, monkeypatch):
    user = {
        "userKey": "guest",
        "kind": "guest",
        "owner": None,
        "nickname": None,
        "realName": None,
        "displayName": "Guest",
        "createdAtMs": 1,
        "updatedAtMs": 1,
        "entityToken": "not-public",
    }
    monkeypatch.setattr(
        UserStore, "add_user", lambda self, **kwargs: SimpleNamespace(to_json=lambda: user)
    )
    with pytest.raises(DaemonRequestError) as error:
        invoke(
            real_app,
            "identity.users.add",
            {
                "kind": "guest",
                "displayName": "Guest",
                "confirmedBy": "isolated-test",
            },
        )
    assert error.value.code == "DAEMON_ERROR"
