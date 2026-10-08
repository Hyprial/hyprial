"""``hyprial daemon run/stop/status``."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from hyprial.kernel import ipc_errors
import os
import typer

from hyprial.shell.impl.cli.commands.common.daemon_stop import _stop_daemon_for_operator
from hyprial.shell.impl.cli.commands.common.support import JsonObject


daemon_app = typer.Typer(help="Run, stop, and inspect the Harness daemon.")


@daemon_app.command("status")
def daemon_status(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show exactly the daemon portion of ``hyprial ps``."""
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


@daemon_app.command("run")
def daemon_run(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON after shutdown."),
) -> None:
    """Run the product daemon in the foreground until SIGINT or SIGTERM."""
    services = get_services()

    def operation() -> JsonObject:
        from hyprial.identity import IdentityTransactionBusy
        services = get_services()
        services.require_initialized_hyprial_home()
        from hyprial.daemon import DaemonApplication
        from hyprial.daemon import application_lock_wait_timeout as _lock_wait_timeout
        from hyprial.daemon import (
            DaemonOwnershipBusy,
            DaemonStateOwnershipFence,
        )

        from hyprial.identity import IdentityTransactionLock

        state_dir = services._state_dir()
        try:
            transaction = IdentityTransactionLock.acquire_or_adopt(
                services._hyprial_home(), os.environ, timeout=_lock_wait_timeout()
            )
        except IdentityTransactionBusy as error:
            raise services.CliError(error.code, str(error)) from error
        with transaction:
            try:
                fence = DaemonStateOwnershipFence.acquire(
                    state_dir, timeout=_lock_wait_timeout()
                )
            except DaemonOwnershipBusy as error:
                raise services.CliError(error.code, str(error)) from error
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
) -> None:
    """Ask the isolated daemon owning this socket to stop gracefully."""
    services = get_services()

    services._execute(_stop_daemon_for_operator, json_output=json_output)
