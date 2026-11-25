"""Tests for ``hyprial adapter remove`` — de-registering an external (Lark) adapter.

Two layers, mirroring ``test_cli_adapter_add.py``:

- Subprocess tests exercise the real argv/stdout surface against an isolated
  ``HYPRIAL_HOME``/``HARNESS_STATE_DIR`` with no daemon socket (so the daemon
  liveness probe takes the DAEMON_UNAVAILABLE path) and ``HYPRIAL_ZENOH_LISTEN``
  stripped, so nothing can touch a real ``~/.hyprial`` or the bus.
- In-process ``CliRunner`` tests monkeypatch ``cli._daemon_request`` to script
  daemon liveness answers (running / stopped / stop-failed) without spawning a
  daemon or Lark workers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from hyprial.kernel import ipc_errors
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)
from tests.support.runtime.environment import home_with_node_id

ROOT = Path(__file__).resolve().parents[4]

SECRET = "remove-secret-must-never-be-printed-7d2a"


def _env(home: Path) -> dict[str, str]:
    home_with_node_id(home)
    environment = {
        **os.environ,
        "HYPRIAL_HOME": str(home),
        "HARNESS_STATE_DIR": str(home),
    }
    environment.pop("HYPRIAL_ZENOH_LISTEN", None)
    return environment


def run_remove(
    home: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "hyprial.cli", "adapter", "remove", *arguments],
        cwd=ROOT,
        env=_env(home),
        text=True,
        capture_output=True,
        check=False,
    )


def payload(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.stdout, result.stderr
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, dict)
    return parsed


def _seed_adapter(home: Path, name: str = "feishu") -> None:
    from hyprial.daemon.impl.bootstrap.adapter_registration import RouteInput, add_lark_gateway

    add_lark_gateway(
        hyprial_home=home,
        name=name,
        app_id="cli_app_123",
        app_secret=SECRET,
        routes=[RouteInput(name="dm", native_id="oc_native_1")],
    )


def _seed_desired_state(home: Path, *, provider: bool = True) -> None:
    from hyprial.daemon.impl.desired_state import DesiredStateStore

    store = DesiredStateStore(home / "desired-state.json")
    if provider:
        store.set_adapter("feishu", True)


def _desired_state_json(home: Path) -> dict[str, Any]:
    """The desired-state document.  It lives in SQLite since U0a-2; the
    asserted content is exactly what the .v1 file used to hold."""
    return DesiredStateStore(home / "desired-state.json").load().to_json()


def _channels_json(home: Path) -> dict[str, Any]:
    return json.loads((home / "channels.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Subprocess layer: real CLI, no daemon (DAEMON_UNAVAILABLE liveness path).
# ---------------------------------------------------------------------------


def test_remove_cleans_channels_secret_and_desired_state(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_adapter(home)
    _seed_desired_state(home)

    result = run_remove(home, "feishu", "--json")
    assert result.returncode == 0, result.stderr
    body = payload(result)
    assert body["ok"] is True
    assert body["adapter"] == {"provider": "lark", "name": "feishu"}
    assert body["removed"] == {
        "channelsEntry": True,
        "secretFile": True,
        "desiredStateProvider": True,
    }
    # The note points at a hot reload, never at a full daemon restart.
    assert "hyprial adapter reload" in body["note"]
    assert "restart the daemon" not in body["note"].lower()

    # channels.json keeps version/shape with zero gateways.
    assert _channels_json(home) == {"version": 1, "gateways": []}
    # The credential file is gone.
    assert not (home / "secrets" / "lark-feishu.json").exists()
    state = _desired_state_json(home)
    assert state["providers"] == []
    assert "channelPins" not in state


def test_remove_never_touches_the_agents_database(tmp_path: Path) -> None:
    """Adapter bindings live in cell documents now; removal touches no agent
    record, and the registry schema no longer creates a pins table."""

    import sqlite3

    from hyprial.identity.impl.agents import AgentRegistry

    home = tmp_path / "home"
    _seed_adapter(home)
    registry = AgentRegistry(home / "agents.sqlite3", owner="alice", machine="node-1")
    address = registry.create("worker").uri
    registry.close()

    result = run_remove(home, "feishu", "--json")
    assert result.returncode == 0, result.stderr
    body = payload(result)
    assert "adapterPin" not in body["removed"]
    assert "pinnedActor" not in body["removed"]

    reloaded = AgentRegistry(home / "agents.sqlite3", owner="alice", machine="node-1")
    try:
        assert reloaded.require("worker").uri == address
    finally:
        reloaded.close()
    with sqlite3.connect(home / "agents.sqlite3") as database:
        tables = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "pins" not in tables


def test_remove_leaves_other_gateways_loadable(tmp_path: Path) -> None:
    """The surviving config must still pass the daemon's own loader."""

    from hyprial.kernel import PersistentConfigStore

    home = tmp_path / "home"
    _seed_adapter(home)
    _seed_adapter(home, name="kanban")  # writes lark-kanban.json

    result = run_remove(home, "feishu", "--json")
    assert result.returncode == 0, result.stderr

    store = PersistentConfigStore(home, home)
    configuration = store.load()  # would raise if any secret dangled
    names = [item.name for item in configuration.channels.gateways]
    assert names == ["kanban"]
    assert store.lark_app_secret("lark-kanban") == SECRET
    assert (home / "secrets" / "lark-kanban.json").exists()


def test_remove_is_idempotent_with_clear_not_found(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_adapter(home)

    first = run_remove(home, "feishu", "--json")
    assert first.returncode == 0, first.stderr

    second = run_remove(home, "feishu", "--json")
    assert second.returncode != 0
    body = payload(second)
    assert body["ok"] is False
    assert body["code"] == "ADAPTER_NOT_FOUND"
    assert "feishu" in body["error"]


def test_remove_never_configured_errors_without_writes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = run_remove(home, "ghost", "--json")
    assert result.returncode != 0
    assert payload(result)["code"] == "ADAPTER_NOT_FOUND"
    # No config files were materialized by the failed removal.
    assert not (home / "channels.json").exists()
    assert not (home / "desired-state.json").exists()
    assert not (home / "lifecycle-operations.sqlite3").exists()
    assert not (home / "agents.sqlite3").exists()
    assert not (home / "daemon.lock").exists()
    assert not (home / ".adapter-config.lock").exists()


def test_remove_bad_name_rejected_nothing_touched(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_adapter(home)
    before_channels = (home / "channels.json").read_bytes()

    for bad in ("../escape", "has/slash", "Has:Colon", "UPPER"):
        result = run_remove(home, bad, "--json")
        assert result.returncode != 0, bad
        assert payload(result)["code"] == "INVALID_ARGUMENT", bad

    assert (home / "channels.json").read_bytes() == before_channels
    assert (home / "secrets" / "lark-feishu.json").exists()
    assert not (home / "agents.sqlite3").exists()
    assert not (home / "daemon.lock").exists()
    # No stray deletions escaped the secrets directory either.
    assert not list(tmp_path.glob("**/*.restore"))


def test_remove_secret_value_never_printed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_adapter(home)
    result = run_remove(home, "feishu", "--json")
    assert result.returncode == 0, result.stderr
    assert SECRET not in result.stdout
    assert SECRET not in result.stderr


def test_remove_keeps_secret_for_nonstandard_credential_ref(tmp_path: Path) -> None:
    """A hand-edited credentialRef other than lark-<name> is never deleted."""

    from hyprial.kernel import atomic_json_write

    home = tmp_path / "home"
    (home / "secrets").mkdir(parents=True, mode=0o700)
    atomic_json_write(
        home / "channels.json",
        {
            "version": 1,
            "gateways": [
                {
                    "provider": "lark",
                    "name": "feishu",
                    "appId": "cli_app_123",
                    "credentialRef": "lark-shared",
                    "routes": [{"name": "dm", "type": "direct", "nativeId": "oc_1"}],
                }
            ],
        },
    )
    shared_secret = home / "secrets" / "lark-shared.json"
    atomic_json_write(shared_secret, {"appSecret": SECRET})

    result = run_remove(home, "feishu", "--json")
    assert result.returncode == 0, result.stderr
    body = payload(result)
    assert body["removed"]["secretFile"] is False
    assert body["secretKept"].endswith("lark-shared.json")
    assert shared_secret.exists()
    assert _channels_json(home) == {"version": 1, "gateways": []}


def test_channels_bytes_match_shared_atomic_writer(tmp_path: Path) -> None:
    """Post-remove channels.json must be byte-identical to the shared writer."""

    from hyprial.kernel import (
        ChannelConfiguration,
        ChannelRouteConfig,
        LarkGatewayConfig,
        atomic_json_write,
    )

    home = tmp_path / "home"
    _seed_adapter(home)
    _seed_adapter(home, name="kanban")

    assert run_remove(home, "feishu", "--json").returncode == 0

    expected = ChannelConfiguration(
        gateways=(
            LarkGatewayConfig(
                name="kanban",
                app_id="cli_app_123",
                credential_ref="lark-kanban",
                routes=(
                    ChannelRouteConfig(
                        name="dm", type="direct", native_id="oc_native_1"
                    ),
                ),
                default_route="dm",
            ),
        )
    )
    expected_path = tmp_path / "expected-channels.json"
    atomic_json_write(expected_path, expected.to_json())
    assert (home / "channels.json").read_bytes() == expected_path.read_bytes()


# ---------------------------------------------------------------------------
# In-process layer: scripted daemon liveness via a monkeypatched transport.
# ---------------------------------------------------------------------------


def _isolate(monkeypatch: Any, tmp_path: Path) -> Path:
    home = home_with_node_id(tmp_path / "home")
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home))
    monkeypatch.delenv("HYPRIAL_ZENOH_LISTEN", raising=False)
    return home


def _daemon_script(
    monkeypatch: Any, responses: dict[str, Any], calls: list[str]
) -> None:
    def daemon_request(
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 15.0,
        restore_wait: float | None = None,
    ) -> Any:
        calls.append(method)
        answer = responses[method]
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return answer(params or {})
        return answer

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)


def _management_remove(home: Path):  # type: ignore[no-untyped-def]
    def remove(params: dict[str, Any]) -> dict[str, object]:
        from hyprial.daemon.impl.bootstrap.adapter_registration import remove_lark_gateway

        assert params == {"operationId": "remove-preview"}
        return remove_lark_gateway(
            hyprial_home=home,
            state_dir=home,
            name="feishu",
        )

    return remove


def _running_status(name: str = "feishu") -> dict[str, Any]:
    return {
        "ok": True,
        "adapter": {
            "id": f"lark:{name}",
            "provider": "lark",
            "name": name,
            "status": "online",
            "online": True,
            "desired": True,
            "pid": 4242,
        },
    }


def _stopped_status(name: str = "feishu") -> dict[str, Any]:
    return {
        "ok": True,
        "adapter": {
            "id": f"lark:{name}",
            "provider": "lark",
            "name": name,
            "status": "stopped",
            "online": False,
            "desired": False,
        },
    }


def test_remove_refuses_while_daemon_reports_running(
    monkeypatch: Any, tmp_path: Path
) -> None:
    home = _isolate(monkeypatch, tmp_path)
    _seed_adapter(home)
    calls: list[str] = []
    _daemon_script(monkeypatch, {"adapter.status": _running_status()}, calls)

    result = CliRunner().invoke(_cli_root_app, ["adapter", "remove", "feishu", "--json"])
    assert result.exit_code != 0
    body = json.loads(result.stdout)
    assert body["ok"] is False
    assert body["code"] == "ADAPTER_RUNNING"
    assert "stop" in body["error"]
    # Fail-closed: nothing was deleted, and stop was never attempted.
    assert calls == ["adapter.status"]
    assert len(_channels_json(home)["gateways"]) == 1
    assert (home / "secrets" / "lark-feishu.json").exists()


def test_remove_refuses_on_starting_or_error_with_live_pid(
    monkeypatch: Any, tmp_path: Path
) -> None:
    for status in ("starting", "error"):
        home = _isolate(monkeypatch, tmp_path / status)
        _seed_adapter(home)
        running = _running_status()
        running["adapter"]["status"] = status
        running["adapter"]["online"] = status == "starting"
        calls: list[str] = []
        _daemon_script(monkeypatch, {"adapter.status": running}, calls)

        result = CliRunner().invoke(_cli_root_app, ["adapter", "remove", "feishu", "--json"])
        assert result.exit_code != 0, status
        assert json.loads(result.stdout)["code"] == "ADAPTER_RUNNING", status
        assert (home / "secrets" / "lark-feishu.json").exists(), status


def test_remove_force_stops_first_then_removes(
    monkeypatch: Any, tmp_path: Path
) -> None:
    home = _isolate(monkeypatch, tmp_path)
    _seed_adapter(home)
    _seed_desired_state(home)
    calls: list[str] = []
    _daemon_script(
        monkeypatch,
        {
            "adapter.status": _running_status(),
            "adapter.stop": _stopped_status(),
            "management.adapter.remove.preview": {"ok": True, "operationId": "remove-preview"},
            "management.adapter.remove": _management_remove(home),
            "adapter.reload": {"ok": True, "removed": ["feishu"]},
        },
        calls,
    )

    result = CliRunner().invoke(
        _cli_root_app, ["adapter", "remove", "feishu", "--force", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert calls == [
        "adapter.status",
        "adapter.stop",
        "management.adapter.remove.preview",
        "management.adapter.remove",
        "adapter.reload",
    ]
    body = json.loads(result.stdout)
    assert body["ok"] is True
    assert not (home / "secrets" / "lark-feishu.json").exists()
    assert _channels_json(home) == {"version": 1, "gateways": []}
    assert "channelPins" not in _desired_state_json(home)


def test_remove_force_aborts_when_stop_does_not_stop(
    monkeypatch: Any, tmp_path: Path
) -> None:
    home = _isolate(monkeypatch, tmp_path)
    _seed_adapter(home)
    calls: list[str] = []
    _daemon_script(
        monkeypatch,
        {"adapter.status": _running_status(), "adapter.stop": _running_status()},
        calls,
    )

    result = CliRunner().invoke(
        _cli_root_app, ["adapter", "remove", "feishu", "--force", "--json"]
    )
    assert result.exit_code != 0
    assert json.loads(result.stdout)["code"] == "ADAPTER_STOP_FAILED"
    assert len(_channels_json(home)["gateways"]) == 1
    assert (home / "secrets" / "lark-feishu.json").exists()


def test_remove_proceeds_when_daemon_unavailable(
    monkeypatch: Any, tmp_path: Path
) -> None:
    home = _isolate(monkeypatch, tmp_path)
    _seed_adapter(home)
    calls: list[str] = []
    _daemon_script(
        monkeypatch,
        {
            "adapter.status": ipc_errors.DaemonUnavailableError("no daemon"),
            "management.adapter.remove.preview": ipc_errors.DaemonUnavailableError("no daemon"),
        },
        calls,
    )

    result = CliRunner().invoke(_cli_root_app, ["adapter", "remove", "feishu", "--json"])
    assert result.exit_code == 0, result.output
    assert calls == ["adapter.status", "management.adapter.remove.preview"]
    assert json.loads(result.stdout)["ok"] is True
    assert not (home / "secrets" / "lark-feishu.json").exists()


def test_remove_daemon_start_race_fails_without_offline_writes(
    monkeypatch: Any, tmp_path: Path
) -> None:
    from hyprial.daemon.impl.configuration.ownership import DaemonStateOwnershipFence

    home = _isolate(monkeypatch, tmp_path)
    _seed_adapter(home)
    _seed_desired_state(home)
    before_channels = (home / "channels.json").read_bytes()
    before_secret = (home / "secrets" / "lark-feishu.json").read_bytes()
    before_desired = _desired_state_json(home)
    calls: list[str] = []
    _daemon_script(
        monkeypatch,
        {
            "adapter.status": ipc_errors.DaemonUnavailableError("starting"),
            "management.adapter.remove.preview": ipc_errors.DaemonUnavailableError("starting"),
        },
        calls,
    )

    with DaemonStateOwnershipFence.acquire(home):
        result = CliRunner().invoke(_cli_root_app, ["adapter", "remove", "feishu", "--json"])

    assert result.exit_code != 0
    assert json.loads(result.stdout)["code"] == "DAEMON_OWNERSHIP_BUSY"
    assert calls == ["adapter.status", "management.adapter.remove.preview"]
    assert (home / "channels.json").read_bytes() == before_channels
    assert (home / "secrets" / "lark-feishu.json").read_bytes() == before_secret
    assert _desired_state_json(home) == before_desired


def test_remove_requires_reload_when_daemon_snapshot_lacks_adapter(
    monkeypatch: Any, tmp_path: Path
) -> None:
    home = _isolate(monkeypatch, tmp_path)
    _seed_adapter(home)
    before = (home / "channels.json").read_bytes()
    calls: list[str] = []
    _daemon_script(monkeypatch, {
        "adapter.status": _cli_get_services().CliError("ADAPTER_NOT_FOUND", "not configured"),
        "management.adapter.remove.preview": _cli_get_services().CliError("PREVIEW_STALE", "reload before deletion"),
    }, calls)
    result = CliRunner().invoke(_cli_root_app, ["adapter", "remove", "feishu", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "PREVIEW_STALE"
    assert (home / "channels.json").read_bytes() == before
    assert (home / "secrets" / "lark-feishu.json").exists()


def test_remove_stopped_adapter_via_daemon_then_files(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The normal path: daemon says stopped, no --force needed, no stop call."""

    home = _isolate(monkeypatch, tmp_path)
    _seed_adapter(home)
    calls: list[str] = []
    _daemon_script(
        monkeypatch,
        {
            "adapter.status": _stopped_status(),
            "management.adapter.remove.preview": {"ok": True, "operationId": "remove-preview"},
            "management.adapter.remove": _management_remove(home),
            "adapter.reload": {"ok": True, "removed": ["feishu"]},
        },
        calls,
    )

    result = CliRunner().invoke(_cli_root_app, ["adapter", "remove", "feishu", "--json"])
    assert result.exit_code == 0, result.output
    assert calls == ["adapter.status", "management.adapter.remove.preview", "management.adapter.remove", "adapter.reload"]
    assert not (home / "secrets" / "lark-feishu.json").exists()


def test_other_daemon_errors_propagate(monkeypatch: Any, tmp_path: Path) -> None:
    home = _isolate(monkeypatch, tmp_path)
    _seed_adapter(home)
    _daemon_script(
        monkeypatch,
        {"adapter.status": ipc_errors.IpcTimeoutError("timed out")},
        [],
    )

    result = CliRunner().invoke(_cli_root_app, ["adapter", "remove", "feishu", "--json"])
    assert result.exit_code != 0
    assert json.loads(result.stdout)["code"] == "IPC_TIMEOUT"
    assert (home / "secrets" / "lark-feishu.json").exists()


def test_corrupt_channels_fails_closed(tmp_path: Path) -> None:
    """An unparseable channels.json is never rewritten by remove."""

    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / "channels.json").write_text("{not json", encoding="utf-8")

    result = run_remove(home, "feishu", "--json")
    assert result.returncode != 0
    assert payload(result)["code"] == "INVALID_ARGUMENT"
    assert (home / "channels.json").read_text(encoding="utf-8") == "{not json"


def test_malformed_preview_never_executes_or_falls_back(tmp_path, monkeypatch):
    cli_home = _isolate(monkeypatch, tmp_path)
    _seed_adapter(cli_home)
    for token in (None, "", 123):
        calls = []
        _daemon_script(monkeypatch, {
            "adapter.status": {"ok": True, "adapter": {"online": False, "desired": False}},
            "management.adapter.remove.preview": {"ok": True, "operationId": token},
        }, calls)
        result = CliRunner().invoke(_cli_root_app, ["adapter", "remove", "feishu", "--json"])
        assert result.exit_code == 1
        assert json.loads(result.stdout)["code"] == "INVALID_RESPONSE"
        assert calls == ["adapter.status", "management.adapter.remove.preview"]
        assert (cli_home / "secrets/lark-feishu.json").exists()
