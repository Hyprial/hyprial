"""run.py integration tests: plan safety, apply gating, idempotency, recovery."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

from tests.tooling.refactor.support._helpers import GIT_ENV, RepoFixture, standard_repo

from scripts.refactor import run as runner


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PipelineTests(RepoFixture):
    def setUp(self):
        super().setUp()
        self._toolchain = Path(__file__).resolve().parents[4]
        standard_repo(self.root)
        self.write("src/hyprial/agents/registry.py", "class AgentRegistry:\n    pass\n")
        self.write(
            "src/hyprial/agents/user.py",
            "from hyprial.agents.registry import AgentRegistry\n\nx = AgentRegistry\n",
        )
        self.write(
            "docs/notes/evidence/worm.txt",
            "WORM-EVIDENCE-LINE\n" * 5,
        )
        # fixture evidence committed up front so plan snapshots are stable
        evidence_dir = self.root / "docs/evidence/refactor-c0-2026-10-01/scan-final"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "references.json").write_text(json.dumps({
            "python_references": [
                {"kind": "import_module", "path": "contract/e2e/run-host.py", "line": 5,
                 "value": "hyprial.agents.registry"}
            ],
            "tracked_text_path_references": [],
        }))
        self.head = self.commit_all()
        self.registry_sha = sha((self.root / "src/hyprial/agents/registry.py").read_bytes())

    def apply(self, manifest_path, extra=None):
        # plan first to obtain the token
        counter = getattr(self, "_plan_counter", 0) + 1
        self._plan_counter = counter
        out = self.outside / f"plan-{manifest_path.stem}-{counter}"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.refactor.run",
                "plan",
                "--manifest",
                str(manifest_path),
                "--root",
                str(self.root),
                "--all",
                "--output",
                str(out),
            ],
            cwd=str(self._toolchain),
            capture_output=True,
            text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )
        # a plan without configured evidence is legally non-executable (rc=7)
        # while still emitting the cumulative simulation and its token
        self.assertIn(proc.returncode, (0, runner.EXIT_RULES), proc.stderr)
        plan = json.loads((out / "plan.json").read_text())
        argv = [
            sys.executable,
            "-m",
            "scripts.refactor.run",
            "apply",
            "--manifest",
            str(manifest_path),
            "--root",
            str(self.root),
            "--all",
            "--confirm",
            plan["apply_token"],
        ]
        proc = subprocess.run(
            argv,
            cwd=str(self._toolchain),
            capture_output=True,
            text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )
        return proc, plan

    def apply_raw(self, manifest_path, token):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.refactor.run",
                "apply",
                "--manifest",
                str(manifest_path),
                "--root",
                str(self.root),
                "--all",
                "--confirm",
                token,
            ],
            cwd=str(self._toolchain),
            capture_output=True,
            text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )

    def import_symbol_manifest(self, **overrides):
        rule = {
            "rule_id": "move-registry",
            "kind": "import_symbol",
            "stage": "S1",
            "status": "approved",
            "expected_matches": 1,
            "reason": "synthetic",
            "source_path": "src/hyprial/agents/registry.py",
            "source_sha256": self.registry_sha,
            "old_module": "hyprial.agents.registry",
            "old_symbol": "AgentRegistry",
            "new_module": "hyprial.identity.registry",
            "new_symbol": "AgentRegistry",
        }
        rule.update(overrides)
        return {
            "schema_version": 1,
            "baseline_commit": self.head,
            "evidence": {
                "references_path": "docs/evidence/refactor-c0-2026-10-01/scan-final/references.json"
            },
            "rules": [rule],
        }

    # -- plan ----------------------------------------------------------------
    def test_plan_makes_no_writes_and_reports_matches(self):
        before = {
            p.relative_to(self.root).as_posix(): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file() and ".git" not in p.parts
        }
        mp = self.write_manifest(self.import_symbol_manifest())
        out = self.outside / "plan-a"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.refactor.run",
                "plan",
                "--manifest",
                str(mp),
                "--root",
                str(self.root),
                "--all",
                "--output",
                str(out),
            ],
            cwd=str(self._toolchain),
            capture_output=True,
            text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )
        plan = json.loads((out / "plan.json").read_text())
        self.assertTrue(plan["executable"])
        self.assertEqual(plan["stages"][0]["rules"][0]["matches"], 1)
        self.assertIn("src/hyprial/agents/user.py", plan["changed_paths"])
        after = {
            p.relative_to(self.root).as_posix(): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file() and ".git" not in p.parts
        }
        self.assertEqual(before, after, "plan must not modify the repository")

    def test_cumulative_stages_simulate_forward_state(self):
        extract = {
            "rule_id": "extract",
            "kind": "symbol_extract",
            "stage": "S1",
            "status": "approved",
            "expected_matches": 1,
            "reason": "synthetic",
            "source_path": "src/hyprial/agents/registry.py",
            "source_sha256": self.registry_sha,
            "symbols": ["AgentRegistry"],
            "target_path": "src/hyprial/kernel/contracts.py",
            "dependencies": [],
        }
        rewrite = self.import_symbol_manifest()["rules"][0]
        mp = self.write_manifest(
            {"schema_version": 1, "baseline_commit": self.head, "rules": [extract, rewrite]}
        )
        out = self.outside / "plan-cum"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.refactor.run",
                "plan",
                "--manifest",
                str(mp),
                "--root",
                str(self.root),
                "--all",
                "--output",
                str(out),
            ],
            cwd=str(self._toolchain),
            capture_output=True,
            text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )
        plan = json.loads((out / "plan.json").read_text())
        statuses = {r["rule_id"]: r["status"] for s in plan["stages"] for r in s["rules"]}
        self.assertEqual(
            statuses, {"extract": "applied", "move-registry": "applied"}, statuses
        )
        # the S1 extraction created the target in simulation, so the import
        # rule (same stage, later) still matches its expected site
        diff_text = "".join(d["diff"] for d in plan["diff_files"])
        self.assertIn("contracts.py", diff_text)

    def test_plan_refuses_when_manifest_invalid(self):
        mp = self.write_manifest({"schema_version": 1, "baseline_commit": self.head, "rules": []})
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.refactor.run",
                "plan",
                "--manifest",
                str(mp),
                "--root",
                str(self.root),
            ],
            cwd=str(self._toolchain),
            capture_output=True,
            text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("validation failed", proc.stderr)

    def test_plan_reports_draft_rules_as_pending(self):
        mp = self.write_manifest(
            self.import_symbol_manifest(status="draft")
        )
        out = self.outside / "plan-draft"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.refactor.run",
                "plan",
                "--manifest",
                str(mp),
                "--root",
                str(self.root),
                "--all",
                "--output",
                str(out),
            ],
            cwd=str(self._toolchain),
            capture_output=True,
            text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )
        self.assertEqual(proc.returncode, runner.EXIT_RULES)
        plan = json.loads((out / "plan.json").read_text())
        self.assertFalse(plan["executable"])
        self.assertEqual(len(plan["pending_decisions"]), 1)

    def test_plan_uncovered_references_reported(self):
        refs = {
            "python_references": [
                {
                    "kind": "import_module",
                    "path": "contract/other.py",
                    "line": 10,
                    "value": "hyprial.agents.registry",
                }
            ],
            "tracked_text_path_references": [],
        }
        evidence_dir = self.root / "docs/evidence/refactor-c0-2026-10-01/scan-final"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "references.json").write_text(json.dumps(refs))
        manifest = self.import_symbol_manifest()
        manifest["evidence"] = {
            "references_path": "docs/evidence/refactor-c0-2026-10-01/scan-final/references.json"
        }
        mp = self.write_manifest(manifest)
        out = self.outside / "plan-uncovered"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.refactor.run",
                "plan",
                "--manifest",
                str(mp),
                "--root",
                str(self.root),
                "--all",
                "--output",
                str(out),
            ],
            cwd=str(self._toolchain),
            capture_output=True,
            text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )
        plan = json.loads((out / "plan.json").read_text())
        self.assertTrue(
            any(r["value"] == "hyprial.agents.registry" for r in plan["uncovered_references"]),
            plan["uncovered_references"],
        )

    # -- apply ----------------------------------------------------------------
    def test_apply_success_then_idempotent_zero_diff(self):
        mp = self.write_manifest(self.import_symbol_manifest())
        proc, plan = self.apply(mp)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(summary["applied"], 1)
        self.assertEqual(
            (self.root / "src/hyprial/agents/user.py").read_text(),
            "from hyprial.identity.registry import AgentRegistry\n\nx = AgentRegistry\n",
        )
        proc2, _ = self.apply(mp)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        summary2 = json.loads(proc2.stdout.strip().splitlines()[-1])
        self.assertEqual(summary2["already_done"], 1)
        self.assertEqual(summary2["applied"], 0)
        self.assertEqual(summary2["modifications"], 0)
        # status reflects completion
        proc3 = subprocess.run(
            [sys.executable, "-m", "scripts.refactor.run", "status", "--root", str(self.root)],
            cwd=str(self._toolchain),
            capture_output=True,
            text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )
        status = json.loads(proc3.stdout)
        entry = next(iter(status["manifests"].values()))
        self.assertEqual(entry["stages"]["S1"]["status"], "applied")

    def test_apply_refuses_wrong_confirm_token(self):
        mp = self.write_manifest(self.import_symbol_manifest())
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.refactor.run",
                "apply",
                "--manifest",
                str(mp),
                "--root",
                str(self.root),
                "--all",
                "--confirm",
                "0" * 64,
            ],
            cwd=str(self._toolchain),
            capture_output=True,
            text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )
        self.assertEqual(proc.returncode, runner.EXIT_CONFIRM)

    def test_apply_refuses_unapproved_rules(self):
        mp = self.write_manifest(self.import_symbol_manifest(status="draft"))
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.refactor.run",
                "apply",
                "--manifest",
                str(mp),
                "--root",
                str(self.root),
                "--all",
                "--confirm",
                "x",
            ],
            cwd=str(self._toolchain),
            capture_output=True,
            text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )
        self.assertEqual(proc.returncode, runner.EXIT_UNAPPROVED)

    def test_apply_refuses_baseline_mismatch(self):
        mp = self.write_manifest(self.import_symbol_manifest())
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.refactor.run",
                "apply",
                "--manifest",
                str(mp),
                "--root",
                str(self.root),
                "--all",
                "--confirm",
                "x",
            ],
            cwd=str(self._toolchain),
            capture_output=True,
            text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )
        self.assertEqual(proc.returncode, runner.EXIT_CONFIRM)
        # now drift the baseline: add a commit
        self.write("README.md", "drift\n")
        self.git_add("README.md")
        proc2 = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.refactor.run",
                "apply",
                "--manifest",
                str(mp),
                "--root",
                str(self.root),
                "--all",
                "--confirm",
                "x",
            ],
            cwd=str(self._toolchain),
            capture_output=True,
            text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )
        self.assertEqual(proc2.returncode, runner.EXIT_BASELINE)

    def test_apply_refuses_dirty_manifest_inputs(self):
        mp = self.write_manifest(self.import_symbol_manifest())
        # modify a manifest-touched file (source_path) without committing
        path = self.root / "src/hyprial/agents/registry.py"
        path.write_text(path.read_text() + "# local edit\n")
        out = self.outside / "plan-dirty"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.refactor.run",
                "plan",
                "--manifest",
                str(mp),
                "--root",
                str(self.root),
                "--all",
                "--output",
                str(out),
            ],
            cwd=str(self._toolchain),
            capture_output=True,
            text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )
        plan = json.loads((out / "plan.json").read_text())
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.refactor.run",
                "apply",
                "--manifest",
                str(mp),
                "--root",
                str(self.root),
                "--all",
                "--confirm",
                plan["apply_token"],
            ],
            cwd=str(self._toolchain),
            capture_output=True,
            text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )
        self.assertEqual(proc.returncode, runner.EXIT_DRIFT)
        self.assertIn("dirty-inputs", proc.stderr)

    def test_match_drift_preserves_state_and_fails(self):
        rule = self.import_symbol_manifest()["rules"][0]
        rule["expected_matches"] = 99
        mp = self.write_manifest({"schema_version": 1, "baseline_commit": self.head, "rules": [rule]})
        out = self.outside / "plan-drift"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.refactor.run",
                "plan",
                "--manifest",
                str(mp),
                "--root",
                str(self.root),
                "--all",
                "--output",
                str(out),
            ],
            cwd=str(self._toolchain),
            capture_output=True,
            text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )
        plan = json.loads((out / "plan.json").read_text())
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.refactor.run",
                "apply",
                "--manifest",
                str(mp),
                "--root",
                str(self.root),
                "--all",
                "--confirm",
                plan["apply_token"],
            ],
            cwd=str(self._toolchain),
            capture_output=True,
            text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )
        self.assertEqual(proc.returncode, runner.EXIT_RULES)
        # nothing was written
        self.assertEqual(
            (self.root / "src/hyprial/agents/user.py").read_text().splitlines()[0],
            "from hyprial.agents.registry import AgentRegistry",
        )
        failure = None
        for state_dir in (self.root / ".refactor-state").glob("m-*"):
            candidate = state_dir / "last-failure.json"
            if candidate.exists():
                failure = json.loads(candidate.read_text())
        self.assertIsNotNone(failure)
        self.assertEqual(failure["status"], "failed")

    def test_source_vanished_is_not_already_done(self):
        (self.root / "src/hyprial/agents/user.py").write_text("x = 1\n")
        self.git_add("src/hyprial/agents/user.py")
        new_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.root,
            capture_output=True,
            text=True,
            env=GIT_ENV,
        ).stdout.strip()
        rule = self.import_symbol_manifest()["rules"][0]
        mp = self.write_manifest({"schema_version": 1, "baseline_commit": new_head, "rules": [rule]})
        out = self.outside / "plan-vanish"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.refactor.run",
                "plan",
                "--manifest",
                str(mp),
                "--root",
                str(self.root),
                "--all",
                "--output",
                str(out),
            ],
            cwd=str(self._toolchain),
            capture_output=True,
            text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )
        plan = json.loads((out / "plan.json").read_text())
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.refactor.run",
                "apply",
                "--manifest",
                str(mp),
                "--root",
                str(self.root),
                "--all",
                "--confirm",
                plan["apply_token"],
            ],
            cwd=str(self._toolchain),
            capture_output=True,
            text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )
        self.assertEqual(proc.returncode, runner.EXIT_RULES)

    def test_corrupt_state_refused(self):
        mp = self.write_manifest(self.import_symbol_manifest())
        proc, _ = self.apply(mp)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state_dir = next((self.root / ".refactor-state").glob("m-*"))
        (state_dir / "stages.json").write_text("{not json")
        proc2, _ = self.apply(mp)
        self.assertEqual(proc2.returncode, runner.EXIT_STATE)

    def test_post_completion_external_edit_refused_as_stage_drift(self):
        mp = self.write_manifest(self.import_symbol_manifest())
        proc, plan = self.apply(mp)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # external edit AFTER completion: no longer satisfies the manifest
        user = self.root / "src/hyprial/agents/user.py"
        user.write_text("# externally reverted\n")  # left uncommitted on purpose
        proc2 = self.apply_raw(mp, plan["apply_token"])
        # the external edit is caught as a completed-record mismatch
        self.assertEqual(proc2.returncode, runner.EXIT_DRIFT, proc2.stderr)
        self.assertIn("already-done-mismatch", proc2.stderr)

    def test_worm_layout_move_via_apply(self):
        rule = {
            "rule_id": "move-worm",
            "kind": "layout_move",
            "stage": "S5",
            "status": "approved",
            "expected_matches": 1,
            "reason": "synthetic worm move",
            "moves": [
                {
                    "from": "docs/notes/evidence/worm.txt",
                    "to": "docs/notes/archive/worm.txt",
                    "worm": True,
                    "sha256": sha((self.root / "docs/notes/evidence/worm.txt").read_bytes()),
                }
            ],
        }
        mp = self.write_manifest({"schema_version": 1, "baseline_commit": self.head, "rules": [rule]})
        proc, _ = self.apply(mp)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse((self.root / "docs/notes/evidence/worm.txt").exists())
        moved = (self.root / "docs/notes/archive/worm.txt").read_bytes()
        self.assertEqual(sha(moved), sha(b"WORM-EVIDENCE-LINE\n" * 5))
        proc2, _ = self.apply(mp)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        summary2 = json.loads(proc2.stdout.strip().splitlines()[-1])
        self.assertEqual(summary2["already_done"], 1)


if __name__ == "__main__":
    unittest.main()
