"""Contract §8.1/§8.5: legacy ``agent.secret.provider-write`` onto login/rotate of a legacyRef key."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.provider_keys import legacy
from hyprial.identity import ProviderKeys
from hyprial.kernel import DaemonRequestError, ipc_errors

SENTINEL = "sk-legacy-sentinel-value-000"
WRITE = "agent.secret.provider-write"


def _write(app, **changes):
    return legacy.dispatch(app, WRITE, {"entryId": "deepseek", "fieldName": "key", "value": SENTINEL,
                                        **changes})


def test_a_first_write_logs_in_a_legacy_key(app, service) -> None:
    result = _write(app)

    assert result == {"ok": True, "entryId": "deepseek", "fieldNames": ["key"]}
    login = next(kwargs for name, kwargs in service.calls if name == "login")
    assert login == {
        "provider": "deepseek", "kind": "api-key", "label": "legacy:deepseek/key",
        "environment_names": None, "legacy_ref": {"entryId": "deepseek", "fieldName": "key"},
        "secret_length": len(SENTINEL),
    }
    assert "rotate" not in service.names()


def test_a_second_write_replaces_the_secret_of_the_same_key(app, service) -> None:
    _write(app)
    key_id = service.keys[0]["keyId"]

    result = _write(app, value="sk-replacement")

    assert result == {"ok": True, "entryId": "deepseek", "fieldNames": ["key"]}
    assert service.names().count("login") == 1
    rotate = next(kwargs for name, kwargs in service.calls if name == "rotate")
    assert rotate == {"key_id": key_id, "replaced": True, "secret_length": len("sk-replacement")}


def test_a_different_field_is_a_different_key(app, service) -> None:
    _write(app)
    _write(app, fieldName="secret")

    assert service.names().count("login") == 2
    assert [key["legacyRef"] for key in service.keys] == [
        {"entryId": "deepseek", "fieldName": "key"}, {"entryId": "deepseek", "fieldName": "secret"},
    ]


def test_a_write_completes_an_awaiting_grant_through_login(app, service, registry) -> None:
    ref = registry.add("alpha")
    legacy.dispatch(app, "agent.secret.grant", {
        "actor": "alpha", "grantId": "slot", "source": "user-provider", "entryId": "deepseek",
        "fieldName": "key", "environmentNames": ["DEEPSEEK_API_KEY"], "revision": 1,
    })
    assert service.records[(ref.agent, "slot")]["state"] == "awaiting-key"

    _write(app)

    assert service.names()[-2:] == ["find_key_by_legacy_ref", "login"]


@pytest.mark.parametrize(("changes", "message"), [
    ({"entryId": ""}, "entryId must be a non-empty string"),
    ({"fieldName": None}, "fieldName must be a non-empty string"),
    ({"value": ""}, "secret model-vendor value must be non-empty"),
    ({"value": 5}, "secret model-vendor value must be non-empty"),
    ({"value": "a\nb"}, "secret model-vendor value must contain one line"),
    ({"value": "a\rb"}, "secret model-vendor value must contain one line"),
])
def test_legacy_param_messages_are_preserved(app, service, changes, message) -> None:
    with pytest.raises(DaemonRequestError) as refused:
        _write(app, **changes)

    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert str(refused.value) == message
    assert service.calls == []


@pytest.mark.parametrize("changes", [{"entryId": "../x"}, {"entryId": "a" * 129}, {"fieldName": "a b"}])
def test_unsafe_segments_are_invalid_arguments(app, service, changes) -> None:
    with pytest.raises(DaemonRequestError) as refused:
        _write(app, **changes)

    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert SENTINEL not in str(refused.value)
    assert service.calls == []


@pytest.mark.parametrize("code", [
    ipc_errors.USER_KEY_UNBOUND, ipc_errors.USER_KEY_QUARANTINED, ipc_errors.PROVIDER_STORE_UNAVAILABLE,
    ipc_errors.PROVIDER_STORE_CONFLICT, ipc_errors.PROVIDER_KEY_UNSEAL_FAILED,
    ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID, ipc_errors.PROVIDER_KEY_NOT_FOUND,
])
@pytest.mark.parametrize("step", ["find_key_by_legacy_ref", "login", "rotate"])
def test_new_authority_failures_map_to_secret_resolution_failed(app, service, code, step) -> None:
    if step == "rotate":
        _write(app)
    service.fail[step] = ProviderKeys.Error(code)

    with pytest.raises(DaemonRequestError) as refused:
        _write(app)

    assert refused.value.code == ipc_errors.AGENT_SECRET_RESOLUTION_FAILED
    assert str(refused.value) == "provider key authority refused the request"
    assert refused.value.data is None
    assert SENTINEL not in repr(refused.value)


def test_service_invalid_arguments_pass_through(app, service) -> None:
    service.fail["login"] = DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "secret is too long")

    with pytest.raises(DaemonRequestError) as refused:
        _write(app)

    assert refused.value.code == ipc_errors.INVALID_ARGUMENT


def test_a_long_legacy_label_is_capped_to_the_record_bound(app, service) -> None:
    _write(app, entryId="e" * 128, fieldName="f" * 128)

    login = next(kwargs for name, kwargs in service.calls if name == "login")
    assert login["label"] == ("legacy:" + "e" * 128 + "/" + "f" * 128)[:128]
    assert login["legacy_ref"] == {"entryId": "e" * 128, "fieldName": "f" * 128}
