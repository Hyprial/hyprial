"""Idempotency state preservation and full read/write path-set gating.

Defect 3: a zero-modification replay used to erase the first run's recorded
input/output hashes, so a third run failed with dirty-inputs.
Defect 4: preconditions used only the manifest's explicit fields; real import
consumers and expanded directory moves escaped the dirty check, and git
status parsing was line-based.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from tests.tooling.refactor.support._helpers import GIT_ENV, RepoFixture, standard_repo

from scripts.refactor import run as runner


def sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


class IdempotencyTests(RepoFixture):
    def setUp(self):
        super().setUp()
        self._toolchain = Path(__file__).resolve().parents[4]
        standard_repo(self.root)
        self.write("src/hyprial/daemon/desired.py", "class Spec:\n    pass\n")
        self.head = self.commit_all()
        self.rule = {
            "rule_id": "extract",
            "kind": "symbol_extract",
            "stage": "S1",
            "status": "approved",
            "expected_matches": 1,
            "reason": "test",
            "source_path": "src/hyprial/daemon/desired.py",
            "source_sha256": sha((self.root / "src/hyprial/daemon/desired.py").read_bytes()),
            "symbols": ["Spec"],
            "target_path": "src/hyprial/kernel/contracts.py",
            "dependencies": [],
        }

    def _plan_apply(self, mp):
        out = self.outside / "plan"
        if out.exists():
            out = self.outside / f"plan-{self._plan_counter}"
        self._plan_counter = getattr(self, "_plan_counter", 0) + 1
        out = self.outside / f"plan-{self._plan_counter}"
        subprocess.run(
            [
                sys.executable, "-m", "scripts.refactor.run", "plan",
                "--manifest", str(mp), "--root", str(self.root),
                "--stage", "S1", "--output", str(out),
            ],
            cwd=str(self._toolchain), capture_output=True, text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )
        plan = json.loads((out / "plan.json").read_text())
        argv = [
            sys.executable, "-m", "scripts.refactor.run", "apply",
            "--manifest", str(mp), "--root", str(self.root),
            "--stage", "S1", "--confirm", plan["apply_token"],
        ]
        return subprocess.run(
            argv, cwd=str(self._toolchain), capture_output=True, text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )

    def _manifest(self):
        mp = self.write_manifest(
            {"schema_version": 1, "baseline_commit": self.head, "rules": [self.rule]}
        )
        return mp

    def test_three_consecutive_runs_are_zero_mod_after_first(self):
        mp = self._manifest()
        first = self._plan_apply(mp)
        self.assertEqual(first.returncode, 0, first.stderr)
        summary1 = json.loads(first.stdout.strip().splitlines()[-1])
        self.assertEqual(summary1["applied"], 1)
        state_file = next((self.root / ".refactor-state").glob("m-*/stages.json"))
        state1 = json.loads(state_file.read_text())
        self.assertTrue(state1["stages"]["S1"]["outputs"])
        second = self._plan_apply(mp)
        self.assertEqual(second.returncode, 0, second.stderr)
        summary2 = json.loads(second.stdout.strip().splitlines()[-1])
        self.assertEqual(summary2["applied"], 0)
        self.assertEqual(summary2["already_done"], 1)
        self.assertEqual(summary2["modifications"], 0)
        # the first run's evidence must survive the replay
        state_file = next((self.root / ".refactor-state").glob("m-*/stages.json"))
        state2 = json.loads(state_file.read_text())
        self.assertEqual(
            state1["stages"]["S1"]["outputs"], state2["stages"]["S1"]["outputs"]
        )
        third = self._plan_apply(mp)
        self.assertEqual(third.returncode, 0, third.stderr)
        summary3 = json.loads(third.stdout.strip().splitlines()[-1])
        self.assertEqual(summary3["applied"], 0)
        self.assertEqual(summary3["modifications"], 0)

    def test_tampered_output_refused_not_already_done(self):
        mp = self._manifest()
        first = self._plan_apply(mp)
        self.assertEqual(first.returncode, 0, first.stderr)
        target = self.root / "src/hyprial/kernel/contracts.py"
        target.write_text("# externally tampered\n")  # left uncommitted on purpose
        second = self._plan_apply(mp)
        self.assertEqual(second.returncode, runner.EXIT_DRIFT, second.stderr)
        self.assertIn("already-done-mismatch", second.stderr)

    def test_deleted_output_refused_not_already_done(self):
        mp = self._manifest()
        first = self._plan_apply(mp)
        self.assertEqual(first.returncode, 0, first.stderr)
        (self.root / "src/hyprial/kernel/contracts.py").unlink()
        second = self._plan_apply(mp)
        # the deletion is caught as a completed-record mismatch (EXIT_DRIFT),
        # never reported as a legitimate already-done state
        self.assertEqual(second.returncode, runner.EXIT_DRIFT, second.stderr)
        self.assertIn("already-done-mismatch", second.stderr)

    def test_partial_completion_keeps_first_rule_state(self):
        second_rule = {
            "rule_id": "extract2",
            "kind": "symbol_extract",
            "stage": "S1",
            "status": "approved",
            "expected_matches": 1,
            "reason": "test",
            "source_path": "src/hyprial/daemon/missing.py",
            "source_sha256": "0" * 64,
            "symbols": ["Nope"],
            "target_path": "src/hyprial/kernel/other.py",
            "dependencies": [],
        }
        mp = self.write_manifest(
            {
                "schema_version": 1,
                "baseline_commit": self.head,
                "rules": [self.rule, second_rule],
            }
        )
        first = self._plan_apply(mp)
        self.assertNotEqual(first.returncode, 0, first.stdout + first.stderr)
        # rule 1 must NOT be recorded completed when the stage failed
        state_files = list((self.root / ".refactor-state").glob("m-*/stages.json"))
        self.assertFalse(
            [f for f in state_files if f.name == "stages.json"],
            "failed stage must not record completion",
        )


class TouchedPathsTests(RepoFixture):
    def setUp(self):
        super().setUp()
        self._toolchain = Path(__file__).resolve().parents[4]
        standard_repo(self.root)
        self.write("src/hyprial/daemon/core.py", "class Pump:\n    pass\n")
        self.write("src/hyprial/agents/consumer.py", "from hyprial.daemon.impl.core import Pump\n\nx = Pump\n")
        self.write("src/hyprial/agents/dir/inner.py", "A = 1\n")
        self.head = self.commit_all()

    def _apply(self, rules):
        mp = self.write_manifest(
            {"schema_version": 1, "baseline_commit": self.head, "rules": rules}
        )
        out = self.outside / "plan"
        out = self.outside / f"plan-{getattr(self, '_n', 0) + 1}"
        self._n = getattr(self, "_n", 0) + 1
        subprocess.run(
            [
                sys.executable, "-m", "scripts.refactor.run", "plan",
                "--manifest", str(mp), "--root", str(self.root),
                "--stage", "S1", "--output", str(out),
            ],
            cwd=str(self._toolchain), capture_output=True, text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )
        plan = json.loads((out / "plan.json").read_text())
        return subprocess.run(
            [
                sys.executable, "-m", "scripts.refactor.run", "apply",
                "--manifest", str(mp), "--root", str(self.root),
                "--stage", "S1", "--confirm", plan["apply_token"],
            ],
            cwd=str(self._toolchain), capture_output=True, text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )

    def _rule(self, **kw):
        base = {
            "rule_id": "t",
            "kind": "import_symbol",
            "stage": "S1",
            "status": "approved",
            "expected_matches": 1,
            "reason": "test",
            "source_path": "src/hyprial/daemon/core.py",
            "source_sha256": sha((self.root / "src/hyprial/daemon/core.py").read_bytes()),
            "old_module": "hyprial.daemon.impl.core",
            "old_symbol": "Pump",
            "new_module": "hyprial.kernel.contracts",
            "new_symbol": "Pump",
        }
        base.update(kw)
        return base

    def test_dirty_consumer_blocks_apply_even_with_matching_count(self):
        # first-fail: _touched_paths only had explicit fields, so the dirty
        # consumer escaped the drift gate and would have been rewritten
        consumer = self.root / "src/hyprial/agents/consumer.py"
        consumer.write_text(consumer.read_text() + "# local edit\n")
        proc = self._apply([self._rule()])
        self.assertEqual(proc.returncode, runner.EXIT_DRIFT, proc.stderr)
        self.assertIn("consumer.py", proc.stderr)

    def test_dirty_file_inside_moved_directory_blocks_apply(self):
        rule = {
            "rule_id": "mv",
            "kind": "layout_move",
            "stage": "S1",
            "status": "approved",
            "expected_matches": 1,
            "reason": "test",
            "moves": [
                {"from": "src/hyprial/agents/dir", "to": "src/hyprial/kernel/dir"}
            ],
        }
        (self.root / "src/hyprial/agents/dir/inner.py").write_text("A = 2\n")
        proc = self._apply([rule])
        self.assertEqual(proc.returncode, runner.EXIT_DRIFT, proc.stderr)
        self.assertIn("inner.py", proc.stderr)

    def test_untracked_target_file_is_not_overwritten(self):
        # symbol_extract CREATES its target: an untracked file already sitting
        # there must not be silently overwritten
        rule = {
            "rule_id": "extract",
            "kind": "symbol_extract",
            "stage": "S1",
            "status": "approved",
            "expected_matches": 1,
            "reason": "test",
            "source_path": "src/hyprial/daemon/core.py",
            "source_sha256": sha((self.root / "src/hyprial/daemon/core.py").read_bytes()),
            "symbols": ["Pump"],
            "target_path": "src/hyprial/kernel/contracts.py",
            "dependencies": [],
        }
        (self.root / "src/hyprial/kernel/contracts.py").write_text("# untracked\n")
        proc = self._apply([rule])
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # the untracked file is refused as a target conflict and survives
        self.assertIn("TARGET-CONFLICT", proc.stdout + proc.stderr)
        self.assertEqual(
            (self.root / "src/hyprial/kernel/contracts.py").read_text(), "# untracked\n"
        )

    def test_git_status_z_parsing_with_spaces_and_unicode(self):
        from scripts.refactor.run import _dirty_tracked_paths

        weird = "src/hyprial/agents/wei rd 名.py"
        self.write(weird, "x = 1\n")
        self.git_add(weird)
        (self.root / weird).write_text("x = 2\n")
        dirty = _dirty_tracked_paths(self.root)
        self.assertIn(weird, dirty)


if __name__ == "__main__":
    unittest.main()
