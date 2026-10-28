"""Canonical, side-effect-free inventory of the registered CLI command tree."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

import typer
from typer.main import get_command

SCHEMA_VERSION = 1
REGENERATE_COMMAND = (
    "uv run --python 3.12 python scripts/verify/update_cli_surface_snapshot.py"
)


def _literal(value: object) -> tuple[bool, object]:
    """Return a JSON value only for stable literal defaults.

    Click retains callables and richer Python objects in ``default``.  Calling or
    stringifying those objects can read the environment or embed a machine path,
    so the inventory deliberately omits them.
    """

    if value is None or isinstance(value, (bool, int, str)):
        return True, value
    if isinstance(value, float):
        return (True, value) if math.isfinite(value) else (False, None)
    if isinstance(value, (list, tuple)):
        items: list[object] = []
        for item in value:
            stable, encoded = _literal(item)
            if not stable:
                return False, None
            items.append(encoded)
        return True, items
    if isinstance(value, Mapping) and all(isinstance(key, str) for key in value):
        result: dict[str, object] = {}
        for key in sorted(value):
            stable, encoded = _literal(value[key])
            if not stable:
                return False, None
            result[key] = encoded
        return True, result
    return False, None


def _short_help(command: Any) -> str:
    raw = command.short_help or command.help or ""
    for line in raw.splitlines():
        if line.strip():
            return " ".join(line.split())
    return ""


def _choices(parameter: Any) -> list[object] | None:
    raw = getattr(parameter.type, "choices", None)
    if raw is None:
        return None
    choices: list[object] = []
    for choice in raw:
        stable, encoded = _literal(choice)
        choices.append(encoded if stable else str(choice))
    return sorted(
        choices,
        key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
    )


def _argument(parameter: Any) -> dict[str, object]:
    return {
        "name": parameter.name,
        "nargs": parameter.nargs,
        "required": bool(parameter.required),
    }


def _option(parameter: Any) -> dict[str, object]:
    flags = sorted({*parameter.opts, *parameter.secondary_opts})
    result: dict[str, object] = {
        "flags": flags,
        "is_flag": bool(parameter.is_flag),
        "multiple": bool(parameter.multiple),
        "name": parameter.name,
        "required": bool(parameter.required),
    }
    stable, default = _literal(parameter.default)
    if stable:
        result["default"] = default
    choices = _choices(parameter)
    if choices is not None:
        result["choices"] = choices
    return result


def _command_record(command: Any, path: tuple[str, ...]) -> dict[str, object]:
    arguments = [
        _argument(parameter)
        for parameter in command.params
        if parameter.param_type_name == "argument"
    ]
    options = sorted(
        (
            _option(parameter)
            for parameter in command.params
            if parameter.param_type_name == "option"
        ),
        key=lambda item: (item["flags"], item["name"]),
    )
    return {
        "arguments": arguments,
        "deprecated": bool(command.deprecated),
        "help": _short_help(command),
        "hidden": bool(command.hidden),
        "name": path[-1],
        "options": options,
        "path": " ".join(path),
    }


def extract_cli_surface(
    typer_app: typer.Typer, *, root_name: str = "hyprial"
) -> dict[str, object]:
    """Walk the real Typer/Click tree and return a canonical JSON document."""

    root = get_command(typer_app)
    records: list[dict[str, object]] = []

    def visit(command: Any, path: tuple[str, ...]) -> None:
        records.append(_command_record(command, path))
        children = getattr(command, "commands", None)
        if children is None:
            return
        for name in sorted(children):
            visit(children[name], (*path, name))

    visit(root, (root_name,))
    records.sort(key=lambda item: item["path"])
    return {"commands": records, "schemaVersion": SCHEMA_VERSION}


def render_cli_surface(document: Mapping[str, object]) -> str:
    """Render stable JSON with one command per line to stay below line gates."""

    commands = document.get("commands")
    if not isinstance(commands, Sequence):
        raise ValueError("CLI surface document has no commands array")
    lines = ["{", f'  "schemaVersion": {SCHEMA_VERSION},', '  "commands": [']
    for index, command in enumerate(commands):
        suffix = "," if index + 1 < len(commands) else ""
        lines.append(
            "    "
            + json.dumps(
                command,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + suffix
        )
    lines.extend(["  ]", "}"])
    return "\n".join(lines) + "\n"


def _option_label(option: Mapping[str, object]) -> str:
    flags = option.get("flags")
    if isinstance(flags, list) and flags:
        return "/".join(str(flag) for flag in flags)
    return str(option.get("name", "<unnamed>"))


def _format_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def format_cli_surface_diff(
    expected: Mapping[str, object], actual: Mapping[str, object]
) -> str:
    """Return a grouped command/option diff suitable for a pytest failure."""

    expected_by_path = {
        item["path"]: item for item in expected.get("commands", [])  # type: ignore[union-attr]
    }
    actual_by_path = {
        item["path"]: item for item in actual.get("commands", [])  # type: ignore[union-attr]
    }
    expected_paths = set(expected_by_path)
    actual_paths = set(actual_by_path)
    lines = ["CLI command surface differs from contract/cli/surface.json."]

    lines.append("\nREMOVED COMMAND PATHS")
    removed = sorted(expected_paths - actual_paths)
    lines.extend(f"  - {path}" for path in removed)
    if not removed:
        lines.append("  (none)")

    lines.append("\nADDED COMMAND PATHS")
    added = sorted(actual_paths - expected_paths)
    lines.extend(f"  + {path}" for path in added)
    if not added:
        lines.append("  (none)")

    changed_paths = [
        path
        for path in sorted(expected_paths & actual_paths)
        if expected_by_path[path] != actual_by_path[path]
    ]
    lines.append("\nCHANGED COMMAND PATHS AND OPTIONS")
    if not changed_paths:
        lines.append("  (none)")
    for path in changed_paths:
        before = expected_by_path[path]
        after = actual_by_path[path]
        lines.append(f"  * {path}")
        for field in ("name", "hidden", "deprecated", "help", "arguments"):
            if before.get(field) != after.get(field):
                lines.append(
                    f"      {field}: {_format_value(before.get(field))} -> "
                    f"{_format_value(after.get(field))}"
                )
        before_options = {item["name"]: item for item in before.get("options", [])}
        after_options = {item["name"]: item for item in after.get("options", [])}
        for name in sorted(set(before_options) - set(after_options)):
            lines.append(f"      - option {_option_label(before_options[name])}")
        for name in sorted(set(after_options) - set(before_options)):
            lines.append(f"      + option {_option_label(after_options[name])}")
        for name in sorted(set(before_options) & set(after_options)):
            if before_options[name] != after_options[name]:
                lines.append(
                    f"      * option {_option_label(after_options[name])}: "
                    f"{_format_value(before_options[name])} -> "
                    f"{_format_value(after_options[name])}"
                )

    lines.extend(["", f"Regenerate with: {REGENERATE_COMMAND}"])
    return "\n".join(lines)
