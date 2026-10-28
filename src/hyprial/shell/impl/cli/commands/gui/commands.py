"""``hyprial gui``: start the shared web GUI in the foreground."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

import sys
from typing import Any
from pathlib import Path
import typer

from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.output import emit_value
from hyprial.shell.impl.gui import launcher, termination


@app.command("gui")
def gui_command(
    action: str = typer.Argument(
        "",
        metavar="[dev]",
        help="Omit for the normal GUI, or `dev` for the Vite development server.",
    ),
    ip: str = typer.Option(
        launcher.DEFAULT_HOST,
        "--ip",
        help="Frontend bind address (IP or localhost; default loopback).",
    ),
    port: int = typer.Option(
        launcher.DEFAULT_PORT,
        "--port",
        min=1,
        max=65535,
        help="Frontend port; conflicts fail instead of falling back.",
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
            "GUI npm project root (contains package.json), normally the "
            "gui/ directory of an independent Git worktree. Overrides "
            "HYPRIAL_GUI_HOME."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit one startup JSON record."),
) -> None:
    """Start the shared GUI from its npm project and wait in the foreground.

    Bare ``hyprial gui`` runs ``npm start``; ``hyprial gui dev`` runs
    ``npm run dev`` with Vite hot reload.  Both use the same GUI source and the
    same independent Node placeholder service.  Retired verbs are rejected.
    """
    services = get_services()

    def operation() -> Any:
        session: launcher.GuiSession | None = None

        def remember_session(started: launcher.GuiSession) -> None:
            nonlocal session
            session = started

        return_code = 0
        # Discovery, worktree creation, and npm install are part of startup;
        # install SIGTERM/SIGINT handling before them so those paths also exit
        # 143/130.  A plan failure before a session exists still propagates
        # normally, and the finally block only reclaims what was recorded.
        try:
            with termination.termination_guard():
                try:
                    plan = launcher.build_plan(
                        action,
                        ip=ip,
                        port=port,
                        gui_home=gui_home,
                        no_open=no_open,
                    )
                    session = launcher.start_session(
                        plan,
                        json_output=json_output,
                        on_started=remember_session,
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
                    if session is not None:
                        session.stop()
        except termination.GuiTerminationRequested:
            # SIGTERM uses the same finally-reclaimed process groups as Ctrl-C,
            # but keeps the shell's conventional 128 + signal status.
            return_code = termination.SIGTERM_EXIT_CODE
        raise typer.Exit(code=return_code)

    services._execute(operation, json_output=json_output, allow_missing_home=True)


def gui_status(hyprial_home: Path, name: str) -> dict[str, Any]:
    from hyprial.shell.impl.gui.runtime import gui_status as operation

    return operation(hyprial_home, name)


def start_gui_background(hyprial_home: Path, name: str) -> dict[str, Any]:
    from hyprial.shell.impl.gui.runtime import start_gui_background as operation

    return operation(hyprial_home, name)


def stop_gui(hyprial_home: Path, name: str) -> dict[str, Any]:
    from hyprial.shell.impl.gui.runtime import stop_gui as operation

    return operation(hyprial_home, name)
