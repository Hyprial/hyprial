"""``hyprial gui``: start the shared web GUI in the foreground.

Decision: command registration stays import-light so ``hyprial.cli`` does not
eagerly load the GUI backend and daemon; the launcher is imported when the
command actually runs, keeping capability admission and identity checks on the
real execution path.  A home without a running daemon, owned or not, is
started by the launcher through ``ensure_daemon`` (DL-1, Z-6a), after its
cheap validation and before admission; this file only supplies the launch
primitive.  Termination
handling is installed before plan building (source discovery and, for the
wheel's managed dev worktree, creation and npm install) so those paths also
exit 143/130.  ``--stdin-stop`` adds one more stop request, EOF or a
``shutdown`` line on stdin, which exits 0 after the same reclamation; stdin is
not read without the flag.  ``--port 0`` is allowed: the record names the port
the host bound.  ``--with-dev-tools`` (PR 4b-C) is the only switch for the
developer tool layer: it becomes the host's ``--dev-tools`` argument, ``dev``
implies it, and no environment variable stands in for it, so a plain
``hyprial gui`` and the native shell never serve the tools.  ``--live-source``
(PR 5) is the second, independent switch: the Vite dev server behind the host.
The positional ``dev`` is an alias for both flags and stays idempotent with
either; ``--gui-home`` belongs to live source.

Decision (owner, GUI line, 2026-11-16): ``hyprial gui`` never installs or
re-homes an OS service.  The ensure hook passes ``autoinstall=False``, so the
Z-8c auto-install path is closed here; an already installed attributable unit
still launches the daemon through the service manager.  Installing stays with
``hyprial init`` / ``hyprial daemon service install`` / explicit CLI paths.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any
from pathlib import Path
import typer

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.common.support import (
    DAEMON_START_IDLE_BUDGET_SECONDS,
)
from hyprial.shell.impl.cli.output import emit_value, progress

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4173


def _backend_waiter(*, json_output: bool) -> Callable[[], None]:
    """The launcher's slow-start notice, announced on stderr."""

    def announce() -> None:
        progress(
            "gui-backend-wait",
            "Still starting the GUI; a slow or busy machine can take up to a minute...",
            json_output=json_output,
        )

    return announce


def _host_exit_reporter(*, json_output: bool) -> Callable[[int, str], None]:
    """The launcher's explanation of a host that ended on its own, on stderr."""

    def explain(exit_code: int, log_tail: str) -> None:
        message = f"The GUI host exited unexpectedly with exit code {exit_code}."
        if log_tail.strip():
            message += f" Last host output (redacted):\n{log_tail.rstrip()}"
        progress(
            "gui-host-exited",
            message,
            json_output=json_output,
            exit_code=exit_code,
            log_tail=log_tail,
        )

    return explain


def _daemon_starter(services: Any, *, json_output: bool) -> Callable[[], Any]:
    """The launch primitive as the launcher's ensure hook, announced on stderr."""

    def start() -> Any:
        progress(
            "gui-daemon-ensure",
            "Making sure the daemon for this home is running (this can take a while)...",
            json_output=json_output,
        )
        return services._launch_daemon_process(
            ready_timeout=DAEMON_START_IDLE_BUDGET_SECONDS,
            autoinstall=False,
        )

    return start


@app.command("gui")
def gui_command(
    action: str = typer.Argument(
        "",
        metavar="[dev]",
        help="Omit for the built GUI; `dev` is an alias for --with-dev-tools --live-source.",
    ),
    ip: str = typer.Option(
        DEFAULT_HOST,
        "--ip",
        help="Frontend bind address (IP or localhost; default loopback).",
    ),
    port: int = typer.Option(
        DEFAULT_PORT,
        "--port",
        min=0,
        max=65535,
        help=(
            "Frontend port; 0 picks a free loopback port and the startup record "
            "names it; conflicts fail instead of falling back."
        ),
    ),
    no_open: bool = typer.Option(
        False,
        "--no-open",
        help="Print the GUI URL without opening a browser.",
    ),
    gui_home: Path | None = typer.Option(
        None,
        "--gui-home",
        help=(
            "Live source only (`--live-source` or `dev`): the gui/ project directory "
            "(with package.json) inside a Git worktree; overrides HYPRIAL_DEV_GUI_HOME. "
            "The built GUI rejects this option."
        ),
    ),
    with_dev_tools: bool = typer.Option(
        False,
        "--with-dev-tools",
        help=(
            "Serve the developer tool layer (module index, data source, and the "
            "API docs page when the daemon driver is used); `dev` implies it."
        ),
    ),
    live_source: bool = typer.Option(
        False,
        "--live-source",
        help=(
            "Serve the GUI source through the Vite dev server behind the host, so "
            "edits reload in the browser; needs Git, Node and npm. `dev` implies it."
        ),
    ),
    stdin_stop: bool = typer.Option(
        False,
        "--stdin-stop",
        help=(
            "Stop cleanly (exit 0) when standard input reaches EOF or a "
            "`shutdown` line arrives; stdin is ignored without it."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit one startup JSON record."),
) -> None:
    """Start the GUI and wait in the foreground.

    One host process serves the GUI and the same-origin session, and with the
    daemon driver it relays requests to the daemon in-process (request-http
    v1), so no separate backend process is started; the explicit contract-mock
    driver instead runs its own Node child.  The daemon is the only long-lived
    backend and keeps running when the GUI is closed.  The installed
    wheel serves its built-in verified assets; a source checkout builds the GUI
    and serves ``gui/dist``, and ``--live-source`` puts the Vite development
    server behind the host instead.  ``--with-dev-tools`` adds the developer
    tool layer to either; ``dev`` is both flags.  The backend driver defaults
    to the daemon.  Retired verbs are rejected.
    """
    services = get_services()

    def operation() -> Any:
        from hyprial.shell.impl.gui import launcher, termination

        session: launcher.GuiSession | None = None

        def remember_session(started: launcher.GuiSession) -> None:
            nonlocal session
            session = started

        return_code = 0
        try:
            with termination.termination_guard(stdin_stop=stdin_stop) as guard:
                try:
                    plan = launcher.build_plan(
                        action,
                        ip=ip,
                        port=port,
                        gui_home=gui_home,
                        no_open=no_open,
                        with_dev_tools=with_dev_tools,
                        live_source=live_source,
                        ensure_daemon=_daemon_starter(services, json_output=json_output),
                    )
                    session = launcher.start_session(
                        plan,
                        json_output=json_output,
                        on_started=remember_session,
                        on_backend_wait=_backend_waiter(json_output=json_output),
                        on_host_exit=_host_exit_reporter(json_output=json_output),
                    )
                    payload = launcher.startup_payload(session)
                    emit_value(
                        payload if json_output else launcher.render_startup(payload),
                        json_output=json_output,
                    )
                    # stdout is often redirected or piped while the foreground
                    # wait runs; without an explicit flush the startup URL stays
                    # buffered.
                    sys.stdout.flush()
                    return_code = session.wait()
                except KeyboardInterrupt:
                    return_code = 130
                finally:
                    guard.seal()
                    if session is not None:
                        session.stop()
        except termination.GuiTerminationRequested as stop:
            return_code = 0 if stop.clean else termination.SIGTERM_EXIT_CODE
        raise typer.Exit(code=return_code)

    services.runtime.execute(
        operation,
        json_output=json_output,
        allow_missing_home=True,
    )
