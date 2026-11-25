"""Slice 7: ``hyprial agent move`` and the hidden ``agent transfer`` are gone, no alias.

The command tree is read from the real assembled Typer app (the process root
that ``hyprial`` runs), hidden commands included.  An old invocation must be a
plain click usage error (exit 2, nothing on stdout): no handler runs, no
daemon request is made and no JSON payload is printed.  The suite's autouse
fixture keeps the home isolated.
"""

from __future__ import annotations

import click
import pytest
import typer
from typer.testing import CliRunner

from hyprial.shell.impl.cli.runtime import entry as cli


def _walk(command: click.Command, path: tuple[str, ...] = ()):
    yield path, command
    for name, sub in getattr(command, "commands", {}).items():
        yield from _walk(sub, path + (name,))


def _root() -> click.Command:
    return typer.main.get_command(cli.app)


def test_agent_group_has_teleport_and_neither_move_nor_transfer() -> None:
    agent = _root().commands["agent"]
    names = set(agent.commands)
    assert "teleport" in names
    assert "move" not in names
    assert "transfer" not in names


def test_no_transfer_command_anywhere_in_the_tree() -> None:
    paths = sorted(" ".join(path) for path, _command in _walk(_root()) if "transfer" in path)
    assert paths == []


def test_agent_help_lists_teleport_only() -> None:
    result = CliRunner().invoke(cli.app, ["agent", "--help"])
    assert result.exit_code == 0, result.output
    lines = [line.strip("│ ").split(" ", 1)[0] for line in result.output.splitlines()]
    assert "teleport" in lines
    assert "move" not in lines
    assert "transfer" not in lines


@pytest.mark.parametrize(
    "argv",
    [
        ("agent", "move", "w1", "--to", "node-b", "--json"),
        ("agent", "move", "--help"),
        ("agent", "transfer", "receive"),
        ("agent", "transfer", "recipient-key", "--json"),
        ("agent", "transfer", "bundle-export", "--help"),
        ("agent", "transfer", "runtime", "probe", "--help"),
    ],
)
def test_old_invocation_is_an_unknown_command(argv: tuple[str, ...]) -> None:
    result = CliRunner().invoke(cli.app, list(argv))
    printed = (result.stdout or "") + (result.stderr or "")
    assert result.exit_code == 2, printed
    assert (result.stdout or "") == ""
    assert f"No such command '{argv[1]}'" in printed, printed
