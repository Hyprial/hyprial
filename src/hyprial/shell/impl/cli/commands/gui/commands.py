"""``hyprial gui``: the bundled GUI as a built-in command."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from pathlib import Path
from hyprial.kernel import ipc_errors
import typer

from hyprial.shell.impl.cli.commands.common.root import app


@app.command("gui")
def gui_command(
    app_or_action: str = typer.Argument("start", help="start (default), status, stop, or upgrade the GUI."),
    check: bool = typer.Option(False, "--check", help="Upgrade: report only."),
    force: bool = typer.Option(False, "--force", help="Upgrade: replace the unpacked GUI even when current."),
    yes: bool = typer.Option(False, "--yes", help="Accepted for compatibility; the bundled GUI needs no confirmation."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Start the bundled GUI; upgrade unpacks the component paired with this Hyprial."""
    services = get_services()

    def operation() -> Any:
        services = get_services()
        from hyprial.shell.impl.gui.apps import perform, resolve_invocation, upgrade

        verb, _ = resolve_invocation(app_or_action)
        if verb != "upgrade" and (check or force):
            raise services.CliError(ipc_errors.INVALID_ARGUMENT, "--check and --force require hyprial gui upgrade")
        if verb in ("stop", "status") and yes:
            raise services.CliError(ipc_errors.INVALID_ARGUMENT, "--yes requires GUI start or upgrade")
        hyprial_home = services._hyprial_home()
        if verb == "upgrade":
            if check and (force or yes):
                raise services.CliError(ipc_errors.INVALID_ARGUMENT, "--check cannot be combined with --force or --yes")
            return upgrade(hyprial_home, check_only=check, force=force)
        return perform(hyprial_home, verb)

    services._execute(operation, json_output=json_output)


def gui_status(hyprial_home: Path, name: str) -> dict[str, Any]:
    from hyprial.shell.impl.gui.runtime import gui_status as operation

    return operation(hyprial_home, name)


def start_gui_background(hyprial_home: Path, name: str) -> dict[str, Any]:
    from hyprial.shell.impl.gui.runtime import start_gui_background as operation

    return operation(hyprial_home, name)


def stop_gui(hyprial_home: Path, name: str) -> dict[str, Any]:
    from hyprial.shell.impl.gui.runtime import stop_gui as operation

    return operation(hyprial_home, name)
