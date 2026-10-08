"""CLI output: commands return a CliResult (or CliStream) and this package is
the only code that writes results, errors and progress to stdout/stderr.
Rules and design: docs/design/cli-output-unification-2026-10-04.md.
"""

from hyprial.shell.impl.cli.output.emit import (
    emit_error,
    emit_result,
    emit_value,
    report_error,
    run_command,
    with_ok,
)
from hyprial.shell.impl.cli.output.errors import human_message, json_failure
from hyprial.shell.impl.cli.output import tables
from hyprial.shell.impl.cli.output.channels import confirm, notice, progress, warn
from hyprial.shell.impl.cli.output.render import render_generic, scalar_text
from hyprial.shell.impl.cli.output.result import CliResult, CliStream, Render

__all__ = [
    "CliResult",
    "CliStream",
    "Render",
    "confirm",
    "emit_error",
    "emit_result",
    "emit_value",
    "human_message",
    "json_failure",
    "notice",
    "progress",
    "render_generic",
    "report_error",
    "run_command",
    "scalar_text",
    "tables",
    "warn",
    "with_ok",
]
