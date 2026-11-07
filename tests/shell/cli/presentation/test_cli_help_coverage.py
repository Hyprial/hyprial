"""CLI help coverage: the reference layer of a skill can only be as good as
the command surface it is generated from.

The old feature base measured 63 of 244 parameters with no help text; current
integration has already improved that count to 59. Generating a skill
reference from that surface would emit 59 blank entries, which is worse than
emitting nothing. This module
holds the exact measured budget: it is the covering test for the "补 help 再落
生成器" sequencing recorded in that note.

The count and budget must move together. The test fails if undocumented help
grows and also if documentation improves without lowering the budget, so both
directions are locked rather than hidden behind a tolerance window.
"""

from __future__ import annotations

import typer
from typer._click import Context

from hyprial.shell.impl.cli.commands.common.root import app

#: Parameters with no help text, measured on this branch. Only ever lower it.
#: 2026-09-18: squire setup --owner-key/--login-name/--machine/--machine-key
#: gained help text along with their derived defaults (58 -> 54).
#: R11: the ``mcp`` channel servers left the CLI tree, so their undocumented
#: parameters no longer exist (54 -> 42).
_UNDOCUMENTED_BUDGET = 42  # R11: mcp 出 CLI 树(54 → 42)

#: Parameters click contributes itself; they are not ours to document.
_INHERITED = frozenset({"help", "version"})


def _walk(command: object, ctx: Context) -> tuple[list[str], list[str]]:
    commands: list[str] = []
    undocumented: list[str] = []
    list_commands = getattr(command, "list_commands", None)
    if callable(list_commands):
        for name in list_commands(ctx):
            child = command.get_command(ctx, name)  # type: ignore[attr-defined]
            if child is None:
                continue
            found, missing = _walk(child, Context(child, info_name=name, parent=ctx))
            commands.extend(found)
            undocumented.extend(missing)
        return commands, undocumented

    path = ctx.command_path
    commands.append(path)
    for param in command.params:  # type: ignore[attr-defined]
        name = getattr(param, "name", None)
        if name in _INHERITED:
            continue
        if not getattr(param, "help", None):
            undocumented.append(f"{path} --{name}")
    return commands, undocumented


def _measure() -> tuple[list[str], list[str]]:
    root = typer.main.get_command(app)
    return _walk(root, Context(root, info_name="hyprial"))


def test_every_command_has_help_text() -> None:
    """Command-level help is at 100% and must stay there."""

    root = typer.main.get_command(app)
    ctx = Context(root, info_name="hyprial")
    commands, _ = _walk(root, ctx)
    assert commands, "sanity: the CLI must expose commands"

    missing: list[str] = []

    def check(command: object, ctx: Context) -> None:
        list_commands = getattr(command, "list_commands", None)
        if callable(list_commands):
            for name in list_commands(ctx):
                child = command.get_command(ctx, name)  # type: ignore[attr-defined]
                if child is not None:
                    check(child, Context(child, info_name=name, parent=ctx))
            return
        if not (command.help or command.short_help):  # type: ignore[attr-defined]
            missing.append(ctx.command_path)

    check(root, ctx)
    assert not missing, f"commands without help text: {missing}"


def test_undocumented_parameters_do_not_grow() -> None:
    _, undocumented = _measure()
    count = len(undocumented)
    assert count == _UNDOCUMENTED_BUDGET, (
        f"undocumented parameter count is {count}, budget is "
        f"{_UNDOCUMENTED_BUDGET}; add missing help when the count grew, or "
        "lower _UNDOCUMENTED_BUDGET in the same change when it shrank. "
        "Current undocumented parameters:\n  "
        + "\n  ".join(sorted(undocumented))
    )
