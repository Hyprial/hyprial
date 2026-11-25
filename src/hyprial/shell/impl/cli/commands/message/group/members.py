"""``hyprial message group members add|remove`` over the group member methods."""

from __future__ import annotations

from typing import Any

import typer

from hyprial.shell.impl.cli.commands.message.group import members_app
from hyprial.shell.impl.cli.commands.message.group.groups import GROUP_HELP, answer, caller, new_key
from hyprial.shell.impl.cli.commands.message.group.names import member_id, read_members
from hyprial.shell.impl.cli.commands.message.group.render import render_added, render_removed
from hyprial.shell.impl.cli.commands.common.services import get_services


@members_app.command("add")
def members_add(
    conversation_id: str = typer.Argument(..., help=GROUP_HELP),
    agent: str = typer.Argument(..., help="Agent to add: its name, a.<id> or address."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Add an agent to a group."""

    services = get_services()

    def operation() -> Any:
        params = {
            **caller(),
            "conversationId": conversation_id,
            "member": agent,
            "idempotencyKey": new_key(),
        }
        return answer(
            services.runtime.request("conversations.members.add", params),
            lambda data: render_added(agent, data),
        )

    services.runtime.execute(operation, json_output=json_output)


@members_app.command("remove")
def members_remove(
    conversation_id: str = typer.Argument(..., help=GROUP_HELP),
    member: str = typer.Argument(
        ..., help="Member id as 'message group show' lists it, or the member's name."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Remove a member from a group; an agent removing itself leaves it."""

    services = get_services()

    def operation() -> Any:
        identity = caller()
        members = read_members(services, identity, conversation_id)
        params = {
            **identity,
            "conversationId": conversation_id,
            "memberId": member_id(members, member),
            "idempotencyKey": new_key(),
        }
        return answer(
            services.runtime.request("conversations.members.remove", params),
            lambda data: render_removed(member, data),
        )

    services.runtime.execute(operation, json_output=json_output)
