"""The ``hyprial message group`` command tree, its parameters and help, as a snapshot.

The tree is the one ``docs/reference/cli-command-inventory.json`` records;
this snapshot pins the parts a user reads: command names, parameters and
help text.  The group commands live under the one message noun; sending,
replying and history stay ``message send|reply|history``.
"""

from __future__ import annotations

from typing import Any

from typer.main import get_command
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401

JSON = ("--json", "Emit JSON only.")

SNAPSHOT: dict[tuple[str, ...], tuple[str, list[tuple[str, str | None]]]] = {
    ("message", "group"): ("Create group conversations and manage their members.", []),
    ("message", "group", "create"): (
        "Create a group conversation with you and the agents you name.",
        [
            ("--title", "Title of the group."),
            ("--member", "Agent to add: its name, a.<id> or address; repeatable."),
            JSON,
        ],
    ),
    ("message", "group", "show"): (
        "Show one group: its title, its members and what you may do.",
        [("CONVERSATION_ID", "Group id (g.…)."), JSON],
    ),
    ("message", "group", "list"): (
        "List the groups you are a member of, newest activity first.",
        [
            ("--limit", "Groups per page."),
            ("--cursor", "nextCursor of the previous page."),
            JSON,
        ],
    ),
    ("message", "group", "members"): ("Add and remove members of a group.", []),
    ("message", "group", "members", "add"): (
        "Add an agent to a group.",
        [
            ("CONVERSATION_ID", "Group id (g.…)."),
            ("AGENT", "Agent to add: its name, a.<id> or address."),
            JSON,
        ],
    ),
    ("message", "group", "members", "remove"): (
        "Remove a member from a group; an agent removing itself leaves it.",
        [
            ("CONVERSATION_ID", "Group id (g.…)."),
            ("MEMBER", "Member id as 'message group show' lists it, or the member's name."),
            JSON,
        ],
    ),
}


def _command(path: tuple[str, ...]) -> Any:
    command = get_command(_cli_root_app)
    for word in path:
        command = command.commands[word]
    return command


def _parameters(command: Any) -> list[tuple[str, str | None]]:
    rows = []
    for parameter in command.params:
        if parameter.name == "help":
            continue
        label = parameter.opts[0] if parameter.opts[0].startswith("-") else parameter.name.upper()
        rows.append((label, getattr(parameter, "help", None)))
    return rows


def test_the_group_tree_is_exactly_the_snapshot() -> None:
    assert set(_command(("message", "group")).commands) == {"create", "show", "list", "members"}
    assert set(_command(("message", "group", "members")).commands) == {"add", "remove"}
    for path, (summary, parameters) in SNAPSHOT.items():
        command = _command(path)
        assert (command.help or "").splitlines()[0] == summary, path
        assert _parameters(command) == parameters, path


def test_no_top_level_chat_command_exists() -> None:
    assert "chat" not in get_command(_cli_root_app).commands
    assert "group" in _command(("message",)).commands


def test_a_bare_group_prints_its_commands() -> None:
    result = CliRunner().invoke(_cli_root_app, ["message", "group"])

    assert result.exit_code == 2
    for name in ("create", "show", "list", "members"):
        assert name in result.output
