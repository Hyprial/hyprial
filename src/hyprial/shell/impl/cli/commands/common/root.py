"""The root Typer application object for the Harness Bridge CLI.

Kept in its own module so command-family modules can decorate against it
without importing the composition root (``hyprial.cli``).
"""
from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.groups import group
from hyprial.shell.impl.cli.runtime.legacy_paths import MovedPathGroup


app = group(
    add_completion=False,
    cls=MovedPathGroup,
    help="Harness Bridge command line interface.",
    rich_markup_mode="rich",
)
