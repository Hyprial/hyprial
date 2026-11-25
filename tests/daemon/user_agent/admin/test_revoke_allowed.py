"""Revoking a grant held by the user agent stays possible; granting stays refused.

A grant an older build wrote for the user agent must remain removable, as
with ``org.acl.revoke``: revocation only removes.  Each test seeds the grant
straight into its store (the public grant methods refuse the user agent) and
revokes it through the public method.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.provider_keys import api
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.daemon.impl.user_agent import USER_AGENT_NAME, local_user_agent, run_user_agent_ensure
from hyprial.daemon.impl.user_agent.naming import user_agent_capabilities
from hyprial.identity import (
    AgentCapabilityGrantCompleted,
    AgentSecretGrantCompleted,
    GrantAgentCapabilityCommand,
    GrantAgentSecretCommand,
    SecretSource,
)
from hyprial.kernel import canonical_user_uri
from tests.daemon.provider_keys.ipc.conftest import make_app
from tests.daemon.provider_keys.service.conftest import World
from tests.identity.agents.registry.entity.support import (
    OWNER,
    _close_test_applications as _close_test_applications,
    application,
)

CODE = "USER_AGENT_IMMUTABLE"
_SCOPE = '["agent:visitor:elsewhere:helper"]'


def _ensured(tmp_path: Path):  # type: ignore[no-untyped-def]
    app = application(tmp_path)
    assert run_user_agent_ensure(app) == "created"
    agent = local_user_agent(app.agents.list(), OWNER)
    assert agent is not None
    return app, agent


def _refused(app, method: str, params: dict[str, object]) -> str | None:  # type: ignore[no-untyped-def]
    try:
        app.handle(method, params, _trusted_message_origin="ipc")
    except DaemonRequestError as error:
        return error.code
    return None


def test_a_capability_grant_on_the_user_agent_can_be_revoked(tmp_path: Path) -> None:
    app, agent = _ensured(tmp_path)
    app._agent_session_domains.call_agent(
        GrantAgentCapabilityCommand("seed:grant", agent.uri, "g-1", "send-to", _SCOPE,
                                    canonical_user_uri(OWNER), 1),
        AgentCapabilityGrantCompleted,
    )

    assert _refused(app, "agent.grant", {"actor": agent.uri, "grantId": "g-2",
                                         "capability": "send-to", "scope": _SCOPE,
                                         "revision": 1}) == CODE
    revoked = app.handle("agent.revoke", {"actor": agent.uri, "grantId": "g-1"},
                         _trusted_message_origin="ipc")

    assert revoked == {"ok": True, "revoked": True}
    grants = app.handle("agent.grants", {"actor": agent.uri})
    assert [row for row in grants["grants"] if row.get("revokedAtMs") is None] == []


def test_a_secret_grant_on_the_user_agent_can_be_revoked(tmp_path: Path) -> None:
    app, agent = _ensured(tmp_path)
    app._agent_session_domains.call_agent(
        GrantAgentSecretCommand("seed:secret", agent.uri, "g-1", SecretSource.USER_PROVIDER,
                                "e-1", "f", ("X_TOKEN",), 1, None, None),
        AgentSecretGrantCompleted,
    )

    assert _refused(app, "agent.secret.grant", {
        "actor": agent.uri, "grantId": "g-2", "source": "agent-private", "entryId": "e-1",
        "fieldName": "f", "environmentNames": ["Y_TOKEN"], "revision": 1,
    }) == CODE
    revoked = app.handle("agent.secret.revoke", {"actor": agent.uri, "grantId": "g-1"},
                         _trusted_message_origin="ipc")

    assert revoked == {"ok": True, "revoked": True}
    assert app.handle("agent.secret.list", {"actor": agent.uri})["grants"] == []


@pytest.fixture
def keys_world(tmp_path: Path):  # type: ignore[no-untyped-def]
    world = World(tmp_path)
    yield world
    world.close()


def test_a_provider_grant_on_the_user_agent_can_be_revoked(keys_world, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    world = keys_world
    world.registry.create(USER_AGENT_NAME, capabilities=user_agent_capabilities())
    world.service().put_awaiting(
        agent=world.ref(USER_AGENT_NAME), grant_id="deepseek", provider="deepseek",
        environment_names=["DEEPSEEK_API_KEY"],
        legacy_ref={"entryId": "deepseek", "fieldName": "key", "revision": 1},
    )
    app = make_app(tmp_path / "app")
    for name in ("hyprial_home", "state_dir", "owner", "agents", "_user_key_signer",
                 "_provider_key_space_resolver", "_provider_key_owner_key_port",
                 "_provider_key_clock"):
        setattr(app, name, getattr(world.app, name))

    with pytest.raises(DaemonRequestError) as raised:
        METHOD_REGISTRY.invoke(app, api.AGENT_PROVIDER_ALLOW,
                               {"agent": USER_AGENT_NAME, "provider": "deepseek"},
                               trusted_message_origin="ipc")
    assert raised.value.code == CODE
    revoked = METHOD_REGISTRY.invoke(app, api.AGENT_PROVIDER_REVOKE,
                                     {"agent": USER_AGENT_NAME, "provider": "deepseek"},
                                     trusted_message_origin="ipc")

    assert revoked["changed"] is True, json.dumps(revoked)
    status = METHOD_REGISTRY.invoke(app, api.AGENT_PROVIDER_STATUS,
                                    {"agent": USER_AGENT_NAME}, trusted_message_origin="ipc")
    assert [row["state"] for row in status["grants"]] == ["revoked"]
