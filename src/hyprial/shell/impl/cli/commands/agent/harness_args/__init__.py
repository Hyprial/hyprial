"""Persistent managed harness argument CLI commands."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import typer

from hyprial.shell.impl.cli.commands.common.groups import group
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.output import CliResult, confirm
from hyprial.shell.impl.cli.output.errors import (
    HARNESS_ARGS_CONFLICT,
    HARNESS_ARGS_DANGEROUS_CONFIRMATION_REQUIRED,
    HARNESS_ARGS_INVALID,
    HARNESS_ARGS_INVALID_ARGUMENT,
    HARNESS_ARGS_METHOD_NOT_FOUND,
)
from hyprial.shell.impl.cli.runtime import CliRuntime

JsonObject = dict[str, Any]

args_app = group(
    help="Inspect, validate, replace, or clear persistent managed harness arguments.",
    no_args_is_help=True,
)

_GET = "agent.harness-args.get"
_VALIDATE = "agent.harness-args.validate"
_SET = "agent.harness-args.set"
_CLEAR = "agent.harness-args.clear"


def _cli_error(
    code: str, message: str, data: Mapping[str, Any] | None = None,
) -> Exception:
    return get_services().CliError(code, message, dict(data or {}))


def _request(
    runtime: CliRuntime, method: str, params: JsonObject,
) -> JsonObject:
    try:
        result = runtime.request(method, params)
    except Exception as error:
        if getattr(error, "code", None) == HARNESS_ARGS_METHOD_NOT_FOUND:
            raise _cli_error(
                HARNESS_ARGS_METHOD_NOT_FOUND,
                "the daemon does not support persistent harness arguments; "
                "upgrade the daemon and retry",
                getattr(error, "data", None),
            ) from error
        raise
    if not isinstance(result, dict):
        raise _cli_error(
            "INVALID_RESPONSE", f"{method} must return an object"
        )
    return result


def _stored_map(result: Mapping[str, Any]) -> JsonObject:
    value = result.get("harnessArgs")
    if not isinstance(value, dict) or any(
        not isinstance(key, str)
        or not isinstance(args, list)
        or any(not isinstance(arg, str) for arg in args)
        for key, args in value.items()
    ):
        raise _cli_error(
            "INVALID_RESPONSE",
            "daemon harness-args result must contain a string-array harnessArgs map",
        )
    return {key: list(args) for key, args in value.items()}


def _dangerous(result: Mapping[str, Any]) -> list[JsonObject]:
    value = result.get("dangerous")
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise _cli_error(
            "INVALID_RESPONSE", "daemon validation result must contain dangerous rows"
        )
    return [dict(row) for row in value]


def _validated(result: Mapping[str, Any]) -> bool:
    valid = result.get("valid")
    if not isinstance(valid, bool):
        raise _cli_error(
            "INVALID_RESPONSE", "daemon validation result must contain valid=true|false"
        )
    issues = result.get("issues")
    if not isinstance(issues, list) or any(
        not isinstance(issue, str) for issue in issues
    ):
        raise _cli_error(
            "INVALID_RESPONSE", "daemon validation result must contain issue strings"
        )
    _dangerous(result)
    return valid


def _mutation_request(
    runtime: CliRuntime, method: str, params: JsonObject,
) -> JsonObject:
    try:
        result = _request(runtime, method, params)
    except Exception as error:
        if getattr(error, "code", None) == HARNESS_ARGS_CONFLICT:
            raise _cli_error(
                HARNESS_ARGS_CONFLICT,
                "harnessArgs changed since read; re-run hyprial agent args show "
                "and then re-run set/clear",
                getattr(error, "data", None),
            ) from error
        raise
    _stored_map(result)
    if not isinstance(result.get("changed"), bool):
        raise _cli_error(
            "INVALID_RESPONSE", f"{method} must return changed=true|false"
        )
    return result


def _render_dangerous(rows: list[JsonObject]) -> str:
    lines = ["dangerous harness arguments:"]
    for row in rows:
        lines.append(
            f"  - {row.get('token', '?')} ({row.get('category', 'unknown')}): "
            f"{row.get('reason', '')}"
        )
    return "\n".join(lines)


def _render_show(data: Mapping[str, Any]) -> str:
    agent = data.get("agent")
    actor = agent.get("actor") if isinstance(agent, dict) else None
    lines = ["agent: " + str(actor or "?")]
    previews = data.get("harnesses")
    if not isinstance(previews, list) or not previews:
        return "\n".join([*lines, "stored harness args: (none)"])
    for preview in previews:
        if not isinstance(preview, dict):
            continue
        lines.extend(
            [
                f"{preview.get('harness', '?')}:",
                "  stored: "
                + json.dumps(preview.get("args", []), ensure_ascii=False),
                "  validation: "
                + ("VALID" if preview.get("valid") is True else "INVALID"),
            ]
        )
        for issue in preview.get("issues", []):
            lines.append(f"    - {issue}")
        for row in preview.get("dangerous", []):
            if isinstance(row, dict):
                lines.append(
                    f"  dangerous: {row.get('token', '?')} "
                    f"({row.get('category', 'unknown')})"
                )
    return "\n".join(lines)


def _render_validate(data: Mapping[str, Any]) -> str:
    lines = [
        f"harness: {data.get('harness', '?')}",
        "valid" if data.get("valid") is True else "invalid",
    ]
    for issue in data.get("issues", []):
        lines.append(f"  - {issue}")
    rows = _dangerous(data)
    if rows:
        lines.append(_render_dangerous(rows))
    return "\n".join(lines)


def _render_mutation(data: Mapping[str, Any], target: str) -> str:
    changed = "true" if data.get("changed") is True else "false"
    audit_id = data.get("auditId")
    return "\n".join(
        [
            f"changed: {changed}",
            f"target: {target}",
            f"auditId: {audit_id if audit_id is not None else '-'}",
        ]
    )


def register_harness_args_commands(
    app: typer.Typer, runtime: CliRuntime,
) -> Mapping[str, Any]:
    """Register the command leaves against the process runtime ports."""

    @app.command("show")
    def show(
        name: str = typer.Argument(..., help="Agent name."),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    ) -> None:
        """Show stored groups, validation issues, and dangerous classifications."""

        def operation() -> CliResult:
            result = _request(runtime, _GET, {"name": name})
            _stored_map(result)
            return CliResult(result, render=_render_show)

        runtime.execute(operation, json_output=json_output)

    @app.command("set", context_settings={"allow_extra_args": True})
    def set_group(
        ctx: typer.Context,
        name: str = typer.Argument(..., help="Agent name."),
        harness: str = typer.Argument(..., help="claude, codex, or pi."),
        yes_dangerous: bool = typer.Option(
            False, "--yes-dangerous", help="Confirm every reported dangerous argument."
        ),
        reason: str | None = typer.Option(None, "--reason", help="Audit reason."),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    ) -> None:
        """Replace one group after validation and explicit dangerous confirmation."""

        def operation() -> CliResult:
            args = list(ctx.args)
            if not args:
                raise _cli_error(
                    HARNESS_ARGS_INVALID_ARGUMENT,
                    "set requires harness arguments after --; use agent args clear "
                    "to remove a group",
                )
            current = _request(runtime, _GET, {"name": name})
            expected = _stored_map(current)
            validation = _request(
                runtime, _VALIDATE, {"harness": harness, "args": args}
            )
            if not _validated(validation):
                raise _cli_error(
                    HARNESS_ARGS_INVALID,
                    "harness arguments are invalid; run agent args validate for details",
                    {
                        "harness": harness,
                        "issues": list(validation["issues"]),
                    },
                )
            dangerous = _dangerous(validation)
            confirmed = False
            if dangerous:
                if yes_dangerous:
                    confirmed = True
                elif json_output or not get_services()._stdin_isatty():
                    raise _cli_error(
                        HARNESS_ARGS_DANGEROUS_CONFIRMATION_REQUIRED,
                        "dangerous harness arguments require --yes-dangerous "
                        "in non-interactive or --json mode",
                        {"harness": harness, "dangerous": dangerous},
                    )
                elif not confirm(
                    "Store these dangerous harness arguments?",
                    preview=_render_dangerous(dangerous),
                ):
                    raise _cli_error("CANCELLED", "harness argument set cancelled")
                else:
                    confirmed = True
            params: JsonObject = {
                "name": name,
                "harness": harness,
                "args": args,
                "expectedHarnessArgs": expected,
            }
            if confirmed:
                params["confirmDangerous"] = True
            if reason is not None:
                params["reason"] = reason
            result = _mutation_request(runtime, _SET, params)
            return CliResult(
                result,
                render=lambda data: _render_mutation(data, harness),
            )

        runtime.execute(operation, json_output=json_output)

    @app.command("validate", context_settings={"allow_extra_args": True})
    def validate(
        ctx: typer.Context,
        harness: str = typer.Argument(..., help="claude, codex, or pi."),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    ) -> None:
        """Validate arguments after a required double-dash separator."""

        def operation() -> CliResult:
            result = _request(
                runtime,
                _VALIDATE,
                {"harness": harness, "args": list(ctx.args)},
            )
            valid = _validated(result)
            return CliResult(
                {**result, "ok": valid},
                render=_render_validate,
            )

        runtime.execute(operation, json_output=json_output)

    @app.command("clear")
    def clear(
        name: str = typer.Argument(..., help="Agent name."),
        harness: str | None = typer.Argument(None, help="One stored harness group."),
        all_groups: bool = typer.Option(
            False, "--all", help="Clear every stored group, including historical ones."
        ),
        yes_all: bool = typer.Option(
            False, "--yes-all", help="Confirm clearing every stored group."
        ),
        reason: str | None = typer.Option(None, "--reason", help="Audit reason."),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    ) -> None:
        """Clear one group or, with explicit confirmation, every group."""

        def operation() -> CliResult:
            if (harness is not None) == all_groups:
                raise _cli_error(
                    HARNESS_ARGS_INVALID_ARGUMENT,
                    "pass exactly one harness or --all",
                )
            current = _request(runtime, _GET, {"name": name})
            expected = _stored_map(current)
            if all_groups and not yes_all:
                if json_output or not get_services()._stdin_isatty():
                    raise _cli_error(
                        "CONFIRMATION_REQUIRED",
                        "clearing all harness arguments requires --yes-all "
                        "in non-interactive or --json mode",
                    )
                groups = ", ".join(sorted(expected)) or "(none)"
                if not confirm(
                    "Clear every stored harness argument group?",
                    preview=f"stored groups: {groups}",
                ):
                    raise _cli_error("CANCELLED", "harness argument clear cancelled")
            params: JsonObject = {
                "name": name,
                "expectedHarnessArgs": expected,
            }
            if all_groups:
                params.update({"all": True, "confirmAll": True})
            else:
                params["harness"] = harness
            if reason is not None:
                params["reason"] = reason
            result = _mutation_request(runtime, _CLEAR, params)
            target = "all" if all_groups else str(harness)
            return CliResult(
                result,
                render=lambda data: _render_mutation(data, target),
            )

        runtime.execute(operation, json_output=json_output)

    order = {"show": 0, "validate": 1, "set": 2, "clear": 3}
    app.registered_commands.sort(
        key=lambda command: order.get(command.name or "", len(order))
    )
    return {
        "show": show,
        "validate": validate,
        "set": set_group,
        "clear": clear,
    }


__all__ = [
    "args_app",
    "register_harness_args_commands",
]
