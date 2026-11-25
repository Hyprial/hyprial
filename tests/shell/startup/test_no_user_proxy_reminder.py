"""S2c (owner 2026-11-21): no command reminds the owner to set up a user-proxy.

init (daemon already running and freshly launched), the daemon-run first login
and ``daemon restart`` carry no ``SQUIRE_USER_PROXY_NOT_CONFIGURED`` warning, and
the ``hints.userProxy`` switch that silenced it is no longer a config key.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.kernel import DaemonLaunchResult, ipc_errors
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

OWNER = "allen"
RETIRED = "SQUIRE_USER_PROXY_NOT_CONFIGURED"
SERVING = {"running": True, "pid": 4101, "epoch": "e1", "phase": "serving", "owner": OWNER}


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    services = _cli_get_services()
    monkeypatch.setattr(services, "_state_dir", lambda: state)
    monkeypatch.setattr(services, "_hyprial_home", lambda: tmp_path)
    monkeypatch.setenv("HYPRIAL_OWNER", OWNER)
    monkeypatch.delenv("HYPRIAL_NETWORK_ISOLATED", raising=False)
    return tmp_path


def _codes(result: dict[str, Any]) -> list[str]:
    return [item.get("code") for item in result.get("warnings") or [] if isinstance(item, dict)]


def test_init_on_a_running_daemon_has_no_user_proxy_reminder(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.shell.impl.cli.commands.init import init as init_module

    monkeypatch.setattr(_cli_get_services(), "_daemon_probe", lambda timeout: {"daemon": SERVING})
    result = init_module._complete_initialization(
        ready_timeout=1.0, listen=None, connect=None, org_warning=None
    )
    assert RETIRED not in _codes(result)
    assert not (home / "state" / "users.json").exists()


def test_init_that_launches_the_daemon_has_no_user_proxy_reminder(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.shell.impl.cli.commands.init import init as init_module

    def unavailable(timeout: float) -> dict[str, Any]:
        raise ipc_errors.DaemonUnavailableError("not running")

    monkeypatch.setattr(_cli_get_services(), "_daemon_probe", unavailable)
    monkeypatch.setattr(
        _cli_get_services(),
        "_launch_daemon_process",
        lambda **_kwargs: DaemonLaunchResult.launched(SERVING),
    )
    result = init_module._complete_initialization(
        ready_timeout=1.0, listen=None, connect=None, org_warning=None
    )
    assert RETIRED not in _codes(result)


def test_first_login_has_no_user_proxy_reminder(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hyprial.shell.impl.cli.commands.login import aftercare, commands as login_commands
    from hyprial.shell.impl.cli.commands.login import first_login

    monkeypatch.setattr(
        first_login, "run_first_login", lambda **_kwargs: {"ok": True, "daemon": dict(SERVING)}
    )
    monkeypatch.setattr(aftercare, "_pickup_org_invites_after_login", lambda *_a, **_k: [])
    monkeypatch.setattr(login_commands, "_organization_next_steps", lambda _services: None)
    response = login_commands._first_login(
        _cli_get_services(), started=True, no_open=True, json_output=False
    )
    assert RETIRED not in _codes(response["daemon"])
    assert "user proxy setup" not in capsys.readouterr().err


def test_hints_user_proxy_is_not_a_config_key(home: Path) -> None:
    result = CliRunner().invoke(_cli_root_app, ["config", "set", "hints.userProxy", "off", "--json"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["code"] == "INVALID_CONFIGURATION"
    assert "unknown config key 'hints.userProxy'" in payload["error"]
    assert "hints." not in payload["error"].split("supported:", 1)[1]
    assert not (home / "settings.json").exists()
