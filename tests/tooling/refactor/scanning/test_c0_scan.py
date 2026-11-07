from __future__ import annotations

import ast
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.refactor import c0_scan


class ImportResolutionTests(unittest.TestCase):
    def test_type_checking_aliases_and_else(self):
        tree = ast.parse(
            "from typing import TYPE_CHECKING as TC\nimport typing as t\nif TC:\n    import a\nelse:\n    import b\nif t.TYPE_CHECKING:\n    import c\n"
        )
        c0_scan.annotate_parents(tree)
        names, modules = c0_scan.type_checking_names(tree)
        guards = {
            n.names[0].name: c0_scan.in_type_checking(n, names, modules)
            for n in ast.walk(tree)
            if isinstance(n, ast.Import)
        }
        self.assertTrue(guards["a"])
        self.assertFalse(guards["b"])
        self.assertTrue(guards["c"])

    def test_relative_aliased_and_from_package_child(self):
        modules = {
            "hyprial.pkg": "src/hyprial/pkg/__init__.py",
            "hyprial.pkg.child": "src/hyprial/pkg/child.py",
            "hyprial.base": "src/hyprial/base.py",
        }
        rel = ast.parse(
            "from . import child as c\nfrom .. import base as b\nfrom .child import Thing as T"
        )
        pkg = "hyprial.pkg"
        base, found = c0_scan.resolve_from(rel.body[0], "hyprial.pkg.mod", pkg, modules)
        self.assertEqual((base, found), ("hyprial.pkg", "hyprial.pkg.child"))
        base, found = c0_scan.resolve_from(rel.body[1], "hyprial.pkg.mod", pkg, modules)
        self.assertEqual((base, found), ("hyprial", "hyprial.base"))
        base, found = c0_scan.resolve_from(rel.body[2], "hyprial.pkg.mod", pkg, modules)
        self.assertEqual((base, found), ("hyprial.pkg.child", "hyprial.pkg.child"))

    def test_scc_includes_isolated_and_ignores_self_only_as_cycle(self):
        graph = {"a": {"b"}, "b": {"a"}, "isolated": set(), "self": {"self"}}
        self.assertEqual(
            c0_scan.strongly_connected(graph), [["a", "b"], ["isolated"], ["self"]]
        )


class ScannerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.out = Path(self.temp.name) / "report"
        self.mapping = Path(self.temp.name) / "map.json"
        self.mapping.write_text(
            json.dumps(
                {
                    "source_root": "src/hyprial",
                    "modules": {
                        "kernel": {"packages": ["kernel"], "files": []},
                        "identity": {"packages": ["identity"], "files": []},
                        "daemon": {"packages": ["daemon"], "files": []},
                        "biz": {"packages": ["biz"], "files": []},
                        "shell": {"packages": ["shell"], "files": []},
                    },
                    "retained_paths": {},
                    "resource_only_directories": [],
                }
            )
        )
        (self.root / "src/hyprial/kernel").mkdir(parents=True)
        (self.root / "src/hyprial/daemon").mkdir(parents=True)
        (self.root / "src/hyprial/kernel/__init__.py").write_text(
            "from hyprial.kernel.port import A, B\n"
        )
        (self.root / "src/hyprial/kernel/port.py").write_text("A = 1\nB = 2\n")
        (self.root / "src/hyprial/daemon/caller.py").write_text(
            "from hyprial.kernel.port import A, B\nfrom hyprial.missing import X\nfrom hyprial.kernel import port as p\nfrom typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from hyprial.kernel.port import A\ndef local():\n    import hyprial.kernel.port as p\nimport importlib\nimportlib.import_module('hyprial.kernel.port')\nfrom pathlib import Path\nHERE = Path(__file__).parent\nMODE = '-m'\n"
        )
        (self.root / "src/hyprial/daemon/broken.py").write_text("def nope(:\n")
        (self.root / "src/hyprial/odd.py").write_text("VALUE = 1\n")
        (self.root / "README.md").write_text("See src/hyprial/kernel/port.py\n")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Scanner",
                "-c",
                "user.email=scan@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
            cwd=self.root,
            check=True,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_visibility_site_counts_determinism_and_no_source_writes(self):
        before = {
            p.relative_to(self.root): p.read_bytes()
            for p in (self.root / "src").rglob("*")
            if p.is_file()
        }
        first = c0_scan.scan(self.root, self.out, self.mapping)
        outputs1 = {p.name: p.read_bytes() for p in self.out.iterdir()}
        other = self.out.with_name("report-repeat")
        c0_scan.scan(self.root, other, self.mapping)
        outputs2 = {p.name: p.read_bytes() for p in other.iterdir()}
        self.assertEqual(outputs1, outputs2)
        self.assertEqual(
            first["import_sites"], 9
        )  # import sites, with aliased symbols sharing one site
        imp = json.loads((self.out / "imports.json").read_text())
        self.assertEqual(imp["site_count"], 9)
        self.assertEqual(imp["target_reference_count"], 11)
        self.assertEqual(len(imp["unresolved"]), 1)
        self.assertTrue(
            any(
                x["target"] == "hyprial.kernel.port" and x["alias"] == "p"
                for x in imp["target_references"]
            )
        )
        self.assertTrue(any(x["type_checking"] for x in imp["import_sites"]))
        self.assertTrue(
            any(x["scope"] == "function-local" for x in imp["import_sites"])
        )
        cov = json.loads((self.out / "coverage.json").read_text())
        self.assertTrue(
            any(x["path"].endswith("broken.py") for x in cov["parse_errors"])
        )
        self.assertTrue(
            any(x["path"].endswith("odd.py") for x in cov["unmapped_source_modules"])
        )
        self.assertFalse(first["c0_ready"])
        self.assertTrue(cov["input_hashes_unchanged"])
        self.assertFalse(cov["refactor_authorized_by_scan"])
        refs = json.loads((self.out / "references.json").read_text())
        self.assertTrue(
            any(x["path"] == "README.md" for x in refs["tracked_text_path_references"])
        )
        self.assertTrue(
            any(
                x["kind"] == "import_module" and x["value"] == "hyprial.kernel.port"
                for x in refs["python_references"]
            )
        )
        self.assertTrue(
            any(
                x["kind"] == "__file__-path-arithmetic"
                for x in refs["python_references"]
            )
        )
        self.assertTrue(
            any(x["kind"] == "argv-module-flag" for x in refs["python_references"])
        )
        after = {
            p.relative_to(self.root): p.read_bytes()
            for p in (self.root / "src").rglob("*")
            if p.is_file()
        }
        self.assertEqual(before, after)

    def test_refuses_existing_reports_and_tracked_symlinks(self):
        self.out.mkdir()
        (self.out / "inventory.json").write_text("user-owned")
        with self.assertRaises(ValueError):
            c0_scan.scan(self.root, self.out, self.mapping)
        self.assertEqual((self.out / "inventory.json").read_text(), "user-owned")
        link = self.root / "src/hyprial/daemon/link.py"
        link.symlink_to(self.root / "src/hyprial/kernel/port.py")
        subprocess.run(["git", "add", str(link)], cwd=self.root, check=True)
        result = c0_scan.scan(self.root, self.out.with_name("symlinks"), self.mapping)
        cov = json.loads((Path(result["output"]) / "coverage.json").read_text())
        self.assertFalse(result["c0_ready"])
        self.assertTrue(
            any(r["path"].endswith("link.py") for r in cov["missing_tracked_files"])
        )

    def test_distinct_forbidden_sites_multiple_child_targets_and_direction(self):
        (self.root / "src/hyprial/kernel/port.py").write_text(
            "from ..daemon import caller as c, broken as b\n"
        )
        c0_scan.scan(self.root, self.out, self.mapping)
        report = json.loads((self.out / "boundaries.json").read_text())
        self.assertEqual(report["forbidden_import_sites"], 1)
        self.assertEqual(report["forbidden_target_edges_site_sum"], 2)
        self.assertTrue(
            any(
                not row["forbidden"] and row["from_module"] == "daemon"
                for row in report["mapped_module_edges"]
            )
        )

    def test_detects_input_drift(self):
        original = c0_scan.scan_references

        def mutate(*args):
            result = original(*args)
            (self.root / "README.md").write_text("modified while scanning\n")
            return result

        with patch.object(c0_scan, "scan_references", side_effect=mutate):
            result = c0_scan.scan(self.root, self.out, self.mapping)
        report = json.loads((self.out / "coverage.json").read_text())
        self.assertFalse(result["c0_ready"])
        self.assertFalse(report["input_hashes_unchanged"])
        self.assertEqual(report["drifted_input_paths"], ["README.md"])

    def test_extraction_state_and_argv_observations(self):
        (self.root / "src/hyprial/daemon/caller.py").write_text(
            "class DaemonApplication:\n    def maintain(self):\n        self.count += 1\n        self.flush()\nARGV = ['python', '-m', 'hyprial.daemon.impl.caller']\n"
        )
        c0_scan.scan(self.root, self.out, self.mapping)
        ex = json.loads((self.out / "extraction.json").read_text())
        method = ex["daemon_application_methods"][0]
        self.assertEqual(method["self_writes"], {"count": 1})
        self.assertEqual(method["self_reads"]["count"], 1)
        self.assertEqual(method["self_calls"], {"flush": 1})
        refs = json.loads((self.out / "references.json").read_text())
        self.assertTrue(
            any(
                row["kind"] == "argv-module-entry"
                and row["value"] == "hyprial.daemon.impl.caller"
                for row in refs["python_references"]
            )
        )

    def test_ambiguous_mapping_fails_before_writes(self):
        value = json.loads(self.mapping.read_text())
        value["modules"]["shell"]["packages"].append("kernel")
        self.mapping.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            c0_scan.scan(self.root, self.out, self.mapping)
        self.assertFalse(self.out.exists())

    def test_tracked_non_product_relative_imports_are_resolved(self):
        fixtures = self.root / "tests/fixtures"
        fixtures.mkdir(parents=True)
        (fixtures / "helper.py").write_text("A = 1\nB = 2\n")
        (fixtures / "caller.py").write_text("from .helper import A, B\n")
        subprocess.run(["git", "add", "tests"], cwd=self.root, check=True)
        c0_scan.scan(self.root, self.out, self.mapping)
        report = json.loads((self.out / "imports.json").read_text())
        rows = [
            row
            for row in report["target_references"]
            if row["path"] == "tests/fixtures/caller.py"
        ]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["resolved_tracked_outside_product"] for row in rows))
        self.assertFalse(
            any(
                row["target"] == "tests.fixtures.helper" for row in report["unresolved"]
            )
        )

    def test_python_encoding_cookie_is_respected(self):
        source = self.root / "src/hyprial/kernel/port.py"
        source.write_bytes(b"# coding: latin-1\nTEXT = 'caf\xe9'\n")
        c0_scan.scan(self.root, self.out, self.mapping)
        report = json.loads((self.out / "coverage.json").read_text())
        self.assertFalse(
            any(row["path"].endswith("port.py") for row in report["parse_errors"])
        )

    def test_rejects_output_under_source_scope(self):
        with self.assertRaises(ValueError):
            c0_scan.reject_output(self.root, self.root / "src/hyprial/report")

    def test_rejects_repository_root_and_git_metadata(self):
        with self.assertRaises(ValueError):
            c0_scan.reject_output(self.root, self.root)
        with self.assertRaises(ValueError):
            c0_scan.reject_output(self.root, self.root / ".git/reports")


if __name__ == "__main__":
    unittest.main()
