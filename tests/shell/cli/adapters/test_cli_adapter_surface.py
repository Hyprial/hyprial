"""R9 command-surface contract for ``hyprial adapter``."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from typer.main import get_command
from typer.testing import CliRunner

from hyprial.daemon.impl.application.actors.agents_admin.adapters import (
    _AdapterAdminMixin,
)
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_quarantine_schedule_survives_cli_json_and_human_output(monkeypatch):
    row = {
        "name": "test-adapter", "status": "quarantined", "desired": True,
        "nextRetryAt": 1033.0, "retryAttempt": 1,
    }
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda *_args, **_kwargs: {"adapter": row})
    runner = CliRunner()
    result = runner.invoke(_cli_root_app, ["adapter", "status", "test-adapter", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["adapter"] == row
    result = runner.invoke(_cli_root_app, ["adapter", "status", "test-adapter"])
    assert result.exit_code == 0, result.output
    assert "nextRetryAt:" in result.stdout and "1033.0" in result.stdout
    assert "retryAttempt:" in result.stdout


def test_adapter_command_tree_has_only_the_consolidated_entries() -> None:
    root = get_command(_cli_root_app)
    adapter = root.commands["adapter"]
    assert hasattr(adapter, "commands")

    assert set(adapter.commands) == {
        "add",
        "downgrade-state",
        "identities",
        "list",
        "media",
        "pin",
        "reload",
        "remove",
        "route",
        "scopes",
        "start",
        "status",
        "stop",
        "unpin",
        "user-auth",
    }
    assert "lark-auth" not in root.commands

    scopes = adapter.commands["scopes"]
    assert hasattr(scopes, "commands")
    assert set(scopes.commands) == {"authorize", "check"}

    user_auth = adapter.commands["user-auth"]
    assert hasattr(user_auth, "commands")
    assert set(user_auth.commands) == {"check", "complete"}


def test_adapter_add_without_existing_app_parameters_uses_onboarding(
    tmp_path: Path, monkeypatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home / "state"))

    result = CliRunner().invoke(_cli_root_app, ["adapter", "add", "feishu", "--json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["code"] == "USER_ACTION_REQUIRED"
    assert payload["data"]["command"] == "hyprial adapter add feishu"


def test_adapter_downgrade_is_offline_and_has_stable_json(tmp_path, monkeypatch):
    from tests.daemon.desired_state.test_adapter_rollback import seed
    from hyprial.daemon.impl.configuration.ownership import DaemonStateOwnershipFence

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    def no_daemon(*args, **kwargs):
        raise AssertionError("offline downgrade must not contact the daemon")
    monkeypatch.setattr(_cli_support, "_daemon_request", no_daemon)
    args = ["adapter", "downgrade-state", "--json"]
    missing = CliRunner().invoke(_cli_root_app, args)
    assert missing.exit_code == 1
    assert json.loads(missing.stdout)["code"] == "ADAPTER_DOWNGRADE_REFUSED"
    seed(state)
    with DaemonStateOwnershipFence.acquire(state):
        busy = CliRunner().invoke(_cli_root_app, args)
    assert busy.exit_code == 1
    assert json.loads(busy.stdout)["code"] == "ADAPTER_DOWNGRADE_REFUSED"
    success = CliRunner().invoke(_cli_root_app, args)
    assert success.exit_code == 0, success.output
    payload = json.loads(success.stdout)
    assert payload["ok"] is True
    assert payload["schemaVersion"] == 2
    assert payload["adapters"] == 2


def test_adapter_downgrade_absent_state_has_stable_refusal(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "absent-state"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("HYPRIAL_NETWORK_ISOLATED", "1")
    result = CliRunner().invoke(_cli_root_app, ["adapter", "downgrade-state", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload.get("code") == "ADAPTER_DOWNGRADE_REFUSED", payload
    assert not state.exists()


def test_adapter_list_projects_each_bound_agent() -> None:
    class FakeAdapter:
        def __init__(self, name: str) -> None:
            self.name = name

        def to_payload(self) -> dict[str, object]:
            return {"provider": "lark", "name": self.name, "status": "stopped"}

    subject = SimpleNamespace(
        _lark_client=SimpleNamespace(
            read_adapters=lambda: [FakeAdapter("feishu"), FakeAdapter("alerts")]
        ),
        agents=SimpleNamespace(
            pins=lambda: {"feishu": "agent:alice:node-1:squire"}
        ),
    )

    payload = _AdapterAdminMixin._ipc_adapter_list(subject)

    assert payload == {
        "ok": True,
        "adapters": [
            {
                "provider": "lark",
                "name": "feishu",
                "status": "stopped",
                "agent": "agent:alice:node-1:squire",
            },
            {
                "provider": "lark",
                "name": "alerts",
                "status": "stopped",
                "agent": None,
            },
        ],
    }
