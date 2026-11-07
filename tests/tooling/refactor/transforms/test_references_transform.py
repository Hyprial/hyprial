"""Reference rewrite tests: module strings, config fields, path spans."""

from __future__ import annotations

import json
import unittest

from tests.tooling.refactor.support._helpers import RepoFixture

from scripts.refactor.transforms import references
from scripts.refactor.transforms.common import Rule


def ref_rule(**kwargs) -> Rule:
    base = {
        "rule_id": "t.ref",
        "kind": "reference_rule",
        "stage": "S2",
        "status": "approved",
        "expected_matches": 1,
        "reason": "test",
    }
    base.update(kwargs)
    return Rule(base)


class ReferenceTests(RepoFixture):
    def outcome(self, **kwargs):
        ws, ctx = self.build_workspace()
        outcome = references.apply_rule(ref_rule(**kwargs), ctx)
        if outcome.status == "applied":
            ws.materialize()
        return outcome, ws, ctx

    def test_python_module_string_rewrite(self):
        self.write(
            "src/hyprial/cli.py",
            "import importlib\n\n\ndef warm():\n    return importlib.import_module('hyprial.daemon.impl.desired_state')\n",
        )
        outcome, _, _ = self.outcome(
            source_path="src/hyprial/cli.py",
            format="python",
            operations=[{"op": "module_string", "old": "hyprial.daemon.impl.desired_state", "new": "hyprial.kernel.contracts", "count": 1}],
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        self.assertIn(
            "importlib.import_module('hyprial.kernel.contracts')",
            self.assertRead("src/hyprial/cli.py"),
        )

    def test_python_module_string_count_mismatch_refused(self):
        self.write("src/hyprial/cli.py", "A = 'hyprial.daemon.impl.x'\nB = 'hyprial.daemon.impl.x'\n")
        outcome, _, _ = self.outcome(
            source_path="src/hyprial/cli.py",
            format="python",
            operations=[{"op": "module_string", "old": "hyprial.daemon.impl.x", "new": "hyprial.kernel.y", "count": 1}],
        )
        self.assertRefused(outcome, "MATCH-COUNT")

    def test_parents_depth_shift(self):
        self.write(
            "scripts/build/worker.py",
            "from pathlib import Path\nROOT = Path(__file__).resolve().parents[2]\nOTHER = Path('x').parents[2]\n",
        )
        outcome, _, _ = self.outcome(
            expected_matches=1,
            source_path="scripts/build/worker.py",
            format="python",
            operations=[{"op": "parents_depth_shift", "delta": -1}],
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        text = self.assertRead("scripts/build/worker.py")
        self.assertIn("Path(__file__).resolve().parents[1]", text)
        self.assertIn("Path('x').parents[2]", text)  # not __file__-rooted: untouched

    def test_json_field_rewrite(self):
        self.write(
            "scripts/config.json",
            json.dumps({"scripts": {"hyprial": "src/hyprial/cli.py"}, "keep": 1}, indent=2) + "\n",
        )
        outcome, _, _ = self.outcome(
            source_path="scripts/config.json",
            format="json",
            operations=[{"op": "json_field", "field": ["scripts", "hyprial"], "old": "src/hyprial/cli.py", "new": "src/hyprial/shell/cli_main.py", "count": 1}],
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        data = json.loads(self.assertRead("scripts/config.json"))
        self.assertEqual(data["scripts"]["hyprial"], "src/hyprial/shell/cli_main.py")
        self.assertEqual(data["keep"], 1)

    def test_json_entry_path_preserves_identity_and_reason(self):
        entries = [
            {"path": "src/hyprial/agents/registry.py", "reason": "Pre-cutover owner; keep.", "symbol": "AgentRegistry"},
            {"path": "src/hyprial/daemon/app.py", "reason": "Another reason stays.", "symbol": "Daemon"},
        ]
        self.write("tests/authority_exemptions.json", json.dumps(entries, indent=2) + "\n")
        outcome, _, _ = self.outcome(
            source_path="tests/authority_exemptions.json",
            format="json",
            operations=[{"op": "json_entry_path", "match_path": "src/hyprial/agents/registry.py", "new_path": "src/hyprial/identity/registry.py", "count": 1}],
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        migrated = json.loads(self.assertRead("tests/authority_exemptions.json"))
        self.assertEqual(len(migrated), 2)
        self.assertEqual(migrated[0]["path"], "src/hyprial/identity/registry.py")
        self.assertEqual(migrated[0]["reason"], "Pre-cutover owner; keep.")
        self.assertEqual(migrated[0]["symbol"], "AgentRegistry")
        self.assertEqual(migrated[1]["path"], "src/hyprial/daemon/app.py")

    def test_toml_key_rewrite(self):
        self.write(
            "scripts/build.toml",
            '[project]\nname = "hyprial"\n\n[tool.hatch.build.hooks.custom]\npath = "src/hyprial/gui_build_hook.py"\n',
        )
        outcome, _, _ = self.outcome(
            source_path="scripts/build.toml",
            format="toml",
            operations=[{"op": "toml_key", "key": ["tool", "hatch", "build", "hooks", "custom", "path"], "old": "src/hyprial/gui_build_hook.py", "new": "src/hyprial/shell/gui_build_hook.py", "count": 1}],
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        self.assertIn('path = "src/hyprial/shell/gui_build_hook.py"', self.assertRead("scripts/build.toml"))

    def test_text_span_with_count(self):
        self.write("docs/guide.md", "Run `python -m hyprial.cli` twice: python -m hyprial.cli\n")
        outcome, _, _ = self.outcome(
            expected_matches=2,
            source_path="docs/guide.md",
            format="text",
            operations=[{"op": "text_span", "old": "python -m hyprial.cli", "new": "python -m hyprial.shell.main", "count": 2}],
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        self.assertEqual(self.assertRead("docs/guide.md").count("python -m hyprial.shell.main"), 2)

    def test_text_span_count_mismatch_refused(self):
        self.write("docs/guide.md", "one reference only\n")
        outcome, _, _ = self.outcome(
            source_path="docs/guide.md",
            format="text",
            operations=[{"op": "text_span", "old": "reference", "new": "x", "count": 2}],
        )
        self.assertRefused(outcome, "MATCH-COUNT")

    def test_missing_input_refused(self):
        outcome, _, _ = self.outcome(
            source_path="does/not/exist.json",
            format="json",
            operations=[{"op": "json_field", "field": ["a"], "old": "x", "new": "y", "count": 1}],
        )
        self.assertRefused(outcome, "MISSING-INPUT")


if __name__ == "__main__":
    unittest.main()
