"""Contract §8.6: the adapter hook in ``_handle_agent`` on a real application.

Covers the router path (``_handle_agent``), the ``agent.secret.list`` registry binding and
internal callers. Incarnation mismatches keep their declared error across both routes.
"""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.provider_keys.api import AgentRef
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.daemon.impl.request_catalog import agent as agent_catalog
from hyprial.identity import AgentError, ProviderKeys
from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.shell.impl.daemon_wiring import daemon_dependencies

SENTINEL = "sk-hook-sentinel-value-000"
USER_GRANT = {
    "actor": "alpha", "grantId": "gui-first-run-pi-deepseek", "source": "user-provider",
    "entryId": "deepseek", "fieldName": "key", "environmentNames": ["DEEPSEEK_API_KEY"], "revision": 1,
}
PRIVATE_GRANT = {
    "actor": "alpha", "grantId": "routes", "source": "agent-private", "entryId": "cloudflare-routes",
    "fieldName": "CLOUDFLARE_API_TOKEN", "environmentNames": ["CLOUDFLARE_API_TOKEN"], "revision": 1,
}


class _Unused:
    pass


class _Harnesses:
    def status(self) -> tuple[()]:
        return ()


@pytest.fixture
def real_app() -> Iterator[DaemonApplication]:
    with tempfile.TemporaryDirectory(prefix="hyprial-pk-legacy-") as directory:
        root = Path(directory)
        application = DaemonApplication(
            state_dir=root, socket_path=root / "daemon.sock", node_id="pk-node", owner="pk-owner",
            **daemon_dependencies(),
        )
        application._inbox = _Unused()  # type: ignore[assignment]
        application._harnesses = _Harnesses()  # type: ignore[assignment]
        application._presence = _Unused()  # type: ignore[assignment]
        try:
            yield application
        finally:
            application._close()
            application._close_home_lease()


def _registry_resolver(app: DaemonApplication):
    def resolve(ref: str, expected: Any) -> AgentRef:
        agent = app.agents.get(ref)
        if agent is None:
            raise DaemonRequestError(ipc_errors.AGENT_NOT_FOUND, f"no agent named {ref!r}")
        if expected is not None and (str(agent.uri) != expected["uri"]
                                     or agent.created_at_ms != expected["createdAtMs"]):
            raise DaemonRequestError(ipc_errors.AGENT_INCARNATION_MISMATCH, "target agent incarnation changed")
        return AgentRef(actor=agent.actor, uri=str(agent.uri), created_at_ms=agent.created_at_ms,
                        entity_token=agent.entity_token, agent=str(uuid.UUID(hex=agent.entity_token)),
                        agent_id=agent.agent_id)
    return resolve


def _open_gate(app: DaemonApplication, service: Any) -> None:
    app._provider_key_service = service  # type: ignore[attr-defined]
    app._provider_key_agent_resolver = _registry_resolver(app)  # type: ignore[attr-defined]


def _providers_dir(app: DaemonApplication) -> Path:
    return Path(app.hyprial_home) / "secrets" / "providers"


def _validate_list(result: Any) -> None:
    errors = list(Draft202012Validator(agent_catalog.AGENT_SECRET_LIST_SPEC.result_schema).iter_errors(result))
    assert not errors, (errors, result)


def test_a_closed_gate_keeps_every_legacy_handler(real_app: DaemonApplication) -> None:
    real_app.agents.create("alpha")

    written = real_app._handle_agent("agent.secret.provider-write",
                                     {"entryId": "deepseek", "fieldName": "key", "value": SENTINEL})
    granted = real_app._handle_agent("agent.secret.grant", dict(USER_GRANT))

    assert written == {"ok": True, "entryId": "deepseek", "fieldNames": ["key"]}
    assert (_providers_dir(real_app) / "deepseek.json").is_file()
    assert real_app._agent_registry.secret_grant("alpha", USER_GRANT["grantId"]) is not None
    listed = METHOD_REGISTRY.invoke(real_app, "agent.secret.list", {"actor": "alpha"})
    assert listed == {"ok": True, "grants": [granted["grant"]]}
    with pytest.raises(Exception) as replay:
        real_app._handle_agent("agent.secret.grant", dict(USER_GRANT))
    assert getattr(replay.value, "code", None) == AgentError.code
    assert real_app._handle_agent("agent.secret.revoke", {"actor": "alpha", "grantId": USER_GRANT["grantId"]}) \
        == {"ok": True, "revoked": True}


def test_an_open_gate_routes_user_provider_calls_to_the_authority(
    real_app: DaemonApplication, new_service,
) -> None:
    real_app.agents.create("alpha")
    service = new_service()
    _open_gate(real_app, service)

    written = real_app._handle_agent("agent.secret.provider-write",
                                     {"entryId": "deepseek", "fieldName": "key", "value": SENTINEL})
    granted = real_app._handle_agent("agent.secret.grant", dict(USER_GRANT))
    private = real_app._handle_agent("agent.secret.grant", dict(PRIVATE_GRANT))

    assert written == {"ok": True, "entryId": "deepseek", "fieldNames": ["key"]}
    assert not (_providers_dir(real_app) / "deepseek.json").exists()
    assert real_app._agent_registry.secret_grant("alpha", USER_GRANT["grantId"]) is None
    assert real_app._agent_registry.secret_grant("alpha", "routes") is not None
    assert [name for name in service.names() if name in {"login", "allow"}] == ["login", "allow"]
    assert granted["grant"] == {**{key: USER_GRANT[key] for key in USER_GRANT if key != "actor"},
                                "actor": "alpha", "ownerActor": None, "ownerEntityToken": None}

    routed = METHOD_REGISTRY.invoke(real_app, "agent.secret.list", {"actor": "alpha"})
    _validate_list(routed)
    assert routed == real_app._handle_agent("agent.secret.list", {"actor": "alpha"})
    assert routed["grants"] == [granted["grant"], private["grant"]]
    assert METHOD_REGISTRY.invoke(real_app, "agent.secret.list", {}) == routed

    revoked = real_app._handle_agent("agent.secret.revoke", {"actor": "alpha", "grantId": USER_GRANT["grantId"]})
    assert revoked == {"ok": True, "revoked": True}
    assert real_app._handle_agent("agent.secret.revoke", {"actor": "alpha", "grantId": "routes"}) \
        == {"ok": True, "revoked": True}
    assert real_app._agent_registry.secret_grant("alpha", "routes") is None
    assert METHOD_REGISTRY.invoke(real_app, "agent.secret.list", {"actor": "alpha"})["grants"] == []
    assert SENTINEL not in repr(service.calls)


def test_the_expected_agent_rules_on_both_list_paths(real_app: DaemonApplication, new_service) -> None:
    agent = real_app.agents.create("alpha")
    _open_gate(real_app, new_service())
    real_app._handle_agent("agent.secret.grant", dict(USER_GRANT))
    matching = {"uri": str(agent.uri), "createdAtMs": agent.created_at_ms}
    stale = {**matching, "createdAtMs": agent.created_at_ms + 1}

    listed = METHOD_REGISTRY.invoke(real_app, "agent.secret.list", {"actor": "alpha", "expectedAgent": matching})
    assert [row["grantId"] for row in listed["grants"]] == [USER_GRANT["grantId"]]

    with pytest.raises(DaemonRequestError) as direct:
        real_app._handle_agent("agent.secret.list", {"actor": "alpha", "expectedAgent": stale})
    assert direct.value.code == ipc_errors.AGENT_INCARNATION_MISMATCH
    with pytest.raises(DaemonRequestError) as routed:
        METHOD_REGISTRY.invoke(real_app, "agent.secret.list", {"actor": "alpha", "expectedAgent": stale})
    assert routed.value.code == ipc_errors.AGENT_INCARNATION_MISMATCH
    with pytest.raises(DaemonRequestError) as grant:
        real_app._handle_agent("agent.secret.grant", {**USER_GRANT, "revision": 2, "expectedAgent": stale})
    assert grant.value.code == ipc_errors.AGENT_INCARNATION_MISMATCH


def test_list_authority_failures_survive_the_registry_as_agent_error(
    real_app: DaemonApplication, new_service,
) -> None:
    real_app.agents.create("alpha")
    service = new_service()
    _open_gate(real_app, service)
    service.fail["grants_for"] = ProviderKeys.Error(ipc_errors.PROVIDER_STORE_UNAVAILABLE)

    with pytest.raises(DaemonRequestError) as refused:
        METHOD_REGISTRY.invoke(real_app, "agent.secret.list", {"actor": "alpha"})

    assert refused.value.code == AgentError.code
    assert str(refused.value) == "provider key authority unavailable"


def test_an_open_gate_with_an_unavailable_store_writes_nothing_legacy(real_app: DaemonApplication) -> None:
    real_app.agents.create("alpha")
    (Path(real_app.hyprial_home) / "settings.json").write_text(
        '{"providerKeys": {"authority": "owner-keys"}}', encoding="utf-8")
    real_app._provider_key_agent_resolver = _registry_resolver(real_app)  # type: ignore[attr-defined]

    for method, params in (
        ("agent.secret.provider-write", {"entryId": "deepseek", "fieldName": "key", "value": SENTINEL}),
        ("agent.secret.grant", dict(USER_GRANT)),
        ("agent.secret.revoke", {"actor": "alpha", "grantId": USER_GRANT["grantId"]}),
    ):
        with pytest.raises(DaemonRequestError) as refused:
            real_app._handle_agent(method, params)
        assert refused.value.code == ipc_errors.AGENT_SECRET_RESOLUTION_FAILED
        assert SENTINEL not in repr(refused.value)
    with pytest.raises(DaemonRequestError) as listed:
        METHOD_REGISTRY.invoke(real_app, "agent.secret.list", {"actor": "alpha"})

    assert listed.value.code == AgentError.code
    assert not _providers_dir(real_app).exists()
    assert real_app._agent_registry.secret_grant("alpha", USER_GRANT["grantId"]) is None
    assert real_app._handle_agent("agent.secret.grant", dict(PRIVATE_GRANT))["grant"]["grantId"] == "routes"


@pytest.mark.parametrize("method", ["agent.secret.grant", "agent.secret.list"])
def test_internal_incarnation_fence_reaches_new_authority(real_app, service, monkeypatch, method):
    from types import SimpleNamespace

    real_app.agents.create("alpha")
    target = real_app.agents.get("alpha")
    expected = {"uri": str(target.uri), "createdAtMs": target.created_at_ms}
    replacement = SimpleNamespace(
        actor="alpha", uri=target.uri, created_at_ms=target.created_at_ms + 1,
        entity_token=uuid.uuid4().hex, agent_id=target.agent_id,
    )
    _open_gate(real_app, service)
    reads = iter((target, replacement, replacement))
    monkeypatch.setattr(real_app.agents, "get", lambda actor: next(reads))
    params = dict(USER_GRANT) if method.endswith("grant") else {"actor": "alpha"}
    params["expectedAgent"] = {"uri": str(replacement.uri), "createdAtMs": replacement.created_at_ms}

    with pytest.raises(DaemonRequestError) as refused:
        real_app._handle_agent(method, params, expected_secret_agent=expected)

    assert refused.value.code == ipc_errors.AGENT_INCARNATION_MISMATCH
    assert service.names() == []


def test_registry_preserves_incarnation_mismatch_error():
    class RefusingApp:
        def _handle_agent(self, method, params):
            raise DaemonRequestError(ipc_errors.AGENT_INCARNATION_MISMATCH, "target changed")

    with pytest.raises(DaemonRequestError) as refused:
        METHOD_REGISTRY.invoke(RefusingApp(), "agent.secret.list", {"actor": "alpha"})

    assert refused.value.code == ipc_errors.AGENT_INCARNATION_MISMATCH


@pytest.mark.parametrize("params", [
    {"expectedAgent": {"uri": "u", "createdAtMs": 1}},
    {"actor": "alpha", "expectedAgent": {"uri": "u"}},
    {"actor": "alpha", "expectedAgent": {"uri": "u", "createdAtMs": False}},
    {"actor": "alpha", "expectedAgent": {"uri": "u", "createdAtMs": 1, "extra": 2}},
])
def test_registry_schema_declares_expected_agent_shape(params):
    validator = Draft202012Validator(agent_catalog.AGENT_SECRET_LIST_SPEC.params_schema)
    assert list(validator.iter_errors(params))


@pytest.mark.parametrize("registered", [False, True])
def test_closed_gate_honors_public_list_incarnation_condition(real_app, registered):
    target = real_app.agents.create("alpha")
    params = {"actor": "alpha", "expectedAgent": {
        "uri": str(target.uri), "createdAtMs": target.created_at_ms + 1,
    }}
    with pytest.raises(DaemonRequestError) as refused:
        if registered:
            METHOD_REGISTRY.invoke(real_app, "agent.secret.list", params)
        else:
            real_app._handle_agent("agent.secret.list", params)
    assert refused.value.code == ipc_errors.AGENT_INCARNATION_MISMATCH
