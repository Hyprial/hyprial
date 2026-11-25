"""Squire's private fence must reach the existing secret authority."""

import pytest

from hyprial.kernel import DaemonRequestError
from tests.daemon.ipc.contracts.agent.test_read_specs import app as app


def params():
    return {"actor": "worker", "grantId": "synthetic", "source": "user-provider",
        "entryId": "synthetic", "fieldName": "key", "environmentNames": ["MODEL_KEY"], "revision": 1}


def test_internal_expected_instance_rejects_write_without_grant(app):
    app.handle("agent.create", {"name": "worker"})
    agent = app._agent_registry.require("worker")
    wrong = {"uri": str(agent.uri), "createdAtMs": agent.created_at_ms + 1}
    with pytest.raises(DaemonRequestError) as caught:
        app._handle_agent("agent.secret.grant", params(), expected_secret_agent=wrong)
    assert caught.value.code == "AGENT_INCARNATION_MISMATCH"
    assert app._agent_registry.secret_inventory("worker") == ()


def test_internal_expected_instance_protects_read_and_allows_current(app):
    app.handle("agent.create", {"name": "worker"})
    agent = app._agent_registry.require("worker")
    expected = {"uri": str(agent.uri), "createdAtMs": agent.created_at_ms}
    receipt = app._handle_agent("agent.secret.grant", params(), expected_secret_agent=expected)
    assert receipt["ok"] is True
    read = app._handle_agent("agent.secret.list", {"actor": "worker"}, expected_secret_agent=expected)
    assert read["grants"] == [receipt["grant"]]
    with pytest.raises(DaemonRequestError) as caught:
        app._handle_agent("agent.secret.list", {"actor": "worker"},
            expected_secret_agent={**expected, "uri": "agent:owner:node:other"})
    assert caught.value.code == "AGENT_INCARNATION_MISMATCH"


def test_public_legacy_extension_is_still_ignored(app):
    app._agent_registry.create("worker")
    receipt = app._handle_agent("agent.secret.grant", {**params(), "expectedAgent": False})
    assert receipt["ok"] is True
