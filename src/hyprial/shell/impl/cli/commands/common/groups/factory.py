"""One factory for every Typer command group in the CLI surface.

Why a factory instead of ``group(...)`` at each site (0.5.5.2 CLI tree
regroup): a bare group invocation is a usage error, so *every* group must print
its command list and exit 2.  Spelling that decision once keeps the tree from
drifting group by group, and it makes the few deliberate exceptions visible:

* ``init`` keeps ``invoke_without_command`` because bare ``hyprial init``
  still runs initialization;
* ``daemon upgrade``, ``agent tiers`` and ``routine templates`` have a declared
  no-argument behavior, so they opt out of ``no_args_is_help`` explicitly.

``tests/shell/cli/surface/test_cli_group_help.py`` asserts the non-compliant set
is exactly that declared exception set, so a new default behavior cannot slip in
and an expired exemption cannot linger.
"""

from __future__ import annotations

import typer
from typer.core import TyperGroup


def group(
    *,
    help: str | None = None,
    no_args_is_help: bool = True,
    invoke_without_command: bool = False,
    add_completion: bool = True,
    rich_markup_mode: str | None = None,
    cls: type[TyperGroup] | None = None,
) -> typer.Typer:
    """Build one command group with the shared bare-invocation contract."""

    return typer.Typer(
        help=help,
        no_args_is_help=no_args_is_help,
        invoke_without_command=invoke_without_command,
        add_completion=add_completion,
        rich_markup_mode=rich_markup_mode,
        cls=cls,
    )
