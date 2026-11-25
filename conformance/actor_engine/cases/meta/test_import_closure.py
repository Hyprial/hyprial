# Decision record (file header; no inline comments by repository rule):
# - The suite must not import any business package (spec 15; prerequisites
#   section 4). Two independent checks: a static AST scan of every suite file
#   and a fresh-interpreter import of the selected backend plus all support
#   code, inspecting sys.modules.
# - Allowed hyprial imports: hyprial._core.actors (native). P8 removed the
#   pykka adapter, so neither remaining backend loads a hyprial.kernel module;
#   hyprial.daemon, identity, biz and shell are forbidden for every backend.
"""Import closure: the suite loads no business package."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

SUITE = Path(__file__).resolve().parents[2]
FORBIDDEN = ("hyprial.daemon", "hyprial.identity", "hyprial.biz", "hyprial.shell")
ALLOWED_STATIC = {
    "hyprial._core.actors": None,
}

PROBE = """
import json, sys
sys.path.insert(0, {suite!r})
import backends
backends.load({backend!r})
import support.helpers, support.catalog, support.coverage
print(json.dumps(sorted(m for m in sys.modules if m == "hyprial" or m.startswith("hyprial."))))
"""


def _static_hyprial_imports() -> list[tuple[str, str]]:
    found = []
    for path in sorted(SUITE.rglob("*.py")):
        rel = path.relative_to(SUITE).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name == "hyprial" or name.startswith("hyprial."):
                    found.append((rel, name))
    return found


def test_static_imports_touch_no_business_package() -> None:
    offending = []
    for rel, name in _static_hyprial_imports():
        owner = next((v for k, v in ALLOWED_STATIC.items() if name == k or name.startswith(k + ".")), "x")
        if owner == "x" or (owner is not None and owner != rel):
            offending.append((rel, name))
    assert offending == []


def test_runtime_import_closure(backend_name: str) -> None:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    probe = PROBE.format(suite=str(SUITE), backend=backend_name)
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                          timeout=120, env=env, cwd=str(SUITE))
    assert done.returncode == 0, done.stderr
    loaded = json.loads(done.stdout.strip().splitlines()[-1])
    assert [m for m in loaded if m.startswith(FORBIDDEN)] == []
    if backend_name in ("reference-abi", "native"):
        assert [m for m in loaded if m.startswith("hyprial.kernel")] == []
    if backend_name == "native":
        assert set(loaded) <= {"hyprial", "hyprial._core", "hyprial._core.actors"}
