"""The ``hyprial message ack`` operator messaging command."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

import typer

from hyprial.shell.impl.cli.commands.message import message_app
from hyprial.shell.impl.cli.commands.common.support import _message_claim_params


@message_app.command("ack")
def ack(
    message_id: str = typer.Argument(..., help="Message ID to acknowledge."),
    source: str = typer.Option(..., "--from", help="Local acknowledging actor."),
    on_behalf_of: str | None = typer.Option(
        None,
        "--on-behalf-of",
        help="Visible actor attribution for an operator ack; never the wire sender.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Acknowledge a message without replying."""
    services = get_services()

    services._execute(
        lambda: services._daemon_request(
            "message.ack",
            {
                **_message_claim_params(source, on_behalf_of=on_behalf_of),
                "messageId": message_id,
            },
        ),
        json_output=json_output,
    )
