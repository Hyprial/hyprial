"""Supported agent config-root rebind and receipt rollback commands."""

from __future__ import annotations

from pathlib import Path

import typer

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.agent.admin import config_app
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import CliError


@config_app.command("rebind")
def agent_config_rebind(
    name: str = typer.Argument(..., help="Existing registered P2 agent name."),
    source: Path | None = typer.Option(
        None,
        "--source",
        help="Absolute private staging directory containing the complete new C tree.",
    ),
    rollback: str | None = typer.Option(
        None,
        "--rollback",
        help="Receipt id whose exact pre-rebind config must be restored.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Validate and report hashes without writing state."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Atomically publish a validated config root or roll back its receipt."""

    services = get_services()

    def operation() -> dict[str, object]:
        if (source is None) == (rollback is None):
            raise CliError(
                ipc_errors.INVALID_ARGUMENT,
                "exactly one of --source or --rollback is required",
            )
        if rollback is not None and dry_run:
            raise CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--dry-run cannot be combined with --rollback",
            )
        expanded_source = None if source is None else source.expanduser()
        if expanded_source is not None and not expanded_source.is_absolute():
            raise CliError(
                ipc_errors.AGENT_CONFIG_REBIND_INVALID_SOURCE,
                "--source must be an absolute path",
            )
        return services._daemon_request(
            "agent.config.rebind",
            {
                "name": name,
                **(
                    {"source": str(expanded_source)}
                    if expanded_source is not None
                    else {"rollbackReceiptId": rollback}
                ),
                **({"dryRun": True} if dry_run else {}),
            },
        )

    services._execute(operation, json_output=json_output)
