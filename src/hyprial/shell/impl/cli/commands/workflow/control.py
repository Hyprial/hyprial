"""PAC workflow CLI command module."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import typer

from hyprial.biz import actor_owner, check_actor_claim, state_dir, worker_binding
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject
from hyprial.shell.impl.cli.commands.workflow.internal.expansion import (
    read_expansion_file,
)
from hyprial.shell.impl.cli.output import CliResult


from hyprial.shell.impl.cli.commands.workflow.run import (
    _call, _call_result, migration_app, notify_app, worker_app, workflow_app,
)


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
        services = get_services()

        worker_actor = os.environ.get("HYPRIAL_WORKER_ACTOR")
        worker_session = os.environ.get("HYPRIAL_WORKER_SESSION_REF")
        identity = (
            {"from": sender, "sessionRef": worker_session}
            if sender == worker_actor and worker_session
            else {"from": actor_owner(), "onBehalfOf": sender}
        )

        result = services._daemon_request(
            "message.send",
            {
                **identity,
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

    def operation() -> JsonObject:
        binding = worker_binding()
        if binding is not None:
            actor, session_ref = binding
            return _call_result(
                "pac.flag.reset",
                {"graphId": graph_id, "nodeId": node_id, "reasonRef": reason_ref, "sessionRef": session_ref},
                claimed=actor,
            )
        from hyprial.daemon import PacReactor, planned_to_json
        from hyprial.daemon import PacGraphStore

        verified = actor_owner()
        store = PacGraphStore(state_dir() / "pac-graph.sqlite3")
        try:
            check_actor_claim(None, verified)
            outcome = PacReactor(store, sender=_WorkflowNotificationSender()).reset_flag(
                graph_id, node_id, actor=verified, reason_ref=reason_ref
            )
        finally:
            store.close()
        return {
            "ok": True,
            "event": outcome.event,
            "notifications": [planned_to_json(item) for item in outcome.planned],
        }

    get_services()._execute(operation, json_output=json_output)

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
    from hyprial.daemon import PacReactor
    from hyprial.daemon import PacGraphStore

    def operation() -> JsonObject:
        store = PacGraphStore(state_dir() / "pac-graph.sqlite3")
        try:
            result = PacReactor(store, sender=_WorkflowNotificationSender()).resend_undelivered(graph_id)
        finally:
            store.close()
        return {"ok": True, **result}

    get_services()._execute(operation, json_output=json_output)

@migration_app.command("status")
def migration_status(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """Read the schema-era owner migration report."""
    import sqlite3

    def operation() -> CliResult:
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
            return CliResult({"ok": True, "era": None}, render=lambda _data: "no schema-era report")
        return CliResult(
            {
                "ok": True,
                "schemaVersion": row[0],
                "migratedAtMs": row[1],
                "graphsTotal": row[2],
                "ownersRewritten": row[3],
                "ownersKept": row[4],
                "report": json.loads(row[5]),
            }
        )

    get_services()._execute(operation, json_output=json_output)

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
    output_text: str | None = typer.Option(
        None, "--output-text", help="Optional bounded result text stored with this request."
    ),
    expansion: Path | None = typer.Option(
        None,
        "--expansion",
        help="Local UTF-8 child workflow document (maximum 65536 bytes).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """Set your node flag against the exact current request."""
    services = get_services()

    def operation() -> CliResult:
        extra: dict[str, Any] = {}
        if expansion is not None:
            try:
                extra["expansion"] = read_expansion_file(expansion)
            except ValueError as error:
                raise services.CliError("PAC_EXPANSION_INVALID", str(error)) from error
        return CliResult(
            _call_result(
                "workflow.complete",
                {
                    "graphId": graph_id,
                    "nodeId": node_id,
                    "requestId": request_id,
                    "reasonRef": reason_ref,
                    **({"outputText": output_text} if output_text is not None else {}),
                    **extra,
                },
            )
        )

    services._execute(operation, json_output=json_output)

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
    output_text: str | None = typer.Option(
        None, "--output-text", help="Optional bounded result text stored with this request."
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
            **({"outputText": output_text} if output_text is not None else {}),
        },
        json_output=json_output,
    )
