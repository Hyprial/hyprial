"""``hyprial restart`` and pending-restart receipt handling."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import confirm, render_generic

from hyprial.shell.impl.cli.commands.common.services import get_services

from hyprial.kernel import DaemonLaunchResult
from pathlib import Path
from hyprial.kernel import ipc_errors
import socket
import sys
import typer

from hyprial.shell.impl.cli.commands.common.daemon_stop import old_pid_from
from hyprial.shell.impl.cli.commands.common.pending_restart import (
    _clear_pending_restart,
    _read_pending_restart,
)
from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.common.support import DAEMON_START_IDLE_BUDGET_SECONDS, JsonObject, _announce_setup_guidance, _append_warning, _squire_setup_warning, _utc_now
def _daemon_restart_identity(status: JsonObject) -> JsonObject:
    """Project the daemon identity fields used by restart reports."""
    services = get_services()

    daemon = status.get("daemon")
    source = daemon if isinstance(daemon, dict) else status
    pid = source.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise services.CliError("INVALID_RESPONSE", "running daemon did not report a valid pid")
    epoch = source.get("epoch")
    version = source.get("version")
    return {
        "running": True,
        "pid": pid,
        "epoch": epoch if isinstance(epoch, str) else None,
        "version": version if isinstance(version, str) else None,
    }


def _restart_status() -> JsonObject | None:
    """Read the same full daemon status as ``hyprial ps`` for a restart gate."""
    services = get_services()

    try:
        result = services._daemon_request("ps", restore_wait=0.0)
    except ipc_errors.DaemonUnavailableError:
        return None
    if not isinstance(result, dict):
        raise services.CliError("INVALID_RESPONSE", "daemon ps result must be an object")
    daemon = result.get("daemon")
    if not isinstance(daemon, dict) or daemon.get("running") is not True:
        return None
    _daemon_restart_identity(result)
    return result


def _interrupted_turns(status: JsonObject) -> list[JsonObject]:
    """List managed worker turns that a daemon restart would interrupt.

    ``connectors[].inFlight`` is the supervisor projection of non-terminal
    worker delivery records.  ``agents[]`` from the same ps response supplies
    the canonical actor URI; message bodies and identifiers are deliberately
    excluded.
    """

    agents = status.get("agents")
    actor_by_name: dict[str, str] = {}
    if isinstance(agents, list):
        for row in agents:
            if not isinstance(row, dict):
                continue
            name = row.get("name")
            actor = row.get("actor")
            if isinstance(name, str) and isinstance(actor, str):
                actor_by_name.setdefault(name, actor)

    interrupted: list[JsonObject] = []
    connectors = status.get("connectors")
    if not isinstance(connectors, list):
        return interrupted
    for row in connectors:
        if not isinstance(row, dict):
            continue
        count = row.get("inFlight")
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            continue
        name = row.get("name")
        harness = row.get("runtime")
        if not isinstance(name, str) or not isinstance(harness, str):
            continue
        interrupted.append(
            {
                "actor": actor_by_name.get(name, name),
                "harness": harness,
                "inFlight": count,
            }
        )
    return interrupted


def _record_autoupdate_restart_confirmation(
    *,
    via: str,
    after: JsonObject,
) -> JsonObject:
    """Make the last-run record agree with a successfully started generation."""
    services = get_services()

    from hyprial.daemon import read_last_run, write_last_run

    record = read_last_run(services._state_dir())
    if not isinstance(record, dict):
        record = {"ok": True}
    confirmation: JsonObject = {
        "restartedAt": _utc_now(),
        "newPid": after.get("pid"),
        "newEpoch": after.get("epoch"),
        "via": via,
    }
    restart = record.get("restart")
    if not isinstance(restart, dict):
        restart = {}
        record["restart"] = restart
    restart.update(
        {
            "attempted": True,
            "restarted": True,
            "awaitingConfirmation": False,
            "reason": f"restart confirmed by {via}",
            "after": {
                key: after.get(key) for key in ("pid", "epoch", "version")
            },
        }
    )
    record["restartRequired"] = False
    record["restartConfirmation"] = confirmation
    write_last_run(services._state_dir(), record)
    return confirmation


def _record_autoupdate_restart_not_required(*, via: str, reason: str) -> None:
    """Resolve a pending marker when no old daemon generation remains."""
    services = get_services()

    from hyprial.daemon import read_last_run, write_last_run

    record = read_last_run(services._state_dir())
    if not isinstance(record, dict):
        record = {"ok": True}
    restart = record.get("restart")
    if not isinstance(restart, dict):
        restart = {}
        record["restart"] = restart
    restart.update(
        {
            "attempted": False,
            "restarted": False,
            "awaitingConfirmation": False,
            "reason": reason,
        }
    )
    record["restartRequired"] = False
    record["restartResolution"] = {
        "resolvedAt": _utc_now(),
        "via": via,
        "outcome": "daemon-not-running",
    }
    write_last_run(services._state_dir(), record)


def _restart_is_interactive() -> bool:
    return sys.stdin.isatty()


@app.command("restart")
def restart_command(
    yes: bool = typer.Option(
        False, "--yes", help="Restart even when active worker turns will be interrupted."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Safely stop and start the daemon, reporting interrupted worker turns."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        started_at = services.time.monotonic()
        status = _restart_status()
        before = (
            {"running": False}
            if status is None
            else _daemon_restart_identity(status)
        )
        interrupted = [] if status is None else _interrupted_turns(status)
        if interrupted and not yes:
            refusal = {
                "before": before,
                "interruptedTurns": interrupted,
            }
            if json_output or not services._restart_is_interactive():
                raise services.CliError(
                    "RESTART_WOULD_INTERRUPT",
                    "restart would interrupt active worker turns; rerun with --yes",
                    refusal,
                )
            if not confirm("Restart and interrupt these active turns?", preview=render_generic(refusal)):
                raise services.CliError("RESTART_DECLINED", "restart declined", refusal)

        pending = _read_pending_restart()
        result: JsonObject = {
            "ok": True,
            "restarted": status is not None,
            "started": False,
            "before": before,
            "interruptedTurns": interrupted,
        }
        if status is not None:
            try:
                services._stop_daemon_gracefully()
            except (services.CliError, ipc_errors.TransientDaemonError) as stop_error:
                # Same contract as the upgrade path's stop (see
                # `_restart_daemon_onto_install` for the 2026-08-30/31 incidents):
                # a failed stop is recorded, never a reason to skip the launch.
                result["stopWarning"] = str(stop_error)
                if stop_error.code == "DAEMON_STOP_SURVIVOR":
                    # Still launch -- refusing would trade a possible failure for a
                    # certain absence again, and the claim that the launch is futile
                    # is not one anyone has verified. What changes is that a person
                    # hears about it in minutes rather than in nine hours, which was
                    # the actual cost that day.
                    result["survivingOldProcess"] = True
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
                        idempotency_key=f"daemon-stop-survivor:hyprial-restart:{stop_error.data}",
                    )
                    result["survivorAlert"] = survivor_alert.to_json()
                    if marker_path is not None:
                        services.record_alert_outcome(marker_path, survivor_alert)

        launched = services._launch_daemon_process(
            ready_timeout=DAEMON_START_IDLE_BUDGET_SECONDS
        )
        if not isinstance(launched, DaemonLaunchResult):
            raise services.CliError(
                "INVALID_RESPONSE", "daemon launch returned an unexpected result shape"
            )
        if launched.running is not True:
            raise services.CliError("INVALID_RESPONSE", "started daemon did not report running")
        after = _daemon_restart_identity(dict(launched.status))
        if status is not None and after["pid"] == before["pid"]:
            raise services.CliError(
                "INVALID_RESPONSE", "daemon restart did not yield a fresh pid"
            )
        result["started"] = True
        result["after"] = after
        result["elapsedSeconds"] = round(services.time.monotonic() - started_at, 3)
        if pending is not None:
            result["restartConfirmation"] = _record_autoupdate_restart_confirmation(
                via="hyprial restart",
                after=after,
            )
            result["pendingRestart"] = pending
            _clear_pending_restart()
        _append_warning(result, _squire_setup_warning())
        if not json_output:
            _announce_setup_guidance(result)
        return result

    services._execute(operation, json_output=json_output)
