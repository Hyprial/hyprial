"""``hyprial targets`` and ``hyprial hosts`` overviews."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import CliResult

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from hyprial.kernel import ipc_errors
import typer

from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.common.support import JsonObject
_TARGETS_KIND_OPTIONS = ("agent", "user", "channel_route")


def _render_targets(rows: list[JsonObject]) -> str:
    """Plain-text table in the ``hyprial top`` style: preamble, header, columns.

    targets lists delivery promises only (Allen's definition), so every row
    is deliverable by construction; the KIND column stays because user and
    channel_route rows join once their delivery paths are verified.
    """

    headers = ["TARGET", "KIND", "STATUS", "hosted", "hostedBy"]
    cells: list[list[str]] = [
        [
            str(row.get("targetUri", "?")),
            str(row.get("targetKind", "?")),
            str(row.get("status", "?")),
            str(row["hosted"]).lower() if "hosted" in row else "-",
            str(row.get("hostedBy") or "-"),
        ]
        for row in rows
    ]
    widths = [
        max([len(headers[index]), *(len(row[index]) for row in cells)])
        for index in range(len(headers))
    ]
    lines = [f"hyprial targets   {len(rows)} targets"]
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
def targets(
    kind: str | None = typer.Option(
        None, "--kind", help="Filter: agent, user, or channel_route."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List live delivery targets.

    A target is a delivery promise (Allen): connectors (``agent``), owners
    with a completed squire profile (``user``), and configured outbound
    routes (``channel_route``).  Nodes are network peers, not targets —
    see ``hyprial hosts`` for node visibility.
    """
    services = get_services()

    def operation() -> Any:
        services = get_services()
        if kind is not None and kind not in _TARGETS_KIND_OPTIONS:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--kind must be agent, user, or channel_route",
            )
        params: JsonObject = {}
        if kind is not None:
            params["kind"] = kind
        result = services._daemon_request("targets", params)
        if not isinstance(result, dict) or not isinstance(result.get("targets"), list):
            raise services.CliError(
                "INVALID_RESPONSE", "daemon targets result must contain an array"
            )
        return CliResult(
            {"ok": True, **result},
            render=lambda data: _render_targets(data["targets"]),
            # --json is pretty-printed (indent 2): the default table is for
            # humans, the JSON is for both.
            json_indent=2,
        )

    services._execute(operation, json_output=json_output)


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
