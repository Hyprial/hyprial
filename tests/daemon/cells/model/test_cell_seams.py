"""Seams of the cell package: the entry and the faces stay OrgFS-free."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
CELLS = ROOT / "src" / "hyprial" / "daemon" / "impl" / "cells"
PURE_FACES = ("__init__.py", "model.py", "codec.py", "permissions.py", "ports.py")
FORBIDDEN = ("hyprial.daemon.impl.orgfs", "hyprial.daemon.impl.cells.runtime",
             "hyprial.daemon.impl.composition")


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_the_package_faces_import_no_orgfs_runtime_or_composition() -> None:
    for name in PURE_FACES:
        imported = _imports(CELLS / name)
        assert not {
            module for module in imported if module.startswith(FORBIDDEN)
        }, name


def test_importing_observability_or_the_model_loads_no_orgfs() -> None:
    probe = (
        "import sys\n"
        "import hyprial.daemon.impl.cells.observability.model\n"
        "import hyprial.daemon.impl.cells.ports\n"
        f"bad = [m for m in sys.modules if m.startswith({FORBIDDEN!r})]\n"
        "print(','.join(bad))\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        capture_output=True, text=True, timeout=120, check=True,
        cwd=ROOT,
    )
    assert result.stdout.strip() == ""
