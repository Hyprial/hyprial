"""Native Shell process entry for Harness Bridge.

The entry owns argv, previous-release machine rewrites, the process exit and
the one composition call.  It does not import command families and it does not
re-export their helpers: command families are composed by
:mod:`hyprial.shell.impl.cli.runtime.registration`, and tests patch the owning
module or the explicit service instance.

The installed ``hyprial.cli`` module delegates only to the public Shell
``main``.  Request/execute stay on :class:`CliRuntime`; ``support`` is the
owner of the real ports, so replacing ``support._daemon_request`` or
``support._execute`` is the supported test/operator seam.
"""

from __future__ import annotations

import os
import sys

# Typer's Rich help renderer treats CI as a terminal and its bundled
# ``NO_COLOR`` handling only removes color styles, not all ANSI styling.  Set
# Typer's own escape hatch before importing Typer so help and errors honor the
# standard no-color contract even in CI capture.
if "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
    os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"

import typer

try:  # Typer 0.27+ vendors Click; older Typer exposes the external class.
    from typer._click.exceptions import ClickException as TyperClickException
except ImportError:  # pragma: no cover - compatibility with older Typer
    from click import ClickException as TyperClickException

try:  # Abort moved out of Typer's vendored exceptions in 0.27.2.
    from typer._click.exceptions import Abort as TyperAbort
except ImportError:  # pragma: no cover - compatibility across Typer layouts
    from click import Abort as TyperAbort

from hyprial.kernel import RequestPortError, ipc_errors
from hyprial.shell.impl.cli.commands.common import support
from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell import worker_session_binding
from hyprial.shell.impl.cli.output import report_error
from hyprial.shell.impl.cli.runtime.composition import CliRuntime
from hyprial.shell.impl.cli.runtime.machine_argv import (
    resolve_machine_compatibility,
    run_machine_mcp_entry,
)
from hyprial.shell.impl.cli.runtime.registration import build_app
from hyprial.shell.impl.cli.runtime.services import install_services

# The public composition boundary B/C register against.  Providers read the
# owning support module on every call, so replacing support._daemon_request /
# support._execute is honored without an entry-module lookup authority.
runtime = CliRuntime(
    app=app,
    request_resolver=lambda: support._daemon_request,
    execute_resolver=lambda: support._execute,
    worker_session_resolver=lambda: worker_session_binding(),
)

app = build_app(runtime)
install_services(runtime)


def main() -> None:
    """Installed ``hyprial`` entry point."""

    raw_argv = tuple(sys.argv[1:])
    compatibility = resolve_machine_compatibility(raw_argv)
    if compatibility is not None and compatibility.target == "mcp":
        exit_code = run_machine_mcp_entry(compatibility.argv)
        if exit_code != 0:
            raise SystemExit(exit_code)
        return

    effective_argv = compatibility.argv if compatibility is not None else raw_argv
    json_output = "--json" in effective_argv
    try:
        exit_code = app(
            args=list(effective_argv) if compatibility is not None else None,
            standalone_mode=False,
        )
    except TyperClickException as error:
        report_error(
            RequestPortError(ipc_errors.INVALID_ARGUMENT, error.format_message()),
            json_output=json_output,
        )
        raise SystemExit(error.exit_code) from error
    except (TyperAbort, typer.Abort) as error:  # typer.confirm raises typer.Abort
        report_error(RequestPortError("ABORTED", "operation aborted"), json_output=json_output)
        raise SystemExit(1) from error
    if isinstance(exit_code, int) and exit_code != 0:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()


__all__ = ["main"]
