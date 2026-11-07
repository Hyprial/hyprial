"""Defect 7/8/9 regressions: plan-apply equivalence, failpoints, edge semantics."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from tests.tooling.refactor.support._helpers import GIT_ENV, RepoFixture, standard_repo

from scripts.refactor import run as runner
from scripts.refactor.transforms import symbols
from scripts.refactor.transforms.common import Rule


def sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


class PlanApplyEquivalenceTests(RepoFixture):
    """Cumulative plan and per-stage apply must agree byte for byte."""

    def _build(self):
        standard_repo(self.root)
        self.write("src/hyprial/kernel/backoff.py", "DEFAULT = 3\n")
        self.write(
            "src/hyprial/daemon/desired.py",
            "import hyprial.kernel.backoff as backoff\n\n\nclass LaunchSpec:\n    r = backoff.DEFAULT\n",
        )
        self.write(
            "src/hyprial/agents/user.py",
            "import hyprial.daemon.impl.desired\n\nx = hyprial.daemon.impl.desired.LaunchSpec\n",
        )
        self.write("docs/evidence/refactor-c0-2026-10-01/scan-final/references.json",
                   json.dumps({"python_references": [], "tracked_text_path_references": []}))
        self.head = self.commit_all()
        s1_source = (self.root / "src/hyprial/daemon/desired.py").read_bytes()
        # stage-input pin: S2's rule pins the file AS S1 LEAVES IT; the exact
        # hash is taken from the plan report's post-stage hashes
        self.rules = [
            {
                "rule_id": "extract",
                "kind": "symbol_extract",
                "stage": "S1",
                "status": "approved",
                "expected_matches": 1,
                "reason": "extract shared contract",
                "source_path": "src/hyprial/daemon/desired.py",
                "source_sha256": sha(s1_source),
                "symbols": ["LaunchSpec"],
                "target_path": "src/hyprial/kernel/contracts.py",
                "dependencies": [
                    {"name": "backoff", "provision": "import", "module": "hyprial.kernel.backoff"}
                ],
            },
            {
                "rule_id": "rewrite",
                "kind": "import_module",
                "stage": "S2",
                "status": "approved",
                "expected_matches": 2,
                "reason": "consumer rewrite",
                "old_module": "hyprial.daemon.impl.desired",
                "new_module": "hyprial.kernel.contracts",
                "old_symbol": None,
                "new_symbol": None,
            },
        ]

    def setUp(self):
        super().setUp()
        self._toolchain = Path(__file__).resolve().parents[4]

    def _run(self, cmd, mp, root, extra=()):
        return subprocess.run(
            [
                sys.executable, "-m", "scripts.refactor.run", cmd,
                "--manifest", str(mp), "--root", str(root), *extra,
            ],
            cwd=str(self._toolchain), capture_output=True, text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )

    def test_cumulative_plan_equals_per_stage_apply(self):
        self._build()
        mp = self.write_manifest(
            {"schema_version": 1, "baseline_commit": self.head,
             "evidence": {"references_path": "docs/evidence/refactor-c0-2026-10-01/scan-final/references.json"},
             "rules": self.rules}
        )
        plan_dir = self.outside / "plan-all"
        proc = self._run("plan", mp, self.root, ("--all", "--output", str(plan_dir)))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        plan = json.loads((plan_dir / "plan.json").read_text())
        self.assertTrue(plan["executable"])
        # the plan exposes per-stage output hashes so S2's stage-input pins
        # are writable against evidence, not guesswork
        self.assertIn("post_stage_hashes", plan)

        # second identical fixture: apply S1 then S2 separately
        other = self.outside / "repo2"
        other.mkdir()
        subprocess.run(
            ["git", "clone", "-q", str(self.root), str(other)], check=True, env=GIT_ENV
        )
        subprocess.run(
            ["git", "-C", str(other), "checkout", "-q", self.head], check=True, env=GIT_ENV
        )
        # the token binds the checkout root: repo2 needs its own plan first
        other_plan_dir = self.outside / "plan-other"
        other_plan = self._run("plan", mp, other, ("--all", "--output", str(other_plan_dir)))
        self.assertEqual(other_plan.returncode, 0, other_plan.stderr)
        other_token = json.loads((other_plan_dir / "plan.json").read_text())["apply_token"]
        s1 = self._run("apply", mp, other, ("--stage", "S1", "--confirm", other_token))
        self.assertEqual(s1.returncode, 0, s1.stderr)
        s2 = self._run("apply", mp, other, ("--stage", "S2", "--confirm", other_token))
        self.assertEqual(s2.returncode, 0, s2.stderr)
        # byte-for-byte equivalence: every file the cumulative plan would
        # change must match the per-stage apply's final content exactly
        final_hashes = plan["post_stage_hashes"]["S2"]
        self.assertTrue(final_hashes)
        import hashlib as _h

        for rel, digest in final_hashes.items():
            data = (other / rel).read_bytes()
            self.assertEqual(
                _h.sha256(data).hexdigest(),
                digest,
                f"{rel}: per-stage apply must match the cumulative plan byte for byte",
            )
        # and the changed set must be identical
        applied_paths = {
            p.relative_to(other).as_posix()
            for p in other.rglob("*")
            if p.is_file() and ".git" not in p.parts and ".refactor-state" not in p.parts
        }
        changed = set(final_hashes)
        for rel in changed:
            self.assertIn(rel, applied_paths, rel)
        self.assertEqual(
            changed,
            {rel for rel in ("src/hyprial/kernel/contracts.py", "src/hyprial/daemon/desired.py", "src/hyprial/agents/user.py")},
            "the changed set must match the plan's declared outputs",
        )

    def test_token_binds_tool_code(self):
        self._build()
        mp = self.write_manifest(
            {"schema_version": 1, "baseline_commit": self.head,
             "evidence": {"references_path": "docs/evidence/refactor-c0-2026-10-01/scan-final/references.json"},
             "rules": self.rules}
        )
        plan_dir = self.outside / "plan-token"
        self._run("plan", mp, self.root, ("--all", "--output", str(plan_dir)))
        plan = json.loads((plan_dir / "plan.json").read_text())
        self.assertTrue(plan["tool_code_sha256"])
        # a mutated tool must invalidate the recorded token
        from scripts.refactor import transforms

        target = Path(transforms.__file__).parent / "imports.py"
        original = target.read_bytes()
        try:
            target.write_bytes(original + b"\n# mutated\n")
            proc2 = self._run(
                "apply", mp, self.root,
                ("--all", "--confirm", plan["apply_token"]),
            )
            self.assertEqual(proc2.returncode, runner.EXIT_CONFIRM, proc2.stderr)
            self.assertIn("tool_code_sha256", proc2.stderr)
        finally:
            target.write_bytes(original)

    def test_plan_reports_baseline_mismatch(self):
        self._build()
        mp = self.write_manifest(
            {"schema_version": 1, "baseline_commit": "0" * 40, "rules": self.rules}
        )
        plan_dir = self.outside / "plan-baseline"
        self._run("plan", mp, self.root, ("--all", "--output", str(plan_dir)))
        plan = json.loads((plan_dir / "plan.json").read_text())
        self.assertFalse(plan["baseline_match"])
        self.assertFalse(plan["executable"])
        self.assertTrue(any("baseline" in r for r in plan["blocked_reasons"]))


class FailpointTests(RepoFixture):
    """Defect 8: injected write failures leave explicit, resumable state."""

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
            "reason": "t",
            "source_path": "src/hyprial/daemon/desired.py",
            "source_sha256": sha((self.root / "src/hyprial/daemon/desired.py").read_bytes()),
            "symbols": ["Spec"],
            "target_path": "src/hyprial/kernel/contracts.py",
            "dependencies": [],
        }
        self.mp = self.write_manifest(
            {"schema_version": 1, "baseline_commit": self.head, "rules": [self.rule]}
        )

    def _apply_inprocess(self):
        argv = [
            "apply", "--manifest", str(self.mp), "--root", str(self.root),
            "--stage", "S1", "--confirm", "0" * 64,
        ]
        subprocess.run(
            [sys.executable, "-m", "scripts.refactor.run", "plan",
             "--manifest", str(self.mp), "--root", str(self.root), "--stage", "S1"],
            cwd=str(self._toolchain), capture_output=True, text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )
        out = self.outside / "plan-fp"
        subprocess.run(
            [sys.executable, "-m", "scripts.refactor.run", "plan",
             "--manifest", str(self.mp), "--root", str(self.root), "--stage", "S1",
             "--output", str(out)],
            cwd=str(self._toolchain), capture_output=True, text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )
        plan = json.loads((out / "plan.json").read_text())
        argv[argv.index("--confirm") + 1] = plan["apply_token"]
        return runner.main(argv)

    def test_content_write_failure_records_partial_state(self):
        import scripts.refactor.run as run_module

        original = run_module._write_output_files
        def exploding(root, ws, materialized):
            raise OSError("injected disk failure")
        run_module._write_output_files = exploding
        try:
            rc = self._apply_inprocess()
        finally:
            run_module._write_output_files = original
        self.assertEqual(rc, runner.EXIT_WRITE)
        failure_dir = next((self.root / ".refactor-state").glob("m-*"))
        failure = json.loads((failure_dir / "last-failure.json").read_text())
        self.assertEqual(failure["status"], "failed")
        self.assertEqual(failure["phase"], "materialize")
        # nothing materialized: the target file must not exist
        self.assertFalse((self.root / "src/hyprial/kernel/contracts.py").exists())

    def test_applied_stage_is_not_verified(self):
        # a completed apply records "applied"; only a passing verify records
        # verification — the two must never be conflated
        token_out = self.outside / "plan-v"
        subprocess.run(
            [sys.executable, "-m", "scripts.refactor.run", "plan",
             "--manifest", str(self.mp), "--root", str(self.root), "--stage", "S1",
             "--output", str(token_out)],
            cwd=str(self._toolchain), capture_output=True, text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )
        plan = json.loads((token_out / "plan.json").read_text())
        rc = runner.main(
            ["apply", "--manifest", str(self.mp), "--root", str(self.root),
             "--stage", "S1", "--confirm", plan["apply_token"]]
        )
        self.assertEqual(rc, 0)
        state_file = next((self.root / ".refactor-state").glob("m-*/stages.json"))
        state = json.loads(state_file.read_text())
        self.assertEqual(state["stages"]["S1"]["status"], "applied")
        self.assertNotIn("verified", state["stages"]["S1"])


class EdgeSemanticsTests(RepoFixture):
    """Defect 9: async, generator, decorator and annotation preservation."""

    def setUp(self):
        super().setUp()
        standard_repo(self.root)

    def _extract_methods(self, source, methods, **kw):
        rule = {
            "rule_id": "collab",
            "kind": "class_member_extract",
            "stage": "S4",
            "status": "approved",
            "expected_matches": len(methods),
            "reason": "t",
            "class_name": "DaemonApplication",
            "symbols": methods,
            "new_class_name": "Collab",
            "target_path": "src/hyprial/daemon/_collab.py",
            "host_attribute": "_collab",
            "dependencies": [],
            "shim_methods": list(methods),
            "wiring": {"method": "__init__", "code": "pass"},
        }
        rule.update(kw)
        self.write("src/hyprial/daemon/application.py", source)
        rule["source_path"] = "src/hyprial/daemon/application.py"
        rule["source_sha256"] = sha(
            (self.root / "src/hyprial/daemon/application.py").read_bytes()
        )
        ws, ctx = self.build_workspace()
        outcome = symbols.apply_rule(Rule(rule), ctx)
        if outcome.status == "applied":
            ws.materialize()
        return outcome

    def test_async_method_shim_and_collab_preserve_async(self):
        source = (
            "class DaemonApplication:\n"
            "    def __init__(self):\n"
            "        pass\n\n"
            "    async def _drain(self):\n"
            "        return 1\n"
        )
        outcome = self._extract_methods(source, ["_drain"])
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        collab = self.assertRead("src/hyprial/daemon/_collab.py")
        self.assertIn("async def _drain(self):", collab)
        host = self.assertRead("src/hyprial/daemon/application.py")
        self.assertIn("async def _drain(self):", host)
        self.assertIn("return await self._collab._drain()", host)

    def test_generator_method_shim_returns_the_generator(self):
        source = (
            "class DaemonApplication:\n"
            "    def __init__(self):\n"
            "        pass\n\n"
            "    def _walk(self):\n"
            "        yield 1\n"
            "        yield 2\n"
        )
        outcome = self._extract_methods(source, ["_walk"])
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        host = self.assertRead("src/hyprial/daemon/application.py")
        # a generator shim MUST return the delegated generator object
        self.assertIn("return self._collab._walk()", host)
        collab = self.assertRead("src/hyprial/daemon/_collab.py")
        self.assertIn("yield 1", collab)

    def test_decorated_method_shim_keeps_decorator(self):
        source = (
            "class DaemonApplication:\n"
            "    def __init__(self):\n"
            "        pass\n\n"
            "    @property\n"
            "    def ready(self):\n"
            "        return True\n"
        )
        outcome = self._extract_methods(source, ["ready"])
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        host = self.assertRead("src/hyprial/daemon/application.py")
        self.assertIn("@property", host)
        self.assertIn("return self._collab.ready\n", host)

    def test_annotated_signature_is_preserved_in_shim(self):
        source = (
            "class DaemonApplication:\n"
            "    def __init__(self):\n"
            "        pass\n\n"
            "    def _send(self, stream: str, count: int = 0) -> bool:\n"
            "        return True\n"
        )
        outcome = self._extract_methods(source, ["_send"])
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        host = self.assertRead("src/hyprial/daemon/application.py")
        self.assertIn("def _send(self, stream: str, count: int = 0) -> bool:", host)


class FacadeConvertPinTests(RepoFixture):
    """Converting an existing facade requires a pinned old content hash."""

    def test_convert_with_matching_pin(self):
        from scripts.refactor.transforms import facades
        from scripts.refactor.transforms.common import Rule as R

        standard_repo(self.root)
        old = "VERSION = '1.0'\n"
        self.write("src/hyprial/kernel/__init__.py", old)
        ws, ctx = self.build_workspace()
        outcome = facades.apply_rule(
            R(
                {
                    "rule_id": "f",
                    "kind": "facade_export",
                    "stage": "S2",
                    "status": "approved",
                    "expected_matches": 1,
                    "reason": "t",
                    "facade_path": "src/hyprial/kernel/__init__.py",
                    "facade_module": "hyprial.kernel",
                    "provider_module": "hyprial.kernel.impl",
                    "symbol": "Thing",
                    "convert_pin": {"content_sha256": sha(old.encode())},
                }
            ),
            ctx,
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        ws.materialize()
        self.assertIn("_FACADE_EXPORTS", self.assertRead("src/hyprial/kernel/__init__.py"))

    def test_convert_with_wrong_pin_refused(self):
        from scripts.refactor.transforms import facades
        from scripts.refactor.transforms.common import Rule as R

        standard_repo(self.root)
        self.write("src/hyprial/kernel/__init__.py", "VERSION = '1.0'\n")
        ws, ctx = self.build_workspace()
        outcome = facades.apply_rule(
            R(
                {
                    "rule_id": "f",
                    "kind": "facade_export",
                    "stage": "S2",
                    "status": "approved",
                    "expected_matches": 1,
                    "reason": "t",
                    "facade_path": "src/hyprial/kernel/__init__.py",
                    "facade_module": "hyprial.kernel",
                    "provider_module": "hyprial.kernel.impl",
                    "symbol": "Thing",
                    "convert_pin": {"content_sha256": "0" * 64},
                }
            ),
            ctx,
        )
        self.assertRefused(outcome, "CONVERT-PIN-MISMATCH")


if __name__ == "__main__":
    unittest.main()
