"""The ``hyprial ps`` process inspection command."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from hyprial.kernel import ipc_errors
import typer

from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.common.support import JsonObject
@app.command("ps")
def process_status(
    connector_kind: str | None = typer.Argument(None, help="Optional connector kind."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    refresh: bool = typer.Option(
        False, "--refresh", help="Queue an asynchronous status refresh."
    ),
    refresh_operation_id: str | None = typer.Option(
        None,
        "--refresh-operation-id",
        help="Look up one recent asynchronous status refresh.",
    ),
) -> None:
    """Show daemon and connector status."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        try:
            params: JsonObject = {}
            if refresh:
                params["refresh"] = True
            if refresh_operation_id is not None:
                params["refreshOperationId"] = refresh_operation_id
            result = services._daemon_request("ps", params, restore_wait=0.0)
        # PR #332 F4②: both projections branch on the registered transient
        # classes instead of comparing code strings.  Other transient
        # failures (disconnected / timeout) surface to the CLI error
        # boundary, as the old ``!= DAEMON_RESTORING``/``!=
        # DAEMON_UNAVAILABLE`` raises did.
        except ipc_errors.DaemonRestoringError:
            # The daemon is up and answering but restore has not
            # finished: answer from the readiness probe instead of
            # hanging until ps is served.  `phase`/"restorePending" are
            # the restoring signal; connectors stay empty because the
            # fleet table is exactly what is still being built.
            probe = services._daemon_probe(timeout=2.0)
            return {
                "ok": True,
                "restoring": True,
                "daemon": {
                    key: probe[key]
                    for key in (
                        "running",
                        "pid",
                        "epoch",
                        "nodeId",
                        "owner",
                        "socket",
                        "phase",
                    )
                    if key in probe
                },
                "restorePending": probe.get("restorePending", True),
                "connectors": [],
            }
        except ipc_errors.DaemonUnavailableError:
            return {"ok": True, "daemon": {"running": False}, "connectors": []}
        if not isinstance(result, dict):
            raise services.CliError("INVALID_RESPONSE", "daemon ps result must be an object")
        connectors = result.get("connectors", [])
        if connector_kind is not None and isinstance(connectors, list):
            connectors = [
                item
                for item in connectors
                if isinstance(item, dict) and item.get("runtime") == connector_kind
            ]
        return {"ok": True, **result, "connectors": connectors}

    services._execute(operation, json_output=json_output)
