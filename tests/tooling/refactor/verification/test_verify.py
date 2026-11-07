"""Verification dispatch tests: gates, authority preservation, command gating."""

from __future__ import annotations

import json
import sys
import unittest

from tests.tooling.refactor.support._helpers import RepoFixture

from scripts.refactor import verify as verify_module


class BoundaryDirectionTests(unittest.TestCase):
    def test_detects_reverse_direction_and_unmapped_pass(self):
        fixture = RepoFixture()
        fixture.setUp()
        try:
            root = fixture.root
            fixture.write(
                "src/hyprial/kernel/bad.py",
                "import hyprial.biz.workflow\n",
            )
            fixture.write("src/hyprial/biz/workflow.py", "X = 1\n")
            report = verify_module.check_boundary_direction(
                root,
                {
                    "layer_order": ["kernel", "identity", "daemon", "biz", "shell"],
                    "layer_of_module": {
                        "hyprial.kernel": "kernel",
                        "hyprial.biz": "biz",
                    },
                },
            )
            self.assertEqual(report["status"], verify_module.FAILED)
            self.assertEqual(len(report["violations"]), 1)
            self.assertEqual(report["violations"][0]["from"].startswith("kernel"), True)
            # allowed direction: biz imports kernel (violation file removed)
            (root / "src/hyprial/kernel/bad.py").unlink()
            fixture.write("src/hyprial/kernel/base.py", "X = 1\n")
            fixture.write("src/hyprial/biz/ok.py", "import hyprial.kernel.base\n")
            report = verify_module.check_boundary_direction(
                root,
                {
                    "layer_order": ["kernel", "identity", "daemon", "biz", "shell"],
                    "layer_of_module": {
                        "hyprial.kernel": "kernel",
                        "hyprial.biz": "biz",
                    },
                },
            )
            self.assertEqual(report["status"], verify_module.PASSED, report["violations"])
        finally:
            fixture.tearDown()


class PublicFaceGateTests(unittest.TestCase):
    def test_cross_layer_import_must_target_face(self):
        fixture = RepoFixture()
        fixture.setUp()
        try:
            root = fixture.root
            fixture.write("src/hyprial/daemon/user.py", "import hyprial.biz.internal_thing\n")
            fixture.write("src/hyprial/biz/internal_thing.py", "X = 1\n")
            config = {
                "layer_of_module": {"hyprial.daemon": "daemon", "hyprial.biz": "biz"},
                "public_faces": ["hyprial.biz"],
            }
            # a registered face does not whitelist its own submodules:
            # hyprial.biz.internal_thing stays a bypass
            report = verify_module.check_public_face_gate(root, config)
            self.assertEqual(report["status"], verify_module.FAILED, report["violations"])
            # importing the face itself is the allowed form
            (root / "src/hyprial/daemon/user.py").write_text(
                "import hyprial.biz\n", encoding="utf-8"
            )
            (root / "src/hyprial/biz/__init__.py").write_text("", encoding="utf-8")
            report = verify_module.check_public_face_gate(root, config)
            self.assertEqual(report["status"], verify_module.PASSED, report["violations"])
            config_bad = {"layer_of_module": config["layer_of_module"], "public_faces": []}
            report = verify_module.check_public_face_gate(root, config_bad)
            self.assertEqual(report["status"], verify_module.FAILED)
        finally:
            fixture.tearDown()


class AuthorityManifestTests(unittest.TestCase):
    def test_identity_and_reasons_must_be_preserved(self):
        fixture = RepoFixture()
        fixture.setUp()
        try:
            root = fixture.root
            baseline = [
                {"path": "src/hyprial/agents/registry.py", "reason": "Pre-cutover owner.", "symbol": "AgentRegistry"},
            ]
            import hashlib as _h
            baseline_text = json.dumps(baseline, indent=2) + "\n"
            fixture.write("tests/baseline.json", baseline_text)
            config = {
                "manifests": [
                    {
                        "path": "tests/current.json",
                        "expected_entries": 1,
                        "preserved_from": "tests/baseline.json",
                        "pin_sha256": _h.sha256(baseline_text.encode()).hexdigest(),
                        "allowed_moves": [
                            {"from": "src/hyprial/agents/registry.py", "to": "src/hyprial/identity/registry.py"}
                        ],
                    }
                ]
            }
            # path migrated via an approved move: passes
            fixture.write(
                "tests/current.json",
                json.dumps(
                    [{"path": "src/hyprial/identity/registry.py", "reason": "Pre-cutover owner.", "symbol": "AgentRegistry"}],
                    indent=2,
                )
                + "\n",
            )
            report = verify_module.check_authority_manifests(root, config)
            self.assertEqual(report["status"], verify_module.PASSED, report["problems"])
            # reason tampered: fails
            fixture.write(
                "tests/current.json",
                json.dumps(
                    [{"path": "src/hyprial/identity/registry.py", "reason": "Rewritten reason.", "symbol": "AgentRegistry"}],
                    indent=2,
                )
                + "\n",
            )
            report = verify_module.check_authority_manifests(root, config)
            self.assertEqual(report["status"], verify_module.FAILED)
            # entry dropped: fails on count
            fixture.write("tests/current.json", "[]\n")
            report = verify_module.check_authority_manifests(root, config)
            self.assertEqual(report["status"], verify_module.FAILED)
        finally:
            fixture.tearDown()

    def test_expected_entries_count(self):
        fixture = RepoFixture()
        fixture.setUp()
        try:
            root = fixture.root
            fixture.write("tests/exemptions.json", json.dumps([{"path": "a", "symbol": "S", "reason": "r"}]) + "\n")
            report = verify_module.check_authority_manifests(
                root, {"manifests": [{"path": "tests/exemptions.json", "expected_entries": 129}]}
            )
            self.assertEqual(report["status"], verify_module.FAILED)
            self.assertTrue(any("expected 129" in p["error"] for p in report["problems"]))
        finally:
            fixture.tearDown()


class ResourceAndCommandTests(unittest.TestCase):
    def test_resource_present(self):
        fixture = RepoFixture()
        fixture.setUp()
        try:
            root = fixture.root
            fixture.write("src/hyprial/routine/templates/daily.txt", "DAY")
            report = verify_module.check_resource_present(
                root, {"paths": ["src/hyprial/routine/templates/daily.txt"]}
            )
            self.assertEqual(report["status"], verify_module.PASSED)
            report = verify_module.check_resource_present(root, {"paths": ["missing/file.txt"]})
            self.assertEqual(report["status"], verify_module.FAILED)
        finally:
            fixture.tearDown()

    def test_command_skipped_without_flag(self):
        fixture = RepoFixture()
        fixture.setUp()
        try:
            report = verify_module.check_command(
                fixture.root, {"argv": [sys.executable, "-c", "print('hi')"]}, run_commands=False
            )
            self.assertEqual(report["status"], verify_module.SKIPPED)
        finally:
            fixture.tearDown()

    def test_command_runs_with_flag(self):
        fixture = RepoFixture()
        fixture.setUp()
        try:
            report = verify_module.check_command(
                fixture.root,
                {"argv": [sys.executable, "-c", "print('hi')"], "expect_exit": 0},
                run_commands=True,
            )
            self.assertEqual(report["status"], verify_module.PASSED, report)
            report = verify_module.check_command(
                fixture.root, {"argv": [sys.executable, "-c", "raise SystemExit(3)"]}, run_commands=True
            )
            self.assertEqual(report["status"], verify_module.FAILED)
        finally:
            fixture.tearDown()

    def test_stage_verification_report_shape(self):
        fixture = RepoFixture()
        fixture.setUp()
        try:
            manifest = {
                "schema_version": 1,
                "baseline_commit": "a" * 40,
                "rules": [],
                "verification": {
                    "S2": [
                        {"check": "resource_present", "paths": ["missing.txt"]},
                        {"check": "command", "argv": [sys.executable, "-c", "print('x')"]},
                    ]
                },
            }
            report = verify_module.run_stage_verification(fixture.root, manifest, "S2", run_commands=False)
            self.assertEqual(report["status"], verify_module.FAILED)
            results = report["stages"]["S2"]["results"]
            self.assertEqual(results[0]["status"], verify_module.FAILED)
            self.assertEqual(results[1]["status"], verify_module.SKIPPED)
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
