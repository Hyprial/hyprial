"""Shell and GUI code never import the daemon's private teleport package.

Handoff §3: CLI and GUI only wrap the ``teleport.run`` daemon request; they
neither choose the next step nor import ``hyprial.daemon.impl.teleport``.
Python sources under ``src/hyprial/shell`` are checked by AST (every import
form) plus dynamic-import strings; the root ``gui/`` tree is checked by text,
because it is TypeScript/JavaScript and must not even name the path.  The CLI
command lives in ``hyprial.shell.impl.cli.commands.agent.teleport`` so the
fence is known to cover it.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SHELL = ROOT / "src" / "hyprial" / "shell"
GUI = ROOT / "gui"
CLI_COMMAND = SHELL / "impl" / "cli" / "commands" / "agent" / "teleport" / "__init__.py"
PRIVATE = "hyprial.daemon.impl.teleport"
GUI_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".json", ".vue"}
GUI_SKIP = {"node_modules", "dist", ".vite", "coverage"}


def _private(module: str) -> bool:
    return module == PRIVATE or module.startswith(PRIVATE + ".")


def python_offences(source: str) -> list[str]:
    """Every way ``source`` reaches the private teleport package."""

    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names if _private(alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if _private(node.module):
                found.append(node.module)
            elif node.module == "hyprial.daemon.impl":
                found.extend(
                    f"{node.module}.{alias.name}"
                    for alias in node.names
                    if alias.name == "teleport"
                )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if PRIVATE in node.value or "daemon/impl/teleport" in node.value:
                found.append(node.value)
    return found


def test_checker_catches_every_import_form() -> None:
    for sample in (
        "import hyprial.daemon.impl.teleport\n",
        "import hyprial.daemon.impl.teleport.source as s\n",
        "from hyprial.daemon.impl.teleport.source import AgentTeleportService\n",
        "from hyprial.daemon.impl import teleport\n",
        "import importlib\nimportlib.import_module('hyprial.daemon.impl.teleport')\n",
    ):
        assert python_offences(sample), sample
    assert python_offences("from hyprial.daemon import daemon_catalog\n") == []
    assert python_offences("x = 'teleport.run'\n") == []


def test_the_cli_command_is_inside_the_fenced_tree() -> None:
    assert CLI_COMMAND.is_file()
    assert SHELL in CLI_COMMAND.parents


def test_shell_never_imports_daemon_teleport() -> None:
    offenders = {
        str(path.relative_to(ROOT)): hits
        for path in sorted(SHELL.rglob("*.py"))
        if (hits := python_offences(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}


def test_gui_never_names_daemon_teleport() -> None:
    offenders = []
    for directory, subdirs, files in os.walk(GUI):
        subdirs[:] = sorted(name for name in subdirs if name not in GUI_SKIP)
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix not in GUI_SUFFIXES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if PRIVATE in text or "daemon/impl/teleport" in text:
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
