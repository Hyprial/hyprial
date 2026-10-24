"""The ``hyprial message targets`` delivery-target overview command."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import CliResult

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from hyprial.kernel import ipc_errors
import typer

from hyprial.shell.impl.cli.commands.common.support import JsonObject
from hyprial.shell.impl.cli.commands.message import message_app
# Allen/R2 (B9): the CLI filter vocabulary drops ``channel_route``; configured
# outbound routes are managed through ``adapter route list``.  An unfiltered
# ``message targets`` may still surface channel-route rows from the daemon.
_TARGETS_KIND_OPTIONS = ("agent", "user")


def _render_targets(rows: list[JsonObject]) -> str:
    """Plain-text table in the ``hyprial agent top`` style: preamble, header, columns.

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

    A target is a delivery promise (Allen): connectors (``agent``) and owners
    with a completed squire profile (``user``).  Configured outbound
    ``channel_route`` entries are managed through ``adapter route list``.
    Nodes are network peers, not targets — see ``hyprial host list``.
    """
    services = get_services()

    def operation() -> Any:
        services = get_services()
        if kind is not None and kind not in _TARGETS_KIND_OPTIONS:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--kind must be agent or user",
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
