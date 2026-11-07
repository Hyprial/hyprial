"""Main-model review round 3 regressions: PY-01 .. PY-08 counterexamples.

Each test first failed against the round-2 delivery; the fixes make them
pass without weakening any earlier guarantee.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from tests.tooling.refactor.support._helpers import GIT_ENV, RepoFixture, standard_repo

from scripts.refactor import run as runner
from scripts.refactor import verify as verify_module
from scripts.refactor.transforms.common import ToolError, Workspace
from scripts.refactor.transforms.scope_policy import classify_path, default_policy


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- PY-07


class ScopeDenyByDefaultTests(unittest.TestCase):
    PROTECTED = [
        ".git/config",
        "pyproject.toml",
        "uv.lock",
        "uv.toml",
        "thirdparty/lib.py",
        "vendor/lib/x.py",
        "scripts/verify/gui-refactor/lib/engine.mjs",
        "external/anything.py",
    ]
    WRITABLE = [
        "src/hyprial/cli.py",
        "tests/test_x.py",
        "scripts/ci/step.sh",
        "contract/e2e/run.py",
        "app/src/main.py",
        "docs/design/note.md",
    ]

    def test_protected_and_unknown_paths_denied(self):
        for rel in self.PROTECTED:
            decision = classify_path(rel, default_policy())
            self.assertFalse(decision["writable"], f"{rel}: {decision}")

    def test_known_product_scopes_stay_writable(self):
        for rel in self.WRITABLE:
            decision = classify_path(rel, default_policy())
            self.assertTrue(decision["writable"], f"{rel}: {decision}")

    def test_explicit_allow_is_per_file_and_visible(self):
        policy = default_policy()
        decision = classify_path("thirdparty/lib.py", policy)
        self.assertFalse(decision["writable"])
        self.assertEqual(decision["reason"], "unclassified-not-authorized")


class WriterContainmentTests(RepoFixture):
    def test_write_through_symlinked_ancestor_refused(self):
        standard_repo(self.root)
        (self.root / "outside").mkdir()
        self.write("src/hyprial/real.py", "x = 1\n")
        (self.root / "src/hyprial/escape").symlink_to(self.root / "outside")
        ws = Workspace(self.root, ["src/hyprial/real.py"])
        with self.assertRaises(ToolError):
            ws.write("src/hyprial/escape/evil.py", b"pwn\n")

    def test_write_outside_root_refused(self):
        ws = Workspace(self.root, [])
        with self.assertRaises(ToolError):
            ws.write("../evil.py", b"pwn\n")


# ---------------------------------------------------------------- PY-01


class DynamicImportGateTests(RepoFixture):
    LAYERS = {
        "roots": ["src/hyprial"],
        "layer_order": ["kernel", "biz"],
        "layer_of_module": {"hyprial.kernel": "kernel", "hyprial.biz": "biz"},
    }

    def _check(self, code):
        self.write("src/hyprial/kernel/__init__.py", "")
        self.write("src/hyprial/biz/__init__.py", "")
        self.write("src/hyprial/biz/impl/__init__.py", "")
        self.write("src/hyprial/biz/impl/work.py", "value = 1\n")
        self.write("src/hyprial/kernel/use.py", code)
        return verify_module.check_boundary_direction(self.root, self.LAYERS)

    def test_aliased_dynamic_reverse_import_fails(self):
        report = self._check(
            "from importlib import import_module as load\nload('hyprial.biz.impl.work')\n"
        )
        self.assertEqual(report["status"], verify_module.FAILED, report)
        self.assertTrue(
            any(v["target"] == "hyprial.biz.impl.work" for v in report["violations"]),
            report["violations"],
        )

    def test_literal_dynamic_reverse_import_fails_not_pending_pass(self):
        report = self._check(
            "import importlib\nimportlib.import_module('hyprial.biz.impl.work')\n"
        )
        self.assertEqual(report["status"], verify_module.FAILED, report)
        self.assertTrue(any(v.get("import_kind") == "dynamic" for v in report["violations"]))

    def test_nonliteral_local_constant_is_propagated(self):
        report = self._check(
            "import importlib\nname = 'hyprial.biz.impl.work'\nimportlib.import_module(name)\n"
        )
        self.assertEqual(report["status"], verify_module.FAILED, report)

    def test_unresolvable_internal_dynamic_blocks(self):
        report = self._check(
            "import importlib\n\ndef go(name):\n    return importlib.import_module(name)\n"
        )
        self.assertEqual(report["status"], verify_module.BLOCKED, report)
        self.assertTrue(report["pending_dynamic_refs"])
        # BLOCKED must not satisfy acceptance
        self.assertNotEqual(report["status"], verify_module.PASSED)


# ---------------------------------------------------------------- PY-02


class ModuleExistenceAndExportsTests(RepoFixture):
    CFG = {
        "roots": ["src/hyprial"],
        "layer_order": ["kernel", "biz"],
        "layer_of_module": {"hyprial.kernel": "kernel", "hyprial.biz": "biz"},
        "public_faces": ["hyprial.kernel", "hyprial.biz"],
    }

    def setUp(self):
        super().setUp()
        self.write("src/hyprial/kernel/__init__.py", '__all__ = ["public"]\npublic = 1\nprivate_secret = 2\n')
        self.write("src/hyprial/biz/__init__.py", "")

    def test_nonexistent_same_layer_module_rejected(self):
        self.write("src/hyprial/kernel/use.py", "import hyprial.kernel.does_not_exist\n")
        report = verify_module.check_boundary_direction(self.root, self.CFG)
        self.assertEqual(report["status"], verify_module.FAILED, report)
        self.assertTrue(
            any(v.get("kind") == "nonexistent-internal-module" for v in report["violations"]),
            report["violations"],
        )

    def test_private_symbol_across_face_rejected(self):
        self.write("src/hyprial/biz/use.py", "from hyprial.kernel import private_secret\n")
        report = verify_module.check_public_face_gate(self.root, self.CFG)
        self.assertEqual(report["status"], verify_module.FAILED, report)
        self.assertTrue(
            any("private_secret" in json.dumps(v) for v in report["violations"]),
            report["violations"],
        )

    def test_allowlisted_symbol_across_face_passes(self):
        self.write("src/hyprial/biz/use.py", "from hyprial.kernel import public\n\nx = public\n")
        report = verify_module.check_public_face_gate(self.root, self.CFG)
        self.assertEqual(report["status"], verify_module.PASSED, report["violations"])

    def test_star_import_from_face_rejected(self):
        self.write("src/hyprial/biz/use.py", "from hyprial.kernel import *\n")
        report = verify_module.check_public_face_gate(self.root, self.CFG)
        self.assertEqual(report["status"], verify_module.FAILED, report)

    def test_face_without_all_blocks_symbol_imports(self):
        # no allowlist available: cannot prove symbol legality -> blocked, not pass
        self.write("src/hyprial/kernel/__init__.py", "public = 1\n")
        self.write("src/hyprial/biz/use.py", "from hyprial.kernel import public\n")
        report = verify_module.check_public_face_gate(self.root, self.CFG)
        self.assertEqual(report["status"], verify_module.BLOCKED, report)


# ---------------------------------------------------------------- PY-03


class AuthorityTypeFidelityTests(RepoFixture):
    def _baseline(self):
        return {
            "schema_version": 1,
            "policy": "deny",
            "entries": [
                {"path": "src/hyprial/kernel/use.py", "symbol": "X", "reason": "fixed", "extra": 1}
            ],
        }

    def _check(self, current):
        baseline = self._baseline()
        raw = json.dumps(baseline)
        self.write("baseline.json", raw)
        self.write("authority.json", json.dumps(current))
        config = {
            "manifests": [
                {
                    "path": "authority.json",
                    "preserved_from": "baseline.json",
                    "pin_sha256": sha(raw.encode()),
                    "expected_entries": 1,
                }
            ]
        }
        return verify_module.check_authority_manifests(self.root, config)

    def test_int_to_string_value_mutation_fails(self):
        current = self._baseline()
        current["entries"][0]["extra"] = "1"
        report = self._check(current)
        self.assertEqual(report["status"], verify_module.FAILED, report["problems"])

    def test_top_level_policy_mutation_fails(self):
        current = self._baseline()
        current["policy"] = "allow"
        report = self._check(current)
        self.assertEqual(report["status"], verify_module.FAILED, report["problems"])

    def test_identical_document_with_int_value_passes(self):
        report = self._check(self._baseline())
        self.assertEqual(report["status"], verify_module.PASSED, report["problems"])


# --------------------------------------------- PY-04 / PY-05 / PY-06 / PY-08


class CrossStageTests(RepoFixture):
    def setUp(self):
        super().setUp()
        self._toolchain = Path(__file__).resolve().parents[4]
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
        self.head = self.commit_all()

    def _run(self, cmd, mp, root, extra=()):
        return subprocess.run(
            [
                sys.executable, "-m", "scripts.refactor.run", cmd,
                "--manifest", str(mp), "--root", str(root), *extra,
            ],
            cwd=str(self._toolchain), capture_output=True, text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )

    def _extract_and_rewrite_rules(self):
        s1_source = (self.root / "src/hyprial/daemon/desired.py").read_bytes()
        return [
            {
                "rule_id": "extract",
                "kind": "symbol_extract",
                "stage": "S1",
                "status": "approved",
                "expected_matches": 1,
                "reason": "extract",
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
                "reason": "rewrite both backoff consumers incl. the S1-generated module",
                "old_module": "hyprial.kernel.backoff",
                "new_module": "hyprial.kernel.constants",
                "old_symbol": None,
                "new_symbol": None,
            },
        ]

    def test_py04_generated_module_reachable_in_next_stage(self):
        # PY-04: S1 creates contracts.py (untracked); a standalone S2 apply
        # must still see it through manifest-owned state, not MATCH-DRIFT
        mp = self.write_manifest(
            {
                "schema_version": 1,
                "baseline_commit": self.head,
                "rules": self._extract_and_rewrite_rules(),
            }
        )
        pd = self.outside / "plan"
        proc = self._run("plan", mp, self.root, ("--all", "--output", str(pd)))
        # plan may be non-executable (no evidence configured here) but must
        # still emit the cumulative simulation and its apply token
        self.assertIn(proc.returncode, (0, runner.EXIT_RULES), proc.stderr)
        plan_doc = json.loads((pd / "plan.json").read_text())
        s2_plan = [r for s in plan_doc["stages"] for r in s["rules"] if r["rule_id"] == "rewrite"][0]
        self.assertEqual(s2_plan["status"], "applied")
        self.assertEqual(s2_plan["matches"], 2)
        token = plan_doc["apply_token"]
        s1 = self._run("apply", mp, self.root, ("--stage", "S1", "--confirm", token))
        self.assertEqual(s1.returncode, 0, s1.stderr)
        s2 = self._run("apply", mp, self.root, ("--stage", "S2", "--confirm", token))
        self.assertEqual(s2.returncode, 0, s2.stderr)
        # BOTH consumers rewritten: the tracked file AND the S1-generated
        # module that only exists via manifest-owned state
        self.assertIn("hyprial.kernel.constants", (self.root / "src/hyprial/daemon/desired.py").read_text())
        self.assertIn("hyprial.kernel.constants", (self.root / "src/hyprial/kernel/contracts.py").read_text())

    def test_py05_same_file_chain_replay_is_zero_mod(self):
        # S1 a->b, S2 b->c on the same tracked file; replaying S1 or --all
        # must be an explicit zero-modification success
        for name in ("a", "b", "c"):
            self.write(f"src/hyprial/kernel/{name}.py", "V = 3\n")
        self.write("src/hyprial/agents/chain.py", "import hyprial.kernel.a as a\nv = a.V\n")
        self.git_add("src/hyprial/kernel/a.py", "src/hyprial/kernel/b.py", "src/hyprial/kernel/c.py", "src/hyprial/agents/chain.py")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.root,
            capture_output=True, text=True, env=GIT_ENV,
        ).stdout.strip()
        rules = [
            {
                "rule_id": stage, "kind": "import_module", "stage": stage,
                "status": "approved", "expected_matches": 1, "reason": "chain",
                "old_module": f"hyprial.kernel.{old}", "new_module": f"hyprial.kernel.{new}",
                "old_symbol": None, "new_symbol": None,
            }
            for stage, old, new in (("S1", "a", "b"), ("S2", "b", "c"))
        ]
        mp = self.write_manifest({"schema_version": 1, "baseline_commit": head, "rules": rules})
        pd = self.outside / "plan-chain"
        self._run("plan", mp, self.root, ("--all", "--output", str(pd)))
        token = json.loads((pd / "plan.json").read_text())["apply_token"]
        s1 = self._run("apply", mp, self.root, ("--stage", "S1", "--confirm", token))
        self.assertEqual(s1.returncode, 0, s1.stderr)
        s2 = self._run("apply", mp, self.root, ("--stage", "S2", "--confirm", token))
        self.assertEqual(s2.returncode, 0, s2.stderr)
        replay_s1 = self._run("apply", mp, self.root, ("--stage", "S1", "--confirm", token))
        self.assertEqual(replay_s1.returncode, 0, replay_s1.stderr)
        self.assertEqual(json.loads(replay_s1.stdout.strip().splitlines()[-1])["modifications"], 0)
        replay_all = self._run("apply", mp, self.root, ("--all", "--confirm", token))
        self.assertEqual(replay_all.returncode, 0, replay_all.stderr)
        self.assertEqual(json.loads(replay_all.stdout.strip().splitlines()[-1])["modifications"], 0)
        # tampering after the LAST stage must still fail
        chain = self.root / "src/hyprial/agents/chain.py"
        chain.write_text("# tampered\n")
        tampered = self._run("apply", mp, self.root, ("--stage", "S1", "--confirm", token))
        self.assertEqual(tampered.returncode, runner.EXIT_DRIFT, tampered.stderr)

    def test_py06_verify_preflight_rejects_tampered_output(self):
        rule = {
            "rule_id": "extract",
            "kind": "symbol_extract",
            "stage": "S1",
            "status": "approved",
            "expected_matches": 1,
            "reason": "extract",
            "source_path": "src/hyprial/daemon/desired.py",
            "source_sha256": sha((self.root / "src/hyprial/daemon/desired.py").read_bytes()),
            "symbols": ["LaunchSpec"],
            "target_path": "src/hyprial/kernel/contracts.py",
            "dependencies": [
                {"name": "backoff", "provision": "import", "module": "hyprial.kernel.backoff"}
            ],
        }
        mp = self.write_manifest(
            {
                "schema_version": 1,
                "baseline_commit": self.head,
                "rules": [rule],
                "verification": {
                    "S1": [
                        {"check": "resource_present", "paths": ["src/hyprial/kernel/contracts.py"], "required": True}
                    ]
                },
            }
        )
        pd = self.outside / "plan-pf"
        self._run("plan", mp, self.root, ("--stage", "S1", "--output", str(pd)))
        token = json.loads((pd / "plan.json").read_text())["apply_token"]
        applied = self._run("apply", mp, self.root, ("--stage", "S1", "--confirm", token))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        # tamper with the extraction output
        (self.root / "src/hyprial/kernel/contracts.py").write_text("class LaunchSpec:\n    corrupted = True\n")
        vd = self.outside / "verify-out"
        checked = self._run("verify", mp, self.root, ("--stage", "S1", "--output", str(vd)))
        self.assertNotEqual(checked.returncode, 0, checked.stdout)
        report = json.loads(vd.read_text())
        self.assertEqual(report["status"], verify_module.FAILED)
        self.assertIn("_preflight", report["stages"])
        self.assertEqual(report["stages"]["_preflight"]["status"], verify_module.FAILED)


class FingerprintAndEvidenceTests(RepoFixture):
    def setUp(self):
        super().setUp()
        self._toolchain = Path(__file__).resolve().parents[4]
        standard_repo(self.root)
        self.write("src/hyprial/a.py", "class S:\n    pass\n")
        self.head = self.commit_all()

    def test_py08_fingerprint_excludes_reports_includes_lockfile(self):
        from scripts.refactor.run import tool_code_hash

        base = Path(runner.__file__).resolve().parent
        lock = base / "uv.lock"
        self.assertTrue(lock.is_file())
        before = tool_code_hash()
        # a generated report under preview/ must not move the fingerprint
        preview = base / "preview" / "scratch.json"
        preview.parent.mkdir(exist_ok=True)
        preview.write_text("{}\n")
        try:
            self.assertEqual(tool_code_hash(), before)
        finally:
            preview.unlink()
        # the dependency lockfile is part of the executed identity
        original = lock.read_bytes()
        try:
            lock.write_bytes(original + b"\n")
            self.assertNotEqual(tool_code_hash(), before)
        finally:
            lock.write_bytes(original)

    def test_py08_token_mutation_uses_throwaway_copy_not_install(self):
        mp = self.write_manifest(
            {"schema_version": 1, "baseline_commit": self.head, "rules": [
                {"rule_id": "r", "kind": "import_symbol", "stage": "S1", "status": "approved",
                 "expected_matches": 1, "reason": "t", "old_module": "hyprial.a",
                 "new_module": "hyprial.b", "old_symbol": "S", "new_symbol": "S"}
            ]}
        )
        tmp_tool = self.outside / "toolcopy" / "scripts"
        tmp_tool.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            self._toolchain / "scripts", tmp_tool,
            ignore=shutil.ignore_patterns(".venv", "__pycache__", ".ruff_cache", "preview"),
        )
        env = {**GIT_ENV, "PYTHONPATH": str(tmp_tool.parent)}
        pd = self.outside / "plan-copy"
        plan_proc = subprocess.run(
            [sys.executable, "-m", "scripts.refactor.run", "plan",
             "--manifest", str(mp), "--root", str(self.root), "--stage", "S1",
             "--output", str(pd)],
            cwd=str(tmp_tool.parent), capture_output=True, text=True, env=env,
        )
        self.assertIn(plan_proc.returncode, (0, runner.EXIT_RULES), plan_proc.stderr)
        token = json.loads((pd / "plan.json").read_text())["apply_token"]
        # mutate the COPY only; the installed tool tree stays untouched
        target = tmp_tool / "refactor" / "transforms" / "imports.py"
        assert target.is_file()
        target.write_bytes(target.read_bytes() + b"\n# mutated copy\n")
        apply_proc = subprocess.run(
            [sys.executable, "-m", "scripts.refactor.run", "apply",
             "--manifest", str(mp), "--root", str(self.root), "--stage", "S1",
             "--confirm", token],
            cwd=str(tmp_tool.parent), capture_output=True, text=True, env=env,
        )
        self.assertEqual(apply_proc.returncode, runner.EXIT_CONFIRM, apply_proc.stderr)
        # the installed instance must be byte-identical to the delivery
        installed = self._toolchain / "scripts" / "refactor" / "transforms" / "imports.py"
        self.assertNotIn(b"# mutated copy", installed.read_bytes())

    def test_missing_c0_evidence_blocks_uncovered_claim(self):
        manifest = {
            "schema_version": 1,
            "baseline_commit": self.head,
            "evidence": {"references_path": "docs/evidence/does-not-exist/references.json"},
            "rules": [
                {"rule_id": "r", "kind": "import_symbol", "stage": "S1", "status": "approved",
                 "expected_matches": 1, "reason": "t", "old_module": "hyprial.a",
                 "new_module": "hyprial.b", "old_symbol": "S", "new_symbol": "S"}
            ],
        }
        mp = self.write_manifest(manifest)
        pd = self.outside / "plan-noevidence"
        self._run_tool("plan", mp, ("--stage", "S1", "--output", str(pd)))
        report = json.loads((pd / "plan.json").read_text())
        self.assertTrue(report["uncovered_evidence_blocked"])
        self.assertFalse(report["executable"])
        self.assertTrue(any("evidence" in r for r in report["blocked_reasons"]))

    def _run_tool(self, cmd, mp, extra=()):
        return subprocess.run(
            [sys.executable, "-m", "scripts.refactor.run", cmd,
             "--manifest", str(mp), "--root", str(self.root), *extra],
            cwd=str(self._toolchain), capture_output=True, text=True,
            env={**GIT_ENV, "PYTHONPATH": str(self._toolchain)},
        )


if __name__ == "__main__":
    unittest.main()
