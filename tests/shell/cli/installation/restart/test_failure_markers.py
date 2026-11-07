"""cli_upgrade_restart: failure markers scenarios."""

from __future__ import annotations
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
import pytest
from typer.testing import CliRunner
from hyprial.daemon.impl.autoupdate.alert import (
    RestartProcessObservation,
    RestartProcessState,
)

from .support import (
    _fail_launch_writing_log,
    _fake_upgrade,
    _locked_constraints as _locked_constraints,
    _read_failure_marker,
    _silence_owner_alert,
    _start_failed_entry,
)
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.common.daemon_stop as _cli_owner_1
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_restart_failure_marker_names_the_root_cause_not_only_the_symptom(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The durable half must name the cause, not only the symptom.

    Before this the marker said "daemon restart failed: <timeout>" and the
    `daemon.start.failed` diagnosis lived only in daemon.jsonl -- so the record
    an unattended failure leaves behind required already knowing where else to
    look. This is the gap the spec closes, asserted on the real CLI path.
    """

    _silence_owner_alert(monkeypatch)
    fail_launch = _fail_launch_writing_log(
        tmp_path,
        lambda: _start_failed_entry(
            datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            phase="actor-runtime",
            error_type="DomainCommandError",
            error="no agent named 'ciia-dipc' on this machine",
        ),
    )
    _fake_upgrade(monkeypatch, tmp_path, launch=fail_launch)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 1
    marker = _read_failure_marker(tmp_path)
    assert marker["detail"] == (
        "daemon restart failed: daemon did not become ready within 15s\n"
        "本次 daemon.start.failed: phase=actor-runtime "
        "errorType=DomainCommandError "
        "error=no agent named 'ciia-dipc' on this machine"
    )
    assert marker["startFailure"] == {
        "phase": "actor-runtime",
        "errorType": "DomainCommandError",
        "error": "no agent named 'ciia-dipc' on this machine",
    }


def test_restart_failure_marker_says_there_is_no_start_failure_record_yet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No log record at all: the marker states that, and invents nothing."""

    _silence_owner_alert(monkeypatch)
    _fake_upgrade(
        monkeypatch, tmp_path, launch=_fail_launch_writing_log(tmp_path, None)
    )

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 1
    marker = _read_failure_marker(tmp_path)
    assert marker["detail"] == (
        "daemon restart failed: daemon did not become ready within 15s\n"
        "本次启动: 日志里还没有 start.failed 记录"
    )
    assert marker["startFailure"] is None


def test_restart_failure_marker_does_not_borrow_a_previous_launchs_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An older record is not this restart's cause; the marker must not cite it.

    `latest_start_failure` is bounded by the parent-side launch time on purpose
    (the previous spec's mutation check exercises the same boundary for the
    alert). This asserts the boundary holds on the marker path too, where a
    stale cause would otherwise be indistinguishable from the real one.
    """

    _silence_owner_alert(monkeypatch)
    fail_launch = _fail_launch_writing_log(
        tmp_path,
        lambda: _start_failed_entry(
            "2020-01-01T00:00:00.000Z",
            phase="ipc-server",
            error_type="OldFailure",
            error="stale failure from a previous generation",
        ),
    )
    _fake_upgrade(monkeypatch, tmp_path, launch=fail_launch)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 1
    marker = _read_failure_marker(tmp_path)
    assert marker["startFailure"] is None
    assert "本次启动: 日志里还没有 start.failed 记录" in marker["detail"]
    assert "stale failure from a previous generation" not in marker["detail"]


def test_survivor_marker_omits_start_failure_instead_of_claiming_a_search(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A survivor is not a start failure; its marker must not say it looked.

    The survivor branch writes a durable marker too, and it never calls
    `latest_start_failure`. A written-out `startFailure: null` there would read
    as "we looked for this launch's daemon.start.failed and found none" -- the
    silent omission the restart-failure path's sentence exists to prevent, in
    the one place where that sentence is not written either. So the key must be
    absent, and this test exercises the real call site, not the helper alone.
    """

    _silence_owner_alert(monkeypatch)
    _fake_upgrade(monkeypatch, tmp_path)

    def stop_with_survivor(*, require_exit: bool = True) -> dict[str, Any]:
        # Same code, message shape and payload as the real raise in
        # `_wait_daemon_teardown_receipt`; that shape is pinned there by
        # `tests/test_daemon_restart_lock.py`, so this stub cannot invent a
        # form the production branch never produces.
        assert require_exit is True
        raise _cli_get_services().CliError(
            "DAEMON_STOP_SURVIVOR",
            "daemon teardown completed but process 999993 did not exit within "
            "1.0s; it is still alive and may still own harness children.",
            {"oldPid": 999_993, "teardownCompleted": True},
        )

    monkeypatch.setattr(_cli_get_services(), "_stop_daemon_gracefully", stop_with_survivor)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["restart"]["survivingOldProcess"] is True
    marker = _read_failure_marker(tmp_path)
    assert marker["summary"] == "旧 daemon 拆解完成但进程没有退出"
    assert "startFailure" not in marker
    assert "本次启动: 日志里还没有 start.failed 记录" not in marker["detail"]


@pytest.mark.parametrize(
    ("owner_status", "expected"),
    [
        ("alive", RestartProcessState.RUNNING),
        ("pid-missing", RestartProcessState.EXITED),
        ("unknown", RestartProcessState.UNKNOWN),
    ],
)
def test_restart_process_observation_preserves_all_three_pid_birth_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    owner_status: str,
    expected: RestartProcessState,
) -> None:
    from hyprial.daemon.impl.mcp.channel import ownership as channel

    error = _cli_get_services().CliError("DAEMON_START_TIMEOUT", "fixture timeout")
    setattr(error, "_daemon_process_pid", 44710)
    setattr(error, "_daemon_process_identity", "proc-starttime:123")
    setattr(error, "_daemon_process_exited", False)
    status = channel._OwnerProcessStatus(owner_status)
    monkeypatch.setattr(channel, "_owner_process_status", lambda *_args: status)

    observation = _cli_owner_1._observe_restart_process(error)

    assert observation == RestartProcessObservation(44710, expected)
