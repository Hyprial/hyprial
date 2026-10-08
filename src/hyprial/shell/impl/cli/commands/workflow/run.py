"""PAC workflow CLI command module."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from hyprial.biz import actor_owner, check_actor_claim, worker_binding
from hyprial.identity import PacError
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject
from hyprial.shell.impl.cli.commands.workflow.internal.expansion import (
    parse_expansion_target,
    read_expansion_file,
)
from hyprial.shell.impl.cli.output import CliResult, confirm, render_generic, warn

if TYPE_CHECKING:
    from hyprial.daemon import WorkflowSpec


workflow_app = typer.Typer(
    help="Plan and run PAC graphs with managed workers.", no_args_is_help=True
)

history_app = typer.Typer(
    help="Read archived legacy workflow history.", no_args_is_help=True
)

worker_app = typer.Typer(help="Control workers owned by a workflow.", no_args_is_help=True)

notify_app = typer.Typer(help="Workflow notification maintenance.", no_args_is_help=True)

migration_app = typer.Typer(help="Read-only workflow migration reports.", no_args_is_help=True)

overview_app = typer.Typer(
    help="PAC overview page: this node's task lines, workflows and routines.",
    no_args_is_help=True,
)

workflow_app.add_typer(history_app, name="history")
workflow_app.add_typer(overview_app, name="overview")
workflow_app.add_typer(worker_app, name="worker")
workflow_app.add_typer(notify_app, name="notify")
workflow_app.add_typer(migration_app, name="migration")


def _plan_document(spec: WorkflowSpec) -> dict[str, Any]:
    return spec.to_json()

def _identity(claimed: str | None = None) -> dict[str, str]:
    binding = worker_binding()
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
    get_services()._execute(
        lambda: _call_result(method, params, claimed=claimed), json_output=json_output
    )

def _call_result(method: str, params: dict[str, Any], *, claimed: str | None = None) -> JsonObject:
    services = get_services()
    try:
        identity = _identity(claimed)
    except PacError as error:
        raise services.CliError(error.code, str(error)) from error
    result = services._daemon_request(method, {**params, **identity})
    if not isinstance(result, dict):
        raise services.CliError("INVALID_RESPONSE", f"{method} returned a non-object")
    return {"ok": True, **result}

def _load(file: Path):
    services = get_services()

    from hyprial.daemon import (
        WorkflowSchemaError,
        load_workflow_text,
    )

    try:
        raw = file.read_text(encoding="utf-8")
        spec = load_workflow_text(raw)
        return spec, raw
    except (WorkflowSchemaError, OSError) as error:
        raise services.CliError("WORKFLOW_SCHEMA_ERROR", str(error)) from error

@workflow_app.command("plan")
def plan(
    file: Path = typer.Argument(..., help="Workflow YAML file (format 2)."),
    expansion_for: str | None = typer.Option(
        None,
        "--expansion-for",
        help="Preview a child expansion for graph:placeholder through the daemon.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON only."),
):
    """Validate and expand ownership, model selection and failure policy."""
    services = get_services()

    def operation():
        if expansion_for is not None:
            try:
                target = parse_expansion_target(expansion_for)
                raw = read_expansion_file(file)
            except ValueError as error:
                raise services.CliError("PAC_EXPANSION_INVALID", str(error)) from error
            try:
                identity = _identity()
            except PacError as error:
                raise services.CliError(error.code, str(error)) from error
            preview = services._daemon_request(
                "workflow.expansion.plan", {**target, "yaml": raw, **identity}
            )
            if not isinstance(preview, dict):
                raise services.CliError(
                    "INVALID_RESPONSE", "workflow.expansion.plan returned a non-object"
                )
            return {"ok": True, **preview}
        spec, _ = _load(file)
        from hyprial.daemon import dispatch_reminders

        reminders, reminder_error = dispatch_reminders()
        if reminder_error is not None:
            warn(f"reminder unavailable: {reminder_error}", json_output=json_out)
        if any(node.expands is not None for node in spec.nodes):
            try:
                identity = _identity()
            except PacError as error:
                raise services.CliError(error.code, str(error)) from error
            preview = services._daemon_request(
                "workflow.plan", {"yaml": file.read_text(encoding="utf-8"), **identity}
            )
            if not isinstance(preview, dict):
                raise services.CliError("INVALID_RESPONSE", "workflow.plan returned a non-object")
            return {
                "ok": True,
                "plan": preview.get("plan", preview),
                "reminders": reminders,
                **({"reminderError": reminder_error} if reminder_error else {}),
            }
        return {
            "ok": True,
            "plan": spec.to_json(),
            "reminders": reminders,
            **({"reminderError": reminder_error} if reminder_error else {}),
        }

    services._execute(operation, json_output=json_out)

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
    services = get_services()

    # Loading runs through the same JSON error boundary as other commands.
    def operation():
        services = get_services()
        from hyprial.daemon import dispatch_reminders

        spec, raw = _load(file)
        # Advice only: a broken policy file yields no reminder, never a
        # failed dispatch.
        reminders, reminder_error = dispatch_reminders()
        if reminder_error is not None:
            warn(f"reminder unavailable: {reminder_error}", json_output=json_output)
        try:
            identity = _identity(from_identity)
        except PacError as error:
            raise services.CliError(error.code, str(error)) from error
        for reminder in reminders:
            warn(reminder, json_output=json_output)
        authoritative_plan = spec.to_json()
        if any(node.expands is not None for node in spec.nodes):
            preview = services._daemon_request(
                "workflow.plan", {"yaml": raw, **identity}
            )
            if not isinstance(preview, dict):
                raise services.CliError("INVALID_RESPONSE", "workflow.plan returned a non-object")
            authoritative_plan = preview.get("plan", preview)
        if not yes and not json_output:
            plan = json.dumps(authoritative_plan, ensure_ascii=False, indent=2)
            if not confirm("Dispatch this graph?", preview=plan):
                raise services.CliError("CONFIRMATION_DECLINED", "graph not dispatched")
        result = services._daemon_request(
            "workflow.start",
            {
                "yaml": raw,
                **identity,
                **(
                    {"operationKey": operation_key} if operation_key is not None else {}
                ),
            },
        )
        return CliResult(
            {
                "ok": True,
                "reminders": reminders,
                **({"reminderError": reminder_error} if reminder_error else {}),
                **result,
            },
            # Reminders were already shown on stderr; the view repeats the rest.
            render=lambda data: render_generic(
                {k: v for k, v in data.items() if k not in {"reminders", "reminderError"}}
            ),
        )

    services._execute(operation, json_output=json_output)
