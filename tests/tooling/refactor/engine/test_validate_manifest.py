"""Manifest validation tests and schema/validator agreement checks."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.tooling.refactor.support._helpers import RepoFixture

from scripts.refactor import validate_manifest
from scripts.refactor.transforms.common import ToolError


def base_rule(**kwargs) -> dict:
    rule = {
        "rule_id": "r1",
        "kind": "import_symbol",
        "stage": "S1",
        "status": "approved",
        "expected_matches": 1,
        "reason": "test rule",
        "old_module": "hyprial.daemon.impl.desired_state",
        "new_module": "hyprial.kernel.contracts",
        "old_symbol": "HarnessLaunchSpec",
        "new_symbol": "HarnessLaunchSpec",
    }
    rule.update(kwargs)
    return rule


def manifest(*rules, **extra) -> dict:
    data = {"schema_version": 1, "baseline_commit": "a" * 40, "rules": list(rules)}
    data.update(extra)
    return data


class ValidationTests(unittest.TestCase):
    def validate(self, data: dict, require_approved: bool = False) -> list[str]:
        return validate_manifest.validate_manifest(data, require_approved=require_approved)

    def test_valid_manifest_passes(self):
        self.assertEqual(self.validate(manifest(base_rule())), [])

    def test_missing_common_field(self):
        rule = base_rule()
        del rule["reason"]
        errors = self.validate(manifest(rule))
        self.assertTrue(any("reason" in e for e in errors))

    def test_bad_baseline_commit(self):
        errors = self.validate(manifest(base_rule(), baseline_commit="xyz"))
        self.assertTrue(any("baseline_commit" in e for e in errors))

    def test_unknown_kind(self):
        errors = self.validate(manifest(base_rule(kind="magic")))
        self.assertTrue(any("unknown kind" in e for e in errors))

    def test_bad_stage(self):
        errors = self.validate(manifest(base_rule(stage="S9")))
        self.assertTrue(any("stage" in e for e in errors))

    def test_draft_accepted_for_plan_refused_for_apply(self):
        rule = base_rule(status="draft")
        self.assertEqual(self.validate(manifest(rule)), [])
        errors = self.validate(manifest(rule), require_approved=True)
        self.assertTrue(any("not approved" in e for e in errors))

    def test_import_rule_expected_zero_rejected(self):
        errors = self.validate(manifest(base_rule(expected_matches=0)))
        self.assertTrue(any("at least one site" in e for e in errors))

    def test_symbol_extract_expected_must_equal_symbols(self):
        rule = base_rule(
            kind="symbol_extract",
            symbols=["A", "B"],
            source_path="x.py",
            source_sha256="b" * 64,
            target_path="y.py",
            dependencies=[{"name": "a", "provision": "import", "module": "hyprial.kernel"}],
        )
        errors = self.validate(manifest(rule))
        self.assertTrue(any("expected_matches must equal len(symbols)" in e for e in errors))
        rule["expected_matches"] = 2
        self.assertEqual(self.validate(manifest(rule)), [])

    def test_class_member_requires_wiring(self):
        rule = base_rule(
            kind="class_member_extract",
            class_name="DaemonApplication",
            symbols=["_m"],
            new_class_name="C",
            source_path="x.py",
            source_sha256="b" * 64,
            target_path="y.py",
            dependencies=[],
            expected_matches=1,
        )
        errors = self.validate(manifest(rule))
        self.assertTrue(any("wiring" in e for e in errors))
        rule["wiring"] = {"method": "__init__", "code": "pass"}
        self.assertEqual(self.validate(manifest(rule)), [])

    def test_duplicate_rule_id(self):
        errors = self.validate(manifest(base_rule(), base_rule(rule_id="r1")))
        self.assertTrue(any("duplicate rule_id" in e for e in errors))

    def test_conflicting_facade_exports(self):
        rules = [
            base_rule(
                rule_id="f1",
                kind="facade_export",
                facade_path="src/hyprial/kernel/__init__.py",
                facade_module="hyprial.kernel",
                provider_module="hyprial.kernel.impl.a",
                symbol="X",
                old_symbol="X",
            ),
            base_rule(
                rule_id="f2",
                kind="facade_export",
                facade_path="src/hyprial/kernel/__init__.py",
                facade_module="hyprial.kernel",
                provider_module="hyprial.kernel.impl.b",
                symbol="X",
                old_symbol="X",
            ),
        ]
        errors = self.validate(manifest(*rules))
        self.assertTrue(any("conflicting providers" in e for e in errors))

    def test_reference_rule_count_sum(self):
        rule = base_rule(
            rule_id="ref",
            kind="reference_rule",
            source_path="config.json",
            format="json",
            expected_matches=3,
            operations=[
                {"op": "json_field", "field": ["a"], "old": "x", "new": "y", "count": 1},
                {"op": "json_field", "field": ["b"], "old": "p", "new": "q", "count": 1},
            ],
        )
        errors = self.validate(manifest(rule))
        self.assertTrue(any("sum of operation counts" in e for e in errors))
        rule["expected_matches"] = 2
        self.assertEqual(self.validate(manifest(rule)), [])

    def test_reference_op_unsupported_for_format(self):
        rule = base_rule(
            rule_id="ref",
            kind="reference_rule",
            source_path="notes.txt",
            format="text",
            expected_matches=1,
            operations=[{"op": "json_field", "field": ["a"], "old": "x", "new": "y", "count": 1}],
        )
        errors = self.validate(manifest(rule))
        self.assertTrue(any("unsupported for text" in e for e in errors))

    def test_layout_expected_equals_moves(self):
        rule = base_rule(
            rule_id="mv",
            kind="layout_move",
            expected_matches=2,
            moves=[{"from": "a.py", "to": "b.py"}],
        )
        errors = self.validate(manifest(rule))
        self.assertTrue(any("layout_move expected_matches" in e for e in errors))
        rule["expected_matches"] = 1
        self.assertEqual(self.validate(manifest(rule)), [])

    def test_unknown_verification_stage(self):
        errors = self.validate(
            manifest(base_rule(), verification={"S0": [{"check": "command", "argv": ["x"]}]})
        )
        self.assertTrue(any("unknown stage key" in e for e in errors))

    def test_unknown_check_name(self):
        errors = self.validate(
            manifest(base_rule(), verification={"S1": [{"check": "make_coffee"}]})
        )
        self.assertTrue(any("unknown check" in e for e in errors))

    def test_load_and_validate_rejects_on_error(self):
        fixture = RepoFixture()
        fixture.setUp()
        try:
            path = fixture.outside / "m.json"
            path.write_text(json.dumps(manifest(base_rule(expected_matches=0))))
            with self.assertRaises(ToolError):
                validate_manifest.load_and_validate(path, require_approved=True)
        finally:
            fixture.tearDown()

    def test_schema_json_matches_validator_on_examples(self):
        """The shipped JSON Schema must accept everything the validator accepts
        for a representative corpus (structure parity smoke)."""
        schema_path = (
            Path(validate_manifest.__file__).parent / "manifest.schema.json"
        )
        self.assertTrue(schema_path.is_file())
        schema = json.loads(schema_path.read_text())
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        kinds = schema["$defs"]["rule"]["properties"]["kind"]["enum"]
        self.assertEqual(
            set(kinds),
            set(validate_manifest.KIND_FIELDS),
            "schema kinds and validator kinds must stay in sync",
        )


if __name__ == "__main__":
    unittest.main()
