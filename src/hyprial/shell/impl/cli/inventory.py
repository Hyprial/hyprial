"""Render the top-level CLI inventory from Typer's registered command tree."""

from __future__ import annotations

from dataclasses import dataclass

import typer
from typer._click import Context
from typer.main import get_command


@dataclass(frozen=True)
class TopLevelCommand:
    """One command registered directly under the ``hyprial`` root."""

    name: str
    hidden: bool


def registered_top_level(typer_app: typer.Typer) -> tuple[TopLevelCommand, ...]:
    """Return root commands in Typer's registration order."""

    root = get_command(typer_app)
    context = Context(root, info_name="hyprial")
    commands: list[TopLevelCommand] = []
    for name in root.list_commands(context):
        command = root.get_command(context, name)
        if command is not None:
            commands.append(TopLevelCommand(name=name, hidden=bool(command.hidden)))
    return tuple(commands)


def render_top_level_help(typer_app: typer.Typer) -> str:
    """Build ``hyprial help`` without maintaining a second command snapshot."""

    inventory = registered_top_level(typer_app)
    visible = tuple(item.name for item in inventory if not item.hidden)
    hidden = tuple(item.name for item in inventory if item.hidden)

    lines = [
        "Harness Bridge",
        "",
        "Usage:",
        "  hyprial COMMAND",
        "",
        "Commands:",
        *(f"  hyprial {name}" for name in visible),
    ]
    if hidden:
        lines.extend(
            [
                "",
                "Hidden commands (internal):",
                *(f"  hyprial {name}" for name in hidden),
            ]
        )
    return "\n".join(lines)
