"""PAC workflow CLI command module."""

from __future__ import annotations

import json
from typing import Any

import typer

from hyprial.identity import PacError
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.output import CliResult


from hyprial.shell.impl.cli.commands.workflow.missions import _missions_request
from hyprial.shell.impl.cli.commands.workflow.run import _identity, overview_app


def _overview_model(
    json_output: bool, window_days: int, missions_space: str | None = None
) -> tuple[dict[str, Any], Any]:
    """Collect this node's PAC data as the caller sees it, as a page model.

    With ``missions_space``, graphs listed in its mission files are grouped
    by mission; a space that cannot be read leaves the inferred lines alone
    and is reported as ``missionsError`` (not part of the published data).
    """

    import time as _time

    services = get_services()
    from hyprial.daemon import views_overview as overview

    try:
        identity = _identity()
    except PacError as error:
        raise services.CliError(error.code, str(error)) from error

    def request(method: str, params: Any) -> Any:
        result = services._daemon_request(method, {**params, **identity})
        if not isinstance(result, dict):
            raise services.CliError("INVALID_RESPONSE", f"{method} returned a non-object")
        return result

    status = services._daemon_request("ps", {})
    daemon = status.get("daemon") if isinstance(status, dict) else None
    node = daemon.get("nodeId") if isinstance(daemon, dict) else None
    if not isinstance(node, str) or not node:
        raise services.CliError("INVALID_RESPONSE", "daemon ps did not report a node id")
    snapshot = overview.collect(
        request, now_ms=int(_time.time() * 1000), window_days=window_days
    )
    loaded: list[dict[str, Any]] = []
    problem = None
    if missions_space:
        from hyprial.daemon import views_missions as missions

        try:
            found = missions.load_missions(_missions_request(json_output), missions_space)
            loaded = found["missions"]
            if found["unparseable"] or found["conflicts"]:
                problem = "needs repair: " + ", ".join(
                    [u["path"] for u in found["unparseable"]] + found["conflicts"]
                )
        except services.CliError as error:
            problem = str(error)
    model = overview.build_model(snapshot, node=node, missions=loaded)
    return ({**model, "missionsError": problem} if problem else model), overview

@overview_app.command("show")
def overview_show(
    window_days: int = typer.Option(
        7, "--window-days", min=1, max=60, help="Include graphs created within this many days."
    ),
    missions_space: str | None = typer.Option(
        None, "--missions", help="orgfs space holding missions/: group graphs by mission."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """Print this node's overview model (what publish would write)."""
    services = get_services()

    def operation():
        model, _ = _overview_model(json_output, window_days, missions_space)

        def render(_data: Any) -> str:
            runs = len(model["runs"])
            strips = sum(len(line["routines"]) for line in model["lines"])
            return (
                f"hyprial workflow overview   node {model['node']}: "
                f"{len(model['lines'])} task lines, {runs} graphs, {strips} routines, "
                f"{len(model['links'])} inferred links"
            )

        return CliResult({"ok": True, **model}, render=render)

    services._execute(operation, json_output=json_output)

@overview_app.command("publish")
def overview_publish(
    space_id: str = typer.Argument(..., help="orgfs space UUID to publish into."),
    path: str = typer.Option(
        "pac-overview", "--path", help="Folder in the space for index.html and nodes/."
    ),
    window_days: int = typer.Option(
        7, "--window-days", min=1, max=60, help="Include graphs created within this many days."
    ),
    missions_space: str | None = typer.Option(
        None, "--missions", help="orgfs space holding missions/: group graphs by mission."
    ),
    force: bool = typer.Option(
        False, "--force", help="Write even when nothing changed since the last publish."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """Write this node's data (and the page) into an orgfs space, if it changed.

    Open <checkout>/<path>/index.html from `hyprial fs checkout <space>` to view
    every publishing node together; the page reloads itself every minute.
    Publishing enables this node's read-only checkout of the space and leaves
    it on (`hyprial fs checkout <space> --disable` turns it off).  Every member
    who can write the space can put scripts on the page: publish only into a
    space whose writers you trust.
    """
    services = get_services()

    def operation():
        model, overview = _overview_model(json_output, window_days, missions_space)
        problem = model.pop("missionsError", None)

        def fs_request(method: str, params: Any) -> Any:
            result = services._daemon_request(method, dict(params))
            if not isinstance(result, dict):
                raise services.CliError("INVALID_RESPONSE", f"{method} returned a non-object")
            return result

        record_path = services._state_dir() / "pac-overview-publish.json"
        try:
            records = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            records = {}
        records = records if isinstance(records, dict) else {}
        # One record per destination: publishing the same space to another
        # --path must write there, not be skipped as unchanged.
        record_key = f"{space_id}:{path.strip('/')}"
        result = overview.publish(
            fs_request,
            space_id=space_id,
            base=path,
            model=model,
            last=records.get(record_key) or {},
            force=force,
        )
        records[record_key] = result.pop("keys")
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
        payload = {"ok": True, **result, **({"missionsError": problem} if problem else {})}

        def render(_data: Any) -> str:
            what = ", ".join(result["written"]) or "nothing (unchanged since the last publish)"
            lines = [
                f"published node {result['node']} to {space_id}: wrote {what}",
                f"page lists {len(result['nodes'])} node(s): {', '.join(result['nodes'])}",
            ]
            if result.get("unavailable"):
                lines.append(
                    "not yet on this node (their holders are offline): "
                    + ", ".join(result["unavailable"])
                )
            if problem:
                lines.append(f"missions: {problem} (unlisted graphs stay on inferred lines)")
            if result.get("page"):
                lines.append(f"open in a browser: file://{result['page']}")
            return "\n".join(lines)

        return CliResult(payload, render=render)

    services._execute(operation, json_output=json_output)
