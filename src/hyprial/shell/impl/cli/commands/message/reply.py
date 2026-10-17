"""The ``hyprial reply`` operator messaging command."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from hyprial.kernel import ipc_errors
import typer

from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.common.support import _message_claim_params
from hyprial.shell.impl.cli.commands.message.send import _require_summary


@app.command()
def reply(
    message_id: str = typer.Argument(..., help="Pending message ID to reply to."),
    message: list[str] = typer.Argument(..., help="Reply text."),
    source: str = typer.Option(..., "--from", help="Local replying identity."),
    summary: str | None = typer.Option(
        None,
        "--summary",
        help="Short summary the peer reads first; required, at most 512 UTF-8 bytes.",
    ),
    on_behalf_of: str | None = typer.Option(
        None,
        "--on-behalf-of",
        help="Visible actor attribution for an operator reply; never the wire sender.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Reply to one pending message and acknowledge it after durable acceptance."""
    services = get_services()

    text = " ".join(message).strip()
    if not text:
        raise services.CliError(ipc_errors.INVALID_ARGUMENT, "message must not be empty")

    def operation() -> Any:
        # Validated inside the operation, before any IPC: a refused local
        # check must never reach message.reply.
        summary_value = _require_summary(summary)
        return services._daemon_request(
            "message.reply",
            {
                **_message_claim_params(source, on_behalf_of=on_behalf_of),
                "messageId": message_id,
                "message": text,
                "summary": summary_value,
            },
        )

    services._execute(operation, json_output=json_output)
