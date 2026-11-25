"""All seven methods through METHOD_REGISTRY.invoke against the real owner service [security].

The registry validates params and results against the declared schemas, so every
success here proves the real service output matches the frozen §5 shapes.
"""
from __future__ import annotations

import json

import pytest

from hyprial.daemon.impl.provider_keys import api
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.kernel import DaemonRequestError, ipc_errors

from ...ipc.conftest import make_app
from ..conftest import NOW, OTHER_SECRET, SECRET


@pytest.fixture
def app(world, tmp_path):
    instance = make_app(tmp_path)
    for name in ("hyprial_home", "state_dir", "owner", "agents", "_user_key_signer",
                 "_provider_key_space_resolver", "_provider_key_owner_key_port", "_provider_key_clock"):
        setattr(instance, name, getattr(world.app, name))
    return instance


def _invoke(app, method, params):
    result = METHOD_REGISTRY.invoke(app, method, params, trusted_message_origin="ipc")
    assert SECRET not in json.dumps(result) and OTHER_SECRET not in json.dumps(result)
    return result


def _error(app, method, params) -> str:
    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(app, method, params, trusted_message_origin="ipc")
    assert SECRET not in str(caught.value) and SECRET not in repr(caught.value.data)
    return caught.value.code


def test_seven_methods_round_trip_through_the_registry(world, app):
    login = _invoke(app, api.USER_PROVIDER_LOGIN, {
        "provider": "anthropic", "kind": "api-key", "secret": SECRET, "label": "work",
        "environmentNames": ["ANTHROPIC_API_KEY"]})
    key_id = login["key"]["keyId"]
    assert login["key"]["state"] == "active" and login["key"]["label"] == "work"
    listed = _invoke(app, api.USER_PROVIDER_LIST, {"provider": "anthropic"})
    assert [row["keyId"] for row in listed["keys"]] == [key_id] and listed["invalid"] == []
    agent = world.agent
    expected = {"uri": agent.uri, "createdAtMs": agent.created_at_ms}
    allowed = _invoke(app, api.AGENT_PROVIDER_ALLOW, {"agent": agent.agent_id, "provider": "anthropic",
                                                     "expectedAgent": expected})
    assert allowed["changed"] is True and allowed["grant"]["keyId"] == key_id
    status = _invoke(app, api.AGENT_PROVIDER_STATUS, {"agent": agent.uri})
    assert [(row["grantId"], row["state"], row["unsealable"]) for row in status["grants"]] == \
        [("anthropic", "ok", True)]
    rotated = _invoke(app, api.USER_PROVIDER_ROTATE, {"keyId": key_id, "replaceSecret": OTHER_SECRET})
    assert rotated["reissuedGrants"] == 1 and rotated["key"]["dekVersion"] == 2
    _, secrets = world.service().launch_secrets(world.ref(), now_ms=NOW + 1)
    assert [secret.value for secret in secrets] == [OTHER_SECRET]
    revoked = _invoke(app, api.AGENT_PROVIDER_REVOKE, {"agent": "worker", "provider": "anthropic"})
    assert revoked["changed"] is True and revoked["restart"] == {"state": "not-supported"}
    logout = _invoke(app, api.USER_PROVIDER_LOGOUT, {"keyId": key_id})
    assert logout == {"keyId": key_id, "state": "deleted", "revokedGrants": [], "alreadyDeleted": False}
    deleted = _invoke(app, api.USER_PROVIDER_LIST, {"includeDeleted": True})
    assert deleted["keys"][0]["grants"][0]["state"] == "revoked"


def test_logout_through_the_registry_lists_revoked_grants(world, app):
    key_id = _invoke(app, api.USER_PROVIDER_LOGIN, {"provider": "anthropic", "kind": "api-key",
                                                    "secret": SECRET})["key"]["keyId"]
    _invoke(app, api.AGENT_PROVIDER_ALLOW, {"agent": "worker", "provider": "anthropic", "keyId": key_id})
    logout = _invoke(app, api.USER_PROVIDER_LOGOUT, {"keyId": key_id})
    assert logout["revokedGrants"] == [{"agent": world.ref().agent, "agentId": world.agent.agent_id,
                                        "grantId": "anthropic", "provider": "anthropic"}]


@pytest.mark.parametrize(("method", "params"), [
    (api.USER_PROVIDER_LOGIN, {"provider": "anthropic", "kind": "api-key", "secret": SECRET}),
    (api.USER_PROVIDER_LIST, {}),
    (api.USER_PROVIDER_LOGOUT, {"keyId": "pk." + "a" * 26}),
    (api.USER_PROVIDER_ROTATE, {"keyId": "pk." + "a" * 26}),
    (api.AGENT_PROVIDER_ALLOW, {"agent": "worker", "provider": "anthropic"}),
    (api.AGENT_PROVIDER_REVOKE, {"agent": "worker", "provider": "anthropic"}),
    (api.AGENT_PROVIDER_STATUS, {"agent": "worker"}),
])
def test_legacy_gate_refuses_every_method(world, app, method, params):
    world.settings({"providerKeys": {"authority": "legacy"}})
    assert _error(app, method, params) == ipc_errors.PROVIDER_STORE_UNAVAILABLE


def test_declared_refusals_pass_through_the_registry(world, app):
    agent = world.agent
    assert _error(app, api.AGENT_PROVIDER_STATUS, {"agent": "ghost"}) == ipc_errors.AGENT_NOT_FOUND
    assert _error(app, api.AGENT_PROVIDER_STATUS, {
        "agent": "worker", "expectedAgent": {"uri": agent.uri, "createdAtMs": agent.created_at_ms + 1}}) == \
        ipc_errors.AGENT_INCARNATION_MISMATCH
    assert _error(app, api.AGENT_PROVIDER_REVOKE, {"agent": "worker", "provider": "anthropic"}) == \
        ipc_errors.PROVIDER_GRANT_MISSING
    assert _error(app, api.AGENT_PROVIDER_ALLOW, {"agent": "worker", "provider": "anthropic"}) == \
        ipc_errors.PROVIDER_KEY_NOT_FOUND
    assert _error(app, api.USER_PROVIDER_ROTATE, {"keyId": "pk." + "a" * 26}) == ipc_errors.PROVIDER_KEY_NOT_FOUND
    app._user_key_signer = type(world.signer)(world.signer.store)
    assert _error(app, api.USER_PROVIDER_LOGIN, {"provider": "anthropic", "kind": "api-key", "secret": SECRET}) \
        == ipc_errors.USER_KEY_UNBOUND


def test_status_of_missing_and_awaiting_slots_matches_the_schema(world, app):
    world.service().put_awaiting(agent=world.ref(), grant_id="legacy-slot", provider="deepseek",
                                 environment_names=["DEEPSEEK_API_KEY"],
                                 legacy_ref={"entryId": "deepseek", "fieldName": "key", "revision": 1})
    status = _invoke(app, api.AGENT_PROVIDER_STATUS, {"agent": "worker"})
    assert [(row["state"], row["code"]) for row in status["grants"]] == \
        [("awaiting-key", ipc_errors.PROVIDER_KEY_NOT_FOUND)]
    missing = _invoke(app, api.AGENT_PROVIDER_STATUS, {"agent": "worker", "provider": "openai"})
    assert missing["grants"][0]["state"] == "missing"
