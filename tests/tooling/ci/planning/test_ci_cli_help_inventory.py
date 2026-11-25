"""Executable CI guard for Typer registrations, help and the generated tree."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

from hyprial.shell.impl.cli.inventory import render_top_level_help
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


ROOT = Path(__file__).resolve().parents[4]
CHECKER = ROOT / "scripts" / "verify" / "check_cli_help_inventory.py"
INVENTORY = ROOT / "docs" / "reference" / "cli-command-inventory.json"


def _run_checker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), *args],
        cwd=ROOT,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )


def _inventory() -> dict[str, object]:
    return json.loads(INVENTORY.read_text(encoding="utf-8"))


def _write_mutated(tmp_path: Path, mutate) -> Path:
    payload = _inventory()
    mutate(payload)
    path = tmp_path / "mutated-inventory.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def test_ci_checker_accepts_live_generated_inventory() -> None:
    result = _run_checker()

    assert result.returncode == 0, result.stderr
    assert "PASS command tree inventory match" in result.stdout
    assert "PASS Typer/help top-level inventory match" in result.stdout


def test_ci_checker_rejects_one_missing_registered_command(tmp_path: Path) -> None:
    mutated = render_top_level_help(_cli_root_app).replace("  hyprial routine\n", "", 1)
    help_file = tmp_path / "missing-routine.txt"
    help_file.write_text(mutated, encoding="utf-8")

    result = _run_checker("--help-file", str(help_file))

    assert result.returncode == 1
    assert "FAIL Typer/help top-level inventory mismatch" in result.stderr
    assert "-visible\troutine" in result.stderr


def test_ci_checker_rejects_a_command_missing_from_the_generated_inventory(
    tmp_path: Path,
) -> None:
    def drop_agent_list(payload: dict[str, object]) -> None:
        commands = payload["commands"]
        assert isinstance(commands, list)
        commands[:] = [row for row in commands if row["path"] != "agent list"]

    result = _run_checker(
        "--inventory-file", str(_write_mutated(tmp_path, drop_agent_list))
    )

    assert result.returncode == 1
    assert "FAIL command tree inventory mismatch" in result.stderr
    assert "registered but absent from inventory: agent list" in result.stderr


def test_ci_checker_rejects_a_stale_inventory_entry(tmp_path: Path) -> None:
    def add_fake_command(payload: dict[str, object]) -> None:
        commands = payload["commands"]
        assert isinstance(commands, list)
        commands.append(
            {
                "path": "agent invented",
                "family": "agent",
                "kind": "leaf",
                "hidden": False,
                "executable": True,
                "params": [],
            }
        )

    result = _run_checker(
        "--inventory-file", str(_write_mutated(tmp_path, add_fake_command))
    )

    assert result.returncode == 1
    assert "inventory entry no longer registered: agent invented" in result.stderr


def test_ci_checker_rejects_parameter_drift(tmp_path: Path) -> None:
    def rename_an_option(payload: dict[str, object]) -> None:
        commands = payload["commands"]
        assert isinstance(commands, list)
        inspect = next(row for row in commands if row["path"] == "workflow inspect")
        option = next(param for param in inspect["params"] if "--node" in param["opts"])
        option["opts"] = ["--graph-node"]

    result = _run_checker(
        "--inventory-file", str(_write_mutated(tmp_path, rename_an_option))
    )

    assert result.returncode == 1
    assert "workflow inspect: params registered=" in result.stderr
    assert "--graph-node" in result.stderr


def test_ci_checker_rejects_visibility_or_executable_drift(tmp_path: Path) -> None:
    for field, value in (("hidden", True), ("executable", False)):
        def mutate(payload: dict[str, object], field: str = field, value: bool = value) -> None:
            commands = payload["commands"]
            assert isinstance(commands, list)
            teleport = next(row for row in commands if row["path"] == "agent teleport")
            teleport[field] = value

        result = _run_checker(
            "--inventory-file", str(_write_mutated(tmp_path, mutate))
        )

        assert result.returncode == 1, field
        assert f"agent teleport: {field} registered=" in result.stderr, field


def test_ci_checker_rejects_root_parameter_drift(tmp_path: Path) -> None:
    def add_root_parameter(payload: dict[str, object]) -> None:
        root_params = payload["rootParams"]
        assert isinstance(root_params, list)
        root_params.append(
            {
                "opts": ["--invented-root"],
                "secondaryOpts": [],
                "required": False,
                "isFlag": True,
                "multiple": False,
                "hidden": False,
                "type": "boolean",
                "help": None,
                "nargs": 1,
                "metavar": None,
                "default": False,
                "choices": [],
                "min": None,
                "max": None,
            }
        )

    result = _run_checker(
        "--inventory-file", str(_write_mutated(tmp_path, add_root_parameter))
    )

    assert result.returncode == 1
    assert "rootParams: registered=" in result.stderr


def test_ci_checker_rejects_constraint_and_help_drift(tmp_path: Path) -> None:
    def mutate(payload: dict[str, object]) -> None:
        commands = payload["commands"]
        assert isinstance(commands, list)
        port = next(
            param
            for command in commands
            if command["path"] == "gui"
            for param in command["params"]
            if param["opts"] == ["--port"]
        )
        port["min"] = 0
        port["max"] = 65536
        names = next(
            param
            for command in commands
            if command["path"] == "socialware install"
            for param in command["params"]
            if param["opts"] == ["names"]
        )
        names["nargs"] = 2
        help_param = next(
            param
            for command in commands
            if command["path"] == "org acl grant"
            for param in command["params"]
            if param["opts"] == ["--until"]
        )
        help_param["help"] = "renamed help text"

    result = _run_checker(
        "--inventory-file", str(_write_mutated(tmp_path, mutate))
    )

    assert result.returncode == 1
    assert "gui: params registered=" in result.stderr
    assert "socialware install: params registered=" in result.stderr
    assert "org acl grant: params registered=" in result.stderr


def test_family_must_match_the_real_callback_module() -> None:
    spec = importlib.util.spec_from_file_location("cli_inventory_gate", CHECKER)
    assert spec is not None and spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)

    assert gate._callback_family(
        "hyprial.shell.impl.cli.commands.agent.teleport"
    ) == "agent"
    assert gate._callback_family("hyprial.shell.impl.cli.commands.common.support") is None

    commands = _inventory()["commands"]
    assert isinstance(commands, list)
    mutated = [dict(command) for command in commands]
    teleport = next(
        command for command in mutated if command["path"] == "agent teleport"
    )
    teleport["family"] = "squire"
    problems = gate.callback_family_problems(mutated)
    assert any(
        "agent teleport: family=squire but callback module is agent" in problem
        for problem in problems
    ), problems

    teleport["callbackModule"] = None
    teleport["family"] = "agent"
    problems = gate.callback_family_problems(mutated)
    assert "agent teleport: leaf has no callback module" in problems


def test_ci_checker_rejects_a_missing_generated_inventory(tmp_path: Path) -> None:
    result = _run_checker("--inventory-file", str(tmp_path / "absent.json"))

    assert result.returncode == 1
    assert "FAIL command tree inventory missing" in result.stderr
    assert "--write" in result.stderr


def test_ci_checker_writes_the_committed_inventory_byte_for_byte(tmp_path: Path) -> None:
    regenerated = tmp_path / "regenerated.json"

    result = _run_checker("--write", "--inventory-file", str(regenerated))

    assert result.returncode == 0, result.stderr
    assert regenerated.read_text(encoding="utf-8") == INVENTORY.read_text(encoding="utf-8")
