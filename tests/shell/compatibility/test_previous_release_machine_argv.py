"""Cross-version contract for machine argv written by Hyprial 0.5.4.

The fixture is a frozen corpus copied from OLD commit
ffb63ad011a57f388c039f6e0c4b11f1bac2834e.  Each source records its path and
relevant line ranges.  The tests derive argv from those sources rather than
retyping the command tails here.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import html
import json
from pathlib import Path
import re
import shlex
import sys
from typing import Any

from typer.main import get_command

import hyprial.cli as installed_cli
from hyprial.shell.impl.cli.runtime import entry as cli
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.runtime import machine_argv as _cli_machine_argv


ROOT = Path(__file__).resolve().parents[3]
CORPUS_PATH = ROOT / "tests/fixtures/cli_compat/old-0.5.4-machine-argv.json"
OLD_COMMIT = "ffb63ad011a57f388c039f6e0c4b11f1bac2834e"


@dataclass(frozen=True)
class OldInvocation:
    origin: str
    argv: tuple[str, ...]
    target: str


def _corpus() -> dict[str, Any]:
    value = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    assert value["oldCommit"] == OLD_COMMIT
    assert value["schemaVersion"] == 1
    return value


def _strings_in_list(source: str, marker: str) -> tuple[ast.List, ...]:
    tree = ast.parse(source)
    return tuple(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.List)
        and any(
            isinstance(item, ast.Constant) and item.value == marker
            for item in node.elts
        )
    )


def _value(
    node: ast.AST,
    *,
    names: dict[str, Any],
    server_args: tuple[str, ...] = (),
) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return names[node.id]
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
        and node.attr == "executable"
    ):
        return "/python"
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        for item in node.values:
            try:
                value = _value(item, names=names, server_args=server_args)
            except KeyError:
                continue
            if value:
                return value
        raise KeyError("old boolean expression has no selected value")
    if isinstance(node, ast.Subscript):
        owner = _value(node.value, names=names, server_args=server_args)
        key = _value(node.slice, names=names, server_args=server_args)
        return owner[key]
    if isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords:
        argument = _value(node.args[0], names=names, server_args=server_args)
        if isinstance(node.func, ast.Name) and node.func.id == "str":
            return str(argument)
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "json"
            and node.func.attr == "dumps"
        ):
            return json.dumps(argument)
    raise AssertionError(f"unsupported OLD argv expression: {ast.dump(node)}")


def _argv_list(
    source: str,
    marker: str,
    *,
    names: dict[str, Any],
    server_args: tuple[str, ...] = (),
    occurrence: int = 0,
) -> tuple[str, ...]:
    candidates = _strings_in_list(source, marker)
    node = candidates[occurrence]
    values: list[str] = []
    for item in node.elts:
        if isinstance(item, ast.Starred):
            expanded = _value(item.value, names=names, server_args=server_args)
            values.extend(str(value) for value in expanded)
        else:
            values.append(str(_value(item, names=names, server_args=server_args)))
    return tuple(values)


def _launchd_argv(source: str) -> tuple[str, ...]:
    block = source.split("<key>ProgramArguments</key>", 1)[1].split("</array>", 1)[0]
    values = tuple(html.unescape(item) for item in re.findall(r"<string>(.*?)</string>", block))
    return tuple("/hyprial" if value == "{{EXECUTABLE}}" else value for value in values)


def _systemd_argv(source: str) -> tuple[str, ...]:
    line = next(line for line in source.splitlines() if line.startswith("ExecStart="))
    return tuple(shlex.split(line.removeprefix("ExecStart=").replace("{{EXECUTABLE_ARG}}", "/hyprial")))


def _root_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    values = list(argv)
    if values and values[0] in {"/python", "/hyprial"}:
        values.pop(0)
    if values[:2] == ["-m", "hyprial.cli"]:
        del values[:2]
    return tuple(values)


def _old_invocations() -> tuple[OldInvocation, ...]:
    corpus = _corpus()
    artifacts = corpus["artifacts"]
    names: dict[str, Any] = {
        "actor": "agent:owner:machine:worker",
        "actor_name": "worker",
        "command": "claude",
        "control_socket": Path("/tmp/control.sock"),
        "cwd": Path("/tmp/workspace"),
        "launch_dir": Path("/tmp/launch"),
        "python_executable": "/python",
        "recovery_path": Path("/tmp/recover"),
        "server": {"args": ()},
        "session_ref": "session-1",
        "session_ref_file": Path("/tmp/session-ref"),
        "turn_signal_dir": Path("/tmp/turns"),
        "tui_argv": ["dsh-tui"],
    }

    worker_args = _argv_list(
        artifacts["worker"]["source"], "agent-channel", names=names
    )
    names["server"] = {"args": worker_args}
    codex_args = _argv_list(
        artifacts["codex"]["source"],
        "--session-ref-file",
        names=names,
        server_args=worker_args,
    )
    recoveries = tuple(
        _argv_list(
            artifacts["claude"]["source"],
            "claude-channel-recover",
            names=names,
            occurrence=index,
        )
        for index in range(2)
    )

    rendered = [
        ("launchd timer", _launchd_argv(artifacts["launchd"]["source"]), "typer"),
        ("systemd timer", _systemd_argv(artifacts["systemd"]["source"]), "typer"),
        (
            "daemon scheduler child",
            _argv_list(artifacts["scheduler"]["source"], "autoupdate", names=names),
            "typer",
        ),
        (
            "Claude --mcp-config",
            _argv_list(artifacts["claude"]["source"], "claude-channel", names=names),
            "mcp",
        ),
        ("Claude SessionStart hook", recoveries[0], "mcp"),
        ("Claude StopFailure hook", recoveries[1], "mcp"),
        ("worker channel", worker_args, "mcp"),
        ("Codex app-server -c", codex_args, "mcp"),
        (
            "dsh bridge client",
            _argv_list(artifacts["dsh"]["source"], "agent-channel", names=names),
            "mcp",
        ),
        (
            "dsh carrier",
            _argv_list(artifacts["dsh"]["source"], "dsh-carrier", names=names),
            "mcp",
        ),
    ]
    rendered.extend(
        ("documented R5 timer variant", tuple(item["argv"]), "typer")
        for item in corpus["documentedVariants"]
    )
    return tuple(
        OldInvocation(origin, _root_argv(tuple(argv)), target)
        for origin, argv, target in rendered
    )


def _assert_every_old_invocation_resolves() -> None:
    for old in _old_invocations():
        resolved = _cli_machine_argv.resolve_machine_compatibility(old.argv)
        assert resolved is not None, old.origin
        assert resolved.target == old.target, old.origin
        if old.target == "typer":
            assert resolved.argv == ("daemon", "upgrade", "run", "--json"), old.origin
        else:
            assert resolved.argv == old.argv[1:], old.origin


def test_frozen_corpus_records_old_sha_path_and_line_provenance() -> None:
    corpus = _corpus()
    assert set(corpus["artifacts"]) == {
        "launchd",
        "systemd",
        "scheduler",
        "claude",
        "worker",
        "codex",
        "dsh",
    }
    for artifact in corpus["artifacts"].values():
        assert artifact["path"].startswith("src/hyprial/")
        assert artifact["lines"]
        assert artifact["source"].endswith("\n")
    variant = corpus["documentedVariants"][0]
    assert variant["commit"] == "f83a2862c015558999c4ac4b16610f91581b823f"
    assert variant["path"] == "docs/design/daemon-upgrade-steps-2026-10-20.md"
    assert variant["lines"] == [204, 210]


def test_every_old_rendered_machine_argv_resolves_to_the_new_handler() -> None:
    _assert_every_old_invocation_resolves()


def test_each_individual_shim_is_required_by_the_cross_version_corpus(monkeypatch) -> None:
    """Mutation proof: deleting any one of the six shims makes the contract red."""

    for prefix in _cli_machine_argv._LEGACY_CLI_REWRITES:
        with monkeypatch.context() as mutation:
            mutation.setattr(
                _cli_machine_argv,
                "_LEGACY_CLI_REWRITES",
                {key: value for key, value in _cli_machine_argv._LEGACY_CLI_REWRITES.items() if key != prefix},
            )
            try:
                _assert_every_old_invocation_resolves()
            except AssertionError:
                pass
            else:  # pragma: no cover - proves the mutation must be observed
                raise AssertionError(f"removing {prefix!r} did not turn the contract red")

    for subcommand in _cli_machine_argv._LEGACY_MCP_SUBCOMMANDS:
        with monkeypatch.context() as mutation:
            mutation.setattr(
                _cli_machine_argv,
                "_LEGACY_MCP_SUBCOMMANDS",
                _cli_machine_argv._LEGACY_MCP_SUBCOMMANDS - {subcommand},
            )
            try:
                _assert_every_old_invocation_resolves()
            except AssertionError:
                pass
            else:  # pragma: no cover - proves the mutation must be observed
                raise AssertionError(
                    f"removing mcp {subcommand!r} did not turn the contract red"
                )


def test_main_routes_old_upgrade_argv_through_the_current_typer_path(monkeypatch) -> None:
    calls: list[tuple[tuple[str, ...] | None, bool]] = []

    def fake_app(*, args=None, standalone_mode=False):
        calls.append((tuple(args) if args is not None else None, standalone_mode))

    monkeypatch.setattr(cli, "app", fake_app)
    monkeypatch.setattr(sys, "argv", ["hyprial", "autoupdate", "run", "--json"])

    cli.main()

    assert calls == [(('daemon', 'upgrade', 'run', '--json'), False)]


def test_main_routes_old_mcp_argv_to_the_private_process_entry(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(cli, "run_machine_mcp_entry", lambda argv: calls.append(tuple(argv)) or 0)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hyprial",
            "mcp",
            "agent-channel",
            "--actor",
            "agent:owner:machine:worker",
        ],
    )

    cli.main()

    assert calls == [
        ("agent-channel", "--actor", "agent:owner:machine:worker")
    ]


def test_compatibility_entries_are_absent_from_all_help_inventories() -> None:
    root = get_command(_cli_root_app)
    assert "autoupdate" not in root.commands
    assert "upgrade" not in root.commands
    assert "mcp" not in root.commands
    assert _cli_machine_argv.resolve_machine_compatibility(("mcp", "agent-channel", "--help")) is None
    assert _cli_machine_argv.resolve_machine_compatibility(("mcp", "agent-channel", "-h")) is None
    assert installed_cli.main is cli.main
