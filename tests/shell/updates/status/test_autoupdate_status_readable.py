"""#28: ``hyprial daemon upgrade status`` tells a person what is actually going on.

The old human output was the raw dict.  Its first lines were ``installed:
False, loaded: False`` -- the *legacy* launchd unit -- while the daemon's
scheduler ran the upgrades at 03:17 and 15:17.  And on macOS/Linux with the
daemon up, ``pendingRestart`` (documented in hyprial-ops) was absent, while a
stale "awaiting confirmation" in ``lastRun`` outlived the restart that
applied it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.runtime import entry as cli
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.common.pending_restart as _cli_owner_2
import hyprial.shell.impl.cli.commands.upgrade.autoupdate as _cli_owner_1
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

_RECORDED_PID = 6786
_PENDING = {
    "tag": "internal",
    "commit": "48e18b897e6d90c794c1b015d62fd732915ece9e",
    "version": "0.4.6",
    "before": {"pid": _RECORDED_PID, "version": "0.4.5"},
    "installedAt": "2026-09-26T07:17:44+00:00",
}
_LAST_RUN = {
    "ok": True,
    "at": "2026-09-26T07:17:46+00:00",
    "resolvedTag": "internal",
    "resolvedCommit": "48e18b897e6d90c794c1b015d62fd732915ece9e",
    "upgraded": True,
    "restartRequired": True,
    "restart": {"attempted": False, "awaitingConfirmation": True},
}


def _status(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "ok": True,
        "platform": "darwin",
        "unit": None,
        "installed": False,
        "loaded": False,
        "schedule": [{"hour": 3, "minute": 17}, {"hour": 15, "minute": 17}],
        "lastRun": _LAST_RUN,
        "autoUpgradeEnabled": True,
        "scheduler": {"running": True, "active": False, "nextRunAt": "2026-09-27T03:17:00"},
        "pendingRestart": None,
        "pendingRestartState": None,
        "pendingAppMigrations": [],
        "pendingAppMigrationsUnreadable": [],
        "pendingAppMigrationsScanError": None,
    }
    base.update(overrides)
    return base


def test_on_and_running_reports_only_the_daemon_scheduler() -> None:
    text = _cli_owner_1._render_autoupdate_status(_status())
    lines = text.splitlines()
    assert lines[0].endswith("automatic upgrade: on")
    assert "03:17 and 15:17" in text
    assert "running, next run 2026-09-27 03:17" in text
    assert "installed internal 48e18b89" in text
    assert "legacy unit" not in text
    assert "False" not in text


def test_every_line_fits_80_columns_so_no_command_is_wrapped_in_half() -> None:
    """Rich hard-wraps at 80 when stdout is not a terminal; a wrapped
    ``hyprial daemon upgrade apply`` cannot be copied."""
    for status in (
        _status(pendingRestart=_PENDING, pendingRestartState="waiting"),
        _status(pendingRestart=_PENDING, pendingRestartState="unverified"),
        _status(autoUpgradeEnabled=False, scheduler={"running": False}),
        _status(installed=True, loaded=True),
    ):
        for line in _cli_owner_1._render_autoupdate_status(status).splitlines():
            assert len(line) <= 80, line


@pytest.mark.parametrize(
    ("state", "expected", "command"),
    [
        ("waiting", "waiting: 0.4.6 48e18b89 installed; the daemon runs", "apply it with: hyprial daemon upgrade apply"),
        ("applied", "done: the running daemon already has 0.4.6 48e18b89", None),
        ("unverified", "could not check the daemon", "check and apply with: hyprial daemon upgrade apply"),
    ],
)
def test_pending_restart_says_whether_it_is_still_owed(
    state: str, expected: str, command: str | None
) -> None:
    text = _cli_owner_1._render_autoupdate_status(
        _status(pendingRestart=_PENDING, pendingRestartState=state)
    )
    assert expected in text
    if command is None:
        assert "hyprial daemon upgrade apply" not in text
    else:
        # On a line of its own, so it is never split by a wrap.
        assert f"               {command}" in text.splitlines()


def test_off_names_the_command_that_turns_it_on() -> None:
    text = _cli_owner_1._render_autoupdate_status(_status(autoUpgradeEnabled=False))
    assert "automatic upgrade: off" in text
    assert "hyprial config set autoUpgrade true" in text


def test_a_failed_or_skipped_run_says_why() -> None:
    failed = _cli_owner_1._render_autoupdate_status(
        _status(lastRun={"ok": False, "at": "2026-09-26T07:17:46+00:00", "code": "UPDATE_FETCH_FAILED", "error": "timed out"})
    )
    assert "failed: UPDATE_FETCH_FAILED: timed out" in failed
    skipped = _cli_owner_1._render_autoupdate_status(
        _status(lastRun={"ok": True, "at": "2026-09-26T07:17:46+00:00", "skipped": True, "skipReason": "disabled"})
    )
    assert "skipped: disabled" in skipped
    never = _cli_owner_1._render_autoupdate_status(_status(lastRun=None))
    assert "last run     never" in never


def test_unreachable_daemon_says_runs_need_it() -> None:
    text = _cli_owner_1._render_autoupdate_status(_status(scheduler={"running": False, "active": False}))
    assert "scheduled runs happen only while the daemon runs" in text


# ── the JSON side: collected on the platform path, judged like restart ─────


def _platform_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, daemon_pid: int | None) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / _cli_owner_2.PENDING_RESTART_FILE).write_text(json.dumps(_PENDING), encoding="utf-8")
    monkeypatch.setattr(_cli_get_services(), "_hyprial_home", lambda: tmp_path)
    monkeypatch.setattr(_cli_get_services(), "_state_dir", lambda: state)
    def request(method: str, **_kwargs: object) -> object:
        if method == "autoupdate.status":
            return {"running": True, "active": False, "nextRunAt": "2026-09-27T03:17:00"}
        raise AssertionError(method)

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    def probe(timeout: float = 0.5) -> dict[str, object]:
        assert timeout <= 1.0  # status stays cheap
        if daemon_pid is None:
            raise cli.ipc_errors.DaemonUnavailableError("no daemon", {})
        return {"running": True, "pid": daemon_pid}

    monkeypatch.setattr(_cli_get_services(), "_daemon_probe", probe)


@pytest.mark.parametrize(
    ("daemon_pid", "state"),
    [(_RECORDED_PID, "waiting"), (_RECORDED_PID + 1, "applied"), (None, "unverified")],
)
def test_status_reports_pending_restart_and_whether_it_is_owed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, daemon_pid: int | None, state: str
) -> None:
    _platform_home(monkeypatch, tmp_path, daemon_pid=daemon_pid)
    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["pendingRestart"] == _PENDING
    assert payload["pendingRestartState"] == state
    # The contract-pinned legacy fields are still there, unchanged.
    assert payload["installed"] is False and payload["loaded"] is False


def test_human_status_on_the_platform_path_is_the_summary_not_a_dict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _platform_home(monkeypatch, tmp_path, daemon_pid=_RECORDED_PID + 1)
    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "status"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("hyprial daemon upgrade   automatic upgrade:")
    assert "'installed'" not in result.output
    assert "done: the running daemon already has 0.4.6" in result.output
