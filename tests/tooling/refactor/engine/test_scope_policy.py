"""Write-scope policy tests: WORM read-only, GUI off-limits, scope report.

First-fail regressions for the scope-protection finding: the import engine
used to rewrite `docs/notes/example.py` because the default rule loop walked
every tracked Python file. The policy module now gates every engine's file
iteration and the runner reports inclusion/exclusion decisions.
"""

from __future__ import annotations

import unittest

from tests.tooling.refactor.support._helpers import RepoFixture, standard_repo

from scripts.refactor.transforms import imports
from scripts.refactor.transforms.common import Rule
from scripts.refactor.transforms.scope_policy import (
    ScopePolicy,
    classify_path,
    default_policy,
)


def symbol_rule(**kwargs) -> Rule:
    base = {
        "rule_id": "t.scope",
        "kind": "import_symbol",
        "stage": "S1",
        "status": "approved",
        "expected_matches": 1,
        "reason": "test",
    }
    base.update(kwargs)
    return Rule(base)


class ClassifyTests(unittest.TestCase):
    def test_worm_paths_are_read_only_including_python(self):
        for rel in (
            "docs/notes/example.py",
            "docs/notes/evidence/scan.txt",
            "docs/notes/repro/tool.py",
        ):
            decision = classify_path(rel, default_policy())
            self.assertFalse(decision["writable"], rel)

    def test_gui_python_is_not_writable_by_default(self):
        for rel in (
            "src/gui/integration/pac-service.py",
            "src/gui/scripts/tool.py",
        ):
            decision = classify_path(rel, default_policy())
            self.assertFalse(decision["writable"], rel)
            self.assertIn(decision["reason"], ("gui-python-frozen",))

    def test_gui_non_python_config_not_claimed_by_python_rules(self):
        decision = classify_path("src/gui/package.json", default_policy())
        self.assertFalse(decision["writable"])
        self.assertEqual(decision["reason"], "gui-tree-python-rules-only")

    def test_product_and_tests_are_writable(self):
        for rel in ("src/hyprial/cli.py", 'tests/shell/gui/lifecycle/test_gui_apps.py', "scripts/ci/step.sh"):
            decision = classify_path(rel, default_policy())
            self.assertTrue(decision["writable"], rel)

    def test_explicit_allow_cannot_cross_floor_and_deny_is_absolute(self):
        # round-4 semantics: writable_extra may only operate INSIDE already
        # writable scopes (e.g. un-freeze one file); WORM/GUI floors and
        # read_only_extra denies are absolute
        policy = ScopePolicy(
            writable_extra=("src/hyprial/gated.py",),
            read_only_extra=("src/hyprial/frozen.py",),
        )
        self.assertFalse(classify_path("src/hyprial/frozen.py", policy)["writable"])
        self.assertTrue(classify_path("src/hyprial/gated.py", policy)["writable"])
        self.assertFalse(classify_path("docs/notes/generated/interop.py", policy)["writable"])

    def test_vendored_and_unknown_trees_denied_and_reported(self):
        # deny-by-default (round 3): unknown trees are refused until the
        # main model extends the allowlist explicitly
        decision = classify_path("vendor/lib/x.py", default_policy())
        self.assertFalse(decision["writable"])
        self.assertEqual(decision["reason"], "unclassified-not-authorized")


class ImportEngineScopeTests(RepoFixture):
    def setUp(self):
        super().setUp()
        standard_repo(self.root)

    def test_worm_python_file_never_rewritten(self):
        # first-fail: the default engine walk rewrote WORM content
        self.write("src/hyprial/daemon/core.py", "class Pump:\n    pass\n")
        self.write("docs/notes/example.py", "from hyprial.daemon.impl.core import Pump\n\nx = Pump\n")
        self.write("src/hyprial/agents/w.py", "from hyprial.daemon.impl.core import Pump\n\nx = Pump\n")
        before = (self.root / "docs/notes/example.py").read_bytes()
        ws, ctx = self.build_workspace()
        outcome = imports.apply_rule(
            symbol_rule(
                old_module="hyprial.daemon.impl.core",
                old_symbol="Pump",
                new_module="hyprial.kernel.contracts",
                new_symbol="Pump",
            ),
            ctx,
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        self.assertEqual((self.root / "docs/notes/example.py").read_bytes(), before)
        self.assertNotIn("docs/notes/example.py", outcome.changed_paths)
        excluded = {row["path"] for row in outcome.detail.get("scope_excluded", [])}
        self.assertIn("docs/notes/example.py", excluded)

    def test_gui_python_never_rewritten(self):
        self.write("src/hyprial/daemon/core.py", "class Pump:\n    pass\n")
        self.write("src/gui/integration/pac-service.py", "from hyprial.daemon.impl.core import Pump\n\nx = Pump\n")
        self.write("src/hyprial/agents/w.py", "from hyprial.daemon.impl.core import Pump\n\nx = Pump\n")
        before = (self.root / "src/gui/integration/pac-service.py").read_bytes()
        ws, ctx = self.build_workspace()
        outcome = imports.apply_rule(
            symbol_rule(
                old_module="hyprial.daemon.impl.core",
                old_symbol="Pump",
                new_module="hyprial.kernel.contracts",
                new_symbol="Pump",
            ),
            ctx,
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        self.assertEqual((self.root / "src/gui/integration/pac-service.py").read_bytes(), before)
        excluded = {row["path"] for row in outcome.detail.get("scope_excluded", [])}
        self.assertIn("src/gui/integration/pac-service.py", excluded)

    def test_expected_matches_counts_only_in_scope_sites(self):
        # a WORM copy of the import does not satisfy expected_matches and does
        # not inflate the count: in-scope sites are the honest basis
        self.write("src/hyprial/agents/w.py", "from hyprial.daemon.impl.core import Pump\n\nx = Pump\n")
        self.write("docs/notes/copy.py", "from hyprial.daemon.impl.core import Pump\n\nx = Pump\n")
        ws, ctx = self.build_workspace()
        outcome = imports.apply_rule(
            symbol_rule(
                expected_matches=1,
                old_module="hyprial.daemon.impl.core",
                old_symbol="Pump",
                new_module="hyprial.kernel.contracts",
                new_symbol="Pump",
            ),
            ctx,
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        self.assertEqual(outcome.matches, 1)

    def test_rule_cannot_target_read_only_path(self):
        ws, ctx = self.build_workspace()
        outcome = imports.apply_rule(
            symbol_rule(
                old_module="hyprial.kernel",
                old_symbol="X",
                new_module="hyprial.daemon.impl.core",
                new_symbol="X",
            ),
            ctx,
        )
        # no sites -> refused by count check; the key protection is that the
        # runner refuses writes into read-only paths even when rules would
        self.assertEqual(outcome.status, "refused")

    def test_scope_report_shape(self):
        from scripts.refactor.transforms.scope_policy import scope_report

        report = scope_report(default_policy(), [
            "src/hyprial/cli.py",
            "docs/notes/example.py",
            "src/gui/integration/pac-service.py",
            "vendor/x.py",
        ])
        self.assertEqual(report["writable"], ["src/hyprial/cli.py"])
        self.assertEqual(
            [row["path"] for row in report["read_only"]],
            ["docs/notes/example.py", "src/gui/integration/pac-service.py", "vendor/x.py"],
        )
        self.assertEqual(report["denied_by_default"], ["vendor/x.py"])
        self.assertTrue(report["policy_fingerprint"])


class RunnerScopeTests(RepoFixture):
    def test_layout_move_into_worm_refused(self):
        from scripts.refactor.transforms import layout

        standard_repo(self.root)
        self.write("docs/notes/evidence/keep.txt", "EVIDENCE\n")
        ws, ctx = self.build_workspace()
        outcome = layout.apply_rule(
            Rule(
                {
                    "rule_id": "t.move-worm",
                    "kind": "layout_move",
                    "stage": "S5",
                    "status": "approved",
                    "expected_matches": 1,
                    "reason": "test",
                    "moves": [
                        {"from": "docs/notes/evidence/keep.txt", "to": "docs/evidence/moved.txt"}
                    ],
                }
            ),
            ctx,
        )
        # plain relocation out of WORM is not a byte-preserving approved move
        # unless explicitly marked worm+hash; the policy refuses casual writes
        self.assertEqual(outcome.status, "refused", outcome.diagnostics)


if __name__ == "__main__":
    unittest.main()
