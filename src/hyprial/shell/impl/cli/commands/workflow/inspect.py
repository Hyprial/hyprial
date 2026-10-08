"""PAC workflow CLI command module."""

from __future__ import annotations

from time import sleep
from typing import Any

import typer

from hyprial.biz import state_dir
from hyprial.identity import PacError
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.workflow.internal.expansion import (
    render_workflow_status,
)
from hyprial.shell.impl.cli.output import CliResult, CliStream, render_generic


from hyprial.shell.impl.cli.commands.workflow.run import (
    _call, _call_result, _identity, history_app, workflow_app,
)


@workflow_app.command("status")
def status(
    run_id: str = typer.Argument(..., help="PAC graph ID."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """Read graph state, nodes, requests and evidence references."""
    services = get_services()

    def operation() -> CliResult:
        result = _call_result("workflow.status", {"runId": run_id})
        return CliResult(result, render=render_workflow_status)

    services._execute(operation, json_output=json_output)

@workflow_app.command("list")
def list_workflows(
    limit: int = typer.Option(50, help="Maximum records to return."),
    all_callers: bool = typer.Option(
        False, "--all", help="List workflows from all callers on this node."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """List recent PAC workflows.

    PROGRESS / `lastProgressAtMs` is the newest graph journal event
    (a `workflow progress` heartbeat verb is planned, #1210, not yet available).
    CURRENT / `currentNode` is the first unfinished node in dependency order.
    """
    from hyprial.shell.impl.cli.commands.common.support import _render_workflow_list
    services = get_services()

    def operation():
        try:
            identity = _identity()
        except PacError as error:
            raise services.CliError(error.code, str(error)) from error
        result = services._daemon_request(
            "workflow.list",
            {
                "limit": limit,
                **({"all": True} if all_callers else {}),
                **identity,
            },
        )
        if not isinstance(result, dict) or not isinstance(result.get("runs"), list):
            raise services.CliError("INVALID_RESPONSE", "workflow.list must return runs")
        return CliResult(
            {"ok": True, **result},
            render=lambda data: _render_workflow_list(data["runs"], all_callers=all_callers),
        )

    services._execute(operation, json_output=json_output)

@workflow_app.command("inspect")
def inspect(
    run_id: str = typer.Argument(
        ..., help="PAC graph ID, or archived run ID for history."
    ),
    node: str | None = typer.Option(
        None, "--node", help="Inspect one graph node by ID."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """Inspect a graph or one node without changing it."""
    _call(
        "workflow.node.inspect" if node else "workflow.status",
        {"runId": run_id, **({"target": node} if node else {})},
        json_output=json_output,
    )

@workflow_app.command("cancel")
def cancel(
    run_id: str = typer.Argument(..., help="Graph ID, or archived run ID for history."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """Close the graph and reclaim its workers; borrowed actors remain owned externally."""
    _call("workflow.cancel", {"runId": run_id}, json_output=json_output)

@workflow_app.command("events")
def events(
    graph_id: str = typer.Argument(..., help="Graph to subscribe to."),
    after: int | None = typer.Option(None, "--after", help="Strict lower bound on journal seq."),
    snapshot: bool = typer.Option(False, "--snapshot", help="Bootstrap one consistent snapshot@cursor."),
    follow: bool = typer.Option(False, "--follow", help="Follow until interrupted or output closes."),
    journal_id: str | None = typer.Option(None, "--journal-id", help="Expected journal identity."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON lines."),
):
    """Read the stable workflow snapshot and event stream."""
    from hyprial.daemon import Projection
    from hyprial.daemon import events_since
    from hyprial.daemon import follow_lifetime

    def items():
        database = state_dir() / "pac-graph.sqlite3"
        journal = journal_id
        with follow_lifetime(follow) as output:
            cursor = after if after is not None else 0
            if snapshot:
                document = Projection(database).public_snapshot(graph_id)
                yield document
                if not follow:
                    return
                cursor, journal = document["cursor"], document["journalId"]
            upper = None
            while True:
                if output is not None and output.closed():
                    return
                page = events_since(database, graph_id, cursor, journal_id=journal, until=upper)
                journal, upper = page["journalId"], page["highWatermark"]
                yield from page["events"]
                cursor = page["cursor"]
                if cursor < upper:
                    continue
                if not follow:
                    return
                upper = None
                sleep(0.2)

    def operation() -> CliStream:
        if snapshot and after is not None:
            raise PacError("PAC_EVENTS_ARGUMENT", "--snapshot and --after are mutually exclusive")
        return CliStream(items(), render_item=_render_event)

    get_services()._execute(operation, json_output=json_output)

def _render_event(item: Any) -> str:
    if "seq" in item and "type" in item:
        return f"#{item['seq']} {item['type']} {item['graphId']}"
    return render_generic(item)

@workflow_app.command("context")
def context(
    graph_id: str = typer.Argument(..., help="Graph id."),
    node_id: str = typer.Argument(..., help="Node id."),
    json_output: bool = typer.Option(False, "--json", help="Emit stable context JSON."),
):
    """Read one node's immutable workflow context."""
    from hyprial.daemon import node_context

    def render(document: Any) -> str:
        activation = document["currentActivation"]
        return "\n".join([
            f"graph {graph_id} node {node_id} version {document['version']} cursor {document['cursor']}",
            f"brief: {document['briefRef']}",
            f"completedCount: {document['completedCount']}",
            f"currentActivation: {activation['round'] if activation is not None else 'none'}",
        ])

    def operation() -> CliResult:
        document = node_context(state_dir() / "pac-graph.sqlite3", graph_id, node_id)
        return CliResult(document, render=render)

    get_services()._execute(operation, json_output=json_output)

@history_app.command("list")
def history_list(
    limit: int = typer.Option(50, help="Maximum records to return."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """List read-only legacy workflow history."""
    _call("workflow.history.list", {"limit": limit}, json_output=json_output)

@history_app.command("status")
def history_status(
    run_id: str = typer.Argument(..., help="Graph ID, or archived run ID for history."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """Read one archived legacy run and its prior state."""
    _call("workflow.history.status", {"runId": run_id}, json_output=json_output)
