"""``hyprial daemon run/stop/status``."""

from __future__ import annotations

from collections.abc import Callable
import time
from typing import Any
from hyprial.shell.impl.cli.commands.common.services import get_services

from hyprial.kernel import ipc_errors
import os
import typer

from hyprial.shell.impl.cli.commands.common.daemon_stop import _stop_daemon_for_operator
from hyprial.shell.impl.cli.commands.common.support import (
    DAEMON_START_IDLE_BUDGET_SECONDS,
    JsonObject,
)


daemon_app = typer.Typer(help="Run, stop, start, and inspect the Harness daemon.")


@daemon_app.command("status")
def daemon_status(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show exactly the daemon portion of ``hyprial daemon status``."""
    services = get_services()

    def operation() -> JsonObject:
        try:
            result = services._daemon_request("ps", restore_wait=0.0)
        except ipc_errors.DaemonRestoringError:
            probe = services._daemon_probe(timeout=2.0)
            daemon = {
                key: probe[key]
                for key in (
                    "running",
                    "pid",
                    "epoch",
                    "nodeId",
                    "owner",
                    "socket",
                    "phase",
                )
                if key in probe
            }
            restoring = True
            restore_pending = probe.get("restorePending", True)
        except ipc_errors.DaemonUnavailableError:
            daemon = {"running": False}
            restoring = None
            restore_pending = None
        else:
            if not isinstance(result, dict) or not isinstance(
                result.get("daemon"), dict
            ):
                raise services.CliError(
                    "INVALID_RESPONSE", "daemon ps result must contain daemon"
                )
            daemon = result["daemon"]
            restoring = result.get("restoring")
            restore_pending = result.get("restorePending")
        return {
            "ok": True,
            **({"restoring": restoring} if isinstance(restoring, bool) else {}),
            **(
                {"restorePending": restore_pending}
                if isinstance(restore_pending, bool)
                else {}
            ),
            "daemon": daemon,
        }

    services._execute(operation, json_output=json_output)


@daemon_app.command("start")
def daemon_start(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Start the daemon in the background and return once it is serving.

    This is the same launch primitive ``init`` uses.  Use ``daemon run`` when a
    supervisor or process manager owns the foreground daemon process.
    """
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        launched = services._launch_daemon_process(
            ready_timeout=DAEMON_START_IDLE_BUDGET_SECONDS
        )
        return launched.to_payload()

    services._execute(operation, json_output=json_output)


def _acquire_daemon_fence(
    services: Any,
    state_dir: Any,
    *,
    timeout: float,
    sleep: Callable[[float], None] | None = None,
    log: Callable[[dict[str, object]], None] | None = None,
    json_output: bool = False,
):
    """Take the daemon lock, waiting while a healthy daemon holds it.

    The bounded first attempt keeps the shutdown-overlap behaviour the daemon
    start path already relies on. After that, Allen's ruling (2026-10-29):
    block while a serving daemon answers and take over when it exits, instead of
    exiting -- exiting is what made launchd's KeepAlive restart the same losing
    process 2,900 times. The wait is observable: every poll reports
    `waiting-for-lock` with the holder pid.
    """

    from hyprial.daemon import (
        DaemonOwnershipBusy,
        DaemonStateOwnershipFence,
        wait_for_state_ownership,
    )

    try:
        return DaemonStateOwnershipFence.acquire(state_dir, timeout=timeout)
    except DaemonOwnershipBusy:
        pass

    def _holder() -> int | None:
        try:
            value = services._daemon_probe(timeout=0.5)
        except Exception:  # noqa: BLE001 - no answer means "not healthy"
            return None
        daemon = value.get("daemon") if isinstance(value, dict) else None
        source = daemon if isinstance(daemon, dict) else value
        pid = source.get("pid") if isinstance(source, dict) else None
        return pid if type(pid) is int and pid > 0 else None

    report = log or (
        lambda event: _report_lock_wait(event, json_output=json_output)
    )
    try:
        return wait_for_state_ownership(
            state_dir,
            acquire=lambda path: DaemonStateOwnershipFence.acquire(path, timeout=0.0),
            probe_holder=_holder,
            sleep=sleep or time.sleep,
            log=report,
        )
    except DaemonOwnershipBusy as error:
        raise services.CliError(error.code, str(error)) from error


def _report_lock_wait(event: dict[str, object], *, json_output: bool) -> None:
    """Report a wait through the CLI output channel, on stderr only.

    Production runs the supervised daemon as ``daemon run --json``, where stdout
    is reserved for the single result object; ``progress`` writes one NDJSON
    event per line under ``--json`` and the human sentence otherwise, and keeps
    every write inside the output module (a direct ``sys.stderr.write`` here is
    an output-discipline violation).
    """

    if event.get("event") != "waiting-for-lock":
        return
    from hyprial.shell.impl.cli.output import progress

    progress(
        "daemon.lock.waiting",
        f"daemon.lock busy: waiting for lock (holder_pid={event.get('holder_pid')})",
        json_output=json_output,
        holderPid=event.get("holder_pid"),
    )


@daemon_app.command("run")
def daemon_run(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON after shutdown."),
) -> None:
    """Run the daemon in the foreground until SIGINT or SIGTERM (debug/process manager)."""
    services = get_services()

    def operation() -> JsonObject:
        from hyprial.identity import IdentityTransactionBusy
        services = get_services()
        services.require_initialized_hyprial_home()
        from hyprial.daemon import DaemonApplication
        from hyprial.daemon import application_lock_wait_timeout as _lock_wait_timeout
        from hyprial.identity import IdentityTransactionLock

        state_dir = services._state_dir()
        try:
            transaction = IdentityTransactionLock.acquire_or_adopt(
                services._hyprial_home(), os.environ, timeout=_lock_wait_timeout()
            )
        except IdentityTransactionBusy as error:
            raise services.CliError(error.code, str(error)) from error
        with transaction:
            fence = _acquire_daemon_fence(
                services,
                state_dir,
                timeout=_lock_wait_timeout(),
                json_output=json_output,
            )
            with fence:
                from hyprial.shell.impl.daemon_wiring import daemon_dependencies, usage_factory

                runtime = DaemonApplication.from_environment(
                    state_dir=state_dir, socket_path=services._socket_path(),
                    usage_factory=usage_factory, **daemon_dependencies(),
                )
                # This interpreter exists to be the daemon, so the daemon is
                # allowed to end it if shutdown leaves a thread that will not join.
                runtime.owns_process_exit()
                runtime.run(
                    ownership_stream=fence.detach(),
                    identity_transaction_stream=transaction.detach(),
                )
        return {"ok": True, "stopped": True}

    services._execute(operation, json_output=json_output, allow_missing_home=True)


@daemon_app.command("stop")
def daemon_stop(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    expected_epoch: str | None = typer.Option(
        None, "--expected-epoch", help="Refuse to stop a different daemon generation.",
    ),
) -> None:
    """Ask the isolated daemon owning this socket to stop gracefully."""
    services = get_services()

    action = (
        _stop_daemon_for_operator if expected_epoch is None
        else lambda: _stop_daemon_for_operator(expected_epoch=expected_epoch)
    )
    services._execute(action, json_output=json_output)
