"""Contract §8.6/§8.7: which legacy agent.secret.* calls the adapter takes and which stay legacy."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.provider_keys import legacy
from hyprial.identity import ProviderKeys
from hyprial.kernel import DaemonRequestError, ipc_errors

GRANT = {
    "actor": "alpha", "grantId": "slot", "source": "user-provider", "entryId": "deepseek",
    "fieldName": "key", "environmentNames": ["DEEPSEEK_API_KEY"], "revision": 1,
}


class _Poison(dict):
    """Params that fail the test if the adapter reads them."""

    def get(self, *args, **kwargs):
        raise AssertionError("params were read")

    def __getitem__(self, key):
        raise AssertionError("params were read")

    def __contains__(self, key):
        raise AssertionError("params were read")


def test_the_four_legacy_names_are_the_routed_set() -> None:
    assert legacy.LEGACY_SECRET_METHODS == frozenset({
        "agent.secret.provider-write", "agent.secret.grant",
        "agent.secret.list", "agent.secret.revoke",
    })
    assert repr(legacy.NOT_HANDLED) == "NOT_HANDLED"


@pytest.mark.parametrize("method", sorted(legacy.LEGACY_SECRET_METHODS))
def test_a_closed_gate_is_not_handled_before_any_param_read(
    method: str, registry, inventory, make_app,
) -> None:
    app = make_app(None)

    assert legacy.dispatch(app, method, _Poison()) is legacy.NOT_HANDLED
    assert registry.resolutions == []
    assert inventory.reads == []


REFUSALS = [
    ("agent.secret.provider-write", {"entryId": "deepseek", "fieldName": "key", "value": "v"},
     ipc_errors.AGENT_SECRET_RESOLUTION_FAILED),
    ("agent.secret.grant", dict(GRANT), ipc_errors.AGENT_SECRET_RESOLUTION_FAILED),
    ("agent.secret.revoke", {"actor": "alpha", "grantId": "slot"}, ipc_errors.AGENT_SECRET_RESOLUTION_FAILED),
    ("agent.secret.list", {"actor": "alpha"}, "AGENT_ERROR"),
]


def _settings(home: Path, text: str) -> None:
    (home / "settings.json").write_text(text, encoding="utf-8")


@pytest.mark.parametrize("text", [None, "{}", '{"providerKeys": {}}', '{"providerKeys": {"authority": "legacy"}}'])
@pytest.mark.parametrize("method", sorted(legacy.LEGACY_SECRET_METHODS))
def test_a_legacy_gate_file_is_not_handled_before_any_param_read(
    method: str, text: str | None, registry, inventory, make_app, tmp_path: Path,
) -> None:
    if text is not None:
        _settings(tmp_path, text)
    app = make_app(None)
    app.hyprial_home = tmp_path

    assert legacy.dispatch(app, method, _Poison()) is legacy.NOT_HANDLED
    assert registry.resolutions == []
    assert inventory.reads == []


@pytest.mark.parametrize(("method", "params", "code"), REFUSALS)
def test_an_open_gate_with_an_unavailable_store_refuses_instead_of_falling_back(
    method: str, params: dict, code: str, registry, make_app, tmp_path: Path,
) -> None:
    _settings(tmp_path, '{"providerKeys": {"authority": "owner-keys"}}')
    registry.add("alpha")
    app = make_app(None)
    app.hyprial_home = tmp_path

    with pytest.raises(DaemonRequestError) as refused:
        legacy.dispatch(app, method, dict(params))

    assert refused.value.code == code
    assert legacy.dispatch(app, "agent.secret.grant", {**GRANT, "source": "agent-private"}) \
        is legacy.NOT_HANDLED


@pytest.mark.parametrize(("method", "params", "code"), REFUSALS)
def test_an_injected_service_whose_store_is_unavailable_refuses(
    method: str, params: dict, code: str, registry, make_app, new_service, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hyprial.daemon.impl.provider_keys import context

    def unavailable(app):
        raise ProviderKeys.Error(ipc_errors.PROVIDER_STORE_UNAVAILABLE)

    monkeypatch.setattr(context, "service_for", unavailable)
    registry.add("alpha")
    app = make_app(new_service())

    with pytest.raises(DaemonRequestError) as refused:
        legacy.dispatch(app, method, dict(params))

    assert refused.value.code == code


@pytest.mark.parametrize("text", [
    "{not json", "[]", '{"providerKeys": "owner-keys"}', '{"providerKeys": {"authority": "Owner-Keys"}}',
    '{"providerKeys": {"authority": null}}', '{"provider_keys": {"authority": "legacy"}}',
    '{"provider_keys": {"authority": "owner-keys"}}',
])
@pytest.mark.parametrize(("method", "params", "code"), REFUSALS)
def test_a_damaged_gate_file_refuses_instead_of_falling_back(
    method: str, params: dict, code: str, text: str, registry, make_app, tmp_path: Path,
) -> None:
    _settings(tmp_path, text)
    registry.add("alpha")
    app = make_app(None)
    app.hyprial_home = tmp_path

    with pytest.raises(DaemonRequestError) as refused:
        legacy.dispatch(app, method, dict(params))

    assert refused.value.code == code


@pytest.mark.parametrize("method", sorted(legacy.LEGACY_SECRET_METHODS))
def test_the_adapter_uses_the_single_storage_gate_reader(
    method: str, registry, inventory, make_app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hyprial.daemon.impl.provider_keys.storage import gate

    homes = []

    def spy(home):
        homes.append(home)
        return "legacy"

    monkeypatch.setattr(gate, "authority", spy)
    _settings(tmp_path, '{"providerKeys": {"authority": "owner-keys"}}')
    app = make_app(None)
    app.hyprial_home = tmp_path

    assert legacy.dispatch(app, method, _Poison()) is legacy.NOT_HANDLED
    assert homes == [tmp_path]
    assert registry.resolutions == [] and inventory.reads == []
    assert not {"_authority", "LEGACY", "OWNER_KEYS"} & set(vars(legacy))


@pytest.mark.parametrize(("method", "params", "code"), [
    ("agent.secret.provider-write", {"entryId": "deepseek", "fieldName": "key", "value": "v"},
     ipc_errors.AGENT_SECRET_RESOLUTION_FAILED),
    ("agent.secret.grant", dict(GRANT), ipc_errors.AGENT_SECRET_RESOLUTION_FAILED),
    ("agent.secret.revoke", {"actor": "alpha", "grantId": "slot"}, ipc_errors.AGENT_SECRET_RESOLUTION_FAILED),
    ("agent.secret.list", {"actor": "alpha"}, "AGENT_ERROR"),
])
def test_an_open_gate_with_a_broken_owner_context_never_falls_back(
    method: str, params: dict, code: str, registry, make_app, new_service, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hyprial.daemon.impl.provider_keys import context

    def broken(app):
        raise ProviderKeys.Error(ipc_errors.USER_KEY_UNBOUND)

    monkeypatch.setattr(context, "service_for", broken)
    registry.add("alpha")
    app = make_app(new_service())

    with pytest.raises(DaemonRequestError) as refused:
        legacy.dispatch(app, method, params)

    assert refused.value.code == code
    assert legacy.dispatch(app, "agent.secret.grant", {**GRANT, "source": "agent-private"}) \
        is legacy.NOT_HANDLED


def test_other_methods_are_never_handled(app, service) -> None:
    for method in ("agent.list", "agent.provider.login", "agent.secret.set", "agent.create"):
        assert legacy.dispatch(app, method, _Poison()) is legacy.NOT_HANDLED
    assert service.calls == []


@pytest.mark.parametrize("source", ["agent-private", "agent-shared", "bogus", None, 3])
def test_grant_for_other_sources_stays_legacy(app, service, registry,
                                              source: object) -> None:
    registry.add("alpha")
    params = {**GRANT, "source": source, "grantId": "provider-claude"}

    assert legacy.dispatch(app, "agent.secret.grant", params) is legacy.NOT_HANDLED
    assert service.calls == []
    assert registry.resolutions == []


def test_the_internal_claude_credential_grant_stays_legacy(app, service) -> None:
    params = {
        "actor": "alpha", "source": "agent-private", "grantId": "provider-claude",
        "entryId": "claude", "fieldName": "CLAUDE_CODE_OAUTH_TOKEN",
        "environmentNames": ["CLAUDE_CODE_OAUTH_TOKEN"], "revision": 3,
    }

    assert legacy.dispatch(app, "agent.secret.grant", params) is legacy.NOT_HANDLED
    assert service.calls == []


def test_revoke_without_a_new_record_stays_legacy(app, service, registry) -> None:
    registry.add("alpha")

    assert legacy.dispatch(app, "agent.secret.revoke", {"actor": "alpha", "grantId": "routes"}) \
        is legacy.NOT_HANDLED
    assert service.names() == ["grants_for"]


@pytest.mark.parametrize("params", [{}, {"actor": "", "grantId": "x"}, {"actor": "alpha", "grantId": 5}])
def test_revoke_with_bad_params_is_left_to_the_legacy_validation(app, service,
                                                                 params: dict) -> None:
    assert legacy.dispatch(app, "agent.secret.revoke", params) is legacy.NOT_HANDLED
    assert service.calls == []


def test_revoke_for_an_unknown_actor_is_left_to_the_legacy_path(app, service) -> None:
    assert legacy.dispatch(app, "agent.secret.revoke", {"actor": "ghost", "grantId": "x"}) \
        is legacy.NOT_HANDLED
    assert service.calls == []


def test_revoke_with_a_new_record_goes_to_the_new_authority(app, service,
                                                             registry) -> None:
    ref = registry.add("alpha")
    legacy.dispatch(app, "agent.secret.grant", dict(GRANT))

    first = legacy.dispatch(app, "agent.secret.revoke", {"actor": "alpha", "grantId": "slot"})
    again = legacy.dispatch(app, "agent.secret.revoke", {"actor": "alpha", "grantId": "slot"})

    assert first == {"ok": True, "revoked": True}
    assert again == {"ok": True, "revoked": False}
    assert service.records[(ref.agent, "slot")]["state"] == "revoked"
    revokes = [kwargs for name, kwargs in service.calls if name == "revoke"]
    assert revokes == [{"agent": ref, "grant_id": "slot"}, {"agent": ref, "grant_id": "slot"}]


def test_revoke_maps_new_authority_failures(app, service, registry) -> None:
    registry.add("alpha")
    legacy.dispatch(app, "agent.secret.grant", dict(GRANT))
    service.fail["revoke"] = ProviderKeys.Error(ipc_errors.USER_KEY_UNBOUND)

    with pytest.raises(DaemonRequestError) as refused:
        legacy.dispatch(app, "agent.secret.revoke", {"actor": "alpha", "grantId": "slot"})

    assert refused.value.code == ipc_errors.AGENT_SECRET_RESOLUTION_FAILED


def test_revoke_fails_closed_when_the_slot_cannot_be_read(app, service,
                                                          registry) -> None:
    registry.add("alpha")
    service.fail["grants_for"] = ProviderKeys.Error(ipc_errors.PROVIDER_STORE_UNAVAILABLE)

    with pytest.raises(DaemonRequestError) as refused:
        legacy.dispatch(app, "agent.secret.revoke", {"actor": "alpha", "grantId": "slot"})

    assert refused.value.code == ipc_errors.AGENT_SECRET_RESOLUTION_FAILED
