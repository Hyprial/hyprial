"""Seams of cells.params: pure over cell data, no OrgFS, runtime or composition."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
PARAMS = ROOT / "src" / "hyprial" / "daemon" / "impl" / "cells" / "params"
FORBIDDEN = (
    "hyprial.daemon.impl.orgfs",
    "hyprial.daemon.impl.cells.runtime",
    "hyprial.daemon.impl.composition",
    "hyprial.daemon.impl.delivery",
    "hyprial.daemon.impl.threads",
    "hyprial.daemon.impl.conversations",
)


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_every_params_module_imports_only_cell_data_and_the_kernel() -> None:
    modules = sorted(PARAMS.glob("*.py"))
    assert {path.name for path in modules} >= {
        "__init__.py",
        "spec.py",
        "resolve.py",
        "snapshot.py",
        "source.py",
    }
    for path in modules:
        bad = {name for name in _imports(path) if name.startswith(FORBIDDEN)}
        assert not bad, (path.name, bad)


def test_the_synthetic_red_case_is_caught(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from hyprial.daemon.impl.orgfs import LocalOrgFs\n", encoding="utf-8"
    )
    assert any(name.startswith(FORBIDDEN) for name in _imports(probe))


def test_importing_the_params_face_loads_no_orgfs_or_runtime() -> None:
    probe = (
        "import sys\n"
        "import hyprial.daemon.impl.cells.params\n"
        f"bad = [m for m in sys.modules if m.startswith({FORBIDDEN!r})]\n"
        "print(','.join(bad))\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
        cwd=ROOT,
    )
    assert result.stdout.strip() == ""
