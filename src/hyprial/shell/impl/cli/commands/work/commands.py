"""Read shortcuts for ordinary documents beneath an organization's work/ folder."""

from __future__ import annotations

from collections.abc import Mapping

import typer

from hyprial.shell.impl.cli.commands.common.groups import group
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.fs.commands import _fs_run

work_app = group(
    help="Read the organization's work/ folder. Use org fs for writes, history, restore, and conflicts."
)


def _resolved_space(org: str | None) -> str:
    result = get_services().runtime.request("org.resolve", {} if org is None else {"org": org})
    space_id = result.get("spaceId") if isinstance(result, Mapping) else None
    if not isinstance(space_id, str) or not space_id:
        raise get_services().CliError("INVALID_RESPONSE", "org.resolve must return a spaceId")
    return space_id


@work_app.command("ls")
def work_list(
    org: str | None = typer.Option(None, "--org", help="Organization name; otherwise use the current or sole organization."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List work/ with the ordinary OrgFS listing."""

    _fs_run(
        "orgfs.ls",
        lambda: {"spaceId": _resolved_space(org), "path": "work"},
        json_output=json_output,
    )


@work_app.command("show")
def work_show(
    path: str = typer.Argument(..., help="Exact relative path beneath work/; no extension is added."),
    wait: float = typer.Option(10.0, "--wait", min=0.0, max=120.0, help="Seconds to wait for verified content."),
    org: str | None = typer.Option(None, "--org", help="Organization name; otherwise use the current or sole organization."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Read the original file content and version through ordinary OrgFS."""

    _fs_run(
        "orgfs.read",
        lambda: {"spaceId": _resolved_space(org), "path": f"work/{path}", "waitSeconds": wait},
        json_output=json_output,
    )
