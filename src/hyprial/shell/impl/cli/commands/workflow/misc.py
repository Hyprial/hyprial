"""PAC workflow CLI command module."""

from __future__ import annotations


import typer

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject


from hyprial.shell.impl.cli.commands.workflow.run import workflow_app


@workflow_app.command("gc")
def workflow_gc(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show the next bounded removal set without changing state.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Preview reclaimed workflow-owned worker-agent removal."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        if not dry_run:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "PAC GC is daemon-owned; pass --dry-run to preview it",
            )
        return services._daemon_request("pac.gc.preview", {})

    services._execute(operation, json_output=json_output)
