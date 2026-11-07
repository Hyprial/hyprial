"""cli_upgrade_restart: notification scenarios."""

from __future__ import annotations
import json
from pathlib import Path
from typing import Any
import pytest
from typer.testing import CliRunner
from hyprial.kernel import ipc_errors

from .support import (
    FRESH_LAUNCH_PING,
    _REAL_LAUNCH_DAEMON_PROCESS,
    _fake_upgrade,
    _locked_constraints as _locked_constraints,
)
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.common.support as _cli_owner_1
import subprocess as _cli_subprocess
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_autoupdate_notification_is_confirmed_before_bus_cutting_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mutation: moving notification below shutdown fails immediately."""

    events = _fake_upgrade(monkeypatch, tmp_path)

    def notify(
        before: dict[str, Any],
        new_version: str,
        resolved_tag: str,
        resolved_commit: str,
    ) -> dict[str, Any]:
        assert not any(event.startswith("shutdown:") for event in events), (
            "restart cut the notification bus before delivery confirmation"
        )
        events.append("notify:confirmed")
        assert before == {
            "pid": 4101,
            "epoch": "old",
            "version": "0.4.0.dev1",
        }
        assert new_version == "0.4.0.dev2"
        assert resolved_tag == "v0.4.0.dev2"
        assert resolved_commit == "d" * 40
        return {"delivered": True, "deliveryConfirmed": True}

    result = _cli_get_services()._perform_upgrade(force=False, before_restart=notify)

    assert result["notification"]["deliveryConfirmed"] is True
    assert events == [
        "ping:15",
        "prime",
        "notify:confirmed",
        "shutdown:2",
        f"launch:{_cli_owner_1.DAEMON_START_HARD_CAP_SECONDS:g}",
        "ping:2",
    ]


def test_a_stub_that_invents_its_own_shape_cannot_pass_the_restart_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """F2's contract: stub returns must come from DaemonLaunchResult.

    The 2026-09-03 masking stub hand-wrote the nested ps shape the upgrade
    reader wrongly expected, and the suite stayed green while every real
    upgrade failed.  A self-invented shape must now fail loudly — never
    paper over the reader again.
    """

    def nested_dict_launch(*, ready_timeout: float) -> dict[str, Any]:
        del ready_timeout
        return {
            "ok": True,
            "started": True,
            "alreadyRunning": False,
            "daemon": {"running": True, "pid": 4202, "epoch": "new"},
        }

    _fake_upgrade(monkeypatch, tmp_path, launch=nested_dict_launch)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["code"] == "UPGRADE_RESTART_FAILED"
    assert "unexpected result shape" in payload["error"]


def test_upgrade_reads_the_real_launcher_answer_not_a_stub_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The production branch of the F2 contract.

    The red-state proof swapped the stub for a typed result, which fails an
    OLD reader on its ``isinstance(launched, dict)`` line — a different line
    from the one production used to hit (flat dict whose ``.get("daemon")``
    is None).  Covering only the stub branch would prove "a swapped stub
    goes red", not "the real return value reads".  So this test routes the
    REAL `_launch_daemon_process` through `_perform_upgrade`: only its ping
    answer and child process are faked, and the upgrade reader must get
    `running`/`pid` out of what the launcher actually returns.
    """

    class FakeDaemonProcess:
        pid = FRESH_LAUNCH_PING["pid"]

        def poll(self) -> int | None:
            return None

    lifecycle = {"stopped": False}

    def daemon_request(
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 15.0,
        restore_wait: float | None = None,
    ) -> dict[str, Any]:
        del params, restore_wait
        events.append(f"{method}:{timeout:g}")
        if method == "ps":
            # The pre-upgrade snapshot shape, carrying the connector list
            # ps really returns -- the restore-pending count reads it.
            return {
                "daemon": {"running": True, "pid": 4101, "epoch": "old"},
                "connectors": [{"running": True}, {"running": False}],
            }
        if method == "shutdown":
            lifecycle["stopped"] = True
            return {"stopping": True, "pid": 4101}
        if method == "ping":
            # By phase (card b9872e94: the pre-restart snapshot asks ping):
            # old generation until shutdown, fresh generation afterwards.
            if not lifecycle["stopped"]:
                return {
                    "running": True,
                    "pid": 4101,
                    "epoch": "old",
                    "version": "0.4.0.dev1",
                    "phase": "reconciled",
                }
            return dict(FRESH_LAUNCH_PING)
        raise AssertionError(f"unexpected daemon request: {method}")

    # The launch wait shares the probe seam now (one probe path for every
    # daemon generation): the first, optimistic call misses -- the old
    # generation was just stopped, so the launcher takes the fresh-start
    # branch exactly as in production -- and every call after the launch
    # answers with the launch-time shape (mid-restore, phase serving); the
    # post-settle read is a separate ping with its own answer below.
    # Three phases on one probe seam: before the stop the pre-restart snapshot
    # finds the old generation; right after the stop the launcher's optimistic
    # probe misses (fresh-start branch); every call after the launch answers
    # with the launch-time shape (mid-restore, phase serving).
    probe_state = {"launched": False}

    def probe_misses(timeout: float = 0.5) -> dict[str, Any]:
        del timeout
        if not lifecycle["stopped"]:
            return {
                "running": True,
                "pid": 4101,
                "epoch": "old",
                "version": "0.4.0.dev1",
                "phase": "reconciled",
            }
        if not probe_state["launched"]:
            probe_state["launched"] = True
            raise ipc_errors.DaemonUnavailableError("old generation stopped")
        return dict(FRESH_LAUNCH_PING)

    events = _fake_upgrade(monkeypatch, tmp_path)
    monkeypatch.setattr(_cli_get_services(), "_launch_daemon_process", _REAL_LAUNCH_DAEMON_PROCESS)
    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setattr(_cli_get_services(), "_daemon_probe", probe_misses)
    monkeypatch.setattr(_cli_get_services(), "_daemon_ready_for_process", lambda pid, **_kw: True)
    monkeypatch.setattr(
        _cli_subprocess, "Popen", lambda *args, **kwargs: FakeDaemonProcess()
    )

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["restart"]["restarted"] is True
    assert payload["restart"]["after"] == {
        "pid": 4202,
        "epoch": "new",
        "version": "0.4.0.dev2",
    }
    assert payload["restart"]["before"] == {
        "pid": 4101,
        "epoch": "old",
        "version": "0.4.0.dev1",
    }
    # The real launcher's ping is mid-restore (phase serving, restore not
    # settled): the restart is recorded unconfirmed with the pending count
    # off ps, the poll budget for a fleetless home is empty, and the
    # follow-up records still-starting -- all from the real launch answer.
    assert payload["restart"]["confirmed"] is False
    assert payload["restart"]["reason"] == "restore in progress, 1 connectors pending"
    assert payload["restart"]["followUp"]["outcome"] == "still-starting"
    assert events == [
        # No `ping:15` here: this test stubs `_daemon_probe` (the seam the
        # pre-restart snapshot and the launcher share), so the snapshot's
        # probe never reaches the recorded `_daemon_request`.
        "prime",
        "shutdown:2",
        # The launch wait's readiness probe goes through `_daemon_probe` now
        # (one probe path for every daemon generation) and is stubbed above,
        # so it no longer shows up as a raw `ping:0.5` request here.
        "ping:2",
        "ps:2",
        "ping:2",
        "ps:2",
    ]


def test_unconfirmed_notification_prevents_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events = _fake_upgrade(monkeypatch, tmp_path)

    def reject(*_args: Any) -> dict[str, Any]:
        raise _cli_get_services().CliError(
            "AUTOUPDATE_NOTIFICATION_UNDELIVERED", "fixture bus rejected delivery"
        )

    with pytest.raises(_cli_get_services().CliError) as caught:
        _cli_get_services()._perform_upgrade(force=False, before_restart=reject)

    assert caught.value.code == "UPGRADE_NOTIFICATION_FAILED"
    assert caught.value.data["upgradeCompleted"] is True
    assert caught.value.data["restart"]["attempted"] is False
    assert events == ["ping:15", "prime"]
