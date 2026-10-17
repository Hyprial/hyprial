"""The ``hyprial hosts`` node-visibility overview command."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import CliResult

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
import typer

from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.common.support import JsonObject


def _render_hosts(rows: list[JsonObject]) -> str:
    """Node visibility table; hosts are not delivery targets."""

    headers = ["NODE", "STATUS"]
    cells: list[list[str]] = [
        [str(row.get("nodeId", "?")), str(row.get("status", "?"))] for row in rows
    ]
    widths = [
        max([len(headers[index]), *(len(row[index]) for row in cells)])
        for index in range(len(headers))
    ]
    lines = [f"hyprial hosts   {len(rows)} nodes"]
    lines.append(
        "  ".join(
            header.ljust(widths[index]) for index, header in enumerate(headers)
        ).rstrip()
    )
    for row in cells:
        lines.append(
            "  ".join(
                cell.ljust(widths[index]) for index, cell in enumerate(row)
            ).rstrip()
        )
    return "\n".join(lines)


@app.command()
def hosts(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List nodes announced on the network (online status only).

    Hosts are not delivery targets: a node receives no actor messages.
    This view exists so operators keep node visibility now that targets
    lists delivery promises only.
    """
    services = get_services()

    def operation() -> Any:
        services = get_services()
        result = services._daemon_request("hosts", {})
        if not isinstance(result, dict) or not isinstance(result.get("hosts"), list):
            raise services.CliError(
                "INVALID_RESPONSE", "daemon hosts result must contain an array"
            )
        return CliResult(
            {"ok": True, **result},
            render=lambda data: _render_hosts(data["hosts"]),
            # --json is pretty-printed (indent 2): the default table is for
            # humans, the JSON is for both.
            json_indent=2,
        )

    services._execute(operation, json_output=json_output)
