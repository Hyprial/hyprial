"""Layout move tests: conflicts, case collisions, symlinks, WORM, rebase."""

from __future__ import annotations

import hashlib
import os
import unittest

from tests.tooling.refactor.support._helpers import RepoFixture, standard_repo

from scripts.refactor.transforms import layout
from scripts.refactor.transforms.common import Rule


def layout_rule(**kwargs) -> Rule:
    base = {
        "rule_id": "t.layout",
        "kind": "layout_move",
        "stage": "S2",
        "status": "approved",
        "expected_matches": 1,
        "reason": "test",
    }
    base.update(kwargs)
    return Rule(base)


class LayoutTests(RepoFixture):
    def setUp(self):
        super().setUp()
        standard_repo(self.root)

    def outcome(self, **kwargs):
        ws, ctx = self.build_workspace()
        outcome = layout.apply_rule(layout_rule(**kwargs), ctx)
        if outcome.status == "applied":
            ws.materialize()
        return outcome, ws, ctx

    def test_file_move_with_consumer_sync(self):
        self.write("src/hyprial/agents/registry.py", "class AgentRegistry:\n    pass\n")
        self.write("src/hyprial/agents/user.py", "from hyprial.agents.registry import AgentRegistry\n\nx = AgentRegistry\n")
        outcome, ws, _ = self.outcome(
            moves=[{"from": "src/hyprial/agents/registry.py", "to": "src/hyprial/identity/registry.py"}],
            sync_imports=[
                {"old_module": "hyprial.agents.registry", "new_module": "hyprial.identity.registry", "expected_matches": 1}
            ],
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        self.assertFalse((self.root / "src/hyprial/agents/registry.py").exists() and not ws.is_dirty("src/hyprial/agents/registry.py"))
        self.assertEqual(ws.read("src/hyprial/identity/registry.py"), b"class AgentRegistry:\n    pass\n")
        self.assertEqual(
            ws.read("src/hyprial/agents/user.py").decode(),
            "from hyprial.identity.registry import AgentRegistry\n\nx = AgentRegistry\n",
        )

    def test_moved_file_relative_imports_rebased(self):
        self.write("src/hyprial/agents/helpers.py", "H = 1\n")
        self.write("src/hyprial/agents/pair.py", "from .helpers import H\n\nv = H\n")
        outcome, ws, _ = self.outcome(
            moves=[{"from": "src/hyprial/agents/pair.py", "to": "src/hyprial/daemon/nested/pair.py"}],
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        content = ws.read("src/hyprial/daemon/nested/pair.py").decode()
        # helpers still lives in hyprial.agents; the moved file's relative
        # import must be re-expressed from its new package
        self.assertIn("from ...agents.helpers import H", content)

    def test_directory_move_preserves_structure(self):
        self.write("src/hyprial/routine/templates/daily.txt", "DAY\n")
        self.write("src/hyprial/routine/templates/weekly.txt", "WEEK\n")
        outcome, ws, _ = self.outcome(
            expected_matches=2,
            moves=[{"from": "src/hyprial/routine/templates", "to": "src/hyprial/biz/templates"}],
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        self.assertEqual(ws.read("src/hyprial/biz/templates/daily.txt"), b"DAY\n")
        self.assertEqual(ws.read("src/hyprial/biz/templates/weekly.txt"), b"WEEK\n")

    def test_target_conflict_refused(self):
        self.write("src/hyprial/a.py", "A\n")
        self.write("src/hyprial/b.py", "B\n")
        outcome, _, _ = self.outcome(
            moves=[{"from": "src/hyprial/a.py", "to": "src/hyprial/b.py"}],
        )
        self.assertRefused(outcome, "TARGET-CONFLICT")

    def test_case_insensitive_collision_refused(self):
        self.write("src/hyprial/Module.py", "x\n")
        self.write("src/hyprial/other.py", "y\n")
        outcome, _, _ = self.outcome(
            moves=[{"from": "src/hyprial/other.py", "to": "src/hyprial/module.py"}],
        )
        self.assertRefused(outcome, "CASE-COLLISION")

    def test_symlink_source_refused(self):
        self.write("src/hyprial/real.py", "x\n")
        os.symlink(self.root / "src/hyprial/real.py", self.root / "src/hyprial/link.py")
        outcome, _, _ = self.outcome(
            moves=[{"from": "src/hyprial/link.py", "to": "src/hyprial/moved.py"}],
        )
        self.assertRefused(outcome, "SYMLINK-SOURCE")

    def test_symlink_target_parent_refused(self):
        self.write("src/hyprial/a.py", "x\n")
        os.symlink(self.root / "src/hyprial", self.root / "src/hyprial/linkdir")
        outcome, _, _ = self.outcome(
            moves=[{"from": "src/hyprial/a.py", "to": "src/hyprial/linkdir/a.py"}],
        )
        self.assertRefused(outcome, "SYMLINK-TARGET-PARENT")

    def test_worm_content_hash_preserved(self):
        content = "EVIDENCE = 'immutable'\n" * 10
        self.write("docs/notes/evidence/scan.txt", content)
        digest = hashlib.sha256(content.encode()).hexdigest()
        outcome, ws, _ = self.outcome(
            moves=[{"from": "docs/notes/evidence/scan.txt", "to": "docs/notes/archive/scan.txt", "worm": True, "sha256": digest}],
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        self.assertEqual(hashlib.sha256(ws.read("docs/notes/archive/scan.txt")).hexdigest(), digest)

    def test_worm_hash_mismatch_refused(self):
        self.write("docs/notes/evidence/scan.txt", "EVIDENCE = 'immutable'\n")
        outcome, _, _ = self.outcome(
            moves=[{"from": "docs/notes/evidence/scan.txt", "to": "docs/notes/a/scan.txt", "worm": True, "sha256": "0" * 64}],
        )
        self.assertRefused(outcome, "INPUT-DRIFT")

    def test_authority_manifest_path_migration_keeps_entries(self):
        entries = [
            {"path": "src/hyprial/agents/registry.py", "reason": "Pre-cutover owner; keep.", "symbol": "AgentRegistry"},
            {"path": "src/hyprial/daemon/app.py", "reason": "Second entry kept.", "symbol": "DaemonApplication"},
        ]
        import json

        self.write("tests/actor_exemptions.json", json.dumps(entries, indent=2) + "\n")
        self.write("src/hyprial/agents/registry.py", "class AgentRegistry:\n    pass\n")
        outcome, ws, _ = self.outcome(
            moves=[{"from": "src/hyprial/agents/registry.py", "to": "src/hyprial/identity/registry.py"}],
            manifest_updates=[
                {
                    "path": "tests/actor_exemptions.json",
                    "operations": [
                        {
                            "op": "json_entry_path",
                            "match_path": "src/hyprial/agents/registry.py",
                            "new_path": "src/hyprial/identity/registry.py",
                            "count": 1,
                        }
                    ],
                }
            ],
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        migrated = json.loads(ws.read("tests/actor_exemptions.json"))
        self.assertEqual(len(migrated), 2, "no entry may be dropped")
        self.assertEqual(migrated[0]["reason"], "Pre-cutover owner; keep.")
        self.assertEqual(migrated[0]["symbol"], "AgentRegistry")
        self.assertEqual(migrated[0]["path"], "src/hyprial/identity/registry.py")
        self.assertEqual(migrated[1]["path"], "src/hyprial/daemon/app.py")

    def test_escape_and_noop_refused(self):
        self.write("src/hyprial/a.py", "x\n")
        outcome, _, _ = self.outcome(moves=[{"from": "../../etc/passwd", "to": "x"}])
        self.assertEqual(outcome.status, "refused")
        outcome2, _, _ = self.outcome(moves=[{"from": "src/hyprial/a.py", "to": "src/hyprial/a.py"}])
        self.assertRefused(outcome2, "NOOP-MOVE")


if __name__ == "__main__":
    unittest.main()
