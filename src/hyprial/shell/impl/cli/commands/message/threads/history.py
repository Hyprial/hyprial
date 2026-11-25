"""``hyprial message history --conversation`` (design §9.5).

One ``conversations.history`` page, paged by the daemon's own
``--mode``/``--cursor``.  A group's ``g.`` id reads the group document
(slice GC5b); its human page names authors, mentions and replies by the
member names one ``conversations.get`` reports, and ``--json`` stays the
daemon page.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import typer

from hyprial.error import codes
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import CliError, JsonObject
from hyprial.shell.impl.cli.commands.message import message_app
from hyprial.shell.impl.cli.commands.message.group.names import (
    display_names,
    is_group,
    read_members,
)
from hyprial.shell.impl.cli.commands.message.group.render import render_group_history
from hyprial.shell.impl.cli.commands.message.send import _validate_from_identity
from hyprial.shell.impl.cli.commands.message.threads.conversation import timestamp
from hyprial.shell.impl.cli.commands.message.threads.reader import (
    FROM_HELP,
    ON_BEHALF_HELP,
    PAGE_MAX,
    reader_params,
    text,
)
from hyprial.shell.impl.cli.output import CliResult


def _entries(answer: object) -> list[Mapping[str, Any]]:
    rows = answer.get("entries") if isinstance(answer, Mapping) else None
    return [row for row in rows or [] if isinstance(row, Mapping)]


def _render(data: Mapping[str, Any], conversation: str) -> str:
    entries = _entries(data)
    if not entries:
        return "no messages"
    lines: list[str] = []
    for entry in entries:
        lines.append(
            f"{timestamp(entry.get('createdAtMs'))}  {text(entry.get('from'))} -> "
            f"{text(entry.get('to'))}  {text(entry.get('intent'))}  {text(entry.get('messageId'))}"
        )
        body = entry.get("message")
        for line in (body if isinstance(body, str) else "").splitlines() or [""]:
            lines.append(f"  {line}")
    older = data.get("nextOlderCursor")
    if isinstance(older, str) and older:
        lines.append(f"older: --conversation {conversation} --mode older --cursor {older}")
    return "\n".join(lines)


@message_app.command("history")
def message_history(
    conversation: str | None = typer.Option(
        None,
        "--conversation",
        help="Conversation id (c.…) from 'message conversation list', or a group id (g.…).",
    ),
    mode: str | None = typer.Option(None, "--mode", help="tail (default), older or after."),
    cursor: str | None = typer.Option(None, "--cursor", help="Cursor of the previous page."),
    limit: int | None = typer.Option(None, "--limit", min=1, max=PAGE_MAX, help="Entries per page."),
    source: str | None = typer.Option(
        None, "--from", help=FROM_HELP, callback=_validate_from_identity
    ),
    on_behalf_of: str | None = typer.Option(None, "--on-behalf-of", help=ON_BEHALF_HELP),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Read the recorded messages of one conversation.

    Read-only: nothing is fetched or acknowledged.  Bodies dropped from the
    delivery queue after ack and retention come back from the conversation
    record.
    """

    services = get_services()

    def operation() -> Any:
        if conversation is None:
            raise CliError(
                codes.INVALID_ARGUMENT,
                "pass --conversation <c.…>; list them with "
                "'hyprial message conversation list'",
            )
        conversation_id = conversation
        caller = reader_params(source, on_behalf_of)
        params: JsonObject = {
            **caller,
            "conversationId": conversation_id,
        }
        for key, value in (("mode", mode), ("cursor", cursor), ("limit", limit)):
            if value is not None:
                params[key] = value
        result = services.runtime.request("conversations.history", params)
        if isinstance(result, Mapping) and is_group(conversation_id) and not json_output:
            names = display_names(read_members(services, caller, conversation_id))
            return CliResult(
                result, render=lambda data: render_group_history(data, conversation_id, names)
            )
        if isinstance(result, Mapping):
            return CliResult(result, render=lambda data: _render(data, conversation_id))
        return result

    services.runtime.execute(operation, json_output=json_output)
