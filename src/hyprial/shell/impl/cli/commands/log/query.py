"""The ``hyprial query`` inbox/outbox reader command."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from hyprial.kernel import ipc_errors
import typer

from hyprial.shell.impl.cli.commands.common.root import app


@app.command("query")
def query_command(
    actor: str = typer.Argument(..., help="Local actor name or agent URI."),
    view: str = typer.Argument(..., help="inbox or outbox."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Read one local actor's inbox or outbox, changing nothing.

    ``inbox`` lists the messages and system notices still waiting for the
    actor, with their text; ``outbox`` lists what the actor sent that is still
    queued for delivery.  Read-only: nothing is fetched, acknowledged or
    drained, so the agent still receives exactly what it would have.
    """
    services = get_services()

    if view not in {"inbox", "outbox"}:
        raise services.CliError(ipc_errors.INVALID_ARGUMENT, "view must be inbox or outbox")

    def operation() -> Any:
        services = get_services()
        return services._daemon_request("message.query", {"from": actor, "view": view})

    services._execute(operation, json_output=json_output)
