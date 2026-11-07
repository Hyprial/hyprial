"""Synthetic positive/negative controls for the A-03 exact import ratchet."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[4] / "scripts/ci/check_import_ratchet.py"
SPEC = importlib.util.spec_from_file_location("import_ratchet", SCRIPT)
GATE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GATE
SPEC.loader.exec_module(GATE)

ROOT = Path(__file__).resolve().parents[4]
BASELINE = ROOT / "scripts/ci/import-ratchet-baseline.json"


class ImportRatchetTests(unittest.TestCase):
    def setUp(self):
        self._original_exception_digest = GATE.REVIEWED_EXCEPTION_DIGEST
        self.addCleanup(setattr, GATE, "REVIEWED_EXCEPTION_DIGEST", self._original_exception_digest)
        directory = tempfile.TemporaryDirectory(prefix="hyprial-import-ratchet-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.cli = self.write(
            "src/hyprial/shell/impl/cli/commands/demo.py",
            """
            from __future__ import annotations

            from hyprial.daemon import BusinessStore
            """,
        )
        self.write("src/hyprial/daemon/impl/thing.py", "class Thing:\n    pass\n")
        self.baseline = self.root / GATE.BASELINE_RELATIVE_PATH

    def write(self, path, source):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(source).lstrip())
        return target

    def kinds(self, report):
        return [row["kind"] for row in report["violations"]]

    def test_repository_baseline_matches(self):
        report = GATE.check(ROOT, BASELINE)
        self.assertEqual(report["status"], "PASS", report)
        self.assertGreater(report["cliEdges"], 0)

    def test_new_edge_grows_and_shrink_requires_a_ledger_update(self):
        first = GATE.write_baseline(self.root, self.baseline)
        self.assertEqual(first["status"], "PASS", first)
        self.assertEqual(GATE.check(self.root, self.baseline)["status"], "PASS")

        self.cli.write_text(
            self.cli.read_text()
            + "\nfrom hyprial.identity import UserStore\n"
        )
        grown = GATE.check(self.root, self.baseline)
        self.assertIn("new-cli-backend-import", self.kinds(grown))
        refused = GATE.write_baseline(self.root, self.baseline)
        self.assertEqual(refused["status"], "FAIL", refused)

        self.cli.write_text("from __future__ import annotations\n")
        stale = GATE.check(self.root, self.baseline)
        self.assertIn("stale-baseline-entry", self.kinds(stale))
        shrink = GATE.write_baseline(self.root, self.baseline)
        self.assertEqual(shrink["status"], "PASS", shrink)
        self.assertEqual(GATE.check(self.root, self.baseline)["status"], "PASS")

    def test_alias_function_import_and_literal_dynamic_import_are_recorded(self):
        self.cli.write_text(
            textwrap.dedent(
                """
                from __future__ import annotations

                import importlib

                def load():
                    from hyprial.identity import AgentStore as Store
                    return Store, importlib.import_module("hyprial.biz.routines")
                """
            ).lstrip()
        )
        GATE.write_baseline(self.root, self.baseline)
        document = json.loads(self.baseline.read_text())
        edges = {(row["path"], row["module"], row["symbol"]) for row in document["edges"]}
        self.assertIn(
            ("src/hyprial/shell/impl/cli/commands/demo.py", "hyprial.identity", "AgentStore"),
            edges,
        )
        self.assertIn(
            ("src/hyprial/shell/impl/cli/commands/demo.py", "hyprial.biz.routines", None),
            edges,
        )

    def test_backend_importing_the_cli_is_refused(self):
        GATE.write_baseline(self.root, self.baseline)
        self.write(
            "src/hyprial/daemon/impl/reverse.py",
            "from hyprial.shell.impl.cli.commands.common.root import app\n",
        )
        report = GATE.check(self.root, self.baseline)
        self.assertIn("reverse-backend-import", self.kinds(report), report)
        refused = GATE.write_baseline(self.root, self.baseline)
        self.assertEqual(refused["status"], "FAIL", refused)

    def test_increasing_a_count_is_not_a_shrink(self):
        GATE.write_baseline(self.root, self.baseline)
        self.cli.write_text(
            "from hyprial.daemon import BusinessStore\nfrom hyprial.daemon import BusinessStore\n"
        )
        report = GATE.check(self.root, self.baseline)
        self.assertIn("cli-backend-import-grew", self.kinds(report), report)

    def test_shell_reexport_and_internal_impl_are_not_laundered(self):
        GATE.write_baseline(self.root, self.baseline)
        self.write(
            "src/hyprial/shell/impl/helpers.py",
            "from hyprial.identity import UserStore as Store\n",
        )
        self.cli.write_text(
            "from hyprial.shell.impl.helpers import Store\n"
        )
        report = GATE.check(self.root, self.baseline)
        self.assertIn("cli-reexport-bypass", self.kinds(report), report)
        self.assertEqual(GATE.write_baseline(self.root, self.baseline)["status"], "FAIL")

        self.cli.write_text(
            "from hyprial.kernel.impl.ipc.client import daemon_request\n"
        )
        report = GATE.check(self.root, self.baseline)
        self.assertIn("new-cli-backend-import", self.kinds(report), report)

    def test_builtin_and_relative_dynamic_imports_are_recorded(self):
        self.cli.write_text(
            textwrap.dedent(
                """
                import importlib

                __import__("hyprial.daemon")
                importlib.import_module(".impl", package="hyprial.kernel")
                """
            ).lstrip()
        )
        GATE.write_baseline(self.root, self.baseline)
        document = json.loads(self.baseline.read_text())
        edges = {(row["module"], row["symbol"]) for row in document["edges"]}
        self.assertIn(("hyprial.daemon", None), edges)
        self.assertIn(("hyprial.kernel.impl", None), edges)

        self.cli.write_text(
            "import importlib\nname = 'hyprial.daemon'\nimportlib.import_module(name)\n"
        )
        report = GATE.check(self.root, self.baseline)
        self.assertIn("unresolved-dynamic-import", self.kinds(report), report)
        self.assertEqual(GATE.write_baseline(self.root, self.baseline)["status"], "FAIL")

    def _write_exceptions(self, entries: list[dict[str, object]]) -> Path:
        path = self.root / GATE.EXCEPTIONS_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "policy": "synthetic exact exception manifest",
                    "entries": entries,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        GATE.REVIEWED_EXCEPTION_DIGEST = hashlib.sha256(path.read_bytes()).hexdigest()
        return path

    def _exception(self, symbol: str, count: int = 1) -> dict[str, object]:
        return {
            "path": "src/hyprial/shell/impl/cli/commands/demo.py",
            "module": "hyprial.identity",
            "symbol": symbol,
            "count": count,
            "category": "protocol",
            "condition": "synthetic exact review condition",
        }

    def test_exception_manifest_is_exact_and_digest_anchored(self):
        GATE.write_baseline(self.root, self.baseline)
        self.cli.write_text(
            "from hyprial.daemon import BusinessStore\n"
            "from hyprial.identity import UserStore\n"
        )
        self._write_exceptions([self._exception("UserStore")])
        self.assertEqual(GATE.check(self.root, self.baseline)["status"], "PASS")

        self.cli.write_text(
            self.cli.read_text() + "from hyprial.biz import RoutineStore\n"
        )
        extra = GATE.check(self.root, self.baseline)
        self.assertIn("new-cli-backend-import", self.kinds(extra), extra)

        self._write_exceptions([self._exception("UserStore", count=2)])
        mismatch = GATE.check(self.root, self.baseline)
        self.assertIn("exception-count-mismatch", self.kinds(mismatch), mismatch)

        self.cli.write_text("from hyprial.daemon import BusinessStore\n")
        self._write_exceptions([
            {
                "path": "src/hyprial/shell/impl/cli/commands/demo.py",
                "module": "hyprial.daemon",
                "symbol": "BusinessStore",
                "count": 1,
                "category": "protocol",
                "condition": "synthetic exact review condition",
            }
        ])
        overlap = GATE.check(self.root, self.baseline)
        self.assertIn("exception-overlaps-baseline", self.kinds(overlap), overlap)

        self._write_exceptions([self._exception("MissingStore")])
        unused = GATE.check(self.root, self.baseline)
        self.assertIn("exception-unused", self.kinds(unused), unused)

        self._write_exceptions([self._exception("UserStore")])
        path = self.root / GATE.EXCEPTIONS_RELATIVE_PATH
        path.write_text(path.read_text() + "\n", encoding="utf-8")
        digest = GATE.check(self.root, self.baseline)
        self.assertIn("exception-digest-mismatch", self.kinds(digest), digest)

    def test_base_growth_still_fails_with_an_exact_exception(self):
        def git(*args):
            return subprocess.run(
                ["git", "-C", str(self.root), *args],
                capture_output=True,
                text=True,
                check=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "ratchet",
                    "GIT_AUTHOR_EMAIL": "ratchet@example.invalid",
                    "GIT_COMMITTER_NAME": "ratchet",
                    "GIT_COMMITTER_EMAIL": "ratchet@example.invalid",
                },
            )

        git("init", "-q")
        GATE.write_baseline(self.root, self.baseline)
        git("add", ".")
        git("commit", "-q", "-m", "base")

        self.cli.write_text(
            "from hyprial.daemon import BusinessStore\n"
            "from hyprial.identity import UserStore\n"
        )
        self._write_exceptions([self._exception("UserStore")])
        covered = GATE.check(self.root, self.baseline, base_ref="HEAD")
        self.assertEqual(covered["status"], "PASS", covered)

        self.cli.write_text(
            self.cli.read_text() + "from hyprial.biz import RoutineStore\n"
        )
        grown = GATE.check(self.root, self.baseline, base_ref="HEAD")
        self.assertIn("base-import-edge-growth", self.kinds(grown), grown)

    def test_exception_manifest_rejects_an_unknown_category(self):
        GATE.write_baseline(self.root, self.baseline)
        self._write_exceptions([self._exception("UserStore")])
        path = self.root / GATE.EXCEPTIONS_RELATIVE_PATH
        document = json.loads(path.read_text())
        document["entries"][0]["category"] = "wildcard"
        path.write_text(json.dumps(document) + "\n", encoding="utf-8")
        GATE.REVIEWED_EXCEPTION_DIGEST = hashlib.sha256(path.read_bytes()).hexdigest()
        report = GATE.check(self.root, self.baseline)
        self.assertIn("invalid-exceptions", self.kinds(report), report)

    def test_base_ref_is_the_growth_ceiling_and_missing_base_fails_closed(self):
        def git(*args):
            return subprocess.run(
                ["git", "-C", str(self.root), *args],
                capture_output=True,
                text=True,
                check=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "ratchet",
                    "GIT_AUTHOR_EMAIL": "ratchet@example.invalid",
                    "GIT_COMMITTER_NAME": "ratchet",
                    "GIT_COMMITTER_EMAIL": "ratchet@example.invalid",
                },
            )

        git("init", "-q")
        GATE.write_baseline(self.root, self.baseline)
        git("add", ".")
        git("commit", "-q", "-m", "base")

        self.cli.write_text(
            "from hyprial.daemon import BusinessStore\nfrom hyprial.identity import UserStore\n"
        )
        report = GATE.check(self.root, self.baseline, base_ref="HEAD")
        self.assertIn("base-import-edge-growth", self.kinds(report), report)

        missing = GATE.check(self.root, self.baseline, base_ref="no-such-base")
        self.assertIn("missing-base-ledger", self.kinds(missing), missing)

    def test_two_hop_shell_reexport_is_still_a_backend_edge(self):
        self.write(
            "src/hyprial/shell/impl/helpers/a.py",
            "from hyprial.daemon import BusinessStore as Thing\n",
        )
        self.write(
            "src/hyprial/shell/impl/helpers/b.py",
            "from hyprial.shell.impl.helpers.a import Thing\n",
        )
        self.cli.write_text(
            "from hyprial.shell.impl.helpers.b import Thing\n"
        )
        GATE.write_baseline(self.root, self.baseline)
        current, _reverse, reexported, unresolved = GATE.scan(self.root)
        assert not unresolved
        edge = GATE.Edge(
            "src/hyprial/shell/impl/cli/commands/demo.py",
            "hyprial.daemon",
            "BusinessStore",
        )
        assert current[edge] == 1
        assert edge in reexported
