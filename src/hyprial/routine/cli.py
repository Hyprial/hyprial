"""`hyprial routine` command group (self-drive; design-selfdrive-routine).

`plan` validates a routine file locally; the lifecycle commands talk to the
daemon.  Everything else is design-documented deterministic machinery.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from hyprial.home import HYPRIALHomeNotInitialized, require_initialized_hyprial_home

from .schema import RoutineSchemaError, RoutineSpec, load_routine

routine_app = typer.Typer(
    help="Self-drive routines: periodic kanban-driven duty cycles over PAC runs.",
    no_args_is_help=True,
)


templates_app = typer.Typer(help="Inspect built-in routine templates.", invoke_without_command=True)
routine_app.add_typer(templates_app, name="templates")


@templates_app.callback()
def templates(
    ctx: typer.Context,
    json_out: bool = typer.Option(False, "--json", help="Machine-readable template list."),
) -> None:
    """List the templates shipped with this hyprial build (no daemon required)."""
    from .templates import BUILTIN_TEMPLATES

    if ctx.invoked_subcommand is not None:
        return
    if json_out:
        typer.echo(json.dumps({"ok": True, "templates": list(BUILTIN_TEMPLATES)}))
    else:
        typer.echo("\n".join(BUILTIN_TEMPLATES))


@templates_app.command("show")
def template_show(name: str = typer.Argument(..., help="Built-in template name.")) -> None:
    """Print the template YAML verbatim, retaining runtime placeholders."""
    from .templates import template_text

    try:
        text = template_text(name)
    except ValueError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from None
    typer.echo(text, nl=False)


def _plan_document(spec: RoutineSpec) -> dict[str, object]:
    return {
        "name": spec.name,
        "intervalSeconds": spec.interval_seconds,
        "source": {
            "kind": spec.source_kind,
            **({"filter": spec.source_filter} if spec.source_kind == "taskwarrior" else {}),
            **(
                {"idleThresholdSeconds": spec.source_idle_threshold_seconds}
                if spec.source_kind == "pac-journal"
                else {}
            ),
        },
        **({"produces": spec.produces} if spec.produces is not None else {}),
        "routes": [{"tag": r.tag, "kind": r.kind, "value": r.value} for r in spec.routes],
        "defaultRoute": spec.default_route,
        "limits": {
            "maxInFlight": spec.limits.max_in_flight,
            "circuitBreaker": {
                "windowRuns": spec.limits.circuit_breaker.window_runs,
                "escalateRatio": spec.limits.circuit_breaker.escalate_ratio,
                "action": spec.limits.circuit_breaker.action,
            },
        },
        "escalateTo": spec.escalate_to,
    }


@routine_app.command("plan")
def plan(
    file: Path = typer.Argument(..., help="Path to the routine.yaml to validate."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable plan."),
) -> None:
    """Validate a routine file and print the expanded duty-cycle plan."""
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
        spec = load_routine(file)
    except RoutineSchemaError as error:
        if json_out:
            typer.echo(
                json.dumps(
                    {"ok": False, "error": {"code": "ROUTINE_SCHEMA_ERROR", "message": str(error)}},
                    ensure_ascii=False,
                )
            )
        else:
            typer.echo(f"routine schema error: {error}", err=True)
        raise typer.Exit(code=2) from None
    document = _plan_document(spec)
    if json_out:
        typer.echo(json.dumps({"ok": True, "plan": document}, ensure_ascii=False, indent=2))
        return
    typer.echo(
        f"routine: {spec.name} — every {spec.interval_seconds:.0f}s, "
        f"source {spec.source_kind} filter {spec.source_filter!r}"
    )
    for rule in spec.routes:
        typer.echo(f"  route: tag {rule.tag!r} -> {rule.kind} {rule.value}")
    typer.echo(f"  default: {spec.default_route}; escalate_to: {spec.escalate_to}")
    typer.echo(
        f"  limits: max_in_flight {spec.limits.max_in_flight}, "
        f"circuit_breaker {spec.limits.circuit_breaker.window_runs} runs "
        f"@ ratio {spec.limits.circuit_breaker.escalate_ratio} -> "
        f"{spec.limits.circuit_breaker.action}"
    )
