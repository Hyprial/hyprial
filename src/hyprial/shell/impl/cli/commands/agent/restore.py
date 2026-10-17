"""``hyprial agent`` administration command modules."""

from __future__ import annotations


import typer

from hyprial.kernel import DurationParseError, ipc_errors, parse_duration
from hyprial.shell.impl.cli.commands.common.services import get_services

from hyprial.shell.impl.cli.commands.agent.admin import agent_app

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
