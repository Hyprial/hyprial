"""Packaged operational text must not emit the retired direct PAC CLI."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCAN_ROOTS = (
    ROOT / "src/hyprial/skills",
    ROOT / ".agents/skills",
    ROOT / "src/hyprial/biz/impl/routine/templates",
    ROOT / "src/hyprial/routine/pac_dispatch.py",
    ROOT / 'src/hyprial/daemon/impl/pac/workflows/runtime/__init__.py',
    ROOT / "contract",
)
TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml"}


def scan_paths(roots: Iterable[Path]) -> list[Path]:
    paths: list[Path] = []
    for root in roots:
        candidates = [root] if root.is_file() else sorted(root.rglob("*"))
        paths.extend(
            path
            for path in candidates
            if path.is_file() and path.suffix in TEXT_SUFFIXES
        )
    return paths


def _positional_strings(call: ast.Call) -> list[object]:
    """Positional arguments in order, list/tuple elements flattened in place.

    String constants keep their value; anything else (a daemon handle, an
    executable path expression) is kept as ``None`` so position is preserved.
    """
    flattened: list[object] = []
    for argument in call.args:
        elements = argument.elts if isinstance(argument, (ast.List, ast.Tuple)) else [argument]
        for element in elements:
            is_text = isinstance(element, ast.Constant) and isinstance(element.value, str)
            flattened.append(element.value if is_text else None)
    return flattened


def _retired_argv_lines(path: Path, source: str) -> list[int]:
    """Calls whose first positional string names the retired ``pac`` command.

    Covers every shape the E2E-018 runner used: ``run_cli(daemon, "pac", ...)``,
    ``run_cli_as(daemon, env, "pac", ...)``, ``daemon.command("pac", ...)`` and
    ``subprocess.run([executable, "pac", ...])``.  A log category such as
    ``logger("warn", "pac", ...)`` is not a command word: its first string is
    the level, so it stays allowed.
    """
    if path.suffix != ".py":
        return []
    tree = ast.parse(source, filename=str(path))
    lines: list[int] = []
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        strings = [item for item in _positional_strings(call) if item is not None]
        if strings and strings[0] == "pac":
            lines.append(call.lineno)
    return lines


def retired_cli_offenders(roots: Iterable[Path]) -> tuple[list[Path], list[str]]:
    paths = scan_paths(roots)
    offenders: list[str] = []
    for path in paths:
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        for number, line in enumerate(lines, 1):
            if re.search(r"\bhyprial\s+pac(?:\s|$)", line):
                offenders.append(f"{path}:{number}: {line.strip()}")
        for number in _retired_argv_lines(path, source):
            offenders.append(f"{path}:{number}: retired pac argv list")
    return paths, offenders


def test_packaged_skills_templates_dispatch_and_contract_emit_no_hyprial_pac() -> None:
    paths, offenders = retired_cli_offenders(SCAN_ROOTS)
    assert paths, "retired CLI spelling guard scanned no files"
    assert not offenders, "retired command emitted by operational text:\n" + "\n".join(
        offenders
    )


def test_retired_cli_spelling_guard_mutation_catches_argv_but_not_log_category(
    tmp_path: Path,
) -> None:
    contract = tmp_path / "contract"
    contract.mkdir()
    mutation = contract / "mutated.py"
    # Every shape the E2E-018 runner actually used before the port.
    for planted in (
        'import subprocess\nsubprocess.run([str(executable), "pac", "events"])\n',
        'code, out, diag = run_cli(daemon, "pac", "graph", "show", "g", "--json")\n',
        'run_cli_as(daemon, env, "pac", "flag", "set", "g", "n")\n',
        'status = daemon.command("pac", "events", "g")\n',
    ):
        mutation.write_text(planted, encoding="utf-8")
        paths, offenders = retired_cli_offenders((contract,))
        assert paths == [mutation]
        assert offenders, f"planted pac command word must turn the guard red: {planted!r}"

    mutation.write_text(
        'self.logger("warn", "pac", "workflow.notification_failed")\n',
        encoding="utf-8",
    )
    paths, offenders = retired_cli_offenders((contract,))
    assert paths == [mutation]
    assert offenders == [], 'legitimate "pac" log category must remain allowed'
