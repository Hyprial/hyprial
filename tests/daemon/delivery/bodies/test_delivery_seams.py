"""Seams of the delivery authority: no OrgFS, cells, conversations or threads.

The delivery package is the authority over the inbox engine (plan Q11), and
the boundary rule says delivery never reaches orgfs, cells or conversations.
The package is stricter than that: every module imports only the standard
library, ``hyprial.kernel`` and itself.  The inbox engine imports delivery
leaf modules, so an inbox import from inside delivery would also be a cycle.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
DELIVERY = ROOT / "src" / "hyprial" / "daemon" / "impl" / "delivery"
FORBIDDEN = (
    "hyprial.daemon.impl.orgfs",
    "hyprial.daemon.impl.cells",
    "hyprial.daemon.impl.conversations",
    "hyprial.daemon.impl.threads",
    "hyprial.daemon.impl.composition",
)
RUNTIME_FORBIDDEN = (*FORBIDDEN, "hyprial.daemon.impl.inbox")
ALLOWED_HYPRIAL = ("hyprial.kernel", "hyprial.daemon.impl.delivery")


def _imports(source: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def _violations(source: str) -> set[str]:
    stdlib = set(sys.stdlib_module_names)
    bad: set[str] = set()
    for module in _imports(source):
        root = module.split(".")[0]
        if module.startswith(FORBIDDEN):
            bad.add(module)
        elif root == "hyprial" and not module.startswith(ALLOWED_HYPRIAL):
            bad.add(module)
        elif root not in stdlib and root not in {"hyprial", "__future__"}:
            bad.add(module)
    return bad


def _modules() -> list[Path]:
    return sorted(DELIVERY.rglob("*.py"))


def test_the_package_exists_with_its_public_faces() -> None:
    names = {path.name for path in _modules()}
    assert {"__init__.py", "ports.py"} <= names


def test_every_delivery_module_imports_only_stdlib_kernel_and_itself() -> None:
    offenders = {
        str(path.relative_to(DELIVERY)): sorted(_violations(path.read_text("utf-8")))
        for path in _modules()
    }
    assert {name: bad for name, bad in offenders.items() if bad} == {}


def test_the_seam_scan_rejects_each_forbidden_import() -> None:
    for module in (*FORBIDDEN, "hyprial.daemon.impl.inbox.envelope"):
        assert _violations(f"from {module} import x\n") == {module}
        assert _violations(f"import {module}.y\n") == {f"{module}.y"}
    assert _violations("import json\nfrom hyprial.kernel import MessageClass\n") == set()


def test_importing_delivery_loads_no_orgfs_cells_conversations_or_inbox() -> None:
    probe = (
        "import sys\n"
        "import hyprial.daemon.impl.delivery\n"
        "import hyprial.daemon.impl.delivery.ports\n"
        f"bad = [m for m in sys.modules if m.startswith({RUNTIME_FORBIDDEN!r})]\n"
        "print(','.join(sorted(bad)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        capture_output=True, text=True, timeout=120, check=True, cwd=ROOT,
    )
    assert result.stdout.strip() == ""
