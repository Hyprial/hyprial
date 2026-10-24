"""The ``hyprial host`` machine command family."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import CliResult

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
import typer

from hyprial.shell.impl.cli.commands.common.support import JsonObject
from hyprial.shell.impl.cli.commands.host import host_app


def _render_host_list(rows: list[JsonObject]) -> str:
    """Node visibility table; hosts are not delivery targets."""

    headers = ["NODE", "STATUS"]
    cells: list[list[str]] = [
        [str(row.get("nodeId", "?")), str(row.get("status", "?"))] for row in rows
    ]
    widths = [
        max([len(headers[index]), *(len(row[index]) for row in cells)])
        for index in range(len(headers))
    ]
    lines = [f"hyprial host list   {len(rows)} nodes"]
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


@host_app.command("list")
def host_list(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    refresh: bool = typer.Option(
        False, "--refresh", help="Queue an asynchronous status refresh."
    ),
) -> None:
    """List nodes announced on the network (online status only).

    Hosts are not delivery targets: a node receives no actor messages.
    This view exists so operators keep node visibility now that targets
    lists delivery promises only.
    """
    services = get_services()

    def operation() -> Any:
        services = get_services()
        result = services._daemon_request(
            "hosts", {"refresh": True} if refresh else {}
        )
        if not isinstance(result, dict) or not isinstance(result.get("hosts"), list):
            raise services.CliError(
                "INVALID_RESPONSE", "daemon hosts result must contain an array"
            )
        return CliResult(
            {"ok": True, **result},
            render=lambda data: _render_host_list(data["hosts"]),
            # --json is pretty-printed (indent 2): the default table is for
            # humans, the JSON is for both.
            json_indent=2,
        )

    services._execute(operation, json_output=json_output)


def _reserved_host_command(command: str, *, json_output: bool) -> None:
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        raise services.CliError(
            "COMING_SOON", f"hyprial host {command} is coming soon"
        )

    services._execute(operation, json_output=json_output, allow_missing_home=True)


@host_app.command("status")
def host_status(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Reserved for the combined local host network status view."""

    _reserved_host_command("status", json_output=json_output)


@host_app.command("create")
def host_create(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Reserved for adding a local or cloud machine as a host."""

    _reserved_host_command("create", json_output=json_output)
