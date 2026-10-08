"""Thin ``hyprial org work`` client for daemon-owned work items."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import typer

from hyprial.biz import worker_binding
from hyprial.identity import PacError
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import _local_operator_identity
from hyprial.shell.impl.cli.output import CliResult, render_generic

work_app = typer.Typer(help="Create and update durable work items in OrgFS.")


def _caller_identity() -> dict[str, str]:
    try:
        binding = worker_binding()
    except PacError as error:
        raise get_services().CliError(error.code, str(error), error.data) from error
    return (
        {"actor": binding[0], "sessionRef": binding[1]}
        if binding is not None
        else {"actor": _local_operator_identity()}
    )


def _run(
    method: str,
    params: Callable[[], Mapping[str, Any]],
    *,
    json_output: bool,
) -> None:
    def operation() -> CliResult:
        identity = _caller_identity()
        result = get_services()._daemon_request(method, {**params(), **identity})
        if not isinstance(result, dict):
            raise get_services().CliError(
                "INVALID_RESPONSE", f"{method} returned a non-object"
            )
        return CliResult(result, render=render_generic)

    get_services()._execute(operation, json_output=json_output)


def _association_request(_json_output: bool) -> Callable[[str, Mapping[str, Any]], Any]:
    """Build the daemon request used by the work-item association reader."""

    identity = _caller_identity()

    def request(method: str, params: Mapping[str, Any]) -> Any:
        extra = identity if method.startswith("workflow.") else {}
        result = get_services()._daemon_request(method, {**params, **extra})
        if not isinstance(result, dict):
            raise get_services().CliError(
                "INVALID_RESPONSE", f"{method} returned a non-object"
            )
        return result

    return request


def _org_space(explicit: str | None) -> str:
    result = get_services()._daemon_request("org.list", {})
    rows = result.get("orgs") if isinstance(result, Mapping) else None
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise get_services().CliError(
            "INVALID_RESPONSE", "org.list must return an orgs list"
        )
    memberships: dict[str, str] = {}
    for row in rows:
        org = row.get("org")
        space_id = row.get("spaceId")
        if not isinstance(org, str) or not org or not isinstance(space_id, str) or not space_id:
            raise get_services().CliError(
                "INVALID_RESPONSE", "org.list memberships must name an org and spaceId"
            )
        memberships[org] = space_id
    candidates = sorted(memberships)
    if explicit is not None:
        selected = explicit.strip()
        if not selected or selected not in memberships:
            raise get_services().CliError(
                "WORK_ORG_UNKNOWN",
                f"unknown work organization {explicit!r}; candidates: "
                + (", ".join(candidates) if candidates else "none"),
                {"org": explicit, "candidates": candidates},
            )
        return memberships[selected]
    if len(candidates) != 1:
        raise get_services().CliError(
            "WORK_ORG_REQUIRED",
            "--org is required when the node belongs to "
            + ("no organizations" if not candidates else "several organizations: " + ", ".join(candidates)),
            {"candidates": candidates},
        )
    return memberships[candidates[0]]


@work_app.command("add")
def work_add(
    title: str = typer.Argument(..., help="Work item title."),
    owner: str | None = typer.Option(None, "--owner", help="Accepting user or agent URI."),
    assignee: str | None = typer.Option(None, "--assignee", help="Current assignee URI."),
    item: list[str] = typer.Option([], "--item", help="Checklist text; repeatable."),
    depends: list[str] = typer.Option([], "--depends", help="Dependency id; repeatable."),
    acceptance: str = typer.Option("", "--acceptance", help="Acceptance criteria."),
    org: str | None = typer.Option(None, "--org", help="Organization name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Create a todo work item with a stable owner-scoped id."""

    _run(
        "work.add",
        lambda: {
            "spaceId": _org_space(org),
            "title": title,
            **({"owner": owner} if owner is not None else {}),
            **({"assignee": assignee} if assignee is not None else {}),
            "checklist": item,
            "depends": depends,
            "acceptance": acceptance,
        },
        json_output=json_output,
    )


@work_app.command("check")
def work_check(
    item_id: str = typer.Argument(..., help="Work item id."),
    number: int = typer.Argument(..., min=1, help="One-based checklist item number."),
    org: str | None = typer.Option(None, "--org", help="Organization name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Tick one checklist item."""

    _run(
        "work.check",
        lambda: {"spaceId": _org_space(org), "itemId": item_id, "number": number},
        json_output=json_output,
    )


@work_app.command("note")
def work_note(
    item_id: str = typer.Argument(..., help="Work item id."),
    text: str = typer.Argument(..., help="One-line log note."),
    org: str | None = typer.Option(None, "--org", help="Organization name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Append one attributed UTC line to the work item log."""

    _run(
        "work.note",
        lambda: {"spaceId": _org_space(org), "itemId": item_id, "text": text},
        json_output=json_output,
    )


@work_app.command("status")
def work_status(
    item_id: str = typer.Argument(..., help="Work item id."),
    status: str = typer.Argument(..., help="todo, doing, blocked, done, or dropped."),
    org: str | None = typer.Option(None, "--org", help="Organization name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Set a work item status; done applies the daemon acceptance gate."""

    _run(
        "work.status",
        lambda: {"spaceId": _org_space(org), "itemId": item_id, "status": status},
        json_output=json_output,
    )


@work_app.command("done")
def work_done(
    item_id: str = typer.Argument(..., help="Work item id."),
    org: str | None = typer.Option(None, "--org", help="Organization name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Mark a fully checked work item done as its verified owner."""

    _run(
        "work.done",
        lambda: {"spaceId": _org_space(org), "itemId": item_id},
        json_output=json_output,
    )


@work_app.command("repair")
def work_repair(
    item_id: str = typer.Argument(..., help="Work item id."),
    org: str | None = typer.Option(None, "--org", help="Organization name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Restore an invalid item from its newest validating OrgFS version."""

    _run(
        "work.repair",
        lambda: {"spaceId": _org_space(org), "itemId": item_id},
        json_output=json_output,
    )


@work_app.command("link")
def work_link(
    item_id: str = typer.Argument(..., help="Work item id."),
    graph_id: str = typer.Argument(..., help="Workflow graph id."),
    org: str | None = typer.Option(None, "--org", help="Organization name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Append a workflow graph association idempotently."""

    _run(
        "work.link",
        lambda: {"spaceId": _org_space(org), "itemId": item_id, "graphId": graph_id},
        json_output=json_output,
    )


@work_app.command("keywords")
def work_keywords(
    item_id: str = typer.Argument(..., help="Work item id."),
    add: list[str] = typer.Option([], "--add", help="Keyword to add; repeatable."),
    remove: list[str] = typer.Option([], "--remove", help="Keyword to remove; repeatable."),
    org: str | None = typer.Option(None, "--org", help="Organization name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Add and remove association keywords through daemon CAS."""

    _run(
        "work.keywords",
        lambda: {
            "spaceId": _org_space(org),
            "itemId": item_id,
            "add": add,
            "remove": remove,
        },
        json_output=json_output,
    )


@work_app.command("show")
def work_show(
    item_id: str = typer.Argument(..., help="Work item id."),
    copy: str | None = typer.Option(
        None, "--copy", help="Read one conflicted copy by id:<nodeId>."
    ),
    org: str | None = typer.Option(None, "--org", help="Organization name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show one work item, including invalid raw-written files.

    "verified" means completed through work.*, unless a process running as the
    daemon's OS user tampered with daemon state (LAX(same-uid)).
    """

    _run(
        "work.show",
        lambda: {
            "spaceId": _org_space(org),
            "itemId": item_id,
            **({"node": copy} if copy is not None else {}),
        },
        json_output=json_output,
    )


@work_app.command("resolve")
def work_resolve(
    item_id: str = typer.Argument(..., help="Conflicted work item id."),
    keep: str = typer.Option(..., "--keep", help="Copy to keep as id:<nodeId>."),
    org: str | None = typer.Option(None, "--org", help="Organization name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Split a name conflict without deleting either copy."""

    _run(
        "work.resolve",
        lambda: {"spaceId": _org_space(org), "itemId": item_id, "keep": keep},
        json_output=json_output,
    )


@work_app.command("ls")
def work_list(
    status: str | None = typer.Option(None, "--status", help="Filter by status."),
    owner: str | None = typer.Option(None, "--owner", help="Filter by owner URI."),
    assignee: str | None = typer.Option(None, "--assignee", help="Filter by assignee URI."),
    invalid: bool = typer.Option(False, "--invalid", help="List only invalid work-item files."),
    org: str | None = typer.Option(None, "--org", help="Organization name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List valid work items and surface invalid raw-written files."""

    _run(
        "work.ls",
        lambda: {
            "spaceId": _org_space(org),
            **({"status": status} if status is not None else {}),
            **({"owner": owner} if owner is not None else {}),
            **({"assignee": assignee} if assignee is not None else {}),
            "invalid": invalid,
        },
        json_output=json_output,
    )


@work_app.command("collect")
def work_collect(
    routine: str = typer.Option(..., "--routine", help="Name of the routine running this round."),
    graph: str = typer.Option(..., "--graph", help="This round's own graph ID."),
    window_days: int = typer.Option(
        7, "--window-days", min=1, max=60, help="Consider graphs created within this many days."
    ),
    org: str | None = typer.Option(None, "--org", help="Organization name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Collect new graphs and candidate work items for one association round."""

    import time as _time

    services = get_services()
    from hyprial.daemon import views_missions as missions

    def operation() -> CliResult:
        try:
            collected = missions.collect(
                _association_request(json_output),
                _org_space(org),
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
            ask = sum(1 for item in collected["graphs"] if item.get("jev"))
            return (
                f"{len(collected['graphs'])} new graph(s), {ask} to ask the jev, "
                f"{collected['deferred']} deferred to the next round, "
                f"{len(collected['unparseable'])} unparseable, "
                f"{len(collected['conflicts'])} name conflicts"
            )

        return CliResult({"ok": True, **collected}, render=render)

    services._execute(operation, json_output=json_output)


@work_app.command("finish")
def work_finish(
    collected_file: Path = typer.Option(
        ..., "--collected", help="`org work collect --json` output."
    ),
    replies_file: Path = typer.Option(
        ..., "--replies", help="JSON object: graphId -> the jev's reply (text or object)."
    ),
    declined: list[str] = typer.Option(
        [], "--declined", help="A graph the owner said belongs to no work item (repeatable)."
    ),
    org: str | None = typer.Option(None, "--org", help="Organization name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Finish an association round and produce its durable output state."""

    services = get_services()
    from hyprial.daemon import views_missions as missions

    def operation() -> CliResult:
        _org_space(org)
        try:
            collected = json.loads(collected_file.read_text(encoding="utf-8"))
            replies = json.loads(replies_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise services.CliError("INVALID_INPUT", str(error)) from error
        if not isinstance(collected, dict) or not isinstance(replies, dict):
            raise services.CliError(
                "INVALID_INPUT", "--collected and --replies must hold JSON objects"
            )
        finished = missions.finish(collected, replies, declined=declined)
        return CliResult(
            {"ok": True, **finished},
            render=lambda _data: json.dumps(finished, ensure_ascii=False, indent=2),
        )

    services._execute(operation, json_output=json_output)
