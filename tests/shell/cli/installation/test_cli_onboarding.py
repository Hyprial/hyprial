"""New-machine ``init`` and ``login --dry-run`` on homes with no account.

Decision: daemon-first-z5-cli-init-20261119.  ``hyprial init`` never signs in: on a
home with no account it starts the bootstrap daemon and says ``hyprial login`` is
next.  ``run_login`` and ``_run_login_cli_flow`` are replaced by recorders that
fail the test, the launch primitive is a recorder, and nothing here contacts an
identity service.  The first login itself is covered in ``tests/shell/login/
daemon_first`` and, against a real controlled daemon, in
``tests/integration/login_daemon_first``.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)
from hyprial.daemon.impl.configuration.identity import resolve_node_owner
from hyprial.kernel import DaemonLaunchResult, ipc_errors
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services

BOOTSTRAP = {
    "running": True,
    "pid": 4242,
    "epoch": "epoch-boot",
    "phase": "bootstrap",
    "owner": None,
    "zenoh": {"listen": ["tcp/127.0.0.1:1"], "connect": []},
}
LOGIN_KEYS = ("identity", "device", "verificationUri", "userCode")


@pytest.fixture(autouse=True)
def _service_doctor_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    import hyprial.shell.impl.cli.commands.daemon.service as service_module

    monkeypatch.setattr(
        service_module,
        "daemon_service_doctor_check",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        service_module,
        "_run_service",
        lambda operation: {"ok": True, "supervision": "supervised"},
    )


def _free_endpoint() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"tcp/127.0.0.1:{probe.getsockname()[1]}"


def _ownerless_environment(
    monkeypatch: pytest.MonkeyPatch, home: Path, state: Path
) -> dict[str, str]:
    """Remove both owner cells supplied by the suite's autouse fixture."""

    fixture_home = Path(os.environ["HYPRIAL_HOME"])
    (fixture_home / "settings.json").unlink(missing_ok=True)
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    monkeypatch.setenv("HYPRIAL_ZENOH_LISTEN", _free_endpoint())
    monkeypatch.setenv("HYPRIAL_ZENOH_CONNECT", "")
    monkeypatch.setenv("HYPRIAL_NODE_ID", "onboarding-test")
    monkeypatch.delenv("HARNESS_SOCKET_PATH", raising=False)

    with pytest.raises(ValueError, match="user identity is not set"):
        resolve_node_owner({"HYPRIAL_HOME": str(home)}, hyprial_home=home)

    return {
        "HYPRIAL_HOME": str(home),
        "HARNESS_STATE_DIR": str(state),
        "HYPRIAL_PEER_DISCOVERY": "0",
        "HYPRIAL_ZENOH_LISTEN": os.environ["HYPRIAL_ZENOH_LISTEN"],
        "HYPRIAL_ZENOH_CONNECT": "",
        "HYPRIAL_NODE_ID": "onboarding-test",
    }


def _no_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda *_a, **_kw: (_ for _ in ()).throw(ipc_errors.DaemonUnavailableError("no daemon")),
    )


def _never_logs_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, *_a, **_kw: pytest.fail(f"unexpected daemon call: {method}")
        if str(method).startswith("auth.")
        else (_ for _ in ()).throw(ipc_errors.DaemonUnavailableError("no daemon")),
    )


def test_init_on_an_ownerless_home_starts_bootstrap_and_never_logs_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, state = tmp_path / "home", tmp_path / "state"
    env = _ownerless_environment(monkeypatch, home, state)
    _no_daemon(monkeypatch)
    _never_logs_in(monkeypatch)
    launches: list[dict[str, Any]] = []

    def launch(**kwargs: Any) -> DaemonLaunchResult:
        launches.append(kwargs)
        return DaemonLaunchResult.launched(BOOTSTRAP)

    monkeypatch.setattr(_cli_get_services(), "_launch_daemon_process", launch)

    result = CliRunner().invoke(
        _cli_root_app, ["init", "--listen", env["HYPRIAL_ZENOH_LISTEN"], "--json"], env=env
    )

    assert result.exit_code == 0, (result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["ok"] is True and payload["started"] is True and payload["alreadyRunning"] is False
    assert payload["phase"] == "bootstrap" and payload["owner"] is None and payload["pid"] == 4242
    assert payload["nextSteps"] == ["hyprial login"]
    for key in LOGIN_KEYS:
        assert key not in payload
    assert len(launches) == 1 and launches[0]["listen"] == env["HYPRIAL_ZENOH_LISTEN"]
    assert home.is_dir()
    assert not (home / "settings.json").exists() or "owner" not in json.loads(
        (home / "settings.json").read_text("utf-8")
    )
    assert "device" not in result.stderr


def test_init_in_human_mode_says_login_is_next(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = _ownerless_environment(monkeypatch, tmp_path / "home", tmp_path / "state")
    _no_daemon(monkeypatch)
    _never_logs_in(monkeypatch)
    monkeypatch.setattr(
        _cli_get_services(), "_launch_daemon_process", lambda **_kw: DaemonLaunchResult.launched(BOOTSTRAP)
    )

    result = CliRunner().invoke(_cli_root_app, ["init"], env=env)

    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert "hyprial login" in result.stderr


@pytest.mark.parametrize(
    "settings_text",
    ["{not json", json.dumps(["owner", "not-an-object"])],
    ids=["unparseable", "not-an-object"],
)
def test_init_with_malformed_settings_stays_loud_and_starts_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings_text: str
) -> None:
    home, state = tmp_path / "home", tmp_path / "state"
    env = _ownerless_environment(monkeypatch, home, state)
    home.mkdir()
    (home / "settings.json").write_text(settings_text, encoding="utf-8")
    _no_daemon(monkeypatch)
    _never_logs_in(monkeypatch)
    monkeypatch.setattr(
        _cli_get_services(),
        "_launch_daemon_process",
        lambda **_kw: pytest.fail("malformed settings must not start a daemon"),
    )

    result = CliRunner().invoke(_cli_root_app, ["init", "--json"], env=env)

    assert result.exit_code == 1, (result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["ok"] is False and "cannot parse" in payload["error"]


def test_init_with_owner_preserves_launch_shape_without_calling_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, state = tmp_path / "home", tmp_path / "state"
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("HYPRIAL_OWNER", "e2e")
    _no_daemon(monkeypatch)
    monkeypatch.setattr(_cli_get_services(), "_initialize_org_context", lambda: None)
    _never_logs_in(monkeypatch)
    status = {
        "running": True,
        "pid": 4242,
        "epoch": "same-shape",
        "phase": "serving",
        "owner": "e2e",
        "zenoh": {"listen": ["tcp/127.0.0.1:1"], "connect": []},
    }
    monkeypatch.setattr(
        _cli_get_services(), "_launch_daemon_process", lambda **_kwargs: DaemonLaunchResult.launched(status)
    )

    result = CliRunner().invoke(_cli_root_app, ["init", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "started": True,
        "alreadyRunning": False,
        "service": {"ok": True, "supervision": "supervised"},
        **status,
    }
