"""Synthetic red controls and the real-tree pass for the GLOSSARY.md anchor gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts/ci/check_glossary_anchors.py"
SPEC = importlib.util.spec_from_file_location("glossary_anchors_gate", SCRIPT)
GATE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GATE
SPEC.loader.exec_module(GATE)

SECTION = "## 组织与会话模型（2026-11-03）"
HEADER = "| 术语 | 定义 | 退役/易混淆 | 锚点 |\n|---|---|---|---|\n"


class GlossaryAnchorTests(unittest.TestCase):
    """Every anchor shape the model section uses is resolved; every drift goes red."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="hyprial-glossary-anchors-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.write("src/pkg/cells/__init__.py", "")
        self.write("src/pkg/cells/model.py", textwrap.dedent("""
            from .ports import Port as Port
            LIMIT = 3
            NAME: str = "cell"
            class Cell:
                kind = "agent"
                size: int = 1
                def derive(self):
                    return None
                @property
                def cell_id(self):
                    return "c.x"
                class Inner:
                    deep = 1
            def subject_cell_id():
                return None
            async def later():
                return None
        """))
        self.write("src/pkg/cells/ports.py", "class Port:\n    pass\n")
        self.write("docs/design/model.md", "# model\n")

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def glossary(self, rows, *, section=SECTION, before=""):
        body = "".join(f"| {term} | 定义 | 易混淆 | {anchor} |\n" for term, anchor in rows)
        self.write("GLOSSARY.md", f"# GLOSSARY\n\n{before}{section}\n\n{HEADER}{body}\n## 其他\n\n{HEADER}| other | x | y | missing/elsewhere.py |\n")

    def scan(self):
        return GATE.scan(self.root)

    def assert_violation(self, kind, term=None, report=None):
        report = report or self.scan()
        self.assertEqual(report["status"], "FAIL", report)
        rows = [row for row in report["violations"] if row["kind"] == kind and (term is None or row["term"] == term)]
        self.assertTrue(rows, report["violations"])
        return rows

    def assert_pass(self):
        report = self.scan()
        self.assertEqual(report["violations"], [])
        self.assertEqual(report["status"], "PASS", report)
        return report

    def test_every_supported_anchor_shape_passes(self):
        self.glossary([
            ("cell", "src/pkg/cells/"),
            ("cell id", "src/pkg/cells/model.py、src/pkg/cells/ports.py"),
            ("thread", "src/pkg/cells/ （待迁移）"),
            ("conversation", "docs/design/model.md"),
            ("department", "同上"),
            ("runtime", "src/pkg/cells/ports.py::Port、`src/pkg/cells/model.py::Cell.cell_id`"),
            ("symbols", "src/pkg/cells/model.py::LIMIT、src/pkg/cells/model.py::NAME、src/pkg/cells/model.py::Cell.kind"),
            ("members", "src/pkg/cells/model.py::Cell.size、src/pkg/cells/model.py::Cell.derive、src/pkg/cells/model.py::Cell.Inner.deep"),
            ("functions", "src/pkg/cells/model.py::subject_cell_id、src/pkg/cells/model.py::later、src/pkg/cells/model.py::Port"),
        ])
        report = self.assert_pass()
        anchors = [row["anchor"] for row in report["checked"]]
        self.assertIn("src/pkg/cells/model.py::Cell.cell_id", anchors)
        self.assertEqual([row["anchor"] for row in report["checked"] if row["term"] == "department"], ["docs/design/model.md"])

    def test_rows_outside_the_checked_sections_are_not_gated(self):
        self.glossary([("cell", "src/pkg/cells/")])
        report = self.assert_pass()
        self.assertNotIn("other", {row["term"] for row in report["checked"]})

    def test_synthetic_bad_path_anchor_fails_and_names_the_term(self):
        self.glossary([("cell", "src/pkg/cells/"), ("thread", "src/pkg/links/ （待迁移）")])
        rows = self.assert_violation("missing-anchor-path", "thread")
        self.assertEqual(rows[0]["anchor"], "src/pkg/links/")
        self.assertEqual(rows[0]["line"], 8)

    def test_each_anchor_of_a_multi_anchor_cell_is_checked(self):
        self.glossary([("cell id", "src/pkg/cells/model.py、src/pkg/cells/gone.py")])
        rows = self.assert_violation("missing-anchor-path", "cell id")
        self.assertEqual([row["anchor"] for row in rows], ["src/pkg/cells/gone.py"])

    def test_directory_anchor_must_be_a_directory(self):
        self.glossary([("cell", "src/pkg/cells/model.py/")])
        self.assert_violation("missing-anchor-path", "cell")

    def test_synthetic_bad_symbol_anchor_fails(self):
        for anchor in (
            "src/pkg/cells/model.py::CellRuntimePort",
            "src/pkg/cells/model.py::Cell.retired",
            "src/pkg/cells/model.py::LIMIT.real",
            "src/pkg/cells/model.py::Cell.Inner.shallow",
        ):
            with self.subTest(anchor=anchor):
                self.glossary([("runtime", anchor)])
                rows = self.assert_violation("missing-anchor-symbol", "runtime")
                self.assertEqual(rows[0]["anchor"], anchor)

    def test_symbol_on_a_missing_file_is_a_missing_path(self):
        self.glossary([("runtime", "src/pkg/cells/gone.py::Port")])
        self.assert_violation("missing-anchor-path", "runtime")

    def test_symbol_on_a_non_python_anchor_fails(self):
        self.glossary([("runtime", "docs/design/model.md::Port")])
        self.assert_violation("symbol-on-non-python", "runtime")

    def test_unparseable_python_symbol_anchor_fails(self):
        self.write("src/pkg/cells/broken.py", "def (:\n")
        self.glossary([("runtime", "src/pkg/cells/broken.py::Port")])
        self.assert_violation("unparseable-python", "runtime")

    def test_prose_or_empty_anchor_in_a_checked_section_fails(self):
        for anchor in ("design.md §2 命名词表", "", "—", "src/pkg/cells/ src/pkg/cells/model.py"):
            with self.subTest(anchor=anchor):
                self.glossary([("cell", anchor)])
                self.assert_violation("unparseable-anchor", "cell")

    def test_paths_may_not_escape_the_repository(self):
        for anchor in ("../outside.md", "/etc/passwd", "src/../../outside.md"):
            with self.subTest(anchor=anchor):
                self.glossary([("cell", anchor)])
                self.assert_violation("unparseable-anchor", "cell")

    def test_same_as_above_on_the_first_row_fails(self):
        self.glossary([("cell", "同上")])
        self.assert_violation("dangling-same-as-above", "cell")

    def test_same_as_above_inherits_a_broken_anchor_and_reports_both_rows(self):
        self.glossary([("conversation", "docs/design/gone.md"), ("department", "同上")])
        rows = self.assert_violation("missing-anchor-path")
        self.assertEqual([row["term"] for row in rows], ["conversation", "department"])

    def test_missing_section_fails(self):
        self.glossary([("cell", "src/pkg/cells/")], section="## 别的模型")
        self.assert_violation("missing-section")

    def test_section_without_an_anchor_column_fails(self):
        self.write("GLOSSARY.md", f"{SECTION}\n\n| 术语 | 定义 |\n|---|---|\n| cell | x |\n")
        self.assert_violation("missing-anchor-column")

    def test_section_without_rows_fails(self):
        self.write("GLOSSARY.md", f"{SECTION}\n\n{HEADER}\n")
        self.assert_violation("no-anchors-checked")

    def test_short_row_fails(self):
        self.write("GLOSSARY.md", f"{SECTION}\n\n{HEADER}| cell | 定义 |\n")
        self.assert_violation("malformed-row", "cell")

    def test_missing_glossary_fails(self):
        self.assert_violation("missing-glossary")

    def test_cli_exits_red_and_names_the_term_and_anchor_on_stderr(self):
        self.glossary([("thread", "src/pkg/links/")])
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("missing-anchor-path", result.stderr)
        self.assertIn("thread", result.stderr)
        self.assertIn("src/pkg/links/", result.stderr)

    def test_cli_exits_green_on_a_clean_glossary(self):
        self.glossary([("cell", "src/pkg/cells/")])
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class RealGlossaryAnchorTests(unittest.TestCase):
    """The checked-in GLOSSARY.md resolves, and the same file with one drifted anchor does not."""

    def test_real_glossary_passes(self):
        report = GATE.scan(REPO)
        self.assertEqual(report["violations"], [])
        self.assertEqual(report["status"], "PASS")

    def test_real_glossary_checks_every_model_term(self):
        terms = {row["term"] for row in GATE.scan(REPO)["checked"]}
        for term in ("cell", "thread", "conversation", "department", "org cell", "交互记录", "系统运行信息", "route", "session"):
            with self.subTest(term=term):
                self.assertIn(term, terms)

    def test_real_glossary_with_a_synthetic_bad_anchor_fails(self):
        text = (REPO / "GLOSSARY.md").read_text(encoding="utf-8")
        anchor = "src/hyprial/daemon/impl/cells/runtime/"
        self.assertIn(f"| {anchor}", text)
        with tempfile.TemporaryDirectory(prefix="hyprial-glossary-drift-") as directory:
            glossary = Path(directory) / "GLOSSARY.md"
            glossary.write_text(text.replace(f"| {anchor}", "| src/hyprial/daemon/impl/cells/runtime_gone/", 1), encoding="utf-8")
            report = GATE.scan(REPO, glossary=glossary)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual([(row["kind"], row["anchor"]) for row in report["violations"]],
                         [("missing-anchor-path", "src/hyprial/daemon/impl/cells/runtime_gone/")])


if __name__ == "__main__":
    unittest.main()
