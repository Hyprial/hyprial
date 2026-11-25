"""``hyprial message group create|show|list`` over the group conversation methods."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from typing import Any

import typer

from hyprial.shell.impl.cli.commands.message.group import group_app
from hyprial.shell.impl.cli.commands.message.group.render import (
    render_group,
    render_group_detail,
    render_group_list,
)
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject
from hyprial.shell.impl.cli.commands.message.threads.reader import PAGE_MAX, reader_params
from hyprial.shell.impl.cli.output import CliResult

GROUP_HELP = "Group id (g.…)."
GROUP_KINDS = ["group"]


def caller() -> JsonObject:
    """The caller fields: the verified agent session, else the local operator."""

    return reader_params(None, None)


def new_key() -> str:
    """A fresh idempotency key: one per invocation of a mutating command."""

    return uuid.uuid4().hex


def answer(result: Any, render: Callable[[Mapping[str, Any]], str]) -> Any:
    """The daemon answer, rendered for humans and unchanged for ``--json``."""

    if isinstance(result, Mapping):
        return CliResult(result, render=render)
    return result


@group_app.command("create")
def group_create(
    title: str = typer.Option(..., "--title", help="Title of the group."),
    members: list[str] = typer.Option(
        [], "--member", help="Agent to add: its name, a.<id> or address; repeatable."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Create a group conversation with you and the agents you name."""

    services = get_services()

    def operation() -> Any:
        params = {
            **caller(),
            "title": title,
            "members": list(members),
            "idempotencyKey": new_key(),
        }
        return answer(services.runtime.request("conversations.create", params), render_group)

    services.runtime.execute(operation, json_output=json_output)


@group_app.command("show")
def group_show(
    conversation_id: str = typer.Argument(..., help=GROUP_HELP),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show one group: its title, its members and what you may do."""

    services = get_services()

    def operation() -> Any:
        params = {**caller(), "conversationId": conversation_id}
        return answer(
            services.runtime.request("conversations.get", params), render_group_detail
        )

    services.runtime.execute(operation, json_output=json_output)


@group_app.command("list")
def group_list(
    limit: int | None = typer.Option(
        None, "--limit", min=1, max=PAGE_MAX, help="Groups per page."
    ),
    cursor: str | None = typer.Option(None, "--cursor", help="nextCursor of the previous page."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List the groups you are a member of, newest activity first."""

    services = get_services()

    def operation() -> Any:
        params: JsonObject = {**caller(), "kinds": list(GROUP_KINDS)}
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        return answer(
            services.runtime.request("conversations.list", params), render_group_list
        )

    services.runtime.execute(operation, json_output=json_output)


__all__ = ["GROUP_HELP", "answer", "caller", "new_key"]
