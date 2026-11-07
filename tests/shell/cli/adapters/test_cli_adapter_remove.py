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
import threading
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from hyprial.kernel import ipc_errors
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

ROOT = Path(__file__).resolve().parents[4]

SECRET = "remove-secret-must-never-be-printed-7d2a"


def _env(home: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
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


def _seed_desired_state(home: Path, *, provider: bool = True, pin: bool = True) -> None:
    from hyprial.daemon.impl.desired_state import DesiredStateStore

    store = DesiredStateStore(home / "desired-state.json")
    if provider:
        store.set_adapter("feishu", True)
    if pin:
        store.set_channel_pin("feishu", "some-actor")
    store.set_channel_pin("#keep", "other-actor")


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
        "adapterPin": True,
        "pinnedActor": "some-actor",
    }
    # The note points at a hot reload, never at a full daemon restart.
    assert "hyprial adapter reload" in body["note"]
    assert "restart the daemon" not in body["note"].lower()

    # channels.json keeps version/shape with zero gateways.
    assert _channels_json(home) == {"version": 1, "gateways": []}
    # The credential file is gone.
    assert not (home / "secrets" / "lark-feishu.json").exists()
    # Desired state: provider and adapter pin removed, unrelated pin kept.
    state = _desired_state_json(home)
    assert state["providers"] == []
    assert state["channelPins"] == {"#keep": "other-actor"}


def test_remove_strips_the_pin_from_the_agents_database(tmp_path: Path) -> None:
    """Pins live in agents.sqlite3 now; removing the adapter must not leave
    a pin row pointing at a gateway that no longer exists."""

    from hyprial.identity.impl.agents import AgentRegistry

    home = tmp_path / "home"
    _seed_adapter(home)
    registry = AgentRegistry(home / "agents.sqlite3", owner="alice", machine="node-1")
    registry.create("worker")
    registry.pin("feishu", "worker")
    registry.close()

    result = run_remove(home, "feishu", "--json")
    assert result.returncode == 0, result.stderr
    body = payload(result)
    assert body["removed"]["adapterPin"] is True
    assert body["removed"]["pinnedActor"] == "agent:alice:node-1:worker"

    reloaded = AgentRegistry(home / "agents.sqlite3", owner="alice", machine="node-1")
    assert reloaded.pins() == {}
    # The agent record itself is untouched -- only its pin went.
    assert reloaded.require("worker").uri == "agent:alice:node-1:worker"


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
    home = tmp_path / "home"
    home.mkdir(parents=True, mode=0o700)
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

        return remove_lark_gateway(
            hyprial_home=home,
            state_dir=home,
            name=str(params["name"]),
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
        "management.adapter.remove",
        "adapter.reload",
    ]
    body = json.loads(result.stdout)
    assert body["ok"] is True
    assert not (home / "secrets" / "lark-feishu.json").exists()
    assert _channels_json(home) == {"version": 1, "gateways": []}
    assert _desired_state_json(home)["channelPins"] == {"#keep": "other-actor"}


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
            "management.adapter.remove": ipc_errors.DaemonUnavailableError("no daemon"),
        },
        calls,
    )

    result = CliRunner().invoke(_cli_root_app, ["adapter", "remove", "feishu", "--json"])
    assert result.exit_code == 0, result.output
    assert calls == ["adapter.status", "management.adapter.remove"]
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
            "management.adapter.remove": ipc_errors.DaemonUnavailableError("starting"),
        },
        calls,
    )

    with DaemonStateOwnershipFence.acquire(home):
        result = CliRunner().invoke(_cli_root_app, ["adapter", "remove", "feishu", "--json"])

    assert result.exit_code != 0
    assert json.loads(result.stdout)["code"] == "DAEMON_OWNERSHIP_BUSY"
    assert calls == ["adapter.status", "management.adapter.remove"]
    assert (home / "channels.json").read_bytes() == before_channels
    assert (home / "secrets" / "lark-feishu.json").read_bytes() == before_secret
    assert _desired_state_json(home) == before_desired


def test_remove_proceeds_when_daemon_snapshot_lacks_adapter(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A daemon that never saw this gateway cannot be running it."""

    home = _isolate(monkeypatch, tmp_path)
    _seed_adapter(home)
    calls: list[str] = []
    _daemon_script(
        monkeypatch,
        {
            "adapter.status": _cli_get_services().CliError("ADAPTER_NOT_FOUND", "not configured"),
            "management.adapter.remove": _management_remove(home),
            "adapter.reload": {"ok": True, "removed": ["feishu"]},
        },
        calls,
    )

    result = CliRunner().invoke(_cli_root_app, ["adapter", "remove", "feishu", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["ok"] is True
    assert not (home / "secrets" / "lark-feishu.json").exists()


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
            "management.adapter.remove": _management_remove(home),
            "adapter.reload": {"ok": True, "removed": ["feishu"]},
        },
        calls,
    )

    result = CliRunner().invoke(_cli_root_app, ["adapter", "remove", "feishu", "--json"])
    assert result.exit_code == 0, result.output
    assert calls == ["adapter.status", "management.adapter.remove", "adapter.reload"]
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


# ---------------------------------------------------------------------------
# Rollback: a failed remove must be a byte-exact no-op.
# ---------------------------------------------------------------------------


def test_rollback_when_desired_state_save_fails(
    monkeypatch: Any, tmp_path: Path
) -> None:
    import pytest

    import hyprial.daemon.impl.bootstrap.adapter_registration as adapter_registration
    from hyprial.daemon.impl import desired_state

    home = tmp_path / "home"
    _seed_adapter(home)
    _seed_desired_state(home)
    before_channels = (home / "channels.json").read_bytes()
    before_versioned = _desired_state_json(home)

    def failing_save(self: Any, state: Any) -> None:
        raise OSError("simulated desired-state write failure")

    monkeypatch.setattr(desired_state.DesiredStateStore, "save", failing_save)

    with pytest.raises(OSError):
        adapter_registration.remove_lark_gateway(
            hyprial_home=home, state_dir=home, name="feishu"
        )

    assert (home / "channels.json").read_bytes() == before_channels
    assert _desired_state_json(home) == before_versioned
    assert (home / "secrets" / "lark-feishu.json").exists()


def test_rollback_preserves_permission_error_fields_from_desired_state(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    import errno
    import pytest

    import hyprial.daemon.impl.bootstrap.adapter_registration as adapter_registration
    from hyprial.daemon.impl import desired_state

    home = tmp_path / "home"
    _seed_adapter(home)
    _seed_desired_state(home)
    before_channels = (home / "channels.json").read_bytes()
    before_versioned = _desired_state_json(home)
    blocked_path = home / "desired-state.json.v1"

    def failing_save(self: Any, state: Any) -> None:
        raise PermissionError(errno.EACCES, "synthetic permission denial", str(blocked_path))

    monkeypatch.setattr(desired_state.DesiredStateStore, "save", failing_save)
    with pytest.raises(PermissionError) as caught:
        adapter_registration.remove_lark_gateway(
            hyprial_home=home, state_dir=home, name="feishu"
        )
    assert caught.value.errno == errno.EACCES
    assert caught.value.filename == str(blocked_path)
    assert caught.value.strerror == "synthetic permission denial"
    assert (home / "channels.json").read_bytes() == before_channels
    assert _desired_state_json(home) == before_versioned
    assert (home / "secrets" / "lark-feishu.json").exists()


def test_rollback_when_secret_delete_fails(monkeypatch: Any, tmp_path: Path) -> None:
    import pytest

    import hyprial.daemon.impl.bootstrap.adapter_registration as adapter_registration
    from hyprial.identity.impl.agents import AgentRegistry
    from hyprial.kernel import resolve_node_id
    from hyprial.daemon.impl.configuration.identity import resolve_node_owner

    home = tmp_path / "home"
    _seed_adapter(home)
    _seed_desired_state(home)
    owner = resolve_node_owner()
    monkeypatch.setenv("HYPRIAL_NODE_ID", "adapter-rollback-fixture")
    machine = resolve_node_id()
    registry = AgentRegistry(home / "agents.sqlite3", owner=owner, machine=machine)
    worker = registry.create("worker")
    registry.pin("feishu", "worker")
    registry.close()
    before_channels = (home / "channels.json").read_bytes()
    before_versioned = _desired_state_json(home)
    secret_path = home / "secrets" / "lark-feishu.json"
    before_secret = secret_path.read_bytes()

    real_unlink = Path.unlink

    def failing_unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == secret_path:
            raise OSError("simulated secret delete failure")
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)

    with pytest.raises(OSError):
        adapter_registration.remove_lark_gateway(
            hyprial_home=home, state_dir=home, name="feishu"
        )

    # Everything is back: channels entry, desired-state provider + pin, secret.
    assert (home / "channels.json").read_bytes() == before_channels
    assert _desired_state_json(home) == before_versioned
    assert secret_path.read_bytes() == before_secret
    assert secret_path.stat().st_mode & 0o777 == 0o600
    reloaded = AgentRegistry(home / "agents.sqlite3", owner=owner, machine=machine)
    assert reloaded.pins() == {"feishu": worker.uri}
    reloaded.close()


def test_rollback_after_database_unpin_mutates_then_raises(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A late actor-port failure must restore all four removal surfaces.

    This is deliberately stronger than making ``unpin_if`` fail before doing
    anything: the pin row is removed first and only then does the port raise.
    The compensation path therefore has to recreate the exact binding as well
    as restore desired state and the two files.
    """

    import pytest

    import hyprial.daemon.impl.bootstrap.adapter_registration as adapter_registration
    from hyprial.identity.impl.agents import AgentRegistry
    from hyprial.daemon.impl.composition import AgentDirectoryFacade
    from hyprial.kernel import resolve_node_id
    from hyprial.daemon.impl.configuration.identity import resolve_node_owner

    home = tmp_path / "home"
    _seed_adapter(home)
    _seed_desired_state(home)
    owner = resolve_node_owner()
    monkeypatch.setenv("HYPRIAL_NODE_ID", "adapter-rollback-fixture")
    machine = resolve_node_id()
    registry = AgentRegistry(home / "agents.sqlite3", owner=owner, machine=machine)
    worker = registry.create("worker")
    registry.pin("feishu", "worker")
    registry.close()

    before_channels = (home / "channels.json").read_bytes()
    before_secret = (home / "secrets" / "lark-feishu.json").read_bytes()
    before_desired = _desired_state_json(home)
    real_unpin_if = AgentDirectoryFacade.unpin_if

    def mutate_then_fail(
        self: AgentDirectoryFacade, adapter: str, expected_actor: str | None
    ) -> str | None:
        real_unpin_if(self, adapter, expected_actor)
        raise OSError("simulated post-unpin port failure")

    monkeypatch.setattr(AgentDirectoryFacade, "unpin_if", mutate_then_fail)

    with pytest.raises(OSError, match="post-unpin"):
        adapter_registration.remove_lark_gateway(
            hyprial_home=home, state_dir=home, name="feishu"
        )

    assert (home / "channels.json").read_bytes() == before_channels
    assert (home / "secrets" / "lark-feishu.json").read_bytes() == before_secret
    assert _desired_state_json(home) == before_desired
    reloaded = AgentRegistry(home / "agents.sqlite3", owner=owner, machine=machine)
    assert reloaded.pins() == {"feishu": worker.uri}
    reloaded.close()


def test_rollback_after_desired_removal_mutates_then_raises(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A lost actor reply after durable desired-state removal is compensated."""

    import pytest

    import hyprial.daemon.impl.bootstrap.adapter_registration as adapter_registration
    from hyprial.daemon.impl.harnesses.runtime.ports import HarnessAdapterRegistrationProjection
    from hyprial.daemon.impl.processes.supervisor import ManagedHarnessRuntime

    home = tmp_path / "home"
    _seed_adapter(home)
    _seed_desired_state(home)
    before_channels = (home / "channels.json").read_bytes()
    before_secret = (home / "secrets" / "lark-feishu.json").read_bytes()
    before_desired = _desired_state_json(home)
    real_remove = ManagedHarnessRuntime.remove_adapter_registration

    def mutate_then_fail(
        self: ManagedHarnessRuntime,
        snapshot: HarnessAdapterRegistrationProjection,
    ) -> bool:
        real_remove(self, snapshot)
        raise TimeoutError("simulated lost harness actor reply")

    monkeypatch.setattr(
        ManagedHarnessRuntime, "remove_adapter_registration", mutate_then_fail
    )

    with pytest.raises(TimeoutError, match="lost harness actor reply"):
        adapter_registration.remove_lark_gateway(
            hyprial_home=home, state_dir=home, name="feishu"
        )

    assert (home / "channels.json").read_bytes() == before_channels
    assert (home / "secrets" / "lark-feishu.json").read_bytes() == before_secret
    assert _desired_state_json(home) == before_desired


def test_channels_write_failure_never_mutates_any_registry(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The first file write is the transaction admission point."""

    import socket

    import pytest

    import hyprial.daemon.impl.bootstrap.adapter_registration as adapter_registration
    from hyprial.identity.impl.agents import AgentRegistry
    from hyprial.daemon.impl.configuration.identity import resolve_node_owner

    home = tmp_path / "home"
    _seed_adapter(home)
    _seed_desired_state(home)
    owner = resolve_node_owner()
    machine = socket.gethostname()
    registry = AgentRegistry(home / "agents.sqlite3", owner=owner, machine=machine)
    worker = registry.create("worker")
    registry.pin("feishu", "worker")
    registry.close()

    before_channels = (home / "channels.json").read_bytes()
    before_secret = (home / "secrets" / "lark-feishu.json").read_bytes()
    before_desired = _desired_state_json(home)

    def fail_channels_write(_path: Path, _payload: object) -> None:
        raise OSError("simulated channels write failure")

    monkeypatch.setattr(adapter_registration, "atomic_json_write", fail_channels_write)

    with pytest.raises(OSError, match="channels write"):
        adapter_registration.remove_lark_gateway(
            hyprial_home=home, state_dir=home, name="feishu"
        )

    assert (home / "channels.json").read_bytes() == before_channels
    assert (home / "secrets" / "lark-feishu.json").read_bytes() == before_secret
    assert _desired_state_json(home) == before_desired
    reloaded = AgentRegistry(home / "agents.sqlite3", owner=owner, machine=machine)
    assert reloaded.pins() == {"feishu": worker.uri}
    reloaded.close()


def test_concurrent_force_add_waits_for_remove_and_keeps_replacement(
    monkeypatch: Any, tmp_path: Path
) -> None:
    import hyprial.daemon.impl.bootstrap.adapter_registration as adapter_registration
    from hyprial.kernel import PersistentConfigStore

    home = tmp_path / "home"
    _seed_adapter(home)
    _seed_desired_state(home)
    at_unlink = threading.Event()
    release_unlink = threading.Event()
    remove_result: list[dict[str, object]] = []
    add_result: list[dict[str, object]] = []
    errors: list[BaseException] = []
    real_unlink = adapter_registration._unlink_if_current

    def paused_unlink(path: Path, expected: bytes | None) -> None:
        at_unlink.set()
        assert release_unlink.wait(2.0)
        real_unlink(path, expected)

    monkeypatch.setattr(adapter_registration, "_unlink_if_current", paused_unlink)

    def remove() -> None:
        try:
            remove_result.append(
                adapter_registration.remove_lark_gateway(
                    hyprial_home=home, state_dir=home, name="feishu"
                )
            )
        except BaseException as error:
            errors.append(error)

    def replace() -> None:
        try:
            add_result.append(
                adapter_registration.add_lark_gateway(
                    hyprial_home=home,
                    name="feishu",
                    app_id="replacement-app",
                    app_secret="replacement-secret",
                    routes=[
                        adapter_registration.RouteInput(
                            name="dm", native_id="oc_replacement"
                        )
                    ],
                    force=True,
                )
            )
        except BaseException as error:
            errors.append(error)

    removing = threading.Thread(target=remove)
    replacing = threading.Thread(target=replace)
    removing.start()
    assert at_unlink.wait(2.0)
    replacing.start()
    replacing.join(0.1)
    assert replacing.is_alive(), "force-add must wait for remove's config lease"
    release_unlink.set()
    removing.join(3.0)
    replacing.join(3.0)

    assert not errors
    assert len(remove_result) == len(add_result) == 1
    gateway = _channels_json(home)["gateways"][0]
    assert gateway["appId"] == "replacement-app"
    assert (home / "secrets" / "lark-feishu.json").read_text(encoding="utf-8").find(
        "replacement-secret"
    ) >= 0
    loaded = PersistentConfigStore(home, home).load()
    assert loaded.channels.gateways[0].app_id == "replacement-app"


def test_rollback_cas_preserves_uncoordinated_replacement(
    monkeypatch: Any, tmp_path: Path
) -> None:
    import pytest

    import hyprial.daemon.impl.bootstrap.adapter_registration as adapter_registration
    from hyprial.kernel import atomic_json_write

    home = tmp_path / "home"
    _seed_adapter(home)
    _seed_desired_state(home)
    secret_path = home / "secrets" / "lark-feishu.json"
    replacement_channels = {
        "version": 1,
        "gateways": [
            {
                "provider": "lark",
                "name": "feishu",
                "appId": "replacement-app",
                "credentialRef": "lark-feishu",
                "routes": [{"name": "dm", "type": "direct", "nativeId": "oc_new"}],
                "defaultRoute": "dm",
            }
        ],
    }

    def replace_then_fail(_path: Path, _expected: bytes | None) -> None:
        atomic_json_write(home / "channels.json", replacement_channels)
        atomic_json_write(secret_path, {"appSecret": "replacement-secret"})
        raise OSError("simulated failure after uncoordinated replacement")

    monkeypatch.setattr(adapter_registration, "_unlink_if_current", replace_then_fail)

    with pytest.raises(adapter_registration.AdapterConfigConflictError) as caught:
        adapter_registration.remove_lark_gateway(
            hyprial_home=home, state_dir=home, name="feishu"
        )

    assert isinstance(caught.value.__cause__, BaseExceptionGroup)
    assert any(
        isinstance(error, adapter_registration.AdapterConfigConflictError)
        for error in caught.value.__cause__.exceptions
    )
    assert _channels_json(home) == replacement_channels
    assert "replacement-secret" in secret_path.read_text(encoding="utf-8")


def test_corrupt_channels_fails_closed(tmp_path: Path) -> None:
    """An unparseable channels.json is never rewritten by remove."""

    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / "channels.json").write_text("{not json", encoding="utf-8")

    result = run_remove(home, "feishu", "--json")
    assert result.returncode != 0
    assert payload(result)["code"] == "INVALID_ARGUMENT"
    assert (home / "channels.json").read_text(encoding="utf-8") == "{not json"
