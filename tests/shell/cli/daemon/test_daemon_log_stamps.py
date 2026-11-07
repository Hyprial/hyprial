"""``hyprial daemon run`` stamps what it writes to stdout/stderr.

A launchd/systemd unit redirects the daemon's own stdio into one file that
outlives every restart.  Before this seam the file held lines such as
``{"ok":false,"code":"DAEMON_OWNERSHIP_BUSY",...}`` with no time and no daemon
instance, so an operator could prove an error happened but not which restart
produced it (M-hq-adjutant-0002).  These tests pin the real command path: the
envelope written by ``daemon run --json`` names ``t``, ``pid`` and -- once the
runtime exists -- the ``epoch``.
"""

from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import hyprial.identity
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.daemon.commands as _daemon_commands
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

ISO_MILLISECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class _FakeTransaction:
    def __enter__(self) -> _FakeTransaction:
        return self

    def __exit__(self, *_error: object) -> None:
        return None

    def detach(self) -> io.StringIO:
        return io.StringIO()

    def close(self) -> None:
        return None


class _FakeIdentityTransactions:
    @staticmethod
    def acquire(*_args: object, **_kwargs: object) -> _FakeTransaction:
        return _FakeTransaction()

    @staticmethod
    def adopt_legacy(*_args: object, **_kwargs: object) -> None:
        return None


class _FakeFence(_FakeTransaction):
    pass


class _FakeRuntime:
    def __init__(self, epoch: str) -> None:
        self.epoch = epoch
        self.owns_process = False
        self.ran = False

    def owns_process_exit(self) -> None:
        self.owns_process = True

    def run(self, **_kwargs: object) -> None:
        self.ran = True


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home, state = tmp_path / "home", tmp_path / "state"
    home.mkdir(mode=0o700)
    services = _cli_get_services()
    monkeypatch.setattr(services, "require_initialized_hyprial_home", lambda: home)
    monkeypatch.setattr(services, "_hyprial_home", lambda: home)
    monkeypatch.setattr(services, "_state_dir", lambda: state)
    monkeypatch.setattr(services, "_socket_path", lambda: state / "daemon.sock")
    monkeypatch.setattr(
        hyprial.identity, "IdentityTransactionLock", _FakeIdentityTransactions
    )


def _install_runtime(monkeypatch: pytest.MonkeyPatch, epoch: str) -> _FakeRuntime:
    runtime = _FakeRuntime(epoch)
    import hyprial.daemon

    class _Factory:
        @staticmethod
        def from_environment(**_kwargs: Any) -> _FakeRuntime:
            return runtime

    monkeypatch.setattr(hyprial.daemon, "DaemonApplication", _Factory)
    return runtime


def test_daemon_run_stamps_the_envelope_with_time_and_epoch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    runtime = _install_runtime(monkeypatch, "epoch-run-1")
    monkeypatch.setattr(
        _daemon_commands, "_acquire_daemon_fence", lambda *_a, **_k: _FakeFence()
    )

    result = CliRunner().invoke(_cli_root_app, ["daemon", "run", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert runtime.ran is True
    # The result the command already produced is untouched.
    assert payload["ok"] is True
    assert payload["stopped"] is True
    # ... and the record now names when it happened and which daemon.
    assert ISO_MILLISECONDS.match(payload["t"]), payload["t"]
    assert payload["epoch"] == "epoch-run-1"
    assert payload["pid"] == os.getpid()


def test_daemon_run_stamps_a_refusal_before_any_epoch_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)

    def _busy(*_args: object, **_kwargs: object) -> _FakeFence:
        raise _cli_get_services().CliError(
            "DAEMON_OWNERSHIP_BUSY", "daemon state ownership is held"
        )

    monkeypatch.setattr(_daemon_commands, "_acquire_daemon_fence", _busy)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "run", "--json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == "DAEMON_OWNERSHIP_BUSY"
    # The refusal happens before the runtime mints an epoch; the pid still
    # names the process a supervisor relaunched.
    assert ISO_MILLISECONDS.match(payload["t"]), payload["t"]
    assert payload["pid"] == os.getpid()
    assert "epoch" not in payload
