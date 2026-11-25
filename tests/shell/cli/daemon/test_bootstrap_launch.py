"""``daemon run`` on an ownerless home: the lifecycle loop, with the terminal login loop gone."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

import hyprial.daemon as _daemon_public
import hyprial.shell.impl.daemon_wiring.onboarding.launch as _wiring_launch
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.daemon.commands as _daemon_commands
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401


@pytest.fixture(autouse=True)
def empty_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)


def _isolate_services(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home, state = tmp_path / "home", tmp_path / "state"
    home.mkdir(mode=0o700)
    services = _cli_get_services()
    monkeypatch.setattr(services, "_hyprial_home", lambda: home)
    monkeypatch.setattr(services, "_state_dir", lambda: state)
    monkeypatch.setattr(services, "_socket_path", lambda: state / "daemon.sock")

    def must_not_require() -> None:
        raise AssertionError("bootstrap branch ran require_initialized_hyprial_home")

    monkeypatch.setattr(
        services, "require_initialized_hyprial_home", must_not_require
    )
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)


def test_daemon_run_bootstrap_branch_precedes_initialized_home_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_services(monkeypatch, tmp_path)
    calls: dict[str, object] = {}

    def fake_launch(**kwargs: object) -> None:
        calls.update(kwargs)

    monkeypatch.setattr(_wiring_launch, "launch_daemon_lifecycle", fake_launch)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "run", "--bootstrap", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["stopped"] is True
    assert payload["pid"] == os.getpid()
    assert calls["home"] == tmp_path / "home"
    assert calls["state_dir"] == tmp_path / "state"
    assert calls["socket_path"] == tmp_path / "state" / "daemon.sock"
    assert "owns_process_exit" not in calls
    assert calls["exit_unless_logout"] is True


def test_daemon_run_reports_a_bootstrap_refusal_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_services(monkeypatch, tmp_path)

    def refuse(**_kwargs: object) -> None:
        raise _daemon_public.BootstrapLaunchError(
            "BOOTSTRAP_AUTH_FAILED", "authorization failed"
        )

    monkeypatch.setattr(_wiring_launch, "launch_daemon_lifecycle", refuse)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "run", "--bootstrap", "--json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == "BOOTSTRAP_AUTH_FAILED"


def test_daemon_run_accepts_an_ownerless_home_without_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, state = tmp_path / "home", tmp_path / "state"
    home.mkdir(mode=0o700)
    services = _cli_get_services()
    monkeypatch.setattr(services, "_hyprial_home", lambda: home)
    monkeypatch.setattr(services, "_state_dir", lambda: state)
    monkeypatch.setattr(services, "_socket_path", lambda: state / "daemon.sock")
    calls: dict[str, object] = {}
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    monkeypatch.setattr(
        _wiring_launch, "launch_daemon_lifecycle", lambda **kwargs: calls.update(kwargs)
    )
    monkeypatch.setattr(
        _daemon_commands,
        "_acquire_daemon_fence",
        lambda *_args, **_kwargs: pytest.fail("normal path reached the daemon fence"),
    )

    result = CliRunner().invoke(_cli_root_app, ["daemon", "run", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert calls["home"] == home


@pytest.mark.parametrize("source", ["environment", "settings"])
def test_daemon_run_resolves_the_owner_as_the_daemon_does(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str
) -> None:
    home, state = tmp_path / "home", tmp_path / "state"
    home.mkdir(mode=0o700)
    services = _cli_get_services()
    monkeypatch.setattr(services, "_hyprial_home", lambda: home)
    monkeypatch.setattr(services, "_state_dir", lambda: state)
    monkeypatch.setattr(services, "_socket_path", lambda: state / "daemon.sock")
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    if source == "environment":
        monkeypatch.setenv("HYPRIAL_OWNER", "env-owner@example.test")
    else:
        (home / "settings.json").write_text(json.dumps({"version": 1, "owner": "settings-owner"}))
    monkeypatch.setattr(
        _wiring_launch,
        "launch_daemon_lifecycle",
        lambda **_kwargs: pytest.fail("an owned home entered the ownerless lifecycle"),
    )
    reached: list[str] = []

    def fence(*_args: object, **_kwargs: object) -> None:
        reached.append("normal-path")
        raise services.CliError("DAEMON_OWNERSHIP_BUSY", "stop here")

    monkeypatch.setattr(_daemon_commands, "_acquire_daemon_fence", fence)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "run", "--json"])

    assert reached == ["normal-path"], result.output
    assert json.loads(result.stdout)["code"] == "DAEMON_OWNERSHIP_BUSY"


def test_ownerless_daemon_run_refuses_a_stale_expected_identity_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from hyprial.identity import EXPECTED_IDENTITY_GENERATION_ENV
    from hyprial.shell.impl.login.generation import IDENTITY_GENERATION_STALE_EXIT_CODE

    _isolate_services(monkeypatch, tmp_path)
    monkeypatch.setenv(EXPECTED_IDENTITY_GENERATION_ENV, "5")
    monkeypatch.setattr(
        _wiring_launch,
        "launch_daemon_lifecycle",
        lambda **_kwargs: pytest.fail("a stale launch expectation reached the ownerless loop"),
    )

    result = CliRunner().invoke(_cli_root_app, ["daemon", "run", "--json"])

    assert result.exit_code == IDENTITY_GENERATION_STALE_EXIT_CODE, result.output
    assert json.loads(result.stdout.strip().splitlines()[-1])["code"] == "IDENTITY_TRANSACTION_STALE"


def test_ownerless_daemon_run_hands_the_loop_a_log_stamp_binder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_services(monkeypatch, tmp_path)
    calls: dict[str, object] = {}
    monkeypatch.setattr(
        _wiring_launch, "launch_daemon_lifecycle", lambda **kwargs: calls.update(kwargs)
    )

    result = CliRunner().invoke(_cli_root_app, ["daemon", "run", "--json"])

    assert result.exit_code == 0, result.output
    binder = calls["bind_epoch"]
    assert callable(binder)
    assert binder.__self__.__class__.__name__ == "LogStamp"


def test_package_exports_no_terminal_login() -> None:
    import hyprial.shell.impl.daemon_wiring.onboarding as onboarding

    assert "launch_bootstrap_daemon" not in onboarding.__all__
    assert not hasattr(onboarding, "launch_bootstrap_daemon")
    assert not hasattr(_wiring_launch, "launch_bootstrap_daemon")
    assert "launch_bootstrap_daemon" not in _wiring_launch.__all__
