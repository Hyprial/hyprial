"""CLI command families composed by hyprial.cli."""

"""Command families for the Harness Bridge CLI.

Modules here own Typer command registrations; ``hyprial.cli`` is the
composition root that imports them in the original registration order and
re-exports their public names.
"""
