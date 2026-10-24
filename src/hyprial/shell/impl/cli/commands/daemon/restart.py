"""``hyprial daemon restart`` and pending-restart receipt handling."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import confirm, render_generic

from hyprial.shell.impl.cli.commands.common.services import get_services

from hyprial.kernel import ipc_errors
import sys
import typer

from hyprial.shell.impl.cli.commands.common.pending_restart import (
    _clear_pending_restart,
    _read_pending_restart,
    restart_daemon,
)
from hyprial.shell.impl.cli.commands.daemon.commands import daemon_app
from hyprial.shell.impl.cli.commands.common.support import JsonObject, _announce_setup_guidance, _append_warning, _squire_setup_warning, _utc_now
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
    """Read the same full daemon status as ``hyprial daemon status`` for a restart gate."""
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


@daemon_app.command("restart")
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
        # Same contract as the upgrade path's stop (see `restart_daemon` for
        # the 2026-08-30/31 incidents): a failed stop is recorded, never a
        # reason to skip the launch. With no running daemon, neither a stop
        # nor a fresh-pid verdict is attempted -- the old `status is not
        # None` gate, kept verbatim.
        launched = restart_daemon(
            result,
            before_pid=before.get("pid") if status is not None else None,
            survivor_idempotency_scope="hyprial-restart",
            stop=status is not None,
        )
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
                via="hyprial daemon restart",
                after=after,
            )
            result["pendingRestart"] = pending
            _clear_pending_restart()
        _append_warning(result, _squire_setup_warning())
        if not json_output:
            _announce_setup_guidance(result)
        return result

    services._execute(operation, json_output=json_output)
