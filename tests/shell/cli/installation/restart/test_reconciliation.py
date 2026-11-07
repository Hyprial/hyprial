"""cli_upgrade_restart: reconciliation scenarios."""

from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Any
import pytest
from typer.testing import CliRunner
from hyprial.kernel import ipc_errors

from .support import (
    _bind_owner,
    _fake_upgrade,
    _locked_constraints as _locked_constraints,
    _record_owner_messages,
    _seed_desired_rows,
)
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.upgrade.commands as _cli_owner_1
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_a_reconciled_restart_is_confirmed_and_needs_no_followup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """T1: launch ok + ping phase=reconciled => confirmed true, reported as a
    plain install, and nobody is pinged twice about it."""

    events = _fake_upgrade(monkeypatch, tmp_path)
    _bind_owner(tmp_path)
    sent = _record_owner_messages(monkeypatch)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["restart"]["confirmed"] is True
    assert payload["restart"]["reason"] == "resolved tag changed; running daemon restarted"
    assert "followUp" not in payload["restart"], "reconciled needs no follow-up"
    assert len(sent) == 1, "a settled restart must not send a follow-up"
    assert "尚未确认完成 adapter 恢复" not in sent[0], (
        "a reconciled restart reported as UPGRADE_UNCONFIRMED"
    )
    assert events.count("ping:2") == 1, "the phase was read more than once"


def test_a_restore_that_settles_during_the_poll_gets_one_recovery_followup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """T2: restoring at the read, reconciled during the poll => exactly one
    「已恢复」 follow-up and confirmed flips to true in the result."""

    events = _fake_upgrade(monkeypatch, tmp_path)
    _bind_owner(tmp_path)
    sent = _record_owner_messages(monkeypatch)
    _seed_desired_rows(tmp_path, 1)

    # One row at the default knobs: a 90s poll budget, so the transition
    # below lands well inside it.  Ping answers in the daemon's real shape.
    ping_phases = iter(["restoring", "restoring", "reconciled"])

    lifecycle = {"stopped": False}


    def phased_daemon_request(
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 15.0,
        restore_wait: float | None = None,
    ) -> dict[str, Any]:
        del params, restore_wait
        events.append(f"{method}:{timeout:g}")
        if method == "ps":
            return {
                "daemon": {"running": True, "pid": 4101, "epoch": "old"},
                "connectors": [{"running": True}, {"running": False}],
            }
        if method == "shutdown":
            lifecycle["stopped"] = True
            return {"stopping": True, "pid": 4101}
        if method == "ping":
            # Card b9872e94: the pre-restart snapshot asks ping, so answer by
            # phase -- the old generation until shutdown, the fresh one after.
            if not lifecycle["stopped"]:
                return {
                    "running": True,
                    "pid": 4101,
                    "epoch": "old",
                    "version": "0.4.0.dev1",
                    "phase": "reconciled",
                }
            phase = next(ping_phases)
            return {
                "running": True,
                "pid": 4202,
                "epoch": "new",
                "version": "0.4.0.dev2",
                "phase": phase,
                "restorePending": phase == "restoring",
            }
        raise AssertionError(f"unexpected daemon request: {method}")

    monkeypatch.setattr(_cli_support, "_daemon_request", phased_daemon_request)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    restart = payload["restart"]
    assert restart["confirmed"] is True
    assert restart["reason"] == "restore settled after poll (phase reconciled)"
    assert restart["followUp"]["outcome"] == "recovered"
    assert len(sent) == 2, "first notice plus exactly one follow-up"
    assert "尚未确认完成 adapter 恢复" in sent[0]
    assert "已恢复" in sent[1]
    assert "reconciled" in sent[1]
    assert events.count("ping:2") == 3, "read once, polled twice, then settled"


def test_an_unsettled_restore_follows_up_once_at_the_derived_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """T3: phase never leaves restoring => one 「仍在启动:N」 follow-up when the
    fleet-derived budget runs out, confirmed stays false.

    The budget is injected small (5 rows, width 2, 0.2s start timeout =>
    ceil(5/2)=3 rounds x 0.2s x 1.5 = 0.9s); the duration assertion is what
    a formula mutation (rows not divided by width => 1.5s) must break."""

    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("HYPRIAL_SOCKET_PATH", raising=False)
    monkeypatch.delenv("HARNESS_CONFIG", raising=False)
    (tmp_path / "home").mkdir(mode=0o700)
    _bind_owner(tmp_path)
    sent = _record_owner_messages(monkeypatch)
    _seed_desired_rows(tmp_path, 5)

    def restoring_daemon_request(
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 15.0,
        restore_wait: float | None = None,
    ) -> dict[str, Any]:
        del params, restore_wait, timeout
        if method == "ping":
            return {
                "running": True,
                "pid": 4202,
                "epoch": "new",
                "phase": "restoring",
                "restorePending": True,
            }
        if method == "ps":
            return {"connectors": [{"running": True}, {"running": False}]}
        raise AssertionError(f"unexpected daemon request: {method}")

    monkeypatch.setattr(_cli_support, "_daemon_request", restoring_daemon_request)

    result: dict[str, Any] = {
        "restart": {"confirmed": False, "reason": "restore in progress"},
        "resolvedTag": "v0.4.0.dev2",
        "resolvedCommit": "d" * 40,
    }
    started = time.monotonic()
    _cli_owner_1._follow_up_restore_confirmation(
        result,
        poll_interval_seconds=0.05,
        admission_width=2,
        start_timeout_seconds=0.2,
    )
    elapsed = time.monotonic() - started

    # Derived budget: ceil(5/2)=3 rounds x 0.2s x 1.5 = 0.9s.  The window is
    # what the spec's mutation check hangs on: with rows not divided by
    # width the budget becomes 1.5s and the elapsed time leaves the window.
    assert 0.7 <= elapsed < 1.15, (
        f"poll ran {elapsed:.2f}s against a derived budget of 0.9s; the "
        "formula no longer matches ceil(rows/width) x start_timeout x margin"
    )
    restart = result["restart"]
    assert restart["confirmed"] is False
    assert restart["followUp"]["outcome"] == "still-starting"
    assert restart["followUp"]["pendingConnectors"] == 1
    assert len(sent) == 1, "exactly one follow-up, never a stream"
    assert "仍在启动:1 个 connector 未回" in sent[0]


def test_a_transient_ping_blip_does_not_abort_the_followup_poll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#332 F4② landed under this card's rebase: the transport's transient
    classes are no longer CliError.  A poll that died on the first blip
    would send no follow-up at all -- the exact silence this card closes --
    so one refused ping must read as \"not reconciled\" and the poll must
    keep its promise to the budget."""

    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("HYPRIAL_SOCKET_PATH", raising=False)
    monkeypatch.delenv("HARNESS_CONFIG", raising=False)
    (tmp_path / "home").mkdir(mode=0o700)
    _bind_owner(tmp_path)
    sent = _record_owner_messages(monkeypatch)
    _seed_desired_rows(tmp_path, 1)

    ping_calls = iter(["blip"] + ["restoring"] * 40)

    def blipping_daemon_request(
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 15.0,
        restore_wait: float | None = None,
    ) -> dict[str, Any]:
        del params, restore_wait, timeout
        if method == "ping":
            if next(ping_calls) == "blip":
                raise ipc_errors.DaemonUnavailableError("mid-restore blip")
            return {
                "running": True,
                "pid": 4202,
                "epoch": "new",
                "phase": "restoring",
                "restorePending": True,
            }
        if method == "ps":
            return {"connectors": [{"running": True}]}
        raise AssertionError(f"unexpected daemon request: {method}")

    monkeypatch.setattr(_cli_support, "_daemon_request", blipping_daemon_request)

    result: dict[str, Any] = {
        "restart": {"confirmed": False, "reason": "restore in progress"},
        "resolvedTag": "v0.4.0.dev2",
        "resolvedCommit": "d" * 40,
    }
    _cli_owner_1._follow_up_restore_confirmation(
        result, poll_interval_seconds=0.05, start_timeout_seconds=0.1
    )

    assert result["restart"]["followUp"]["outcome"] == "still-starting"
    assert len(sent) == 1, "the blip cost the owner its only follow-up"


def test_pre_upgrade_snapshot_reaches_a_daemon_whose_ps_is_storming(monkeypatch, tmp_path) -> None:
    """Card b9872e94: the pre-restart snapshot needs one number, the pid.

    Production 2026-09-05 03:17: autoupdate's precheck asked ``ps`` -- the
    projection that walks every actor -- and the 15 s IPC budget expired
    (card 259's storm, whose fix was in the very build being installed), so
    the upgrade self-locked.  The precheck now asks ``_daemon_probe`` (ping,
    with the ps fallback only for a daemon older than the ping contract).
    Here ``ps`` never answers and ``ping`` does: the snapshot must still
    carry the pid.
    """
    from hyprial.kernel import ipc_errors

    calls: list[str] = []

    def request(method: str, params=None, *, timeout: float = 15.0, **_kw):
        calls.append(method)
        if method == "ping":
            return {
                "running": True,
                "pid": 4101,
                "epoch": "old",
                "version": "0.4.4",
                "phase": "reconciled",
            }
        if method == "ps":
            raise ipc_errors.IpcTimeoutError("timed out after 15s waiting for daemon method 'ps'")
        raise AssertionError(f"unexpected daemon request: {method}")

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    assert _cli_get_services()._running_daemon_before_upgrade("0.4.4") == {
        "pid": 4101,
        "epoch": "old",
        "version": "0.4.4",
    }
    assert "ps" not in calls
