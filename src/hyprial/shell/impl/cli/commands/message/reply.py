"""The ``hyprial message reply`` operator messaging command."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from hyprial.error import codes
import typer

from hyprial.shell.impl.cli.commands.message import message_app
from hyprial.shell.impl.cli.commands.common.support import _message_claim_params
from hyprial.shell.impl.cli.commands.message.group.names import resolve_reply_mentions
from hyprial.shell.impl.cli.commands.message.send import _require_summary


@message_app.command("reply")
def reply(
    message_id: str = typer.Argument(..., help="Pending message ID to reply to."),
    message: list[str] = typer.Argument(..., help="Reply text."),
    source: str = typer.Option(..., "--from", help="Local replying identity."),
    summary: str | None = typer.Option(
        None,
        "--summary",
        help="Short summary the peer reads first; required, at most 512 UTF-8 bytes.",
    ),
    mentions: list[str] | None = typer.Option(
        None,
        "--mention",
        help=(
            "Additional group member (a.<id>, user:<owner>, or agent name) to mention and wake; "
            "repeatable. Group replies mention the replied-to agent author by default."
        ),
    ),
    no_mention: bool = typer.Option(
        False,
        "--no-mention",
        help=(
            "In a group reply, do not mention the replied-to author by default "
            "(explicit --mention still applies)."
        ),
    ),
    on_behalf_of: str | None = typer.Option(
        None,
        "--on-behalf-of",
        help="Visible actor attribution for an operator reply; never the wire sender.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Reply to one pending message and acknowledge it after durable acceptance.

    In a group conversation (``g.…``), replies mention and wake the
    replied-to author by default when that author is another current agent
    member (never the human owner, self, or a removed member). Use
    ``--mention`` to add members or ``--no-mention`` to omit the default @.
    """
    services = get_services()

    text = " ".join(message).strip()
    if not text:
        raise services.CliError(codes.INVALID_ARGUMENT, "message must not be empty")

    def operation() -> Any:
        # Validated inside the operation, before any IPC: a refused local
        # check must never reach message.reply.
        summary_value = _require_summary(summary)
        params: dict[str, Any] = {
            **_message_claim_params(source, on_behalf_of=on_behalf_of),
            "messageId": message_id,
            "message": text,
            "summary": summary_value,
        }
        if mentions:
            params["mentions"] = resolve_reply_mentions(services, mentions)
        if no_mention:
            params["noMention"] = True
        return services.runtime.request("message.reply", params)

    services.runtime.execute(operation, json_output=json_output)
