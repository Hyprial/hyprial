"""``hyprial service`` subcommands.

The daemon owns catalog, access, mapping, and durable state.  This module only
validates the small command surface, requests those operations over IPC, and
chooses a human renderer for the shared result.
"""

from __future__ import annotations

import typer
from hyprial.shell.impl.cli.commands.common.groups import group

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.output import CliResult

from .presentation import (
    render_connect,
    render_disconnect,
    render_guide,
    render_list,
)


service_app = group(
    help=(
        "Publish local services on the org network and connect to named "
        "services through the daemon."
    ),
)


@service_app.command("connect")
def connect(
    name: str = typer.Argument(..., help="Registered service name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Create or reuse a loopback mapping for one registered service."""
    services = get_services()

    def operation() -> CliResult:
        result = services.runtime.request("service.connect", {"name": name})
        return CliResult(result, render=render_connect)

    services.runtime.execute(operation, json_output=json_output)


@service_app.command("disconnect")
def disconnect(
    name: str = typer.Argument(..., help="Registered service name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Remove one service mapping and its durable connection intent."""
    services = get_services()

    def operation() -> CliResult:
        result = services.runtime.request("service.disconnect", {"name": name})
        return CliResult(result, render=render_disconnect)

    services.runtime.execute(operation, json_output=json_output)


@service_app.command("list")
def list_services(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List mappings, path observations, and safe registry diagnostics."""
    services = get_services()

    def operation() -> CliResult:
        result = services.runtime.request("service.list", {})
        return CliResult(result, render=render_list)

    services.runtime.execute(operation, json_output=json_output)


@service_app.command("guide")
def guide(
    name: str = typer.Argument(..., help="Registered service name."),
    full: bool = typer.Option(False, "--full", help="Show catalog fields and environment hints."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show the operator-provided usage guide for one service."""
    services = get_services()

    def operation() -> CliResult:
        result = services.runtime.request("service.guide", {"name": name})
        return CliResult(result, render=lambda data: render_guide(data, full=full))

    services.runtime.execute(operation, json_output=json_output)


__all__ = ["connect", "disconnect", "guide", "list_services", "service_app"]
