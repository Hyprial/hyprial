"""Nested CLI argv in E2E runners, including calls inside f-strings."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[5]


@pytest.mark.parametrize(
    ("relative", "method", "old", "replacement", "minimum"),
    [
        ("run-tailcat-cycle-upgrade.py", "command", "fs", ["org", "fs"], 4),
    ],
)
def test_runner_argv_uses_nested_path(
    relative: str, method: str, old: str, replacement: list[str], minimum: int
) -> None:
    source = ROOT / "contract" / "e2e" / relative
    tree = ast.parse(source.read_text(encoding="utf-8"))
    prefixes = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != method:
            continue
        args = node.args[1:] if method == "exec_json" else node.args
        prefix = [arg.value for arg in args[:2] if isinstance(arg, ast.Constant)]
        assert prefix[:1] != [old], (relative, node.lineno, prefix)
        if prefix == replacement:
            prefixes.append(prefix)
    assert len(prefixes) >= minimum
