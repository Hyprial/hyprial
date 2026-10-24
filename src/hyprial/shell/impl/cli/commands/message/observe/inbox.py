"""The ``hyprial message inbox`` read-only inbox reader command."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
import typer

from hyprial.shell.impl.cli.commands.common.support import _local_operator_identity
from hyprial.shell.impl.cli.commands.message import message_app


@message_app.command("inbox")
def message_inbox(
    actor: str | None = typer.Argument(
        None,
        help="Local actor name or agent URI; defaults to the local owner.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Read one local actor's inbox, changing nothing.

    Lists the messages and system notices still waiting for the actor, with
    their text.  Read-only: nothing is fetched, acknowledged or drained, so the
    agent still receives exactly what it would have.  (``message.pending.list``
    would drain the actor's system notices, which is why this reads the
    non-draining ``message.query`` view instead.)
    """
    services = get_services()

    def operation() -> Any:
        source = actor if actor is not None else _local_operator_identity()
        return services._daemon_request("message.query", {"from": source, "view": "inbox"})

    services._execute(operation, json_output=json_output)
