"""The ``hyprial message read`` message-body command."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import CliResult

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from collections.abc import Mapping
import typer

from hyprial.shell.impl.cli.commands.common.support import _message_claim_params
from hyprial.shell.impl.cli.commands.message import message_app
from hyprial.shell.impl.cli.commands.message.send import _validate_from_identity


def _render_read(data: Mapping[str, Any]) -> str:
    """The body only, byte for byte; a pipe-friendly read.

    The body is not title-decorated, not summarized and its line endings are
    preserved: the output layer adds the one trailing newline only when the
    body has none.
    """
    body = data.get("body")
    if isinstance(body, str):
        return body
    size = data.get("bodyBytes")
    shown = str(size) if type(size) is int else "?"
    return f"<body not text: {shown} bytes; use --json>"


@message_app.command("read")
def read_body(
    message_id: str = typer.Argument(..., help="Message ID whose body to read."),
    source: str = typer.Option(
        ...,
        "--from",
        help="Local actor the message was addressed to.",
        callback=_validate_from_identity,
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Print one message body.

    A consuming action: the daemon records ``fetched`` for this actor.  The
    message stays pending until reply or ack, so ``read`` can be repeated;
    ``hyprial message inbox`` remains read-only and never fetches.
    """
    services = get_services()

    def operation() -> Any:
        from hyprial.daemon import IPC_MESSAGE_READ_BODY

        services = get_services()
        result = services._daemon_request(
            IPC_MESSAGE_READ_BODY,
            {**_message_claim_params(source), "messageId": message_id},
        )
        if isinstance(result, Mapping):
            return CliResult(result, render=_render_read)
        return result

    services._execute(operation, json_output=json_output)
