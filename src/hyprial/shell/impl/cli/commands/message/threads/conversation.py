"""``hyprial message conversation list`` over ``conversations.list``."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import typer

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.message.send import _validate_from_identity
from hyprial.shell.impl.cli.commands.message.threads import conversation_app
from hyprial.shell.impl.cli.commands.message.threads.reader import (
    FROM_HELP,
    ON_BEHALF_HELP,
    PAGE_MAX,
    reader_params,
    text,
)
from hyprial.shell.impl.cli.output import CliResult, tables


def timestamp(value: object) -> str:
    """Epoch milliseconds as a UTC RFC 3339 second, or ``-``."""

    if type(value) is not int:
        return "-"
    moment = datetime.fromtimestamp(value / 1000, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _render_list(data: Mapping[str, Any]) -> str:
    participant = text(data.get("participant"))
    rows = [row for row in data.get("conversations") or [] if isinstance(row, Mapping)]
    if not rows:
        return f"no conversations for {participant}"
    cells = [
        [
            text(row.get("conversationId")),
            text(row.get("kind")),
            text(row.get("peer")),
            str(row.get("messages", "-")),
            timestamp(row.get("lastMessageAtMs")),
        ]
        for row in rows
    ]
    lines = [
        tables.render_table(
            f"conversations of {participant}",
            ["CONVERSATION", "KIND", "PEER", "MESSAGES", "LAST"],
            cells,
        )
    ]
    cursor = data.get("nextCursor")
    if isinstance(cursor, str) and cursor:
        lines.append(f"more: --cursor {cursor}")
    return "\n".join(lines)


@conversation_app.command("list")
def conversation_list(
    limit: int | None = typer.Option(
        None, "--limit", min=1, max=PAGE_MAX, help="Conversations per page."
    ),
    cursor: str | None = typer.Option(None, "--cursor", help="nextCursor of the previous page."),
    source: str | None = typer.Option(
        None, "--from", help=FROM_HELP, callback=_validate_from_identity
    ),
    on_behalf_of: str | None = typer.Option(None, "--on-behalf-of", help=ON_BEHALF_HELP),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List the reader's conversations newest first.

    Each conversation is one conversation cell (``c.…``); read its messages
    with ``hyprial message history --conversation <id>``.
    """

    services = get_services()

    def operation() -> Any:
        caller = reader_params(source, on_behalf_of)
        params = dict(caller)
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        result = services.runtime.request("conversations.list", params)
        if isinstance(result, Mapping):
            return CliResult(result, render=_render_list)
        return result

    services.runtime.execute(operation, json_output=json_output)
