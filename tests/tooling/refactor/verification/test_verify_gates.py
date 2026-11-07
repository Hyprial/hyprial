"""Architecture gate regressions: relative imports, face submodules, no silent PASS."""

from __future__ import annotations

import json
import unittest

from tests.tooling.refactor.support._helpers import RepoFixture

from scripts.refactor import verify as verify_module

LAYERS = {
    "layer_order": ["kernel", "identity", "daemon", "biz", "shell"],
    "layer_of_module": {
        "hyprial.kernel": "kernel",
        "hyprial.identity": "identity",
        "hyprial.daemon": "daemon",
        "hyprial.biz": "biz",
        "hyprial.shell": "shell",
    },
    "roots": ["src"],
}


class BoundaryDirectionGateTests(RepoFixture):
    def _check(self):
        return verify_module.check_boundary_direction(self.root, LAYERS)

    def test_relative_import_across_layers_is_detected(self):
        # first-fail: `if node.level: continue` skipped relative imports
        self.write("src/hyprial/daemon/pkg/__init__.py", "")
        self.write("src/hyprial/daemon/pkg/user.py", "from ...biz.workflow import Thing\n")
        self.write("src/hyprial/biz/workflow.py", "class Thing:\n    pass\n")
        report = self._check()
        self.assertEqual(report["status"], verify_module.FAILED, report)
        self.assertTrue(
            any(v.get("target") == "hyprial.biz.workflow" for v in report["violations"]),
            report["violations"],
        )

    def test_relative_from_package_child_across_layers(self):
        self.write("src/hyprial/daemon/pkg/__init__.py", "")
        self.write("src/hyprial/daemon/pkg/user.py", "from ... import biz\n\nx = biz\n")
        self.write("src/hyprial/biz/__init__.py", "workflow = 1\n")
        report = self._check()
        self.assertEqual(report["status"], verify_module.FAILED, report)

    def test_type_checking_and_function_local_imports_are_checked(self):
        self.write(
            "src/hyprial/daemon/user.py",
            "from typing import TYPE_CHECKING\n\n\n"
            "if TYPE_CHECKING:\n    from hyprial.biz.workflow import Thing\n\n\n"
            "def go():\n    import hyprial.biz.workflow\n    return hyprial.biz.workflow\n",
        )
        self.write("src/hyprial/biz/workflow.py", "class Thing:\n    pass\n")
        report = self._check()
        self.assertEqual(report["status"], verify_module.FAILED, report)
        self.assertGreaterEqual(len(report["violations"]), 2, report["violations"])

    def test_parse_failure_is_not_silent(self):
        self.write("src/hyprial/daemon/broken.py", "def nope(:\n")
        report = self._check()
        self.assertEqual(report["status"], verify_module.FAILED)
        self.assertTrue(any("parse" in str(v) for v in report["violations"]))

    def test_missing_root_and_unmapped_product_files_reported(self):
        report = verify_module.check_boundary_direction(
            self.root, {**LAYERS, "roots": ["src", "missing-root"]}
        )
        self.assertEqual(report["status"], verify_module.FAILED)
        self.assertTrue(any("missing-root" in str(v) for v in report["violations"]))
        self.write("src/hyprial/unknownpkg/loose.py", "x = 1\n")
        report = self._check()
        self.assertTrue(
            any(v.get("kind") == "unmapped-product-file" for v in report["violations"]),
            report["violations"],
        )

    def test_dynamic_import_candidates_become_pending_not_pass(self):
        # round 3 semantics: a statically resolved dynamic reverse import is
        # an outright violation; nothing silently passes as "pending"
        self.write(
            "src/hyprial/daemon/user.py",
            "import importlib\n\n\ndef go():\n    return importlib.import_module('hyprial.biz.workflow')\n",
        )
        self.write("src/hyprial/biz/workflow.py", "x = 1\n")
        report = self._check()
        self.assertEqual(report["status"], verify_module.FAILED, report)
        self.assertTrue(
            any(v.get("import_kind") == "dynamic" for v in report["violations"]),
            report["violations"],
        )

    def test_forward_direction_still_passes_with_coverage(self):
        self.write("src/hyprial/biz/ok.py", "import hyprial.kernel.base\n\nx = hyprial.kernel.base\n")
        self.write("src/hyprial/kernel/base.py", "X = 1\n")
        report = self._check()
        self.assertEqual(report["status"], verify_module.PASSED, report["violations"])
        self.assertEqual(report["pending_dynamic_refs"], [])
        self.assertGreater(report["product_files_covered"], 0)


class PublicFaceGateTests(RepoFixture):
    FACES = {
        "layer_of_module": LAYERS["layer_of_module"],
        "public_faces": ["hyprial.biz", "hyprial.kernel"],
        "roots": ["src"],
    }

    def _check(self):
        return verify_module.check_public_face_gate(self.root, self.FACES)

    def test_registered_face_does_not_whitelist_its_submodules(self):
        # first-fail: hyprial.biz being a face whitelisted hyprial.biz.workflow
        self.write("src/hyprial/daemon/user.py", "import hyprial.biz.workflow\n")
        self.write("src/hyprial/biz/workflow.py", "x = 1\n")
        report = self._check()
        self.assertEqual(report["status"], verify_module.FAILED, report["violations"])
        self.assertTrue(
            any(v["import"] == "hyprial.biz.workflow" for v in report["violations"])
        )

    def test_from_face_import_and_face_itself_allowed(self):
        self.write("src/hyprial/daemon/user.py", "from hyprial.biz import Thing\n\nx = Thing\n")
        # no __all__: the face has no audited allowlist, so symbol-level
        # imports are BLOCKED, never silently passed
        self.write("src/hyprial/biz/__init__.py", "Thing = 1\n")
        report = self._check()
        self.assertEqual(report["status"], verify_module.BLOCKED, report)
        # with an audited allowlist the same import passes
        self.write("src/hyprial/biz/__init__.py", '__all__ = ["Thing"]\nThing = 1\n')
        report = self._check()
        self.assertEqual(report["status"], verify_module.PASSED, report)

    def test_from_face_submodule_import_is_bypass(self):
        self.write("src/hyprial/daemon/user.py", "from hyprial.biz.workflow import Thing\n")
        self.write("src/hyprial/biz/workflow.py", "Thing = 1\n")
        report = self._check()
        self.assertEqual(report["status"], verify_module.FAILED)

    def test_relative_bypass_detected(self):
        self.write("src/hyprial/daemon/pkg/__init__.py", "")
        self.write("src/hyprial/daemon/pkg/user.py", "from ...biz.workflow import Thing\n")
        self.write("src/hyprial/biz/workflow.py", "Thing = 1\n")
        report = self._check()
        self.assertEqual(report["status"], verify_module.FAILED, report["violations"])

    def test_same_layer_internal_use_is_free(self):
        self.write("src/hyprial/biz/app.py", "from hyprial.biz.workflow import Thing\n\nx = Thing\n")
        self.write("src/hyprial/biz/workflow.py", "Thing = 1\n")
        report = self._check()
        self.assertEqual(report["status"], verify_module.PASSED, report["violations"])

    def test_parse_failure_and_missing_root_are_loud(self):
        self.write("src/hyprial/daemon/broken.py", "def nope(:\n")
        report = self._check()
        self.assertEqual(report["status"], verify_module.FAILED)
        self.assertTrue(any("parse" in str(v) for v in report["violations"]))
        report = verify_module.check_public_face_gate(
            self.root, {**self.FACES, "roots": ["src", "nope"]}
        )
        self.assertEqual(report["status"], verify_module.FAILED)

    def test_dynamic_candidates_fail_as_violations(self):
        self.write(
            "src/hyprial/daemon/user.py",
            "from importlib import import_module\n\n\ndef go():\n    return import_module('hyprial.biz.workflow')\n",
        )
        self.write("src/hyprial/biz/workflow.py", "x = 1\n")
        report = self._check()
        self.assertEqual(report["status"], verify_module.FAILED, report["violations"])


class VerifyRequiredSemanticsTests(RepoFixture):
    """Defect 6: skipped required checks must not satisfy a stage."""

    def _manifest(self, checks):
        return {
            "schema_version": 1,
            "baseline_commit": "a" * 40,
            "rules": [],
            "verification": {"S2": checks},
        }

    def test_skipped_required_command_fails_the_stage(self):
        manifest = self._manifest(
            [{"check": "command", "argv": ["false"], "required": True, "name": "pytest"}]
        )
        report = verify_module.run_stage_verification(
            self.root, manifest, "S2", run_commands=False
        )
        self.assertEqual(report["status"], verify_module.REQUIRED_MISSING)
        entry = report["stages"]["S2"]["results"][0]
        self.assertEqual(entry["status"], verify_module.SKIPPED)
        self.assertTrue(entry["required"])

    def test_optional_command_skipped_is_reported_not_passed(self):
        manifest = self._manifest([{"check": "command", "argv": ["false"], "name": "optional"}])
        report = verify_module.run_stage_verification(
            self.root, manifest, "S2", run_commands=False
        )
        self.assertEqual(report["status"], verify_module.SKIPPED)

    def test_empty_verification_is_not_a_pass(self):
        manifest = self._manifest([])
        report = verify_module.run_stage_verification(self.root, manifest, "S2", False)
        self.assertEqual(report["status"], verify_module.NO_COVERAGE)

    def test_required_static_failure_and_pass_paths(self):
        manifest = self._manifest(
            [
                {"check": "resource_present", "paths": ["no/such.txt"], "required": True},
                {"check": "resource_present", "paths": ["ok.txt"]},
            ]
        )
        report = verify_module.run_stage_verification(self.root, manifest, "S2", False)
        self.assertEqual(report["status"], verify_module.FAILED)
        self.write("no/such.txt", "x")
        self.write("ok.txt", "x")
        report = verify_module.run_stage_verification(self.root, manifest, "S2", False)
        # required and optional static checks both ran and passed
        self.assertEqual(report["status"], verify_module.PASSED)
        # an optional command check that cannot run keeps the stage at
        # reported-skipped, never silently passed
        manifest["verification"]["S2"].append(
            {"check": "command", "argv": ["false"], "name": "optional-cmd"}
        )
        report = verify_module.run_stage_verification(self.root, manifest, "S2", False)
        self.assertEqual(report["status"], verify_module.SKIPPED)


class AuthorityRealSchemaTests(RepoFixture):
    """Defect 2: identity follows each manifest's real field shape."""

    ACTOR = {
        "schema_version": 1,
        "policy": "shrinking-only",
        "exemptions": [
            {"path": "src/hyprial/a.py", "symbol": "X", "reason": "r1"},
            {"path": "src/hyprial/b.py", "symbol": "Y", "reason": "r2"},
        ],
    }
    INBOX = {
        "schema_version": 1,
        "policy": "shrinking-only-until-root-composition",
        "exemptions": [
            {"path": "src/hyprial/wire.py", "owner": "_S.__exit__", "operation": "close"},
            {"path": "src/hyprial/wire.py", "owner": "_S.__exit__", "operation": "close"},
            {"path": "src/hyprial/wire.py", "owner": "_T.send", "operation": "write", "reason": "dup ok"},
            {"path": "src/hyprial/wire.py", "owner": "_T.send", "operation": "write", "reason": "dup ok"},
        ],
    }

    def _write_pair(self, current_exemptions, expected_count):
        self.write("tests/base.json", json.dumps(self.ACTOR, indent=1))
        current = {**self.ACTOR, "exemptions": current_exemptions}
        self.write("tests/cur.json", json.dumps(current, indent=1))
        return {
            "manifests": [
                {
                    "path": "tests/cur.json",
                    "expected_entries": expected_count,
                    "preserved_from": "tests/base.json",
                    "pin_sha256": __import__("hashlib")
                    .sha256(json.dumps(self.ACTOR, indent=1).encode())
                    .hexdigest(),
                }
            ]
        }

    def test_untouched_manifest_passes(self):
        config = self._write_pair(self.ACTOR["exemptions"], 2)
        report = verify_module.check_authority_manifests(self.root, config)
        self.assertEqual(report["status"], verify_module.PASSED, report["problems"])

    def test_legal_path_migration_passes(self):
        moved = [
            {"path": "src/hyprial/identity/a.py", "symbol": "X", "reason": "r1"},
            {"path": "src/hyprial/b.py", "symbol": "Y", "reason": "r2"},
        ]
        config = self._write_pair(moved, 2)
        config["manifests"][0]["allowed_moves"] = [
            {"from": "src/hyprial/a.py", "to": "src/hyprial/identity/a.py"}
        ]
        report = verify_module.check_authority_manifests(self.root, config)
        self.assertEqual(report["status"], verify_module.PASSED, report["problems"])

    def test_deleted_entry_fails(self):
        config = self._write_pair(self.ACTOR["exemptions"][:1], 2)
        report = verify_module.check_authority_manifests(self.root, config)
        self.assertEqual(report["status"], verify_module.FAILED)

    def test_duplicated_entry_cannot_mask_deletion(self):
        dup = [self.ACTOR["exemptions"][1], self.ACTOR["exemptions"][1]]
        config = self._write_pair(dup, 2)
        report = verify_module.check_authority_manifests(self.root, config)
        self.assertEqual(report["status"], verify_module.FAILED)

    def test_tampered_field_fails(self):
        tampered = [
            {"path": "src/hyprial/a.py", "symbol": "X", "reason": "REWRITTEN"},
            {"path": "src/hyprial/b.py", "symbol": "Y", "reason": "r2"},
        ]
        config = self._write_pair(tampered, 2)
        report = verify_module.check_authority_manifests(self.root, config)
        self.assertEqual(report["status"], verify_module.FAILED)

    def test_inbox_owner_operation_identity_without_symbol(self):
        self.write("tests/base.json", json.dumps(self.INBOX, indent=1))
        self.write("tests/cur.json", json.dumps(self.INBOX, indent=1))
        config = {
            "manifests": [
                {
                    "path": "tests/cur.json",
                    "expected_entries": 4,
                    "preserved_from": "tests/base.json",
                    "pin_sha256": __import__("hashlib")
                    .sha256(json.dumps(self.INBOX, indent=1).encode())
                    .hexdigest(),
                }
            ]
        }
        report = verify_module.check_authority_manifests(self.root, config)
        self.assertEqual(report["status"], verify_module.PASSED, report["problems"])
        # same-name owner at a different path is a distinct entry: tampering
        # with one path must fail
        altered = json.loads(json.dumps(self.INBOX))
        altered["exemptions"][0]["path"] = "src/hyprial/elsewhere.py"
        self.write("tests/cur.json", json.dumps(altered, indent=1))
        report = verify_module.check_authority_manifests(self.root, config)
        self.assertEqual(report["status"], verify_module.FAILED)

    def test_unpinned_baseline_copy_is_refused(self):
        self.write("tests/base.json", json.dumps(self.ACTOR, indent=1))
        self.write("tests/cur.json", json.dumps(self.ACTOR, indent=1))
        config = {
            "manifests": [
                {"path": "tests/cur.json", "preserved_from": "tests/base.json"}
            ]
        }
        report = verify_module.check_authority_manifests(self.root, config)
        self.assertEqual(report["status"], verify_module.FAILED)
        self.assertTrue(
            any("pin" in p["error"] for p in report["problems"]), report["problems"]
        )


if __name__ == "__main__":
    unittest.main()
