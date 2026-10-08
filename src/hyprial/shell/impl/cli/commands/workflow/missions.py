"""PAC workflow CLI command module."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from hyprial.identity import PacError
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.output import CliResult


from hyprial.shell.impl.cli.commands.workflow.run import _identity, missions_app


def _missions_request(json_output: bool) -> Any:
    """Daemon requests as the caller: workflow reads carry its identity."""

    services = get_services()

    try:
        identity = _identity()
    except PacError as error:
        raise services.CliError(error.code, str(error)) from error

    def request(method: str, params: Any) -> Any:
        extra = identity if method.startswith("workflow.") else {}
        result = services._daemon_request(method, {**params, **extra})
        if not isinstance(result, dict):
            raise services.CliError("INVALID_RESPONSE", f"{method} returned a non-object")
        return result

    return request

@missions_app.command("collect")
def missions_collect(
    space_id: str = typer.Argument(..., help="orgfs space UUID holding missions/."),
    routine: str = typer.Option(..., "--routine", help="Name of the routine running this round."),
    graph: str = typer.Option(..., "--graph", help="This round's own graph ID."),
    window_days: int = typer.Option(
        7, "--window-days", min=1, max=60, help="Consider graphs created within this many days."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """Step 1 of a round: new graphs, their candidate missions and jev payloads.

    Send each graph's `jev` payload (as the whole message body) to the jev
    with `hyprial send`, then pass the replies to `missions finish`.  Exits
    with PREVIOUS_ROUND_UNREADABLE when the last round's state cannot be read
    (for example the routine changed actor): fail the node, do not re-ask.
    When an earlier round is still running it prints `skipped` and a `state`
    to complete this round with, which later rounds pass over.
    """
    import time as _time

    services = get_services()
    from hyprial.daemon import views_missions as missions

    def operation():
        try:
            collected = missions.collect(
                _missions_request(json_output),
                space_id,
                routine=routine,
                current=graph,
                now_ms=int(_time.time() * 1000),
                window_days=window_days,
            )
        except missions.PreviousRoundUnreadable as error:
            raise services.CliError("PREVIOUS_ROUND_UNREADABLE", str(error)) from error

        def render(_data: Any) -> str:
            if collected.get("skipped"):
                running = ", ".join(collected["skipped"]["running"])
                return (
                    f"skipped: an earlier round is still running ({running}); "
                    f"complete with --output-text '{json.dumps(collected['state'])}'"
                )
            ask = sum(1 for g in collected["graphs"] if g.get("jev"))
            return (
                f"{len(collected['graphs'])} new graph(s), {ask} to ask the jev, "
                f"{collected['deferred']} deferred to the next round, "
                f"{len(collected['unparseable'])} unparseable, {len(collected['conflicts'])} name conflicts"
            )

        return CliResult({"ok": True, **collected}, render=render)

    services._execute(operation, json_output=json_output)

@missions_app.command("finish")
def missions_finish(
    collected_file: Path = typer.Option(..., "--collected", help="`missions collect --json` output."),
    replies_file: Path = typer.Option(
        ..., "--replies", help="JSON object: graphId -> the jev's reply (text or object)."
    ),
    declined: list[str] = typer.Option(
        [], "--declined", help="A graph the owner said belongs to no mission (repeatable)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """Step 3: one result per graph, and the state to complete the node with.

    Link the results that meet your auto-link rule, ask the owner about the
    rest, then complete the node with `--output-text` set to the printed
    `state` (compact JSON).
    """
    services = get_services()
    from hyprial.daemon import views_missions as missions

    def operation():
        try:
            collected = json.loads(collected_file.read_text(encoding="utf-8"))
            replies = json.loads(replies_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise services.CliError("INVALID_INPUT", str(error)) from error
        if not isinstance(collected, dict) or not isinstance(replies, dict):
            raise services.CliError("INVALID_INPUT", "--collected and --replies must hold JSON objects")
        finished = missions.finish(collected, replies, declined=declined)

        def render(_data: Any) -> str:
            return json.dumps(finished, ensure_ascii=False, indent=2)

        return CliResult({"ok": True, **finished}, render=render)

    services._execute(operation, json_output=json_output)

@missions_app.command("check")
def missions_check(
    space_id: str = typer.Argument(..., help="orgfs space UUID holding missions/."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """List the missions, and the files a squire must repair (unparseable, name conflicts)."""
    services = get_services()
    from hyprial.daemon import views_missions as missions

    def operation():
        loaded = missions.load_missions(_missions_request(json_output), space_id)

        def render(_data: Any) -> str:
            lines = [f"{m['id']}  {m['status']:<6}  {m['title']}" for m in loaded["missions"]]
            lines += [f"unparseable: {u['path']} ({u['reason']})" for u in loaded["unparseable"]]
            lines += [f"name conflict: {path}" for path in loaded["conflicts"]]
            return "\n".join(lines) or "no missions"

        return CliResult({"ok": True, **loaded}, render=render)

    services._execute(operation, json_output=json_output)
