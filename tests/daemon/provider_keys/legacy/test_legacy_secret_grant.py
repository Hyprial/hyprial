"""Contract §8.1/§8.3/§8.4/§8.5: legacy ``agent.secret.grant`` with ``source=user-provider``."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.provider_keys import legacy
from hyprial.identity import AgentError, ProviderKeys
from hyprial.kernel import DaemonRequestError, ipc_errors

FIRST_RUN = {
    "actor": "alpha", "grantId": "gui-first-run-pi-deepseek", "source": "user-provider",
    "entryId": "deepseek", "fieldName": "key", "environmentNames": ["DEEPSEEK_API_KEY"], "revision": 1,
}
ROW_KEYS = {"actor", "grantId", "source", "entryId", "fieldName", "environmentNames", "revision",
            "ownerActor", "ownerEntityToken"}
REFUSED = "provider key authority refused the request"


def _grant(app, **changes):
    return legacy.dispatch(app, "agent.secret.grant", {**FIRST_RUN, **changes})


def _expected_row(**changes):
    row = {key: value for key, value in FIRST_RUN.items()}
    row.update(ownerActor=None, ownerEntityToken=None)
    row.update(changes)
    return row


def _writes(service):
    return [name for name in service.names() if name in {"allow", "put_awaiting", "revoke"}]


def test_a_grant_on_an_existing_legacy_key_is_an_active_slot(app, service, registry) -> None:
    ref = registry.add("alpha")
    service.login(provider="deepseek", kind="api-key", secret="x",
                  legacy_ref={"entryId": "deepseek", "fieldName": "key"})
    service.calls.clear()

    result = _grant(app)

    assert result == {"ok": True, "grant": _expected_row()}
    assert set(result["grant"]) == ROW_KEYS
    allow = next(kwargs for name, kwargs in service.calls if name == "allow")
    assert allow == {
        "agent": ref, "provider": "deepseek", "key_id": service.keys[0]["keyId"],
        "environment_names": ["DEEPSEEK_API_KEY"], "replace": True,
        "grant_id": "gui-first-run-pi-deepseek",
        "legacy_ref": {"entryId": "deepseek", "fieldName": "key", "revision": 1},
    }
    assert service.records[(ref.agent, "gui-first-run-pi-deepseek")]["state"] == "active"


def test_a_grant_before_the_entry_exists_awaits_the_key(app, service, registry) -> None:
    ref = registry.add("alpha")

    result = _grant(app)

    assert result == {"ok": True, "grant": _expected_row()}
    awaiting = next(kwargs for name, kwargs in service.calls if name == "put_awaiting")
    assert awaiting == {
        "agent": ref, "grant_id": "gui-first-run-pi-deepseek", "provider": "deepseek",
        "environment_names": ["DEEPSEEK_API_KEY"],
        "legacy_ref": {"entryId": "deepseek", "fieldName": "key", "revision": 1},
    }
    assert "allow" not in service.names()
    assert service.records[(ref.agent, "gui-first-run-pi-deepseek")]["state"] == "awaiting-key"


def test_an_equivalent_replay_is_idempotent(app, service, registry) -> None:
    ref = registry.add("alpha")
    first = _grant(app)
    seq = service.records[(ref.agent, "gui-first-run-pi-deepseek")]["seq"]

    again = _grant(app)

    assert again == first
    assert _writes(service) == ["put_awaiting"]
    assert service.records[(ref.agent, "gui-first-run-pi-deepseek")]["seq"] == seq


@pytest.mark.parametrize("change", [
    {"environmentNames": ["OTHER_KEY"]}, {"fieldName": "secret"}, {"entryId": "moonshot"},
])
@pytest.mark.parametrize("revision", [1, 2])
def test_different_content_must_raise_the_revision(app, service, registry, change, revision) -> None:
    registry.add("alpha")
    _grant(app, revision=2)

    with pytest.raises(DaemonRequestError) as refused:
        _grant(app, revision=revision, **change)

    assert refused.value.code == AgentError.code
    assert str(refused.value) == "secret grant revision must increase"
    assert _writes(service) == ["put_awaiting"]


def test_a_higher_revision_rewrites_the_slot(app, service, registry) -> None:
    ref = registry.add("alpha")
    _grant(app)

    result = _grant(app, revision=2, environmentNames=["OTHER_KEY"])

    assert result["grant"] == _expected_row(revision=2, environmentNames=["OTHER_KEY"])
    assert _writes(service) == ["put_awaiting", "put_awaiting"]
    assert service.records[(ref.agent, "gui-first-run-pi-deepseek")]["seq"] == 2


def test_a_revoked_slot_accepts_a_new_grant_without_a_higher_revision(app, service, registry) -> None:
    ref = registry.add("alpha")
    _grant(app)
    legacy.dispatch(app, "agent.secret.revoke", {"actor": "alpha", "grantId": "gui-first-run-pi-deepseek"})

    result = _grant(app)

    assert result["grant"] == _expected_row()
    assert service.records[(ref.agent, "gui-first-run-pi-deepseek")]["state"] == "awaiting-key"
    assert _writes(service) == ["put_awaiting", "revoke", "put_awaiting"]


def test_a_slot_written_by_the_new_api_is_replaced_by_the_legacy_grant(app, service, registry) -> None:
    ref = registry.add("alpha")
    service.allow(agent=ref, provider="deepseek", key_id="pk." + "b" * 26, environment_names=["X"],
                  grant_id="gui-first-run-pi-deepseek")

    result = _grant(app)

    assert result["grant"] == _expected_row()
    assert service.records[(ref.agent, "gui-first-run-pi-deepseek")]["legacyRef"]["revision"] == 1


@pytest.mark.parametrize("expected", [
    None, "x", [], {}, {"uri": "u"}, {"createdAtMs": 1},
    {"uri": "", "createdAtMs": 1}, {"uri": 3, "createdAtMs": 1},
    {"uri": "u", "createdAtMs": -1}, {"uri": "u", "createdAtMs": True},
    {"uri": "u", "createdAtMs": 1.0}, {"uri": "u", "createdAtMs": "1"},
    {"uri": "u", "createdAtMs": 1, "extra": 1},
])
def test_a_malformed_expected_agent_is_invalid(app, service, registry, expected) -> None:
    registry.add("alpha")

    with pytest.raises(DaemonRequestError) as refused:
        _grant(app, expectedAgent=expected)

    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert str(refused.value) == "invalid expectedAgent"
    assert registry.resolutions == []
    assert service.calls == []


def test_a_matching_expected_agent_binds_the_snapshot(app, service, registry) -> None:
    ref = registry.add("alpha")
    expected = {"uri": ref.uri, "createdAtMs": ref.created_at_ms}

    _grant(app, expectedAgent=expected)

    assert registry.resolutions[0] == ("alpha", expected)
    assert service.records[(ref.agent, "gui-first-run-pi-deepseek")]["agent"] == ref.agent


def test_an_expected_agent_mismatch_is_raised_as_is(app, service, registry) -> None:
    ref = registry.add("alpha")

    with pytest.raises(DaemonRequestError) as refused:
        _grant(app, expectedAgent={"uri": ref.uri, "createdAtMs": ref.created_at_ms + 1})

    assert refused.value.code == ipc_errors.AGENT_INCARNATION_MISMATCH
    assert _writes(service) == []


def test_an_incarnation_change_before_the_write_refuses_without_writing(app, service, registry,
                                                                        new_ref) -> None:
    ref = registry.add("alpha")
    registry.changes[2] = ("alpha", new_ref("alpha", created_at_ms=ref.created_at_ms))

    with pytest.raises(DaemonRequestError) as refused:
        _grant(app, expectedAgent={"uri": ref.uri, "createdAtMs": ref.created_at_ms})

    assert refused.value.code == ipc_errors.AGENT_INCARNATION_MISMATCH
    assert _writes(service) == []
    assert service.records == {}


def test_an_unknown_actor_keeps_agent_not_found(app, service) -> None:
    with pytest.raises(DaemonRequestError) as refused:
        _grant(app, actor="ghost")

    assert refused.value.code == ipc_errors.AGENT_NOT_FOUND
    assert _writes(service) == []


@pytest.mark.parametrize(("changes", "message"), [
    ({"actor": ""}, "actor must be a non-empty string"),
    ({"grantId": None}, "grantId must be a non-empty string"),
    ({"entryId": 4}, "entryId must be a non-empty string"),
    ({"fieldName": ""}, "fieldName must be a non-empty string"),
    ({"environmentNames": []}, "environmentNames must be a non-empty string array"),
    ({"environmentNames": "DEEPSEEK_API_KEY"}, "environmentNames must be a non-empty string array"),
    ({"environmentNames": [""]}, "environmentNames must be a non-empty string array"),
    ({"revision": None}, "revision is required"),
])
def test_legacy_param_messages_are_preserved(app, service, registry, changes, message) -> None:
    registry.add("alpha")

    with pytest.raises(DaemonRequestError) as refused:
        _grant(app, **changes)

    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert str(refused.value) == message
    assert service.calls == []


@pytest.mark.parametrize("changes", [
    {"revision": True}, {"revision": 0}, {"revision": "1"},
    {"grantId": "../escape"}, {"entryId": "-lead"}, {"fieldName": "a/b"},
    {"environmentNames": ["lower"]}, {"environmentNames": ["PATH"]},
    {"environmentNames": ["HYPRIAL_TOKEN"]}, {"environmentNames": ["ANTHROPIC_OAUTH_TOKEN"]},
    {"environmentNames": ["A", "A"]}, {"environmentNames": [f"N{index}" for index in range(9)]},
    {"ownerActor": "beta"},
])
def test_unsafe_grant_fields_are_invalid_arguments(app, service, registry, changes) -> None:
    registry.add("alpha")

    with pytest.raises(DaemonRequestError) as refused:
        _grant(app, **changes)

    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert _writes(service) == []


@pytest.mark.parametrize("code", [
    ipc_errors.USER_KEY_UNBOUND, ipc_errors.USER_KEY_ABSENT, ipc_errors.PROVIDER_STORE_UNAVAILABLE,
    ipc_errors.PROVIDER_STORE_CONFLICT, ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID,
    ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING, ipc_errors.PROVIDER_CREDENTIAL_EXISTS,
])
@pytest.mark.parametrize("step", ["find_key_by_legacy_ref", "grants_for", "allow"])
def test_new_authority_failures_map_to_secret_resolution_failed(app, service, registry, code, step) -> None:
    registry.add("alpha")
    service.login(provider="deepseek", kind="api-key", secret="x",
                  legacy_ref={"entryId": "deepseek", "fieldName": "key"})
    service.fail[step] = ProviderKeys.Error(code)

    with pytest.raises(DaemonRequestError) as refused:
        _grant(app)

    assert refused.value.code == ipc_errors.AGENT_SECRET_RESOLUTION_FAILED
    assert str(refused.value) == REFUSED


def test_an_unavailable_agent_resolution_maps_to_secret_resolution_failed(app, service, registry) -> None:
    registry.fail = ProviderKeys.Error(ipc_errors.PROVIDER_STORE_UNAVAILABLE)

    with pytest.raises(DaemonRequestError) as refused:
        _grant(app)

    assert refused.value.code == ipc_errors.AGENT_SECRET_RESOLUTION_FAILED
    assert _writes(service) == []
