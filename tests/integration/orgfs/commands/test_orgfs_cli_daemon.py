from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.kernel.impl.contracts.daemon.ipc_errors import DaemonRequestError
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies


def test_daemon_orgfs_surface_and_local_ro_gate(tmp_path):
    application = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node-a",
        hyprial_home=tmp_path / "home",
        owner="owner@example.com",
    **_deep_module_daemon_dependencies())
    try:
        created = application._handle_orgfs("orgfs.create", {"name": "team"})
        space_id = created["space_id"]
        application._handle_orgfs(
            "orgfs.write", {"spaceId": space_id, "path": "readme.md", "text": "hello"}
        )
        read = application._handle_orgfs(
            "orgfs.read", {"spaceId": space_id, "path": "readme.md"}
        )
        assert read["text"] == "hello"
        events = application._handle_orgfs(
            "orgfs.watch", {"spaceId": space_id, "glob": "*.md"}
        )
        assert [event["kind"] for event in events["events"]] == ["created"]
        application._handle_orgfs(
            "orgfs.invite", {"spaceId": space_id, "user": "user:reader", "mode": "ro"}
        )
        application._orgfs_runtime.facade.author = "user:reader"
        with pytest.raises(DaemonRequestError) as rejected:
            application._handle_orgfs(
                "orgfs.write", {"spaceId": space_id, "path": "blocked.md", "text": "no"}
            )
        assert rejected.value.code == "not-a-member"
        assert all(
            json.loads(row[0])["origin"]["author"] != "user:reader"
            for row in application._orgfs_runtime.stores[space_id]._db.execute(
                "SELECT envelope_bytes FROM commits"
            )
        )
    finally:
        application._orgfs_runtime.close()
        application._orgfs_runtime = None
        application._agent_session_domains.close()


def test_fs_cli_exposes_frozen_commands_and_version_flags(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    calls = []

    def request(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        if method == "orgfs.invite":
            return {"added_by": "user:owner"}
        if method == "orgfs.spaces":
            return {"spaces": [{"space_id": "space-1", "name": "team"}]}
        return {"ok": True}

    monkeypatch.setattr('hyprial.shell.impl.cli.commands.common.support._daemon_request', request)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "org", "fs",
            "write",
            "space-1",
            "id:node-1",
            "hello",
            "--base-version",
            "v1",
            "--expect-version",
            "v2",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "orgfs.write",
            {
                "spaceId": "space-1",
                "path": "id:node-1",
                "text": "hello",
                "baseVersion": "v1",
                "expectVersion": "v2",
            },
        )
    ]
    help_result = runner.invoke(app, ["org", "fs", "--help"])
    assert help_result.exit_code == 0
    for command in (
        "space", "member", "trash", "purge", "ls", "read", "write", "mkdir", "mv", "rm",
        "history", "watch", "import", "export", "resolve",
    ):
        assert command in help_result.output

    calls.clear()
    invite = runner.invoke(
        app, ["org", "fs", "member", "invite", "space-1", "user:reader", "--json"]
    )
    assert invite.exit_code == 0, invite.output
    assert [method for method, _params in calls] == [
        "orgfs.invite",
        "orgfs.spaces",
        "message.send",
    ]
    invitation = json.loads(calls[-1][1]["message"])
    assert invitation == {
        "schemaVersion": 1,
        "type": "orgfs-invite",
        "spaceId": "space-1",
        "name": "team",
        "owner": "user:owner",
        "mode": "rw",
    }


def test_runtime_restart_rehydrates_current_space_state(tmp_path):
    state = tmp_path / "state"
    first = OrgFsRuntime(state, node_id="node-a", author="user:owner")
    space = first.facade.create_space("team")
    node = first.facade.write_text(space.space_id, "readme.md", "durable")
    first.close()

    second = OrgFsRuntime(state, node_id="node-a", author="user:owner")
    try:
        assert [item.space_id for item in second.facade.spaces()] == [space.space_id]
        assert second.facade.read_text(space.space_id, f"id:{node.node_id}")[0] == "durable"
    finally:
        second.close()
