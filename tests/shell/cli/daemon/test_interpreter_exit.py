"""The CLI leaves the interpreter after an ownerless loop only when it owns it.

Decision: daemon-first-z2-bootstrap-exit-20261116.  ``main()`` marks the
interpreter as owned; a command invoked in-process (tests, embedding) returns
normally and never reaches the exit.  The exit helper runs after the result
envelope is emitted and flushes both streams before it leaves.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import hyprial.daemon as _daemon_public
import hyprial.shell.impl.cli.commands.daemon.commands as _daemon_commands
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401
import hyprial.shell.impl.daemon_wiring.onboarding as _onboarding
import hyprial.shell.impl.daemon_wiring.onboarding.launch as _wiring_launch
from hyprial.daemon.impl.bootstrap.exit_independence import (
    EXIT_CLEAN,
    EXIT_DIRTY,
    leave_interpreter_after_loop,
)
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services

OWNED = {"interpreterOwned": True}


@pytest.fixture
def ownerless_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home, state = tmp_path / "home", tmp_path / "state"
    home.mkdir(mode=0o700)
    services = _cli_get_services()
    monkeypatch.setattr(services, "_hyprial_home", lambda: home)
    monkeypatch.setattr(services, "_state_dir", lambda: state)
    monkeypatch.setattr(services, "_socket_path", lambda: state / "daemon.sock")
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)


def _record_leaves(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    leaves: list[dict] = []
    monkeypatch.setattr(
        _onboarding,
        "leave_ownerless_loop",
        lambda **kwargs: leaves.append(kwargs) or (),
    )
    return leaves


def test_an_in_process_run_returns_normally_and_never_leaves(monkeypatch, ownerless_home):
    leaves = _record_leaves(monkeypatch)
    monkeypatch.setattr(_wiring_launch, "launch_daemon_lifecycle", lambda **_kwargs: None)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "run", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["stopped"] is True
    assert leaves == []


def test_an_owned_interpreter_leaves_clean_after_the_envelope_is_written(monkeypatch, ownerless_home):
    monkeypatch.setattr(_wiring_launch, "launch_daemon_lifecycle", lambda **_kwargs: None)
    leaves = _record_leaves(monkeypatch)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "run", "--json"], obj=OWNED)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["stopped"] is True
    assert leaves == [{}]


def test_an_owned_interpreter_leaves_dirty_after_a_failed_loop(monkeypatch, ownerless_home):
    leaves = _record_leaves(monkeypatch)

    def refuse(**_kwargs: object) -> None:
        raise _daemon_public.BootstrapLaunchError("BOOTSTRAP_AUTH_FAILED", "authorization failed")

    monkeypatch.setattr(_wiring_launch, "launch_daemon_lifecycle", refuse)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "run", "--json"], obj=OWNED)

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == "BOOTSTRAP_AUTH_FAILED"
    assert leaves == [{"failed": True}]


def test_an_owned_interpreter_that_never_entered_the_loop_is_not_forced_out(monkeypatch, tmp_path):
    leaves = _record_leaves(monkeypatch)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    services = _cli_get_services()
    monkeypatch.setattr(services, "_hyprial_home", lambda: home)
    monkeypatch.setattr(services, "_state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(services, "_socket_path", lambda: tmp_path / "state" / "daemon.sock")
    monkeypatch.setenv("HYPRIAL_OWNER", "owner@example.test")

    def busy(*_args: object, **_kwargs: object) -> None:
        raise services.CliError("DAEMON_OWNERSHIP_BUSY", "stop here")

    monkeypatch.setattr(_daemon_commands, "_acquire_daemon_fence", busy)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "run", "--json"], obj=OWNED)

    assert json.loads(result.stdout)["code"] == "DAEMON_OWNERSHIP_BUSY"
    assert leaves == []


@pytest.mark.parametrize(("failed", "code"), [(False, EXIT_CLEAN), (True, EXIT_DIRTY)])
def test_the_exit_helper_flushes_both_streams_before_it_leaves(monkeypatch, failed, code):
    events: list[object] = []

    class Stream:
        def __init__(self, name: str) -> None:
            self.name = name

        def flush(self) -> None:
            events.append(f"flush-{self.name}")

    monkeypatch.setattr("sys.stdout", Stream("stdout"))
    monkeypatch.setattr("sys.stderr", Stream("stderr"))

    errors = leave_interpreter_after_loop(failed=failed, exit_process=events.append)

    assert errors == ()
    assert events == ["flush-stdout", "flush-stderr", code]
