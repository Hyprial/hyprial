"""``hyprial agent`` administration command modules."""

from __future__ import annotations


import typer

from hyprial.kernel import DurationParseError, ipc_errors, parse_duration
from hyprial.shell.impl.cli.commands.common.services import get_services

from hyprial.shell.impl.cli.commands.agent.admin import agent_app


@agent_app.command("restore")
def agent_restore(
    name: str | None = typer.Argument(
        None, help="Agent name; required with a policy (active, always, or never)."
    ),
    policy: str | None = typer.Argument(
        None, help="Restart restore policy: active, always, or never."
    ),
    threshold: str | None = typer.Option(
        None,
        "--threshold",
        help="Set the durable global restart idle threshold, such as 12h.",
    ),
    list_: bool = typer.Option(
        False, "--list", help="List every agent's restore policy and the threshold."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Read or set the single durable restart-restore policy store.

    Three shapes share one store: ``agent restore <NAME> active|always|never``
    sets one agent's policy, ``--threshold <DURATION>`` sets the global idle
    threshold, and ``--list`` reads both back.
    """
    services = get_services()

    if list_:
        if name is not None or policy is not None or threshold is not None:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--list cannot be combined with a name, policy, or --threshold",
            )
        services._execute(
            lambda: services._daemon_request("agent.restore-policy", {"list": True}),
            json_output=json_output,
        )
        return
    if threshold is not None:
        if name is not None or policy is not None:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--threshold cannot be combined with a name or policy",
            )
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
        return
    if name is None or policy is None:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            "agent restore needs <NAME> <POLICY>, --threshold <DURATION>, or --list",
        )
    services._execute(
        lambda: services._daemon_request(
            "agent.restore-policy", {"name": name, "policy": policy}
        ),
        json_output=json_output,
    )
