"""Static unique-writer gate for managed Harness native session refs."""

from __future__ import annotations

import ast
from pathlib import Path


def session_ref_store_writers(
    source_root: Path, *, source_overrides: dict[Path, str] | None = None
) -> tuple[str, ...]:
    """Return every production call to the DesiredStateStore write primitive."""

    overrides = source_overrides or {}
    writers: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        source = overrides.get(path, path.read_text(encoding="utf-8"))
        tree = ast.parse(source, filename=str(path))
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "sync_harness_session_refs":
                continue
            owner: ast.AST | None = node
            scope: list[str] = []
            while owner is not None:
                if isinstance(owner, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    scope.append(owner.name)
                owner = parents.get(owner)
            writers.append(
                f"{path.relative_to(source_root)}:{'.'.join(reversed(scope))}"
            )
    return tuple(writers)
