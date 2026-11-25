"""PAC is a foundational concept, not a first-level command.

Allen (2026-09-23): users and agents operate work through ``hyprial workflow``
and ``hyprial routine``; PAC graphs and flags are the model underneath them
and must not be exposed for direct manipulation as ``hyprial workflow``.  A
hidden registration is still a first-level command anyone can type, so it is
rejected too.
"""

from __future__ import annotations

from typer.main import get_command
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def _top_level_names() -> set[str]:
    return set(get_command(_cli_root_app).commands)


def _group_commands(*path: str) -> set[str]:
    """Names registered under one group path, or a failure naming the path."""

    node = get_command(_cli_root_app)
    for step in path:
        children = getattr(node, "commands", None)
        assert children is not None, f"hyprial {' '.join(path)} is not a group"
        node = children[step]
    return set(getattr(node, "commands", {}))


def test_pac_is_not_a_top_level_command() -> None:
    names = _top_level_names()
    # Denominator: the entry points PAC is operated through must exist, or an
    # empty or broken tree would pass this test vacuously.
    assert {"workflow", "routine"} <= names, sorted(names)
    assert "pac" not in names, (
        "`hyprial workflow` is registered at the top level; PAC must be reached "
        "through `hyprial workflow` / `hyprial routine`, not operated directly"
    )


def test_dispatch_is_not_a_top_level_command() -> None:
    names = _top_level_names()
    assert "agent" in names
    assert "dispatch" not in names


def test_restart_is_not_a_top_level_command() -> None:
    """R5 moved the safe restart under ``daemon``; only ``daemon restart`` acts."""

    names = _top_level_names()
    # Denominator: the group the command moved into must exist, or the
    # absence check would pass vacuously on an empty tree.
    assert "daemon" in names, sorted(names)
    assert "restart" not in names, (
        "`hyprial restart` is registered at the top level; the safe restart is "
        "`hyprial daemon restart` and the old spelling must not be an alias"
    )


def test_message_verbs_are_not_top_level_commands() -> None:
    """R12 folded ``send``/``ack``/``reply`` into the ``message`` noun.

    The three verbs stay reachable as ``message send``/``message ack``/
    ``message reply``; a surviving top-level spelling would be a second,
    undeclared alias the plan forbids.
    """

    names = _top_level_names()
    # Denominator: the group the verbs moved into must exist, or the
    # absence check would pass vacuously on an empty tree.
    assert "message" in names, sorted(names)
    for retired in ("send", "ack", "reply"):
        assert retired not in names, (
            f"`hyprial {retired}` is registered at the top level; R12 moved it "
            "under `hyprial message` and left no alias"
        )


def test_user_key_is_reached_through_the_user_noun() -> None:
    """0.5.5.3 freezes the top-level set: signing keys live under ``user``.

    The provider-keys slice first registered ``hyprial user-key`` at the top
    level.  The grouped spelling is ``hyprial user key``; the hyphenated name
    never shipped, so it must not survive as a top-level alias either.
    """

    names = _top_level_names()
    # Denominator: the group the commands moved into must exist, or the
    # absence check would pass vacuously on an empty tree.
    assert "user" in names, sorted(names)
    assert "user-key" not in names, (
        "`hyprial user-key` is registered at the top level; the signing-key "
        "group is `hyprial user key` and 0.5.5.3 leaves no top-level alias"
    )
    assert {"status", "renew", "rotate", "revoke"} <= _group_commands(
        "user", "key"
    ), sorted(_group_commands("user", "key"))

    result = CliRunner().invoke(_cli_root_app, ["user-key", "status"])
    assert result.exit_code != 0, result.output
    assert "No such command" in result.output, result.output
