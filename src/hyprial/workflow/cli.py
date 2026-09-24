"""High-level workflow CLI backed solely by PAC graphs."""

from __future__ import annotations

import json
from pathlib import Path
from time import sleep
from typing import Any

import typer

from hyprial.pac.errors import PacError
from hyprial.pac.workflow_schema import (
    WorkflowSchemaError,
    WorkflowSpec,
    load_workflow_text,
)
from hyprial.workflow.identity import (
    actor_owner,
    check_actor_claim,
    emit,
    fail as identity_fail,
    guard,
    state_dir,
    worker_binding,
)

workflow_app = typer.Typer(
    help="Plan and run PAC graphs with managed workers.", no_args_is_help=True
)
history_app = typer.Typer(
    help="Read archived legacy workflow history.", no_args_is_help=True
)
worker_app = typer.Typer(help="Control workers owned by a workflow.", no_args_is_help=True)
notify_app = typer.Typer(help="Workflow notification maintenance.", no_args_is_help=True)
migration_app = typer.Typer(help="Read-only workflow migration reports.", no_args_is_help=True)
workflow_app.add_typer(history_app, name="history")
workflow_app.add_typer(worker_app, name="worker")
workflow_app.add_typer(notify_app, name="notify")
workflow_app.add_typer(migration_app, name="migration")


def _plan_document(spec: WorkflowSpec) -> dict[str, Any]:
    return spec.to_json()


def _identity(json_output: bool, claimed: str | None = None) -> dict[str, str]:
    guard(json_output)
    binding = worker_binding(json_output)
    if binding is not None:
        actor, session = binding
        check_actor_claim(claimed, actor)
        return {"actor": actor, "sessionRef": session}
    actor = actor_owner()
    check_actor_claim(claimed, actor)
    return {"actor": actor}


def _call(
    method: str,
    params: dict[str, Any],
    *,
    json_output: bool,
    claimed: str | None = None,
):
    from hyprial.cli import CliError, _daemon_request, _execute

    def operation():
        try:
            identity = _identity(json_output, claimed)
        except PacError as error:
            raise CliError(error.code, str(error)) from error
        result = _daemon_request(method, {**params, **identity})
        if not isinstance(result, dict):
            raise CliError("INVALID_RESPONSE", f"{method} returned a non-object")
        return {"ok": True, **result}

    _execute(operation, json_output=json_output)


def _load(file: Path):
    from hyprial.cli import CliError

    try:
        raw = file.read_text(encoding="utf-8")
        spec = load_workflow_text(raw)
        return spec, raw
    except (WorkflowSchemaError, OSError) as error:
        raise CliError("WORKFLOW_SCHEMA_ERROR", str(error)) from error


@workflow_app.command("plan")
def plan(
    file: Path = typer.Argument(..., help="Workflow YAML file (format 2)."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """Validate and expand ownership, model selection and failure policy."""
    from hyprial.cli import _execute

    def operation():
        spec, _ = _load(file)
        return {"ok": True, "plan": spec.to_json()}

    _execute(operation, json_output=json_out)


@workflow_app.command("run")
def run(
    file: Path = typer.Argument(..., help="Workflow YAML file (format 2)."),
    from_identity: str | None = typer.Option(
        None, "--from", help="Must match your verified principal."
    ),
    operation_key: str | None = typer.Option(
        None,
        "--operation-key",
        help="Stable retry/restart identity for this exact dispatch.",
    ),
    yes: bool = typer.Option(
        False, "--yes", help="Skip the interactive plan confirmation."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """Publish one validated graph and let PAC dispatch ready nodes."""
    from hyprial.cli import _execute

    # Loading runs through the same JSON error boundary as other commands.
    def operation():
        from hyprial.cli import CliError, _daemon_request

        spec, raw = _load(file)
        try:
            identity = _identity(json_output, from_identity)
        except PacError as error:
            raise CliError(error.code, str(error)) from error
        if not yes and not json_output:
            typer.echo(json.dumps(spec.to_json(), ensure_ascii=False, indent=2))
            if not typer.confirm("Dispatch this graph?"):
                raise typer.Exit(1)
        result = _daemon_request(
            "workflow.start",
            {
                "yaml": raw,
                **identity,
                **(
                    {"operationKey": operation_key} if operation_key is not None else {}
                ),
            },
        )
        return {"ok": True, **result}

    _execute(operation, json_output=json_output)


@workflow_app.command("status")
def status(
    run_id: str = typer.Argument(..., help="PAC graph ID."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """Read graph state, nodes, requests and evidence references."""
    _call("workflow.status", {"runId": run_id}, json_output=json_output)


@workflow_app.command("list")
def list_workflows(
    limit: int = typer.Option(50, help="Maximum records to return."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """List recent PAC workflows."""
    _call("workflow.list", {"limit": limit}, json_output=json_output)


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
    from hyprial.pac.projection import Projection
    from hyprial.pac.subscription import events_since
    from hyprial.pac.stream import follow_lifetime, silence_broken_pipe

    guard(json_output)
    database = state_dir() / "pac-graph.sqlite3"
    try:
        if snapshot and after is not None:
            raise PacError("PAC_EVENTS_ARGUMENT", "--snapshot and --after are mutually exclusive")
        with follow_lifetime(follow) as output:
            cursor = after if after is not None else 0
            if snapshot:
                document = Projection(database).public_snapshot(graph_id)
                typer.echo(json.dumps(document, ensure_ascii=False))
                if not follow:
                    return
                cursor, journal_id = document["cursor"], document["journalId"]
            upper = None
            while True:
                if output is not None and output.closed():
                    return
                page = events_since(database, graph_id, cursor, journal_id=journal_id, until=upper)
                journal_id, upper = page["journalId"], page["highWatermark"]
                for event in page["events"]:
                    typer.echo(json.dumps(event, ensure_ascii=False) if json_output else f"#{event['seq']} {event['type']} {event['graphId']}")
                cursor = page["cursor"]
                if cursor < upper:
                    continue
                if not follow:
                    return
                upper = None
                sleep(0.2)
    except KeyboardInterrupt:
        return
    except BrokenPipeError:
        silence_broken_pipe()
        return
    except PacError as error:
        typer.echo(json.dumps({"ok": False, "code": error.code, "error": str(error)}), err=True)
        raise typer.Exit(1) from None


@workflow_app.command("context")
def context(
    graph_id: str = typer.Argument(..., help="Graph id."),
    node_id: str = typer.Argument(..., help="Node id."),
    json_output: bool = typer.Option(False, "--json", help="Emit stable context JSON."),
):
    """Read one node's immutable workflow context."""
    from hyprial.pac.context import node_context

    guard(json_output)
    try:
        document = node_context(state_dir() / "pac-graph.sqlite3", graph_id, node_id)
    except PacError as error:
        identity_fail(error, json_output)
    activation = document["currentActivation"]
    human = [
        f"graph {graph_id} node {node_id} version {document['version']} cursor {document['cursor']}",
        f"brief: {document['briefRef']}",
        f"completedCount: {document['completedCount']}",
        f"currentActivation: {activation['round'] if activation is not None else 'none'}",
    ]
    emit(document, json_output, "\n".join(human))


class _WorkflowNotificationSender:
    def send(
        self,
        *,
        recipient: str,
        text: str,
        sender: str,
        conversation_id: str,
        idempotency_key: str,
    ) -> str:
        from hyprial.cli import _daemon_request

        result = _daemon_request(
            "message.send",
            {
                "from": sender,
                "to": [recipient],
                "message": text,
                "conversationId": conversation_id,
                "idempotencyKey": idempotency_key,
            },
        )
        deliveries = result.get("deliveries") if isinstance(result, dict) else None
        if isinstance(deliveries, list):
            for delivery in deliveries:
                if isinstance(delivery, dict) and delivery.get("messageId") and delivery.get("accepted") is not False:
                    return str(delivery["messageId"])
        raise RuntimeError("message.send returned no accepted messageId")


@workflow_app.command("reset")
def reset(
    graph_id: str = typer.Argument(..., help="Graph id."),
    node_id: str = typer.Argument(..., help="Node id."),
    reason_ref: str | None = typer.Option(None, "--reason-ref", help="Evidence reference."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """Withdraw one current request for explicit rework."""
    guard(json_output)
    binding = worker_binding(json_output)
    if binding is not None:
        actor, session_ref = binding
        _call(
            "pac.flag.reset",
            {"graphId": graph_id, "nodeId": node_id, "reasonRef": reason_ref, "sessionRef": session_ref},
            json_output=json_output,
            claimed=actor,
        )
        return
    from hyprial.pac.reactor import PacReactor, planned_to_json
    from hyprial.pac.store import PacGraphStore

    verified = actor_owner()
    store = PacGraphStore(state_dir() / "pac-graph.sqlite3")
    try:
        check_actor_claim(None, verified)
        outcome = PacReactor(store, sender=_WorkflowNotificationSender()).reset_flag(
            graph_id, node_id, actor=verified, reason_ref=reason_ref
        )
    except PacError as error:
        identity_fail(error, json_output)
    finally:
        store.close()
    emit(
        {"ok": True, "event": outcome.event, "notifications": [planned_to_json(item) for item in outcome.planned]},
        json_output,
    )


@worker_app.command("stop")
def worker_stop(
    graph_id: str = typer.Argument(..., help="Owning workflow graph."),
    actor_name: str = typer.Argument(..., help="Graph-owned worker actor."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """Stop an owned worker and fail its unfinished work immediately."""
    _call("workflow.worker.stop", {"graphId": graph_id, "actorName": actor_name}, json_output=json_output)


@worker_app.command("restart")
def worker_restart(
    graph_id: str = typer.Argument(..., help="Owning workflow graph."),
    actor_name: str = typer.Argument(..., help="Graph-owned worker actor."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """Restart an owned worker with a new incarnation and retain its work."""
    _call("workflow.worker.restart", {"graphId": graph_id, "actorName": actor_name}, json_output=json_output)


@notify_app.command("resend")
def notify_resend(
    graph_id: str = typer.Argument(..., help="Graph id."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """Retry only undelivered workflow notification rows."""
    guard(json_output)
    from hyprial.pac.reactor import PacReactor
    from hyprial.pac.store import PacGraphStore

    store = PacGraphStore(state_dir() / "pac-graph.sqlite3")
    try:
        result = PacReactor(store, sender=_WorkflowNotificationSender()).resend_undelivered(graph_id)
    except PacError as error:
        identity_fail(error, json_output)
    finally:
        store.close()
    emit({"ok": True, **result}, json_output)


@migration_app.command("status")
def migration_status(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """Read the schema-era owner migration report."""
    guard(json_output)
    import sqlite3

    database = f"file:{state_dir() / 'pac-graph.sqlite3'}?mode=ro"
    connection = sqlite3.connect(database, uri=True)
    try:
        try:
            row = connection.execute(
                "SELECT user_version,migrated_at_ms,graphs_total,owners_rewritten,owners_kept,report_json "
                "FROM schema_era WHERE id=1"
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
    finally:
        connection.close()
    if row is None:
        emit({"ok": True, "era": None}, json_output, "no schema-era report")
        return
    emit(
        {
            "ok": True,
            "schemaVersion": row[0],
            "migratedAtMs": row[1],
            "graphsTotal": row[2],
            "ownersRewritten": row[3],
            "ownersKept": row[4],
            "report": json.loads(row[5]),
        },
        json_output,
    )


@workflow_app.command("complete")
def complete(
    graph_id: str = typer.Argument(..., help="PAC graph ID."),
    node_id: str = typer.Argument(
        ..., help="Node ID owned by your verified principal."
    ),
    request_id: str = typer.Option(
        ..., "--request-id", help="Exact request ID from the dispatch or latest status."
    ),
    reason_ref: str = typer.Option(
        ..., "--reason-ref", help="Reference to completion or failure evidence."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """Set your node flag against the exact current request."""
    _call(
        "workflow.complete",
        {
            "graphId": graph_id,
            "nodeId": node_id,
            "requestId": request_id,
            "reasonRef": reason_ref,
        },
        json_output=json_output,
    )


@workflow_app.command("fail")
def fail(
    graph_id: str = typer.Argument(..., help="PAC graph ID."),
    node_id: str = typer.Argument(
        ..., help="Node ID owned by your verified principal."
    ),
    request_id: str = typer.Option(
        ..., "--request-id", help="Exact request ID from the dispatch or latest status."
    ),
    reason_ref: str = typer.Option(
        ..., "--reason-ref", help="Reference to completion or failure evidence."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """Report explicit failure and apply this graph's declared failure policy."""
    _call(
        "workflow.fail",
        {
            "graphId": graph_id,
            "nodeId": node_id,
            "requestId": request_id,
            "reasonRef": reason_ref,
        },
        json_output=json_output,
    )


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
