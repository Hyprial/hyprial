"""Exercise the catalog bindings through the native daemon socket."""

from pathlib import Path

import pytest

from hyprial.kernel import RequestPortError, daemon_request_once, ipc_errors


def test_lookup_diagnostics_and_preparation_share_existing_agent(agent_socket):
    agent_socket.agents.create("inspect-me", preferred_harness="pi")

    def call(method, **params):
        return daemon_request_once(
            method, params, socket_path=agent_socket.socket_path, timeout=10
        )

    uri = agent_socket.agents.require("inspect-me").uri
    found = call("agent.get", name=uri)
    assert found["agent"]["actor"] == "inspect-me"
    rows = call("agent.list")["agents"]
    listed = next(row for row in rows if row["uri"] == uri)
    assert listed["actor"] == found["agent"]["actor"]
    assert "activityHints" in listed and "activityHints" not in found["agent"]
    with pytest.raises(RequestPortError) as missing:
        call("agent.get", name="missing-agent")
    assert missing.value.code == ipc_errors.AGENT_NOT_FOUND

    # The existing handler resolves the agent before validating the role.
    with pytest.raises(RequestPortError) as missing:
        call("agent.readiness", name="missing-agent")
    assert missing.value.code == ipc_errors.AGENT_NOT_FOUND

    report = call("agent.readiness", name=uri, role="developer")
    assert report["ok"] and report["ready"] is False
    assert report["checks"]

    home = call("agent.ensure-home", name=uri)["home"]
    assert Path(home["path"]).is_dir()
    assert call("agent.ensure-home", name=uri)["home"] == home

    context = call("agent.runtime-context", name=uri, harness="pi")
    assert context == {"ok": True, "mode": "legacy", "environment": {}}
