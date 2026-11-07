#!/usr/bin/env python3
"""Gate the generated CLI command-tree inventory against Typer's registration.

The committed JSON is derived from the live registration tree, never edited by
hand.  A changed command, visibility, executable group or parameter fails this
gate until the author regenerates the file with ``--write`` and reviews the
diff.  ``hyprial help`` is checked in the same run so the human top-level view
cannot drift from the same tree.
"""

from __future__ import annotations

import argparse
import difflib
import enum
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INVENTORY = ROOT / "docs" / "reference" / "cli-command-inventory.json"

#: The 25 source command families named by the A design.  ``common`` is shared
#: infrastructure, not a family.  Nested groups whose family moved in the
#: 2026 consolidation retain their original source owner until that group's
#: migration lands; the family names still correspond to source directories.
TOP_LEVEL_FAMILIES = {
    "adapter": "adapter",
    "agent": "agent",
    "config": "config",
    "daemon": "daemon",
    "doctor": "doctor",
    "gui": "gui",
    "help": "meta",
    "host": "host",
    "init": "init",
    "log": "log",
    "login": "login",
    "logout": "login",
    "message": "message",
    "org": "org",
    "profile": "profile",
    "routine": "routine",
    "socialware": "socialware",
    "squire": "squire",
    "user": "user",
    "version": "meta",
    "workflow": "workflow",
}
FAMILY_PREFIXES = {
    "agent link": "link",
    "agent move": "transfer",
    "agent top": "daemon",
    "agent transfer": "transfer",
    "daemon upgrade": "upgrade",
    "init onboarding": "init",
    "org fs": "fs",
    "org service": "service",
    "org work": "work",
    "user proxy": "squire",
}
SOURCE_FAMILIES = (
    "adapter",
    "agent",
    "config",
    "daemon",
    "doctor",
    "fs",
    "gui",
    "host",
    "init",
    "link",
    "log",
    "login",
    "message",
    "meta",
    "org",
    "profile",
    "routine",
    "service",
    "socialware",
    "squire",
    "transfer",
    "upgrade",
    "user",
    "work",
    "workflow",
)


def _listed_inventory(usage: str) -> tuple[tuple[str, bool], ...]:
    commands: list[tuple[str, bool]] = []
    hidden: bool | None = None
    for line in usage.splitlines():
        if line == "Commands:":
            hidden = False
        elif line == "Hidden commands (internal):":
            hidden = True
        elif line.startswith("  hyprial ") and hidden is not None:
            commands.append((line.removeprefix("  hyprial "), hidden))
    return tuple(commands)


def _lines(commands: tuple[tuple[str, bool], ...]) -> list[str]:
    return [f"{'hidden' if hidden else 'visible'}\t{name}" for name, hidden in commands]


def _read_live_help() -> str:
    from typer.testing import CliRunner

    from hyprial.shell.impl.cli.runtime import entry as cli

    result = CliRunner().invoke(cli.app, ["help", "--json"])
    if result.exit_code != 0:
        raise RuntimeError(f"hyprial help --json failed: {result.output.strip()}")
    payload = json.loads(result.stdout)
    usage = payload.get("usage")
    if not isinstance(usage, str):
        raise RuntimeError("hyprial help --json did not return a string usage field")
    return usage


def family_for(path: str) -> str:
    for prefix, family in FAMILY_PREFIXES.items():
        if path == prefix or path.startswith(f"{prefix} "):
            return family
    top_level = path.split(" ", 1)[0]
    try:
        return TOP_LEVEL_FAMILIES[top_level]
    except KeyError as error:  # a new top-level command needs an owner
        raise RuntimeError(
            f"registered top-level command {top_level!r} has no source family"
        ) from error


def _stable_value(value: object) -> object:
    """Serialize a default/constraint without calling or repr-ing live objects."""

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, enum.Enum):
        return {
            "kind": "enum",
            "type": type(value).__name__,
            "name": value.name,
        }
    if isinstance(value, (int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_stable_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _stable_value(item) for key, item in value.items()}
    if callable(value):
        # Never invoke a default/parser.  The qualified name is stable and
        # carries no object address.
        return {
            "kind": "callable",
            "name": getattr(value, "__qualname__", type(value).__qualname__),
        }
    return {
        "kind": "unsupported",
        "type": f"{type(value).__module__}.{type(value).__qualname__}",
    }


def _parameter_payload(parameter: object) -> dict[str, object]:
    choices = getattr(parameter, "choices", ()) or ()
    return {
        "opts": list(getattr(parameter, "opts", ())),
        "secondaryOpts": list(getattr(parameter, "secondary_opts", ())),
        "required": bool(getattr(parameter, "required", False)),
        "isFlag": bool(getattr(parameter, "is_flag", False)),
        "multiple": bool(getattr(parameter, "multiple", False)),
        "hidden": bool(getattr(parameter, "hidden", False)),
        "type": getattr(parameter, "type", ""),
        "help": getattr(parameter, "help", None),
        "nargs": getattr(parameter, "nargs", 1),
        "metavar": getattr(parameter, "metavar", None),
        "default": _stable_value(getattr(parameter, "default", None)),
        "choices": [_stable_value(choice) for choice in choices],
        "min": _stable_value(getattr(parameter, "minimum", None)),
        "max": _stable_value(getattr(parameter, "maximum", None)),
    }


def _callback_family(module: str | None) -> str | None:
    prefix = "hyprial.shell.impl.cli.commands."
    if not module or not module.startswith(prefix):
        return None
    family = module[len(prefix) :].split(".", 1)[0]
    return family if family in SOURCE_FAMILIES else None


# Dispatcher wrappers whose callback lives in a shared module would be listed
# here explicitly with their owning family.  The current tree needs none: every
# executable entry's callback is attributed from its real module.
CALLBACK_FAMILY_OVERRIDES: dict[str, str] = {}


def callback_family_problems(commands: list[dict[str, object]]) -> list[str]:
    """Every executable entry must match the family of its actual callback."""

    problems: list[str] = []
    attributed: set[str] = set()
    for command in commands:
        path = str(command["path"])
        module = command.get("callbackModule")
        if module is None:
            if command["kind"] == "leaf":
                problems.append(f"{path}: leaf has no callback module")
            continue
        derived = CALLBACK_FAMILY_OVERRIDES.get(str(module)) or _callback_family(str(module))
        if derived is None:
            problems.append(f"{path}: cannot attribute callback module {module!r}")
            continue
        attributed.add(derived)
        if derived != command["family"]:
            problems.append(
                f"{path}: family={command['family']} but callback module is {derived}"
            )
    missing = sorted(set(SOURCE_FAMILIES) - attributed)
    if missing:
        problems.append(f"source families with no attributed callback: {missing}")
    return problems


def inventory_payload(typer_app: object) -> dict[str, object]:
    from typer.main import get_command

    from hyprial.shell.impl.cli.inventory import (
        registered_command_tree,
        registered_root_parameters,
    )

    entries = registered_command_tree(typer_app)  # type: ignore[arg-type]
    commands: list[dict[str, object]] = []
    for entry in entries:
        commands.append(
            {
                "path": entry.path,
                "family": family_for(entry.path),
                "kind": entry.kind,
                "hidden": entry.hidden,
                "directHidden": entry.direct_hidden,
                "executable": entry.executable,
                "callbackModule": entry.callback_module,
                "help": entry.help,
                "params": [_parameter_payload(p) for p in entry.params],
            }
        )
    family_problems = callback_family_problems(commands)
    if family_problems:
        raise RuntimeError("command family attribution failed:\n" + "\n".join(family_problems))
    root_params = [
        _parameter_payload(parameter)
        for parameter in registered_root_parameters(typer_app)  # type: ignore[arg-type]
    ]
    leaves = sum(command["kind"] == "leaf" for command in commands)
    groups = len(commands) - leaves
    executable_groups = sum(
        command["kind"] == "group" and bool(command["executable"])
        for command in commands
    )
    executable_entries = leaves + executable_groups
    hidden_entries = sum(
        bool(command["hidden"]) and bool(command["executable"])
        for command in commands
    )
    families = sorted({str(command["family"]) for command in commands})
    missing_families = sorted(set(SOURCE_FAMILIES) - set(families))
    if missing_families:
        raise RuntimeError(f"registered tree is missing source families: {missing_families}")
    return {
        "schemaVersion": 1,
        "generator": "scripts/verify/check_cli_help_inventory.py --write",
        "root": "hyprial",
        "rootHelp": getattr(get_command(typer_app), "help", None),
        "rootParams": root_params,
        "sourceFamilies": list(SOURCE_FAMILIES),
        "counts": {
            "nodes": len(commands),
            "rootInclusiveNodes": len(commands) + 1,
            "leaves": leaves,
            "groups": groups,
            "groupsIncludingRoot": groups + 1,
            "executableGroups": executable_groups,
            "entries": executable_entries,
            "hiddenNodes": sum(bool(command["hidden"]) for command in commands),
            "directHiddenNodes": sum(bool(command["directHidden"]) for command in commands),
            "hiddenEntries": hidden_entries,
            "visibleEntries": executable_entries - hidden_entries,
            "rootParams": len(root_params),
        },
        "commands": commands,
    }


def _serialize(payload: dict[str, object]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _mismatches(actual: dict[str, object], expected: dict[str, object]) -> list[str]:
    problems: list[str] = []
    for field in ("schemaVersion", "root", "rootHelp", "rootParams", "sourceFamilies", "counts"):
        if actual.get(field) != expected.get(field):
            problems.append(
                f"{field}: registered={actual.get(field)!r} inventory={expected.get(field)!r}"
            )
    actual_commands = {
        str(command["path"]): command for command in actual.get("commands", [])
    }
    expected_commands = {
        str(command["path"]): command for command in expected.get("commands", [])
    }
    for path in sorted(set(actual_commands) | set(expected_commands)):
        fresh = actual_commands.get(path)
        recorded = expected_commands.get(path)
        if recorded is None:
            problems.append(f"registered but absent from inventory: {path}")
            continue
        if fresh is None:
            problems.append(f"inventory entry no longer registered: {path}")
            continue
        for field in (
            "family",
            "kind",
            "hidden",
            "directHidden",
            "executable",
            "callbackModule",
            "help",
            "params",
        ):
            if fresh.get(field) == recorded.get(field):
                continue
            if field == "params":
                fresh_params = fresh.get("params") or []
                recorded_params = recorded.get("params") or []
                for index in range(max(len(fresh_params), len(recorded_params))):
                    fresh_param = fresh_params[index] if index < len(fresh_params) else None
                    recorded_param = recorded_params[index] if index < len(recorded_params) else None
                    if fresh_param != recorded_param:
                        problems.append(
                            f"{path}: params registered={fresh_param!r} "
                            f"inventory={recorded_param!r}"
                        )
                        break
                continue
            problems.append(
                f"{path}: {field} registered={fresh.get(field)!r} "
                f"inventory={recorded.get(field)!r}"
            )
    return problems


def _load_inventory(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} must contain a JSON object")
    return payload


def _write_inventory(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(_serialize(payload), encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--help-file",
        type=Path,
        help="Compare this captured help text instead of invoking hyprial (test hook).",
    )
    parser.add_argument(
        "--inventory-file",
        type=Path,
        default=DEFAULT_INVENTORY,
        help=f"Generated command-tree inventory (default: {DEFAULT_INVENTORY.relative_to(ROOT)}).",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Regenerate --inventory-file from the live tree instead of comparing.",
    )
    args = parser.parse_args(argv)

    # Import hyprial only after selecting an empty home. This keeps any
    # operator-machine state out of the source-tree CI gate.
    with tempfile.TemporaryDirectory(prefix="hyprial-help-inventory-") as raw:
        home = Path(raw) / "home"
        state = Path(raw) / "state"
        home.mkdir(mode=0o700)
        state.mkdir(mode=0o700)
        os.environ["HYPRIAL_HOME"] = str(home)
        os.environ["HARNESS_STATE_DIR"] = str(state)

        from hyprial.shell.impl.cli.runtime import entry as cli
        from hyprial.shell.impl.cli.inventory import registered_top_level

        registered = registered_top_level(cli.app)
        # The human inventory groups visible commands before internal ones;
        # preserve registration order within each visibility class.
        actual = tuple(
            (item.name, item.hidden)
            for hidden in (False, True)
            for item in registered
            if item.hidden is hidden
        )
        payload = inventory_payload(cli.app)
        usage = (
            args.help_file.read_text(encoding="utf-8")
            if args.help_file is not None
            else _read_live_help()
        )

    if args.write:
        _write_inventory(args.inventory_file, payload)
        print(f"WROTE command tree inventory: {args.inventory_file}")
    else:
        if not args.inventory_file.is_file():
            print(
                "FAIL command tree inventory missing: "
                f"{args.inventory_file} (run --write and review the diff)",
                file=sys.stderr,
            )
            return 1
        problems = _mismatches(payload, _load_inventory(args.inventory_file))
        if problems:
            print("FAIL command tree inventory mismatch", file=sys.stderr)
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            print(
                f"  regenerate with --write after reviewing every drift "
                f"({args.inventory_file})",
                file=sys.stderr,
            )
            return 1
        counts = payload["counts"]
        print(
            "PASS command tree inventory match: "
            f"{counts['nodes']} nodes ({counts['leaves']} leaves, "
            f"{counts['groups']} groups, {counts['executableGroups']} executable groups; "
            f"{counts['entries']} executable entries, {counts['hiddenNodes']} hidden nodes "
            f"({counts['directHiddenNodes']} direct))"
        )

    listed = _listed_inventory(usage)
    if listed != actual:
        diff = "\n".join(
            difflib.unified_diff(
                _lines(actual),
                _lines(listed),
                fromfile="Typer registration tree",
                tofile="hyprial help inventory",
                lineterm="",
            )
        )
        print("FAIL Typer/help top-level inventory mismatch", file=sys.stderr)
        print(diff, file=sys.stderr)
        return 1

    visible = sum(not hidden for _, hidden in actual)
    hidden = len(actual) - visible
    print(
        "PASS Typer/help top-level inventory match: "
        f"{len(actual)} commands ({visible} visible, {hidden} hidden)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
