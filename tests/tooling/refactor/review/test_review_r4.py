"""Round-4 minimal safety patches: PY-R4-01/02/03 counterexamples.

These mirror the main model's r3-python-probes.py semantics. They first
failed against R3; the minimal fixes make them pass without adding any
general framework capability.
"""

from __future__ import annotations

import unittest

from tests.tooling.refactor.support._helpers import RepoFixture

from scripts.refactor import verify as verify_module
from scripts.refactor.transforms.common import ToolError, Workspace
from scripts.refactor.transforms.scope_policy import ScopePolicy, classify_path

CFG = {
    "roots": ["src/hyprial"],
    "layer_order": ["kernel", "biz"],
    "layer_of_module": {"hyprial.kernel": "kernel", "hyprial.biz": "biz"},
}


class DynamicFactsTests(RepoFixture):
    """PY-R4-01: uncovered dynamic patterns must not read as safe."""

    def setUp(self):
        super().setUp()
        self.write("src/hyprial/kernel/__init__.py", "")
        self.write("src/hyprial/kernel/local.py", "value = 1\n")
        self.write("src/hyprial/biz/__init__.py", "")
        self.write("src/hyprial/biz/impl/__init__.py", "")
        self.write("src/hyprial/biz/impl/work.py", "value = 1\n")

    def _check(self, code):
        self.write("src/hyprial/kernel/use.py", code)
        return verify_module.check_boundary_direction(self.root, CFG)

    def test_module_aliased_importlib_reverse_is_caught(self):
        report = self._check(
            "import importlib as il\nil.import_module('hyprial.biz.impl.work')\n"
        )
        self.assertIn(
            report["status"], (verify_module.FAILED, verify_module.BLOCKED), report
        )
        self.assertEqual(report["status"], verify_module.FAILED, report)
        self.assertTrue(
            any(v.get("target") == "hyprial.biz.impl.work" for v in report["violations"]),
            report["violations"],
        )

    def test_builtin_dunder_import_reverse_is_caught(self):
        report = self._check("__import__('hyprial.biz.impl.work')\n")
        self.assertEqual(report["status"], verify_module.FAILED, report)
        self.assertTrue(
            any(v.get("target") == "hyprial.biz.impl.work" for v in report["violations"]),
            report["violations"],
        )

    def test_same_name_constant_in_unrelated_scope_stops_inference(self):
        # outer target points at biz; an unrelated function-local constant of
        # the same name pointing at kernel must NOT mask the reverse target.
        # Conservative outcome: stop inferring -> BLOCKED (lead, not a gate).
        report = self._check(
            "from importlib import import_module as load\n"
            "target = 'hyprial.biz.impl.work'\n"
            "def unrelated():\n    target = 'hyprial.kernel.local'\n"
            "load(target)\n"
        )
        self.assertEqual(report["status"], verify_module.BLOCKED, report)
        self.assertTrue(report["pending_dynamic_refs"], report)

    def test_control_plain_importlib_still_failed(self):
        report = self._check(
            "import importlib\nimportlib.import_module('hyprial.biz.impl.work')\n"
        )
        self.assertEqual(report["status"], verify_module.FAILED, report)

    def test_conservative_leads_are_labelled_not_gates(self):
        report = self._check(
            "from importlib import import_module as load\n"
            "target = 'hyprial.biz.impl.work'\n"
            "def unrelated():\n    target = 'hyprial.kernel.local'\n"
            "load(target)\n"
        )
        for row in report["pending_dynamic_refs"]:
            self.assertTrue(row.get("lead"), "unresolved leads must be labelled as leads")


class ContainmentTests(RepoFixture):
    """PY-R4-02: symlink writes are refused outright on this call path."""

    def test_symlink_to_checkout_parent_refused(self):
        outside_parent = self.root.parent
        (self.root / "src/hyprial").mkdir(parents=True)
        (self.root / "src/hyprial/escape").symlink_to(outside_parent, target_is_directory=True)
        ws = Workspace(self.root, [])
        with self.assertRaises(ToolError):
            ws.write("src/hyprial/escape/virtual-only.py", b"value = 1\n")

    def test_inward_symlink_also_refused(self):
        # a symlink pointing INTO the protected tree is refused too: this
        # call path does not distinguish, it refuses all symlink writes
        (self.root / "src/hyprial").mkdir(parents=True)
        (self.root / "docs/notes").mkdir(parents=True)
        (self.root / "docs/notes/example.py").write_text("E\n")
        (self.root / "src/hyprial/alias").symlink_to(
            self.root / "docs/notes", target_is_directory=True
        )
        ws = Workspace(self.root, [])
        with self.assertRaises(ToolError):
            ws.write("src/hyprial/alias/evil.py", b"x\n")

    def test_plain_path_still_writable(self):
        (self.root / "src/hyprial").mkdir(parents=True)
        ws = Workspace(self.root, [])
        ws.write("src/hyprial/plain.py", b"x = 1\n")
        self.assertEqual(ws.read("src/hyprial/plain.py"), b"x = 1\n")


class ScopeFloorTests(unittest.TestCase):
    """PY-R4-03: writable_extra cannot cross the non-overridable floor."""

    def test_worm_override_refused(self):
        decision = classify_path(
            "docs/notes/example.py", ScopePolicy(writable_extra=("docs/notes/",))
        )
        self.assertFalse(decision["writable"], decision)

    def test_gui_override_refused(self):
        decision = classify_path(
            "src/gui/example.js", ScopePolicy(writable_extra=("src/gui/",))
        )
        self.assertFalse(decision["writable"], decision)

    def test_floor_order_is_above_flags(self):
        policy = ScopePolicy(writable_extra=("docs/notes/", "src/gui/", ".git/", "vendor/"))
        for rel in (
            "docs/notes/a.py",
            "src/gui/b.js",
            ".git/config",
            "vendor/lib.py",
            "pyproject.toml",
            "scripts/verify/gui-refactor/lib/engine.mjs",
        ):
            decision = classify_path(rel, policy)
            self.assertFalse(decision["writable"], f"{rel}: {decision}")

    def test_extra_still_works_inside_writable_scopes(self):
        policy = ScopePolicy(read_only_extra=("src/hyprial/frozen.py",))
        self.assertFalse(classify_path("src/hyprial/frozen.py", policy)["writable"])
        self.assertTrue(classify_path("src/hyprial/other.py", policy)["writable"])


if __name__ == "__main__":
    unittest.main()
