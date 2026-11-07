"""Tests for the structural-limits baseline (only-out ratchet exemptions).

The structural gate is merge-blocking (pr-hygiene rule 2 runs it on the merge
tree).  Exactly two violations predate the gate on ``dev``, so every PR was red
and a NEW violation was indistinguishable from the known ones.  The baseline
names those two known violations; only the DELTA is red.  The 1004-line
``contract/e2e/_daemon_runner.py`` added by #1510 is exactly such a NEW
violation: it is never in the baseline, so it stays red.  The polarity is fixed:
entries may only LEAVE, so a repaired violation leaves a stale entry that is
itself red.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from datetime import date
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[4] / "scripts/ci/check_structural_limits.py"
SPEC = importlib.util.spec_from_file_location("structural_limits_baseline", SCRIPT)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)

TODAY = date(2026, 10, 16)
FUTURE = "2099-01-01"
PAST = "2026-01-01"
STUDIO = "src/gui/studio"
ROOT = SCRIPT.resolve().parents[2]
REGISTRY_OPS = "src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py"
OUTBOX = "tests/daemon/outbox"
DAEMON_RUNNER = "contract/e2e/_daemon_runner.py"
# The two debts that predate the gate on dev, both booked to PR #1502.
KNOWN_DEBTS = (("code-line-count", REGISTRY_OPS), ("module-file-count", OUTBOX))
# Both live debts are repaired; KNOWN_DEBTS still models the synthetic
# two-debt ratchet exercised by the tests below.
LIVE_DEBTS = ()


def baseline_entry(kind, path, actual, limit, *, expires=FUTURE, recorded="2026-10-16"):
    entry = {
        "kind": kind,
        "path": path,
        "actual": actual,
        "limit": limit,
        "fix_pr": "#1502",
        "reason": "known violation under repair",
    }
    if recorded is not None:
        entry["recorded"] = recorded
    if expires is not None:
        entry["expires"] = expires
    return entry


class StructuralBaselineTests(unittest.TestCase):
    def tree(self):
        directory = tempfile.TemporaryDirectory(prefix="hyprial-structural-baseline-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for path in GATE.PRODUCT_ROOTS:
            (root / path).mkdir(parents=True, exist_ok=True)
        (root / "tests").mkdir()
        (root / "src/hyprial/__init__.py").write_text("\n")
        return root

    def write_baseline(self, root, entries, *, raw=None):
        path = root / GATE.BASELINE_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            path.write_text(raw)
        else:
            path.write_text(json.dumps({"version": 1, "entries": entries}, indent=2) + "\n")
        return path

    def studio_with_files(self, root, count):
        module = root / STUDIO
        module.mkdir(parents=True, exist_ok=True)
        for index in range(count):
            (module / f"part{index}.mjs").write_text("export {};\n")
        return module

    def real_violation_tree(self):
        """The two #1502 baseline debts plus the #1510 runner violation."""

        root = self.tree()
        registry = root / REGISTRY_OPS
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text("# line\n" * 1005)
        outbox = root / OUTBOX
        outbox.mkdir(parents=True, exist_ok=True)
        for index in range(8):
            (outbox / f"test_part{index}.py").write_text("\n")
        runner = root / DAEMON_RUNNER
        runner.parent.mkdir(parents=True, exist_ok=True)
        runner.write_text("# line\n" * 1004)
        return root

    def two_debt_baseline(self, root):
        """The corrected baseline: only the two #1502 debts, never the #1510 one."""

        self.write_baseline(
            root,
            [
                baseline_entry("code-line-count", REGISTRY_OPS, 1005, 1000),
                baseline_entry("module-file-count", OUTBOX, 8, 7),
            ],
        )

    def test_violation_at_or_below_the_recorded_actual_is_grandfathered(self):
        root = self.tree()
        self.studio_with_files(root, 8)
        self.write_baseline(root, [baseline_entry("module-file-count", STUDIO, 8, 7)])
        report = GATE.scan(root, today=TODAY)
        self.assertEqual(report["status"], "PASS", report)
        self.assertEqual(report["violations"], [])
        self.assertEqual(len(report["grandfathered"]), 1, report)
        self.assertEqual(report["grandfathered"][0]["recorded"], 8)
        self.assertEqual(report["grandfathered"][0]["actual"], 8)
        self.assertEqual(report["stale_baseline"], [])

    def test_actual_worse_than_recorded_is_a_failing_violation(self):
        root = self.tree()
        self.studio_with_files(root, 9)
        self.write_baseline(root, [baseline_entry("module-file-count", STUDIO, 8, 7)])
        report = GATE.scan(root, today=TODAY)
        self.assertEqual(report["status"], "FAIL", report)
        self.assertEqual(report["grandfathered"], [])
        worse = [row for row in report["violations"] if row["path"] == STUDIO]
        self.assertEqual(len(worse), 1, report)
        self.assertEqual(worse[0]["actual"], 9)
        self.assertEqual(worse[0]["baselineActual"], 8)

    def test_new_violation_is_not_covered_by_the_baseline(self):
        root = self.tree()
        oversized = root / "src/gui/oversized"
        oversized.mkdir(parents=True)
        (oversized / "only.mjs").write_text("export {};\n" * 1001)
        self.write_baseline(root, [])
        report = GATE.scan(root, today=TODAY)
        self.assertEqual(report["status"], "FAIL", report)
        self.assertTrue(any(row["kind"] == "code-line-count" for row in report["violations"]), report)

    def test_expired_entry_stops_grandfathering_and_is_red(self):
        root = self.tree()
        self.studio_with_files(root, 8)
        self.write_baseline(root, [baseline_entry("module-file-count", STUDIO, 8, 7, expires=PAST)])
        report = GATE.scan(root, today=TODAY)
        self.assertEqual(report["status"], "FAIL", report)
        self.assertEqual(report["grandfathered"], [])
        self.assertTrue(any(row["kind"] == "module-file-count" for row in report["violations"]), report)
        issues = [issue for row in report["stale_baseline"] for issue in row["issues"]]
        self.assertIn("expired", issues)

    def test_repaired_entry_must_be_deleted(self):
        root = self.tree()
        self.studio_with_files(root, 7)
        self.write_baseline(root, [baseline_entry("module-file-count", STUDIO, 8, 7)])
        report = GATE.scan(root, today=TODAY)
        self.assertEqual(report["violations"], [])
        self.assertEqual(report["grandfathered"], [])
        self.assertEqual(report["status"], "FAIL", report)
        issues = [issue for row in report["stale_baseline"] for issue in row["issues"]]
        self.assertIn("repaired", issues)

    def test_malformed_baseline_fails_loudly_and_fails_closed(self):
        root = self.tree()
        self.studio_with_files(root, 8)
        self.write_baseline(root, [], raw="{ this is not json\n")
        report = GATE.scan(root, today=TODAY)
        self.assertEqual(report["status"], "FAIL", report)
        self.assertTrue(report["baselineErrors"], report)
        self.assertEqual(report["grandfathered"], [])
        self.assertTrue(any(row["kind"] == "module-file-count" for row in report["violations"]), report)

    def test_baseline_entry_missing_required_fields_fails(self):
        root = self.tree()
        self.write_baseline(root, [{"kind": "code-line-count", "path": "src/x.py"}])
        report = GATE.scan(root, today=TODAY)
        self.assertEqual(report["status"], "FAIL", report)
        self.assertTrue(report["baselineErrors"], report)

    def test_expires_defaults_to_fourteen_days_after_recorded(self):
        root = self.tree()
        self.write_baseline(
            root,
            [baseline_entry("module-file-count", STUDIO, 8, 7, expires=None, recorded="2026-01-01")],
        )
        entries = GATE.load_baseline(root / GATE.BASELINE_RELATIVE_PATH)
        self.assertEqual(entries[0]["expires"], date(2026, 1, 15))

    def test_exit_code_is_decided_by_failing_items_only(self):
        root = self.tree()
        self.studio_with_files(root, 8)
        self.write_baseline(root, [baseline_entry("module-file-count", STUDIO, 8, 7)])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(GATE.main(["--root", str(root)]), 0)
        (root / GATE.BASELINE_RELATIVE_PATH).unlink()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(GATE.main(["--root", str(root)]), 1)

    def test_live_baseline_has_no_remaining_1502_debt(self):
        """The live baseline is empty after both #1502 repairs."""

        entries = GATE.load_baseline(ROOT / GATE.BASELINE_RELATIVE_PATH)
        keys = {(entry["kind"], entry["path"]) for entry in entries}
        self.assertEqual(keys, set(LIVE_DEBTS), entries)
        self.assertEqual(entries, [])

    def test_post_1510_runner_violation_is_new_not_grandfathered(self):
        root = self.real_violation_tree()
        self.two_debt_baseline(root)
        baseline = root / GATE.BASELINE_RELATIVE_PATH
        before = baseline.read_bytes()
        report = GATE.scan(root, today=TODAY)
        self.assertEqual(report["status"], "FAIL", report)
        failing = {(row["kind"], row["path"]) for row in report["violations"]}
        self.assertIn(("code-line-count", DAEMON_RUNNER), failing, report)
        grandfathered = {(row["kind"], row["path"]) for row in report["grandfathered"]}
        self.assertEqual(grandfathered, set(KNOWN_DEBTS), report)
        self.assertNotIn(("code-line-count", DAEMON_RUNNER), grandfathered, report)
        self.assertEqual(report["summary"]["failing"], 1, report)
        self.assertEqual(report["summary"]["grandfathered"], 2, report)
        self.assertEqual(baseline.read_bytes(), before, "scan must never absorb a new violation into the baseline")

    def test_post_1510_runner_violation_exits_non_zero(self):
        root = self.real_violation_tree()
        self.two_debt_baseline(root)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(GATE.main(["--root", str(root)]), 1)

    def test_json_output_names_failing_and_grandfathered_separately(self):
        root = self.real_violation_tree()
        self.two_debt_baseline(root)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            exit_code = GATE.main(["--root", str(root)])
        self.assertEqual(exit_code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "FAIL", payload)
        # Two named populations, never one collapsed total: the NEW #1510
        # violation is a failing item, the #1502 debts stay grandfathered.
        self.assertEqual(
            {(row["kind"], row["path"]) for row in payload["violations"]},
            {("code-line-count", DAEMON_RUNNER)},
            payload["violations"],
        )
        self.assertEqual(
            {(row["kind"], row["path"]) for row in payload["grandfathered"]},
            set(KNOWN_DEBTS),
            payload["grandfathered"],
        )
        self.assertEqual(payload["summary"]["failing"], 1, payload["summary"])
        self.assertEqual(payload["summary"]["grandfathered"], 2, payload["summary"])
        self.assertNotIn("total", payload["summary"])

    def test_live_tree_new_violation_is_red_and_never_absorbed(self):
        entries = GATE.load_baseline(ROOT / GATE.BASELINE_RELATIVE_PATH)
        keys = {(entry["kind"], entry["path"]) for entry in entries}
        self.assertNotIn(("code-line-count", DAEMON_RUNNER), keys, entries)
        if len((ROOT / DAEMON_RUNNER).read_bytes().splitlines()) > GATE.MAX_CODE_LINES:
            report = GATE.scan(ROOT)
            failing = {(row["kind"], row["path"]) for row in report["violations"]}
            grandfathered = {(row["kind"], row["path"]) for row in report["grandfathered"]}
            self.assertIn(("code-line-count", DAEMON_RUNNER), failing, report)
            self.assertNotIn(("code-line-count", DAEMON_RUNNER), grandfathered, report)
            self.assertEqual(report["summary"]["failing"], 1, report)
            self.assertEqual(
                report["summary"]["grandfathered"], len(LIVE_DEBTS), report
            )

    def test_both_1502_debts_repaired_leave_two_stale_entries(self):
        root = self.tree()
        registry = root / REGISTRY_OPS
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text("# line\n" * 1000)
        outbox = root / OUTBOX
        outbox.mkdir(parents=True, exist_ok=True)
        for index in range(7):
            (outbox / f"test_part{index}.py").write_text("\n")
        self.two_debt_baseline(root)
        report = GATE.scan(root, today=TODAY)
        self.assertEqual(report["violations"], [], report)
        self.assertEqual(report["grandfathered"], [], report)
        self.assertEqual(report["status"], "FAIL", report)
        self.assertEqual(
            {(row["kind"], row["path"]) for row in report["stale_baseline"]},
            set(KNOWN_DEBTS),
            report,
        )
        for row in report["stale_baseline"]:
            self.assertIn("repaired", row["issues"], row)
            self.assertIn("delete", row["message"], row)


if __name__ == "__main__":
    unittest.main()
