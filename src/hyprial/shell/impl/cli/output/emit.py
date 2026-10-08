"""The single place a command's result or failure reaches stdout/stderr."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping
from typing import Any, NoReturn

import typer

from hyprial.kernel import RequestPortError
from hyprial.shell.impl.cli.output.errors import human_message, json_failure
from hyprial.shell.impl.cli.output.render import render_generic
from hyprial.shell.impl.cli.output.result import CliResult, CliStream


def _write_json(value: Any, *, indent: int | None = None, stream: Any = None) -> None:
    # Deliberately bypass Rich: --json stdout is exactly one JSON value and
    # Rich must remain completely silent, including on error paths.
    if indent is None:
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        rendered = json.dumps(value, ensure_ascii=False, indent=indent)
    (stream or sys.stdout).write(rendered + "\n")


def _print_human(value: Any) -> None:
    if isinstance(value, str):
        # Rendered text is laid out already: write it verbatim (no Rich wrap,
        # markup, emoji or highlighting), so a template or an eval-able line
        # reaches stdout byte for byte.
        sys.stdout.write(value if value.endswith("\n") else value + "\n")
        return
    from rich.console import Console
    from rich.pretty import Pretty

    console = Console()
    if hasattr(value, "__rich_console__") or hasattr(value, "__rich__"):
        console.print(value, soft_wrap=True)
    else:
        console.print(Pretty(value, expand_all=True))


def _silence_stdout() -> None:
    """Point stdout at /dev/null so interpreter shutdown cannot hit EPIPE again."""
    try:
        fd = sys.stdout.fileno()
    except (AttributeError, ValueError, OSError):
        return
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null, fd)
    finally:
        os.close(null)


def _emit_stream(stream: CliStream, *, json_output: bool) -> None:
    """Write each item as it is produced; a closed reader or Ctrl-C ends it."""
    render = stream.render_item if stream.render_item is not None else render_generic
    try:
        for item in stream.items:
            if json_output:
                _write_json(item)
            else:
                _print_human(render(item))
            sys.stdout.flush()
    except KeyboardInterrupt:
        return
    except BrokenPipeError:
        _silence_stdout()


def emit_value(value: Any, *, json_output: bool, json_indent: int | None = None) -> None:
    """Emit a bare value (the pre-``CliResult`` return shape)."""
    if json_output:
        _write_json(value, indent=json_indent)
    else:
        _print_human(value)


def emit_result(outcome: Any, *, json_output: bool, json_indent: int | None = None) -> None:
    """Emit what an operation returned."""
    if isinstance(outcome, CliStream):
        _emit_stream(outcome, json_output=json_output)
        return
    if not isinstance(outcome, CliResult):
        if not isinstance(outcome, Mapping):
            raise TypeError(
                "a command operation must return CliResult, CliStream or a mapping, "
                f"not {type(outcome).__name__}"
            )
        outcome = CliResult(outcome)
    data = with_ok(outcome.data)
    indent = outcome.json_indent if outcome.json_indent is not None else json_indent
    if json_output:
        _write_json(data, indent=indent)
    else:
        render = outcome.render if outcome.render is not None else render_generic
        _print_human(render(data))
    if data["ok"] is False:
        # The exit status agrees with ok: a result that reports failure exits 1.
        raise typer.Exit(code=1)


def with_ok(data: Mapping[str, Any]) -> dict[str, Any]:
    """``data`` with ``ok``: kept when present, otherwise ``"ok": true`` first."""
    if "ok" in data:
        return dict(data)
    return {"ok": True, **data}


def report_error(error: Exception, *, json_output: bool, in_stream: bool = False) -> None:
    """Write ``error``: one JSON value on stdout, or one line on stderr.

    Inside a stream stdout is NDJSON items, so the JSON error goes to stderr.
    """
    if json_output:
        _write_json(json_failure(error), stream=sys.stderr if in_stream else None)
        return
    from rich.console import Console
    from rich.markup import escape

    Console(stderr=True).print(
        f"[bold red]hyprial:[/bold red] {escape(human_message(error))}", soft_wrap=True
    )


def emit_error(error: Exception, *, json_output: bool, in_stream: bool = False) -> NoReturn:
    """Report ``error`` and exit 1."""
    report_error(error, json_output=json_output, in_stream=in_stream)
    raise typer.Exit(code=1)


def run_command(
    operation: Callable[[], Any],
    *,
    json_output: bool,
    json_indent: int | None = None,
    require_home: Callable[[], Any] | None = None,
) -> None:
    """Run one command operation and emit its outcome.

    Control flow is not an error: ``typer.Exit`` and ``typer.Abort`` (both
    ``RuntimeError`` subclasses in click) pass through to the entry point
    with their own exit code, instead of being reported as an empty failure.
    """
    streaming = False
    try:
        if require_home is not None:
            require_home()
        outcome = operation()
        if isinstance(outcome, CliStream):
            # A stream is produced while it is written, so its errors surface here.
            streaming = True
            emit_result(outcome, json_output=json_output)
            return
    except (typer.Exit, typer.Abort):
        raise
    except KeyboardInterrupt:
        emit_error(RequestPortError("INTERRUPTED", "operation interrupted"), json_output=json_output)
    except Exception as error:  # noqa: BLE001 - CLI error boundary
        emit_error(error, json_output=json_output, in_stream=streaming)
    emit_result(outcome, json_output=json_output, json_indent=json_indent)
