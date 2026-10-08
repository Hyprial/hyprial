"""`hyprial routine` command group (self-drive; design-selfdrive-routine).

`plan` validates a routine file locally; the lifecycle commands talk to the
daemon.  Everything else is design-documented deterministic machinery.
"""

from __future__ import annotations

from hyprial.shell.impl.cli.output import CliResult

from pathlib import Path

import typer


from hyprial.biz  import RoutineSchemaError, RoutineSpec, load_routine
from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common.support import _render_routine_list
from hyprial.shell.impl.cli.commands.common.services import get_services
from typing import Any

routine_app = typer.Typer(
    help="Self-drive routines: periodic task-board-driven duty cycles over PAC runs.",
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
    from hyprial.biz  import BUILTIN_TEMPLATES

    if ctx.invoked_subcommand is not None:
        return
    get_services()._execute(
        lambda: CliResult(
            {"ok": True, "templates": list(BUILTIN_TEMPLATES)},
            render=lambda data: "\n".join(data["templates"]),
        ),
        json_output=json_out,
        allow_missing_home=True,
    )


@templates_app.command("show")
def template_show(
    name: str = typer.Argument(..., help="Built-in template name."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable template text."),
) -> None:
    """Print the template YAML verbatim, retaining runtime placeholders."""
    from hyprial.biz  import template_text

    def operation() -> CliResult:
        try:
            text = template_text(name)
        except ValueError as error:
            raise get_services().CliError("TEMPLATE_NOT_FOUND", str(error)) from None
        return CliResult({"ok": True, "name": name, "yaml": text}, render=lambda data: data["yaml"])

    get_services()._execute(operation, json_output=json_out, allow_missing_home=True)


def _plan_document(spec: RoutineSpec) -> dict[str, object]:
    return {
        "name": spec.name,
        "mode": spec.mode,
        "role": spec.role,
        "actor": spec.actor or spec.produces,
        "actorOwnership": "borrowed" if spec.actor else "routine",
        "launch": spec.launch,
        "overlap": "skip" if spec.mode == "scheduled" else None,
        "missedPeriods": "skip" if spec.mode == "scheduled" else None,
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

    def render(_data: Any) -> str:
        lines = [
            f"routine: {spec.name} — every {spec.interval_seconds:.0f}s, "
            f"source {spec.source_kind} filter {spec.source_filter!r}"
        ]
        lines += [f"  route: tag {rule.tag!r} -> {rule.kind} {rule.value}" for rule in spec.routes]
        lines.append(f"  default: {spec.default_route}; escalate_to: {spec.escalate_to}")
        lines.append(
            f"  limits: max_in_flight {spec.limits.max_in_flight}, "
            f"circuit_breaker {spec.limits.circuit_breaker.window_runs} runs "
            f"@ ratio {spec.limits.circuit_breaker.escalate_ratio} -> "
            f"{spec.limits.circuit_breaker.action}"
        )
        return "\n".join(lines)

    def operation() -> CliResult:
        nonlocal spec
        try:
            spec = load_routine(file)
        except RoutineSchemaError as error:
            raise get_services().CliError(
                "ROUTINE_SCHEMA_ERROR", f"routine schema error: {error}"
            ) from None
        return CliResult({"ok": True, "plan": _plan_document(spec)}, render=render, json_indent=2)

    spec: RoutineSpec
    get_services()._execute(operation, json_output=json_out)


def _routine_identity(claimed: str | None = None) -> dict[str, str]:
    services = get_services()
    from hyprial.shell.impl.cli.commands.workflow.run import _identity
    from hyprial.identity import PacError
    try:
        return _identity(claimed)
    except PacError as error:
        raise services.CliError(error.code, str(error)) from error


@routine_app.command("add")
def routine_add(
    file: Path | None = typer.Argument(None, help="Path to the routine.yaml to register."),
    from_identity: str | None = typer.Option(
        None, "--from", help="Registered identity owning a file-based routine."
    ),
    template: str | None = typer.Option(None, "--template", help="Built-in template name."),
    coordinator: str | None = typer.Option(None, "--for", help="Coordinator actor URI (template owner)."),
    escalate_to: str | None = typer.Option(None, "--escalate-to", help="Template escalation destination: user:<owner>."),
    name: str | None = typer.Option(None, "--name", help="Override template routine name."),
    interval: str | None = typer.Option(None, "--interval", help="Override template interval (minimum 60s)."),
    source: str | None = typer.Option(None, "--source", help="Template source: pac-journal or taskwarrior."),
    filter_expr: str | None = typer.Option(None, "--filter", help="Taskwarrior source filter."),
    idle_threshold: str | None = typer.Option(None, "--idle-threshold", help="PAC assignment idle threshold."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Register a YAML file or render and register one built-in template."""
    services = get_services()

    def operation() -> Any:
        services = get_services()
        from hyprial.biz import RoutineSchemaError
        from hyprial.biz import render_template

        if (file is None) == (template is None):
            raise services.CliError(ipc_errors.INVALID_ARGUMENT, "provide exactly one of FILE or --template")
        if template is not None:
            if from_identity is not None or coordinator is None or escalate_to is None:
                raise services.CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    "--template requires --for and --escalate-to; do not use --from",
                )
            try:
                text = render_template(
                    template, owner=coordinator, escalate_to=escalate_to,
                    name=name, interval=interval, source=source, filter_expr=filter_expr,
                    idle_threshold=idle_threshold,
                )
            except RoutineSchemaError as error:
                raise services.CliError("ROUTINE_SCHEMA_ERROR", str(error)) from None
            except ValueError as error:
                raise services.CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from None
        else:
            if any(
                value is not None
                for value in (coordinator, escalate_to, name, interval, source, filter_expr, idle_threshold)
            ):
                raise services.CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    "template options require --template",
                )
            assert file is not None
            try:
                text = file.read_text(encoding="utf-8")
            except OSError as error:
                raise services.CliError(ipc_errors.INVALID_ARGUMENT, f"cannot read {file}: {error}") from None
        # Coordinator admission includes one shared lifecycle start.
        result = services._daemon_request("routine.add", {"yaml": text, **_routine_identity(from_identity)}, timeout=60.0)
        if not isinstance(result, dict) or not isinstance(result.get("name"), str):
            raise services.CliError("INVALID_RESPONSE", "routine.add must return name")
        return {"ok": True, **result}

    services._execute(operation, json_output=json_output)


@routine_app.command("list")
def routine_list(
    all_callers: bool = typer.Option(
        False, "--all", help="List routines from all callers on this node."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List registered routines."""
    services = get_services()

    def operation() -> Any:
        services = get_services()
        result = services._daemon_request(
            "routine.list",
            {
                **({"all": True} if all_callers else {}),
                **_routine_identity(),
            },
        )
        if not isinstance(result, dict) or not isinstance(result.get("routines"), list):
            raise services.CliError("INVALID_RESPONSE", "routine.list must return routines")
        return CliResult(
            {"ok": True, **result},
            render=lambda data: _render_routine_list(data["routines"], all_callers=all_callers),
        )

    services._execute(operation, json_output=json_output)


@routine_app.command("status")
def routine_status(
    name: str = typer.Argument(..., help="Routine name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show one routine's state, in-flight tasks, and outcome ledger."""
    services = get_services()

    def operation() -> Any:
        services = get_services()
        result = services._daemon_request("routine.status", {"name": name, **_routine_identity()})
        if not isinstance(result, dict) or not isinstance(result.get("name"), str):
            raise services.CliError("INVALID_RESPONSE", "routine.status must return name")
        return {"ok": True, **result}

    services._execute(operation, json_output=json_output)


@routine_app.command("rm")
def routine_rm(
    name: str = typer.Argument(..., help="Routine name to remove."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Remove a routine and its in-flight map."""
    services = get_services()

    def operation() -> Any:
        services = get_services()
        result = services._daemon_request("routine.remove", {"name": name, **_routine_identity()})
        if not isinstance(result, dict) or result.get("removed") is not True:
            raise services.CliError(
                "INVALID_RESPONSE", "routine.remove must return removed=true"
            )
        return {"ok": True, **result}

    services._execute(operation, json_output=json_output)


@routine_app.command("set")
def routine_set(
    name: str = typer.Argument(..., help="Existing routine name to keep."),
    file: Path = typer.Argument(..., help="Replacement routine.yaml."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Atomically replace a routine while keeping its name and actor binding."""
    services = get_services()

    def operation() -> Any:
        services = get_services()
        try:
            text = file.read_text(encoding="utf-8")
        except OSError as error:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT, f"cannot read {file}: {error}"
            ) from None
        result = services._daemon_request(
            "routine.set",
            {"name": name, "yaml": text, **_routine_identity()},
            timeout=60.0,
        )
        if not isinstance(result, dict) or result.get("name") != name:
            raise services.CliError("INVALID_RESPONSE", "routine.set must return the same name")
        return {"ok": True, **result}

    services._execute(operation, json_output=json_output)


@routine_app.command("pause")
def routine_pause(
    name: str = typer.Argument(..., help="Routine name to pause."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Pause a routine (stops new duty cycles)."""
    services = get_services()

    def operation() -> Any:
        services = get_services()
        result = services._daemon_request("routine.pause", {"name": name, **_routine_identity()})
        if not isinstance(result, dict) or result.get("enabled") is not False:
            raise services.CliError(
                "INVALID_RESPONSE", "routine.pause must return enabled=false"
            )
        return {"ok": True, **result}

    services._execute(operation, json_output=json_output)


@routine_app.command("resume")
def routine_resume(
    name: str = typer.Argument(..., help="Routine name to resume."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Resume a paused routine with a clean breaker ledger."""
    services = get_services()

    def operation() -> Any:
        services = get_services()
        result = services._daemon_request("routine.resume", {"name": name, **_routine_identity()})
        if not isinstance(result, dict) or result.get("enabled") is not True:
            raise services.CliError(
                "INVALID_RESPONSE", "routine.resume must return enabled=true"
            )
        return {"ok": True, **result}

    services._execute(operation, json_output=json_output)
