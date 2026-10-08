"""``hyprial agent`` administration command modules."""

from __future__ import annotations


import typer

from hyprial.kernel import DurationParseError, ipc_errors, parse_duration
from hyprial.shell.impl.cli.commands.common.services import get_services

from hyprial.shell.impl.cli.commands.agent.admin import agent_app, agent_keep_app


@agent_keep_app.command("add")
def agent_keep_add(
    name: str = typer.Argument(..., help="Agent name to protect."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Add an agent to the inactivity keep-list, idempotently."""
    services = get_services()

    services._execute(
        lambda: services._daemon_request("agent.keep.add", {"name": name}),
        json_output=json_output,
    )

@agent_keep_app.command("remove")
def agent_keep_remove(
    name: str = typer.Argument(..., help="Agent name to unprotect."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Remove an agent from the inactivity keep-list, idempotently."""
    services = get_services()

    services._execute(
        lambda: services._daemon_request("agent.keep.remove", {"name": name}),
        json_output=json_output,
    )

@agent_keep_app.command("list")
def agent_keep_list(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List agents protected from inactivity reports."""
    services = get_services()

    services._execute(
        lambda: services._daemon_request("agent.keep.list"),
        json_output=json_output,
    )

@agent_app.command("restore-policy")
def agent_restore_policy(
    name: str = typer.Argument(..., help="Agent name."),
    policy: str = typer.Argument(..., help="active, always, or never."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Set the durable per-agent restart restore policy."""
    services = get_services()

    services._execute(
        lambda: services._daemon_request(
            "agent.restore-policy", {"name": name, "policy": policy}
        ),
        json_output=json_output,
    )

@agent_app.command("restore-threshold")
def agent_restore_threshold(
    threshold: str = typer.Argument(..., help="Restart threshold, such as 12h."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Set the durable global restart idle threshold."""
    services = get_services()

    try:
        threshold_ms = int(parse_duration(threshold, "threshold") * 1_000)
    except DurationParseError as error:
        raise services.CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
    services._execute(
        lambda: services._daemon_request(
            "agent.restore-threshold", {"thresholdMs": threshold_ms}
        ),
        json_output=json_output,
    )
