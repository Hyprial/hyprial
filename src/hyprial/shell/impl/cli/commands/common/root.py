"""The root Typer application object for the Harness Bridge CLI.

Kept in its own module so command-family modules can decorate against it
without importing the composition root (``hyprial.cli``).
"""
from __future__ import annotations

import typer


app = typer.Typer(
    add_completion=False,
    help="Harness Bridge command line interface.",
    no_args_is_help=False,
    rich_markup_mode="rich",
)
