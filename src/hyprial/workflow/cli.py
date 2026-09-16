"""`hyprial workflow` command group (PAC; design-pac-workflow.md).

M1 ships `plan` only: parse + validate + expand the declared dispatch plan
locally, without touching the daemon.  `run`/`status`/`list`/`cancel` land
with the daemon-side wiring after the design review.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from hyprial.home import HYPRIALHomeNotInitialized, require_initialized_hyprial_home

from .schema import WorkflowSchemaError, WorkflowSpec, expand_template, pinned_node_warnings

workflow_app = typer.Typer(
    help="PAC declarative orchestration: dispatch + tracking only, never task execution.",
    no_args_is_help=True,
)

#: Stand-in nonce for plan output — a real run mints its own per-run nonce.
_PLAN_NONCE = "<run-nonce>"


def _plan_document(spec: WorkflowSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "summary": spec.summary,
        "targets": [target.name for target in spec.targets],
        "await": {
            "kind": spec.await_.kind,
            "timeoutSeconds": spec.await_.timeout_seconds,
            **({"match": spec.await_.match} if spec.await_.match else {}),
        },
        "onTimeout": {
            "action": spec.on_timeout.action,
            "maxAttempts": spec.on_timeout.max_attempts,
            "backoffSeconds": list(spec.on_timeout.backoff_seconds),
            **(
                {"escalateTo": spec.on_timeout.escalate_to}
                if spec.on_timeout.escalate_to
                else {}
            ),
        },
        "reportTo": spec.report_to,
        "worstCaseSecondsPerTarget": spec.worst_case_seconds,
        "warnings": list(pinned_node_warnings(spec)),
        "taskPerTarget": {
            target.name: expand_template(
                spec.task_for(target.name), nonce=_PLAN_NONCE, target=target.name
            )
            for target in spec.targets
        },
    }


@workflow_app.command("plan")
def plan(
    file: Path = typer.Argument(..., help="Path to the workflow.yaml to validate."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable plan."),
) -> None:
    """Validate a workflow file and print the expanded dispatch plan."""
    from .schema import load_workflow

    try:
        require_initialized_hyprial_home()
    except HYPRIALHomeNotInitialized as error:
        if json_out:
            typer.echo(
                json.dumps(
                    {"ok": False, "code": error.code, "error": str(error)},
                    ensure_ascii=False,
                )
            )
        else:
            typer.echo(f"hyprial: {error}", err=True)
        raise typer.Exit(code=1) from None

    try:
        spec = load_workflow(file)
    except WorkflowSchemaError as error:
        if json_out:
            typer.echo(
                json.dumps(
                    {"ok": False, "error": {"code": "WORKFLOW_SCHEMA_ERROR", "message": str(error)}},
                    ensure_ascii=False,
                )
            )
        else:
            typer.echo(f"workflow schema error: {error}", err=True)
        raise typer.Exit(code=2) from None

    document = _plan_document(spec)
    if json_out:
        typer.echo(json.dumps({"ok": True, "plan": document}, ensure_ascii=False, indent=2))
        return
    for warning in document["warnings"]:
        typer.echo(f"warning: {warning}", err=True)
    typer.echo(f"workflow: {spec.name}" + (f" — {spec.summary}" if spec.summary else ""))
    typer.echo(
        f"await: {spec.await_.kind}, timeout {spec.await_.timeout_seconds:.0f}s"
        + (f", match {spec.await_.match!r}" if spec.await_.match else "")
    )
    typer.echo(
        f"on_timeout: {spec.on_timeout.action}"
        + (
            f" (max {spec.on_timeout.max_attempts} attempts"
            + (
                f", backoff {list(spec.on_timeout.backoff_seconds)}s"
                if spec.on_timeout.backoff_seconds
                else ""
            )
            + ")"
            if spec.on_timeout.action == "retry"
            else ""
        )
        + (f" -> {spec.on_timeout.escalate_to}" if spec.on_timeout.escalate_to else "")
    )
    typer.echo(
        f"report_to: {spec.report_to or '<sender>'}; "
        f"worst case per target: {spec.worst_case_seconds:.0f}s"
    )
    for target in spec.targets:
        typer.echo(f"\n== target: {target.name} ==")
        typer.echo(expand_template(spec.task_for(target.name), nonce=_PLAN_NONCE, target=target.name))
