"""The ``hyprial message targets`` delivery-target overview command.

Decision notes (I4b, 2026-11-24): the daemon no longer lists
``channel_route`` rows; the filter vocabulary stays agent and user.
"""

from __future__ import annotations

from hyprial.shell.impl.cli.output import CliResult

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from hyprial.error import codes
import typer

from hyprial.shell.impl.cli.commands.common.support import JsonObject
from hyprial.shell.impl.cli.commands.message import message_app
_TARGETS_KIND_OPTIONS = ("agent", "user")


def _render_targets(rows: list[JsonObject]) -> str:
    """Plain-text table in the ``hyprial agent top`` style: preamble, header, columns.

    targets lists delivery promises only (Allen's definition), so every row
    is deliverable by construction; the KIND column tells agents from users.
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
    lines = [f"hyprial message targets   {len(rows)} targets"]
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


@message_app.command("targets")
def message_targets(
    kind: str | None = typer.Option(
        None, "--kind", help="Filter: agent or user."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List live delivery targets.

    A target is a delivery promise (Allen): connectors (``agent``) and the
    owner while a message to them is queued on this node (``user``).  Configured
    adapter routes are managed through ``adapter route list``.  Nodes are network
    peers, not targets — see ``hyprial host list``.
    """
    services = get_services()

    def operation() -> Any:
        services = get_services()
        if kind is not None and kind not in _TARGETS_KIND_OPTIONS:
            raise services.CliError(
                codes.INVALID_ARGUMENT,
                "--kind must be agent or user",
            )
        params: JsonObject = {}
        if kind is not None:
            params["kind"] = kind
        result = services.runtime.request("targets", params)
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

    services.runtime.execute(operation, json_output=json_output)
