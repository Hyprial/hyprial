# Decision record (I6-4, plan §H.6):
# - ``cell_app`` with ``show`` and ``apply`` is mounted on ``agent_app`` via
#   the CLI root registration; ``agent.cell.*`` are registered catalog methods,
#   so dispatch goes through ``runtime.request(...)`` like every other command.
# - Reads ``CELL.json`` via ``Path.read_text``; the file is a complete
#   members/params fragment, never a field-by-field patch language.
"""Registered ``hyprial agent cell show|apply`` commands (I6-4)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import typer

from hyprial.error import codes
from hyprial.shell.impl.cli.commands.common.groups import group
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.output import CliResult
from hyprial.shell.impl.cli.runtime import CliRuntime

JsonObject = dict[str, Any]

cell_app = group(
    help="Inspect or update an agent's cell document.",
    no_args_is_help=True,
)

_FORBIDDEN_FIELDS = frozenset(
    {
        "id",
        "cellId",
        "kind",
        "authority",
        "binding",
        "scope",
        "retired",
        "retire",
        "revision",
        "permissions",
        "schema",
        "updatedAtMs",
    }
)
_ALLOWED_FILE_FIELDS = frozenset({"name", "cellName", "members", "params"})


def _cli_error(code: str, message: str, data: Mapping[str, Any] | None = None) -> Exception:
    return get_services().CliError(code, message, dict(data) if data is not None else None)


def _runtime(explicit: CliRuntime | None) -> CliRuntime:
    return explicit if explicit is not None else get_services().runtime


def _require_object(result: Any, method: str) -> JsonObject:
    if not isinstance(result, Mapping):
        raise _cli_error("INVALID_RESPONSE", f"{method} must return a JSON object")
    return dict(result)


def _load_cell_fragment(file: Path) -> JsonObject:
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as error:
        raise _cli_error(
            codes.INVALID_ARGUMENT, f"cannot read cell file {file}: {error}"
        ) from error
    try:
        parsed = json.loads(text)
    except ValueError as error:
        raise _cli_error(
            codes.INVALID_ARGUMENT, f"cell file {file} is not valid JSON: {error}"
        ) from error
    if not isinstance(parsed, dict):
        raise _cli_error(
            codes.INVALID_ARGUMENT, f"cell file {file} must contain a JSON object"
        )
    forbidden = sorted(set(parsed) & _FORBIDDEN_FIELDS)
    if forbidden:
        raise _cli_error(
            codes.INVALID_ARGUMENT,
            f"cell file cannot modify immutable field(s): {', '.join(forbidden)}",
        )
    unknown = sorted(set(parsed) - _ALLOWED_FILE_FIELDS)
    if unknown:
        raise _cli_error(
            codes.INVALID_ARGUMENT,
            f"cell file contains unsupported field(s): {', '.join(unknown)}",
        )
    payload: JsonObject = {}
    if "cellName" in parsed:
        payload["cellName"] = parsed["cellName"]
    elif "name" in parsed:
        payload["cellName"] = parsed["name"]
    if "members" in parsed:
        payload["members"] = parsed["members"]
    if "params" in parsed:
        payload["params"] = parsed["params"]
    if not payload:
        raise _cli_error(
            codes.INVALID_ARGUMENT,
            "cell file must contain at least one of members, params, or name",
        )
    return payload


def render_cell_show(data: Mapping[str, Any]) -> str:
    """Format an ``agent.cell.get`` response for human terminal output."""

    cell = data.get("cell") if isinstance(data.get("cell"), Mapping) else {}
    snapshot = data.get("snapshot") if isinstance(data.get("snapshot"), Mapping) else None
    lines = [
        f"cell: {cell.get('cellId', '?')}",
        f"revision: {cell.get('revision', '?')}",
    ]
    if snapshot is None:
        lines.append("state: unknown")
    else:
        lines.append(
            f"state: {snapshot.get('state', 'unknown')} "
            f"(effectiveRevision={snapshot.get('effectiveRevision', 0)})"
        )
    lines.append(f"members: {json.dumps(cell.get('members', []), ensure_ascii=False)}")
    lines.append(f"params: {json.dumps(cell.get('params', {}), ensure_ascii=False, sort_keys=True)}")
    return "\n".join(lines)


def render_cell_apply(data: Mapping[str, Any]) -> str:
    """Format an ``agent.cell.update`` response for human terminal output."""

    return "\n".join(
        [
            f"cell: {data.get('cellId', '?')}",
            f"revision: {data.get('revision', '?')}",
            f"appliedRevision: {data.get('appliedRevision', '?')}",
            f"state: {data.get('state', 'unknown')}",
        ]
    )


def register_cell_commands(
    app: typer.Typer,
    runtime: CliRuntime | None = None,
) -> Mapping[str, Any]:
    """Register ``show`` and ``apply`` onto *app*."""

    @app.command("show")
    def show_cell(
        name: str = typer.Argument(..., help="Registered agent name or canonical URI."),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    ) -> None:
        """Show an agent's cell document fragment and actor snapshot."""

        active = _runtime(runtime)

        def operation() -> CliResult:
            result = _require_object(
                active.request("agent.cell.get", {"name": name}), "agent.cell.get"
            )
            if not isinstance(result.get("cell"), Mapping):
                raise _cli_error(
                    "INVALID_RESPONSE", "daemon agent.cell.get result must contain cell"
                )
            return CliResult(result, render=render_cell_show)

        active.execute(operation, json_output=json_output)

    @app.command("apply")
    def apply_cell(
        name: str = typer.Argument(..., help="Registered agent name or canonical URI."),
        expected_revision: int = typer.Option(
            ..., "--expected-revision", help="Expected current cell revision for CAS update."
        ),
        file: Path = typer.Option(
            ..., "--file", help="Path to JSON file containing cell members/params fragment."
        ),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    ) -> None:
        """Apply a CAS-guarded cell document update from a JSON file."""

        active = _runtime(runtime)

        def operation() -> CliResult:
            if expected_revision < 1:
                raise _cli_error(
                    codes.INVALID_ARGUMENT, "--expected-revision must be >= 1"
                )
            fragment = _load_cell_fragment(file)
            params: JsonObject = {
                "name": name,
                "expectedRevision": expected_revision,
                **fragment,
            }
            result = _require_object(
                active.request("agent.cell.update", params), "agent.cell.update"
            )
            if "cellId" not in result or "revision" not in result:
                raise _cli_error(
                    "INVALID_RESPONSE",
                    "daemon agent.cell.update result must contain cellId and revision",
                )
            return CliResult(result, render=render_cell_apply)

        active.execute(operation, json_output=json_output)

    order = {"show": 0, "apply": 1}
    app.registered_commands.sort(
        key=lambda command: order.get(command.name or "", len(order))
    )
    return {"show": show_cell, "apply": apply_cell}


register_cell_commands(cell_app)

__all__ = [
    "cell_app",
    "register_cell_commands",
    "render_cell_apply",
    "render_cell_show",
]
