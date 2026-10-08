"""``hyprial service`` subcommands.

The daemon owns catalog, access, mapping, and durable state.  This module only
validates the small command surface, requests those operations over IPC, and
chooses a human renderer for the shared result.
"""

from __future__ import annotations

import typer

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.output import CliResult

from .presentation import (
    render_connect,
    render_disconnect,
    render_guide,
    render_list,
)


service_app = typer.Typer(
    help="Connect to named TCP services through the daemon.",
    no_args_is_help=False,
)


@service_app.callback(invoke_without_command=True)
def service(ctx: typer.Context) -> None:
    """Show service help when no subcommand is selected."""
    if ctx.invoked_subcommand is not None:
        return
    services = get_services()
    services._execute(
        lambda: CliResult({"ok": True}, render=lambda _data: ctx.get_help()),
        json_output=False,
        allow_missing_home=True,
    )


@service_app.command("connect")
def connect(
    name: str = typer.Argument(..., help="Registered service name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Create or reuse a loopback mapping for one registered service."""
    services = get_services()

    def operation() -> CliResult:
        result = services._daemon_request("service.connect", {"name": name})
        return CliResult(result, render=render_connect)

    services._execute(operation, json_output=json_output)


@service_app.command("disconnect")
def disconnect(
    name: str = typer.Argument(..., help="Registered service name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Remove one service mapping and its durable connection intent."""
    services = get_services()

    def operation() -> CliResult:
        result = services._daemon_request("service.disconnect", {"name": name})
        return CliResult(result, render=render_disconnect)

    services._execute(operation, json_output=json_output)


@service_app.command("list")
def list_services(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List mappings, path observations, and safe registry diagnostics."""
    services = get_services()

    def operation() -> CliResult:
        result = services._daemon_request("service.list", {})
        return CliResult(result, render=render_list)

    services._execute(operation, json_output=json_output)


@service_app.command("guide")
def guide(
    name: str = typer.Argument(..., help="Registered service name."),
    full: bool = typer.Option(False, "--full", help="Show catalog fields and environment hints."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show the operator-provided usage guide for one service."""
    services = get_services()

    def operation() -> CliResult:
        result = services._daemon_request("service.guide", {"name": name})
        return CliResult(result, render=lambda data: render_guide(data, full=full))

    services._execute(operation, json_output=json_output)


__all__ = ["connect", "disconnect", "guide", "list_services", "service", "service_app"]
