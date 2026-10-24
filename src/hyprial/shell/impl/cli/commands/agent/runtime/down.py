"""``hyprial agent stop`` - stop one agent, or every agent on this machine."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from hyprial.kernel import LIFECYCLE_IPC_MARGIN_SECONDS, LIFECYCLE_OPERATION_DEADLINE_SECONDS, LIFECYCLE_WAIT_MARGIN_SECONDS
from hyprial.kernel import ipc_errors
import typer

from hyprial.shell.impl.cli.commands.agent.admin import agent_app


@agent_app.command("stop")
def agent_stop(
    name: str | None = typer.Argument(
        None, help="Agent name to stop."
    ),
    all_agents: bool = typer.Option(
        False,
        "--all",
        help=(
            "Stop and permanently deregister every daemon-managed harness "
            "connector (from 'hyprial agent start --headless') and every "
            "started agent runtime. Adapters are NOT touched: stop an adapter "
            "with 'hyprial adapter stop'. Interactive Claude sessions "
            "('hyprial agent start claude --name ...' without --headless) "
            "keep running."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Stop one agent, or every agent on this machine with --all.

    This is permanent removal from desired state, not a pause: a stopped
    agent runtime will not come back on daemon restart until it is started
    again.

    Only agents are stopped. Adapters (including the Lark adapter) are
    managed separately by 'hyprial adapter stop'; 'hyprial agent stop' never
    accepts an adapter name. --all does not reach interactive Claude
    sessions started via 'hyprial agent start claude --name ...' (no
    --headless); those are tracked separately and are never stopped by this
    command. Use 'hyprial agent list' to see agents and their real running
    status.
    """
    services = get_services()

    def operation() -> Any:
        services = get_services()
        if all_agents and name is not None:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--all cannot be combined with an agent name",
            )
        if not all_agents and name is None:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "agent stop requires an agent name or --all",
            )
        params: dict[str, Any] = {"all": all_agents, "agentsOnly": True}
        if name is not None:
            params["target"] = name
        # Outlast the daemon-side lifecycle wait (deadline + wait margin) so a
        # stuck operation returns its coded failure here instead of an
        # IPC_TIMEOUT (card 104164aa (c)).
        return services._daemon_request(
            "down",
            params,
            timeout=(
                LIFECYCLE_OPERATION_DEADLINE_SECONDS
                + LIFECYCLE_WAIT_MARGIN_SECONDS
                + LIFECYCLE_IPC_MARGIN_SECONDS
            ),
        )

    services._execute(operation, json_output=json_output)
