"""Contract §8.2/§8.3/§8.4: legacy ``agent.secret.list`` rows projected from signed records."""

from __future__ import annotations

import copy

import pytest

from hyprial.daemon.impl.provider_keys import legacy
from hyprial.identity import AgentError, ProviderKeys, SecretSource
from hyprial.kernel import DaemonRequestError, ipc_errors


def _row(actor, grant_id, source="user-provider", entry_id="deepseek", field_name="key",
         names=("DEEPSEEK_API_KEY",), revision=1):
    return {"actor": actor, "grantId": grant_id, "source": source, "entryId": entry_id,
            "fieldName": field_name, "environmentNames": list(names), "revision": revision,
            "ownerActor": None, "ownerEntityToken": None}


def _grant(app, actor, grant_id, **changes):
    params = {"actor": actor, "grantId": grant_id, "source": "user-provider", "entryId": "deepseek",
              "fieldName": "key", "environmentNames": ["DEEPSEEK_API_KEY"], "revision": 1, **changes}
    return legacy.dispatch(app, "agent.secret.grant", params)


def _list(app, **params):
    return legacy.dispatch(app, "agent.secret.list", params)


def test_user_provider_rows_come_from_records_and_other_sources_from_the_legacy_table(
    app, service, registry, inventory,
) -> None:
    ref = registry.add("alpha")
    inventory.add("alpha", "routes", entry_id="cloudflare-routes", field_name="TOKEN", names=("CF_TOKEN",))
    inventory.add("alpha", "shared", SecretSource.AGENT_SHARED, entry_id="bundle")
    service.login(provider="deepseek", kind="api-key", secret="x",
                  legacy_ref={"entryId": "deepseek", "fieldName": "key"})
    _grant(app, "alpha", "gui-first-run-pi-deepseek")
    _grant(app, "alpha", "pending", entryId="moonshot", environmentNames=["MOONSHOT_API_KEY"], revision=4)
    _grant(app, "alpha", "gone")
    legacy.dispatch(app, "agent.secret.revoke", {"actor": "alpha", "grantId": "gone"})
    service.allow(agent=ref, provider="openai", key_id="pk." + "c" * 26, environment_names=["OPENAI_API_KEY"])

    result = _list(app, actor="alpha")

    assert result == {"ok": True, "grants": [
        _row("alpha", "gui-first-run-pi-deepseek"),
        _row("alpha", "pending", entry_id="moonshot", names=("MOONSHOT_API_KEY",), revision=4),
        _row("alpha", "routes", "agent-private", "cloudflare-routes", "TOKEN", ("CF_TOKEN",)),
        _row("alpha", "shared", "agent-shared", "bundle", "field", ("NAME",)),
    ]}
    states = {grant_id: record["state"] for (_, grant_id), record in service.records.items()}
    assert states == {"gui-first-run-pi-deepseek": "active", "pending": "awaiting-key",
                      "gone": "revoked", "openai": "active"}


def test_a_new_record_shadows_the_legacy_row_of_the_same_slot(app, service, registry, inventory) -> None:
    registry.add("alpha")
    inventory.add("alpha", "held", SecretSource.USER_PROVIDER, entry_id="old", field_name="key")
    inventory.add("alpha", "revoked-held", SecretSource.USER_PROVIDER, entry_id="old", field_name="key")
    inventory.add("alpha", "private-held", entry_id="old", field_name="key")
    inventory.add("alpha", "untouched", SecretSource.USER_PROVIDER, entry_id="kept", field_name="key")
    _grant(app, "alpha", "held", revision=7)
    _grant(app, "alpha", "revoked-held")
    _grant(app, "alpha", "private-held")
    legacy.dispatch(app, "agent.secret.revoke", {"actor": "alpha", "grantId": "revoked-held"})

    result = _list(app, actor="alpha")

    assert result["grants"] == [
        _row("alpha", "held", revision=7),
        _row("alpha", "private-held"),
        _row("alpha", "untouched", entry_id="kept", names=("NAME",)),
    ]


def test_an_all_agents_list_merges_every_local_agent(app, service, registry, inventory) -> None:
    registry.add("beta")
    registry.add("alpha")
    inventory.add("beta", "routes")
    _grant(app, "beta", "deep")
    _grant(app, "alpha", "zeta")

    result = _list(app)

    assert [(row["actor"], row["grantId"]) for row in result["grants"]] == [
        ("alpha", "zeta"), ("beta", "deep"), ("beta", "routes"),
    ]
    assert legacy.dispatch(app, "agent.secret.list", {"actor": None}) == result


def test_an_all_agents_list_skips_an_agent_that_vanished_between_reads(app, registry, monkeypatch) -> None:
    from types import SimpleNamespace

    registry.add("alpha")
    _grant(app, "alpha", "deep")
    monkeypatch.setattr(app.agents, "list", lambda: (SimpleNamespace(actor="alpha"), SimpleNamespace(actor="gone")))

    assert [row["grantId"] for row in _list(app)["grants"]] == ["deep"]


def test_records_of_another_incarnation_are_never_listed(app, service, registry, new_ref) -> None:
    old = registry.add("alpha")
    _grant(app, "alpha", "deep")
    rebuilt = new_ref("alpha", created_at_ms=old.created_at_ms + 5)
    registry.refs["alpha"] = rebuilt
    stray = copy.deepcopy(service.records[(old.agent, "deep")])
    stray["grantId"] = "stray"
    service.records[(rebuilt.agent, "stray")] = stray

    assert _list(app, actor="alpha")["grants"] == []
    assert _list(app, actor="alpha", expectedAgent={"uri": rebuilt.uri,
                                                    "createdAtMs": rebuilt.created_at_ms})["grants"] == []


def test_an_expected_agent_binds_the_listing_to_one_incarnation(app, service, registry) -> None:
    ref = registry.add("alpha")
    _grant(app, "alpha", "deep")
    expected = {"uri": ref.uri, "createdAtMs": ref.created_at_ms}

    assert _list(app, actor="alpha", expectedAgent=expected)["grants"] == [_row("alpha", "deep")]
    assert ("alpha", expected) in registry.resolutions

    with pytest.raises(DaemonRequestError) as mismatch:
        _list(app, actor="alpha", expectedAgent={**expected, "createdAtMs": ref.created_at_ms - 1})
    assert mismatch.value.code == ipc_errors.AGENT_INCARNATION_MISMATCH


def test_legacy_rows_of_another_incarnation_fail_an_expected_agent_listing(app, registry, inventory) -> None:
    ref = registry.add("alpha")
    inventory.add("alpha", "routes", entity_token="f" * 32)

    with pytest.raises(DaemonRequestError) as mismatch:
        _list(app, actor="alpha", expectedAgent={"uri": ref.uri, "createdAtMs": ref.created_at_ms})

    assert mismatch.value.code == ipc_errors.AGENT_INCARNATION_MISMATCH


@pytest.mark.parametrize("params", [
    {"expectedAgent": {"uri": "u", "createdAtMs": 1}},
    {"actor": None, "expectedAgent": {"uri": "u", "createdAtMs": 1}},
    {"actor": "alpha", "expectedAgent": {"uri": "u"}},
    {"actor": "alpha", "expectedAgent": {"uri": "u", "createdAtMs": False}},
    {"actor": "alpha", "expectedAgent": []},
])
def test_a_malformed_expected_agent_is_invalid(app, registry, params) -> None:
    registry.add("alpha")

    with pytest.raises(DaemonRequestError) as refused:
        _list(app, **params)

    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert str(refused.value) == "invalid expectedAgent"
    assert registry.resolutions == []


@pytest.mark.parametrize("actor", ["", 5, []])
def test_a_bad_actor_keeps_the_legacy_message(app, actor) -> None:
    with pytest.raises(DaemonRequestError) as refused:
        _list(app, actor=actor)

    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert str(refused.value) == "actor must be a non-empty string"


def test_an_unknown_actor_is_agent_not_found(app) -> None:
    with pytest.raises(DaemonRequestError) as refused:
        _list(app, actor="ghost")

    assert refused.value.code == ipc_errors.AGENT_NOT_FOUND


@pytest.mark.parametrize("code", [
    ipc_errors.PROVIDER_STORE_UNAVAILABLE, ipc_errors.PROVIDER_STORE_CONFLICT,
    ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID, ipc_errors.USER_KEY_UNBOUND,
])
@pytest.mark.parametrize("actor", ["alpha", None])
def test_authority_failures_map_to_the_declared_agent_error(app, service, registry, code, actor) -> None:
    registry.add("alpha")
    service.fail["grants_for"] = ProviderKeys.Error(code)

    with pytest.raises(DaemonRequestError) as refused:
        _list(app, actor=actor)

    assert refused.value.code == AgentError.code
    assert str(refused.value) == "provider key authority unavailable"


def test_an_unavailable_agent_resolution_maps_to_agent_error(app, registry) -> None:
    registry.add("alpha")
    registry.fail = ProviderKeys.Error(ipc_errors.PROVIDER_STORE_UNAVAILABLE)

    with pytest.raises(DaemonRequestError) as refused:
        _list(app, actor="alpha")

    assert refused.value.code == AgentError.code


def test_the_projection_is_recovered_from_records_alone(app, service, registry, make_app,
                                                        new_service) -> None:
    registry.add("alpha")
    service.login(provider="deepseek", kind="api-key", secret="x",
                  legacy_ref={"entryId": "deepseek", "fieldName": "key"})
    _grant(app, "alpha", "gui-first-run-pi-deepseek", revision=3)
    _grant(app, "alpha", "later", entryId="moonshot", environmentNames=["MOONSHOT_API_KEY"], revision=2)
    before = _list(app, actor="alpha")

    replica = new_service(records=copy.deepcopy(service.records))
    recovered = _list(make_app(replica), actor="alpha")

    assert recovered == before
    assert replica.names() == ["grants_for"]
    assert recovered["grants"][0] == _row("alpha", "gui-first-run-pi-deepseek", revision=3)


def test_an_equivalent_replay_returns_the_listed_row(app, registry) -> None:
    registry.add("alpha")
    granted = _grant(app, "alpha", "gui-first-run-pi-deepseek")["grant"]

    assert _grant(app, "alpha", "gui-first-run-pi-deepseek")["grant"] == granted
    assert _list(app, actor="alpha")["grants"] == [granted]


@pytest.mark.parametrize("owner_local", [False, True])
def test_expected_agent_fences_inventory_even_when_there_are_no_rows(
    app, registry, inventory, monkeypatch, owner_local,
):
    ref = registry.add("alpha")
    expected = {"uri": ref.uri, "createdAtMs": ref.created_at_ms}
    monkeypatch.setattr(legacy, "_list_ref", lambda *args, **kwargs: ref if owner_local else None)

    def read(actor, *, expected_uri=None, expected_created_at_ms=None):
        if expected_uri == ref.uri and expected_created_at_ms == ref.created_at_ms:
            raise DaemonRequestError(ipc_errors.AGENT_INCARNATION_MISMATCH, "target changed")
        return ()

    monkeypatch.setattr(inventory, "read_secret_inventory", read)
    with pytest.raises(DaemonRequestError) as refused:
        _list(app, actor="alpha", expectedAgent=expected)
    assert refused.value.code == ipc_errors.AGENT_INCARNATION_MISMATCH
