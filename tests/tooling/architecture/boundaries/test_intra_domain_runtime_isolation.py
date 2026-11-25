"""Runtime proof of INTRA_DOMAIN_RULES: a fresh ``-I`` interpreter imports a layer and lists what loaded.

The static gate reasons about the AST; this probe imports the real package and every
submodule under it in a clean interpreter, so a lazy import the audit cannot see still
shows up as a loaded forbidden module.
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest

IMPL = "hyprial.daemon.impl"
ISOLATED = tuple(f"{IMPL}.{name}" for name in ("orgfs", "cells", "conversations"))

PROBE = """
import importlib, json, pkgutil, sys
heads, forbidden = json.loads(sys.argv[1]), json.loads(sys.argv[2])
for head in heads:
    package = importlib.import_module(head)
    for info in pkgutil.walk_packages(getattr(package, "__path__", []), head + "."):
        importlib.import_module(info.name)
loaded = sorted(name for name in sys.modules if any(name == p or name.startswith(p + ".") for p in forbidden))
print(json.dumps(loaded))
"""


def loaded_forbidden(heads: tuple[str, ...], forbidden: tuple[str, ...]) -> list[str]:
    result = subprocess.run(
        [sys.executable, "-I", "-c", PROBE, json.dumps(list(heads)), json.dumps(list(forbidden))],
        text=True, capture_output=True, timeout=300,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class RuntimeIsolationTests(unittest.TestCase):
    def test_delivery_loads_no_orgfs_cells_or_conversations(self):
        self.assertEqual(loaded_forbidden((f"{IMPL}.delivery",), ISOLATED), [])

    def test_network_loads_no_orgfs_cells_or_conversations(self):
        self.assertEqual(loaded_forbidden((f"{IMPL}.network",), ISOLATED), [])

    def test_inbox_loads_no_orgfs_cells_conversations_or_threads(self):
        self.assertEqual(loaded_forbidden((f"{IMPL}.inbox",), (*ISOLATED, f"{IMPL}.threads")), [])

    def test_user_delivery_loads_no_orgfs_cells_conversations_or_threads(self):
        forbidden = (*ISOLATED, f"{IMPL}.threads")
        self.assertEqual(loaded_forbidden((f"{IMPL}.adapters.user_delivery",), forbidden), [])

    def test_probe_sees_a_forbidden_load_when_one_happens(self):
        loaded = loaded_forbidden((f"{IMPL}.composition.cells",), ISOLATED)
        self.assertIn(f"{IMPL}.cells", loaded)


if __name__ == "__main__":
    unittest.main()
