"""Every group must answer a bare invocation with help and exit code 2.

Ruling (CLI tree regroup, 0.5.5.2): one shared group factory builds every
Typer group with ``no_args_is_help=True``, and the root app is included — a
bare ``hyprial`` prints the command list and exits 2 instead of only saying
"Missing command."  ``init`` and the three groups that deliberately run a
no-argument report are frozen exceptions; exempting another group requires
editing this table in review.
"""

from __future__ import annotations

from dataclasses import dataclass

import typer
from typer.testing import CliRunner

from hyprial.shell.impl.cli.runtime import entry as cli

#: Frozen no-argument exceptions.  Each entry pairs the group path with why it
#: keeps a no-argument behavior and which decision owns it; the traversal test
#: asserts the non-compliant set is *exactly* this set, so neither adding a
#: default behavior nor exempting a compliant group can pass unnoticed.
@dataclass(frozen=True, slots=True)
class NoArgException:
    path: tuple[str, ...]
    why: str
    owner: str


NO_ARG_EXCEPTIONS: tuple[NoArgException, ...] = (
    NoArgException(
        path=("init",),
        why="bare `hyprial init` still runs initialization (invoke_without_command)",
        owner="cli-survey item 5: onboarding moves under init",
    ),
    NoArgException(
        path=("daemon", "upgrade"),
        why="bare `hyprial daemon upgrade` runs the upgrade report",
        owner="cli-survey item 6: declared no-argument behavior, not help",
    ),
    NoArgException(
        path=("agent", "tiers"),
        why="bare `hyprial agent tiers` reports recorded tiers",
        owner="cli-survey item 6: declared no-argument behavior, not help",
    ),
    NoArgException(
        path=("routine", "templates"),
        why="bare `hyprial routine templates` lists built-in templates",
        owner="cli-survey item 6: declared no-argument behavior, not help",
    ),
)

EXCEPTION_PATHS = frozenset(entry.path for entry in NO_ARG_EXCEPTIONS)

#: Rich help renders the child list under this heading.
HELP_COMMANDS_HEADING = "Commands"


def group_tree(
    group: typer.Typer, path: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], typer.Typer]]:
    """Return every registered group with its command path, depth-first."""

    found: list[tuple[tuple[str, ...], typer.Typer]] = []
    for info in group.registered_groups:
        sub = info.typer_instance
        group_path = path + (info.name or "",)
        found.append((group_path, sub))
        found.extend(group_tree(sub, group_path))
    return found


def _bare_output(runner: CliRunner, path: tuple[str, ...]) -> tuple[int, str]:
    result = runner.invoke(cli.app, list(path))
    return result.exit_code, (result.stdout or "") + (result.stderr or "")


def test_every_group_prints_help_and_exits_two() -> None:
    runner = CliRunner()
    non_compliant: set[tuple[str, ...]] = set()
    detail: list[str] = []
    for path, _group in group_tree(cli.app):
        exit_code, printed = _bare_output(runner, path)
        if exit_code == 2 and HELP_COMMANDS_HEADING in printed:
            continue
        non_compliant.add(path)
        detail.append(
            f"hyprial {' '.join(path)}: exit={exit_code} "
            f"help={HELP_COMMANDS_HEADING in printed}"
        )
    assert non_compliant == set(EXCEPTION_PATHS), (
        "groups whose bare behavior is not help+exit2 must be exactly the declared "
        "exceptions:\n"
        + "\n".join(sorted(detail))
        + f"\nundeclared: {sorted(non_compliant - set(EXCEPTION_PATHS))}"
        + f"\ndeclared but compliant: {sorted(set(EXCEPTION_PATHS) - non_compliant)}"
    )


def test_each_exception_registers_why_and_owner() -> None:
    for entry in NO_ARG_EXCEPTIONS:
        assert entry.why.strip(), entry.path
        assert entry.owner.strip(), entry.path
    assert len(EXCEPTION_PATHS) == len(NO_ARG_EXCEPTIONS), "duplicate exception"


def test_root_app_prints_help_and_exits_two() -> None:
    exit_code, printed = _bare_output(CliRunner(), ())
    assert exit_code == 2, printed
    assert HELP_COMMANDS_HEADING in printed, printed


def test_exception_table_names_real_groups() -> None:
    paths = {path for path, _group in group_tree(cli.app)}
    missing = sorted("/".join(path) for path in EXCEPTION_PATHS if path not in paths)
    assert missing == [], f"declared exceptions that are not groups: {missing}"
