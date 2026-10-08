"""Pending-restart state and install/restart process helpers."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from typing import Any

from hyprial.kernel import DaemonLaunchResult, ipc_errors

from hyprial.shell.impl.cli.commands.common.daemon_stop import (
    _observe_restart_process,
    old_pid_from,
)
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import (
    DAEMON_START_IDLE_BUDGET_SECONDS,
    JsonObject,
)
from hyprial.shell.impl.cli.commands.socialware.commands import _require_installed_commit

PENDING_RESTART_FILE = "autoupdate-pending-restart.json"


def _pending_restart_path() -> Path:
    services = get_services()
    return services._state_dir() / PENDING_RESTART_FILE


def _read_pending_restart() -> JsonObject | None:
    try:
        value = json.loads(_pending_restart_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_pending_restart(record: JsonObject) -> None:
    path = _pending_restart_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _clear_pending_restart() -> None:
    try:
        _pending_restart_path().unlink()
    except FileNotFoundError:
        pass



def _install_locked_requirement(
    *,
    url: str,
    requirement: str,
    resolution: Any,
    resolved: JsonObject,
    resolved_suffix: str,
) -> tuple[subprocess.CompletedProcess[str], Any]:
    """Install one resolved commit using only constraints exported from its lock."""
    services = get_services()

    from hyprial.daemon import updates

    with tempfile.TemporaryDirectory(prefix="hyprial-upgrade-") as raw:
        constraints = Path(raw) / "constraints.txt"
        try:
            updates.export_locked_constraints(url, resolution.commit, constraints)
        except updates.UpdateProbeError as error:
            raise services.CliError("UPGRADE_CHECK_FAILED", str(error), resolved) from error
        try:
            completed = subprocess.run(
                [
                    "uv",
                    "tool",
                    "install",
                    "--force",
                    "--compile-bytecode",
                    # Keep this process's import paths valid after uv replaces
                    # the tool environment. Changing Python is a separate action.
                    "--python",
                    sys.executable,
                    "--constraints",
                    str(constraints),
                    requirement,
                ],
                text=True,
                capture_output=True,
                check=False,
                env=updates.git_env(),
                timeout=updates.UV_INSTALL_TIMEOUT,
            )
        except subprocess.TimeoutExpired as error:
            raise services.CliError(
                "UPGRADE_FAILED",
                f"uv tool install timed out after {updates.UV_INSTALL_TIMEOUT:g}s; "
                f"{resolved_suffix}",
                resolved,
            ) from error
        except OSError as error:
            raise services.CliError(
                "UPGRADE_FAILED",
                f"cannot run uv: {error}; {resolved_suffix}",
                resolved,
            ) from error
        if completed.returncode != 0:
            detail = (
                completed.stderr.strip()
                or completed.stdout.strip()
                or "uv tool install failed"
            )
            raise services.CliError(
                "UPGRADE_FAILED", f"{detail}; {resolved_suffix}", resolved
            )
        installed = updates.read_installation()
        _require_installed_commit(installed, resolution, resolved, resolved_suffix)
        try:
            updates.write_lock_receipt(
                services._hyprial_home(),
                source=url,
                commit=resolution.commit,
                constraints=constraints,
            )
        except OSError as error:
            raise services.CliError(
                "UPGRADE_REPORT_FAILED",
                f"installed the resolved commit but could not save its lock receipt: {error}",
                resolved,
            ) from error
    return completed, installed

def _restart_daemon_onto_install(
    result: JsonObject,
    *,
    before: JsonObject,
    installed_version: str,
    resolution: Any,
    resolved: JsonObject,
    confirmation_via: str | None = None,
) -> JsonObject:
    """Restart the running daemon onto the version already installed.

    Shared by the upgrade path and ``hyprial autoupdate restart`` (the
    person-confirmed restart of an upgrade that was installed without one).
    ``resolution`` needs only ``tag`` and ``commit``.
    """
    from hyprial.daemon import latest_start_failure, restart_failure_detail, start_failure_line
    services = get_services()

    restart_result: JsonObject = {
        "attempted": True,
        "restarted": False,
        "reason": "daemon restart did not complete",
        "before": before,
    }
    restart_started_at: datetime | None = None
    try:
        try:
            services._stop_daemon_gracefully()
        except (services.CliError, ipc_errors.TransientDaemonError) as stop_error:
            # ⚠️ A stop that times out says we stopped watching. It does not
            # say the daemon will not exit -- it has its own guarantee, and on
            # 2026-08-31 the outgoing process did exit, seconds after this
            # branch had already abandoned the upgrade. Nothing launched a
            # replacement, and production was down 56 minutes.
            #
            # 🔑 The launcher below usually adjudicates: the replacement takes
            # `daemon.lock`, and fails loudly if the old daemon still holds it.
            # Giving up here traded a *possible* failure for a *certain*
            # absence.
            #
            # ⛔ But that adjudication is on the **lock**, and there is one
            # shape where the lock is free while the old process is not gone:
            # 2026-08-30, teardown finished, the lock came back, `daemon.json`
            # moved -- and the process lived on for nine hours holding 106
            # descendants that blocked the replacement's bootstrap. In that
            # shape the replacement acquires the lock happily and nothing about
            # the launch looks wrong. So the launcher cannot be relied on here;
            # that is what DAEMON_STOP_SURVIVOR exists to say, and why it does
            # not share this path's silence.
            restart_result["stopWarning"] = str(stop_error)
            if stop_error.code == "DAEMON_STOP_SURVIVOR":
                # Still launch -- refusing would trade a possible failure for a
                # certain absence again, and the claim that the launch is futile
                # is not one anyone has verified. What changes is that a person
                # hears about it in minutes rather than in nine hours, which was
                # the actual cost that day.
                restart_result["survivingOldProcess"] = True
                survivor = services._describe_survivor(old_pid_from(stop_error))
                marker_path: Path | None
                try:
                    # ⛔ No `start_failure` argument, on purpose: a survivor is
                    # not a start failure, so the question does not apply. The
                    # key is left out; passing `None` would assert "we looked
                    # for this launch's daemon.start.failed and found none", a
                    # search this branch never runs. The omission is the honest
                    # record, and it keeps `null` meaning only "looked and there
                    # was none" on the restart-failure path below.
                    marker_path = services.write_failure_marker(
                        state_dir=services._state_dir(),
                        host=socket.gethostname(),
                        summary="旧 daemon 拆解完成但进程没有退出",
                        detail=f"{stop_error}\n{survivor}",
                    )
                except OSError:
                    marker_path = None
                survivor_alert = services.notify_upgrade_failure(
                    hyprial_home=services._hyprial_home(),
                    state_dir=services._state_dir(),
                    host=socket.gethostname(),
                    # ⭐ Deliberately not the same sentence as a stop timeout.
                    # The two ask for different actions: a timeout means "wait,
                    # it is probably fine"; this means "go find the survivor, it
                    # will not leave on its own".
                    summary="⚠️ 旧 daemon 拆解完成但【进程没有退出】—— 新 daemon 已启动",
                    detail=f"{stop_error}\n{survivor}",
                    idempotency_key=(
                        f"daemon-stop-survivor:{resolution.commit}:{stop_error.data}"
                    ),
                )
                restart_result["survivorAlert"] = survivor_alert.to_json()
                if marker_path is not None:
                    services.record_alert_outcome(marker_path, survivor_alert)
        # The restart wait covers only the serving boundary, so it uses init's
        # progress-idle budget and hard cap -- not a fleet-scaled one.  The 2026-08-31
        # measurement that justified 90s here (a six-adapter machine needing
        # 161s) was adapter restore time, and restore is no longer inside this
        # wait: it runs on the daemon's own thread and reports through ping's
        # phase.  A timeout therefore means the new daemon never came up, which
        # `_launch_daemon_process` raises and the restart-failed path below
        # reports -- the "still starting" third verdict this replaces existed
        # only because restore used to sit inside the readiness budget.
        # Parent-side wall clock is captured immediately before the launch
        # primitive. Every event from the child generation must be at or after
        # this lower bound; prior daemon generations are therefore ineligible
        # for the failure diagnosis even when they are the last matching line.
        now = datetime.now(UTC)
        # DaemonLogger serializes milliseconds. Floor the parent boundary to
        # that same precision so an event emitted later in this millisecond is
        # not made to look earlier merely by JSON timestamp truncation.
        restart_started_at = now.replace(microsecond=now.microsecond // 1000 * 1000)
        launched = services._launch_daemon_process(
            ready_timeout=DAEMON_START_IDLE_BUDGET_SECONDS
        )
        # F2 (2026-09-04): this used to dig for the nested ps shape
        # ``launched["daemon"]`` against the flat launch answer, so every
        # upgrade ended in UPGRADE_RESTART_FAILED with a false alert, and a
        # hand-written stub returning the nested shape kept the tests green.
        # The launch answer is the typed ``DaemonLaunchResult`` now; anything
        # that is not the type is a contract break and takes the loud path
        # below, never a silent misread.
        if not isinstance(launched, DaemonLaunchResult):
            raise services.CliError(
                "INVALID_RESPONSE",
                "daemon launch returned an unexpected result shape",
            )
        if launched.running is not True:
            raise services.CliError(
                "INVALID_RESPONSE", "restarted daemon did not report running"
            )
        after_pid = launched.pid
        if not isinstance(after_pid, int) or after_pid <= 0:
            raise services.CliError(
                "INVALID_RESPONSE", "restarted daemon did not report a valid pid"
            )
        if after_pid == before["pid"]:
            raise services.CliError(
                "INVALID_RESPONSE", "daemon restart did not yield a fresh pid"
            )
    except (services.CliError, ipc_errors.TransientDaemonError, OSError) as error:
        restart_result["reason"] = f"daemon restart failed: {error}"
        failure: JsonObject = {
            "upgradeCompleted": True,
            "upgraded": True,
            **resolved,
            "restart": restart_result,
        }
        if result.get("notification") is not None:
            failure["notification"] = result["notification"]
        # ⭐ Write the fact down, *then* try to tell someone -- in that order,
        # never the reverse. The send can fail with nobody left to notice: there
        # is no daemon at this point, and the network is one of the things that
        # may be broken. On 2026-08-31 this exact failure sat in the autoupdate
        # status for 56 minutes; what was missing was not the record, it was the
        # notice. Both halves, and this order.
        summary = (
            f"升级到 {resolution.tag} ({installed_version}) 已完成,但 daemon 重启失败"
        )
        start_failure = (
            latest_start_failure(
                services._state_dir() / "logs" / "daemon.jsonl",
                started_at=restart_started_at,
            )
            if restart_started_at is not None
            else None
        )
        host = socket.gethostname()
        # ⭐ The marker is what an unattended failure leaves behind, and it is
        # read by someone who does not already know to open daemon.jsonl. So it
        # gets the same cause line the alert gets, rendered by the same
        # function (⛔ never a second copy of the format).
        #
        # ⛔ The process state stays out on purpose: it is observed *below*, and
        # the write must stay ahead of that observation. Adding it here would
        # mean moving this write after `_observe_restart_process`, which is the
        # ordering this block exists to preserve.
        marker_detail = (
            f"daemon restart failed: {error}\n{start_failure_line(start_failure)}"
        )
        marker: Path | None
        try:
            marker = services.write_failure_marker(
                state_dir=services._state_dir(),
                host=host,
                summary=summary,
                detail=marker_detail,
                start_failure=start_failure,
            )
        except OSError as marker_error:
            # Said out loud rather than swallowed: "the marker is missing" and
            # "there was nothing to mark" must not look the same afterwards.
            marker = None
            failure["failureMarker"] = {
                "written": False,
                "reason": str(marker_error),
            }
        else:
            failure["failureMarker"] = {"written": True, "path": str(marker)}
        # The durable failure fact is now on disk. Re-observe this exact child
        # immediately before sending so the owner sees its current state, not
        # the state at the end of the progress-based readiness wait.
        process_observation = _observe_restart_process(error)
        detail = restart_failure_detail(
            error=str(error),
            start_failure=start_failure,
            process=process_observation,
        )
        alert = services.notify_upgrade_failure(
            hyprial_home=services._hyprial_home(),
            state_dir=services._state_dir(),
            host=host,
            summary=summary,
            detail=detail,
            idempotency_key=(
                f"upgrade-restart-failed:{resolution.commit}:{installed_version}"
            ),
        )
        failure["alert"] = alert.to_json()
        if marker is not None:
            services.record_alert_outcome(marker, alert)
        raise services.CliError(
            "UPGRADE_RESTART_FAILED",
            f"upgrade completed at {resolution.tag} ({installed_version}), "
            f"but daemon restart failed: {error}",
            failure,
        ) from error

    restart_result.update(
        {
            "restarted": True,
            "reason": "resolved tag changed; running daemon restarted",
            "after": {"pid": after_pid, "version": installed_version},
        }
    )
    # 3c116ad2 (P1): the launch answer proves only the serving boundary.
    # Restore runs on the daemon's own thread and reports through ping's
    # phase, so confirmation reads that phase -- once, without waiting here:
    # ``reconciled`` is the daemon's own "restore settled" verdict, and
    # anything else (or no answer) leaves the restart honestly unconfirmed
    # for the reporter to act on.  The serving wait above stays as small as
    # it is -- restore time does not go back into the launch budget.
    from hyprial.shell.impl.cli.commands.upgrade.commands import (
        _pending_connector_count,
        _read_restore_phase,
    )

    phase = _read_restore_phase()
    if phase == "reconciled":
        restart_result["confirmed"] = True
    else:
        pending = _pending_connector_count()
        restart_result["confirmed"] = False
        restart_result["reason"] = (
            f"restore in progress, {pending} connectors pending"
            if pending is not None
            else "restore in progress"
        )
    result.update({"restartRequired": False, "restart": restart_result})
    if confirmation_via is not None:
        from hyprial.shell.impl.cli.commands.daemon.restart import (
            _record_autoupdate_restart_confirmation,
        )

        _record_autoupdate_restart_confirmation(
            via=confirmation_via,
            after={
                "running": True,
                "pid": after_pid,
                "epoch": launched.epoch,
                "version": installed_version,
            },
        )
    return result
