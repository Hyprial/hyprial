"""``hyprial down`` - stop the daemon and every managed runtime."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from hyprial.kernel import LIFECYCLE_IPC_MARGIN_SECONDS, LIFECYCLE_OPERATION_DEADLINE_SECONDS, LIFECYCLE_WAIT_MARGIN_SECONDS
from hyprial.kernel import ipc_errors
import typer

from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.common.support import JsonObject
@app.command()
def down(
    targets: list[str] | None = typer.Argument(
        None, help="Connector ID or kind and target."
    ),
    all_connectors: bool = typer.Option(
        False,
        "--all",
        help=(
            "Stop and permanently deregister every daemon-managed harness "
            "connector (from 'hyprial start --headless') and every Lark adapter. "
            "Does NOT touch interactive Claude sessions ('hyprial start claude "
            "--name ...' without --headless) -- those keep running."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Stop one connector, or stop and deregister every daemon-managed
    harness connector and Lark adapter with --all.

    This is permanent removal from desired state, not a pause: a stopped
    connector or adapter will not come back on daemon restart until it is
    started again (adapters need 'hyprial adapter start <name>' per adapter).

    --all does not reach interactive Claude sessions started via
    'hyprial start claude --name ...' (no --headless); those are tracked
    separately and are never stopped by this command. Use
    'hyprial ps' to see connectors, adapters, and interactive sessions as
    three distinct categories.
    """
    services = get_services()

    def operation() -> Any:
        services = get_services()
        values = targets or []
        if all_connectors and values:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--all cannot be combined with a connector target",
            )
        if not all_connectors and len(values) not in {1, 2}:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "down requires a connector ID, kind and target, or --all",
            )
        params: JsonObject = {"all": all_connectors}
        if len(values) == 1:
            params["target"] = values[0]
        elif len(values) == 2:
            params.update({"provider": values[0], "target": values[1]})
        # Outlast the daemon-side lifecycle wait (deadline + wait margin) so a
        # stuck operation returns its coded failure here instead of an
        # IPC_TIMEOUT (card 104164aa (c)).
        return services._daemon_request(
            "down",
            params,
            timeout=(
                LIFECYCLE_OPERATION_DEADLINE_SECONDS
                + LIFECYCLE_WAIT_MARGIN_SECONDS
                + LIFECYCLE_IPC_MARGIN_SECONDS
            ),
        )

    services._execute(operation, json_output=json_output)
