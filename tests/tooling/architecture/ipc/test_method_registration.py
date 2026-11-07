"""Legacy-router method-registration gate: new literal branches need a spec."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[4] / "scripts/ci/check_method_registration.py"
SPEC = importlib.util.spec_from_file_location("method_registration", SCRIPT)
GATE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GATE
SPEC.loader.exec_module(GATE)

ROOT = Path(__file__).resolve().parents[4]
BASELINE = ROOT / "scripts/ci/legacy-method-baseline.json"


class MethodRegistrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="hyprial-method-registration-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.router = self.write(
            GATE.ROUTER,
            """
            _METHODS = frozenset({"probe.ping", "probe.status"})

            def handle(method, params):
                if method == "probe.read":
                    return "read"
                if method in {"probe.write"}:
                    return "write"
                if method.startswith("probe."):
                    return "family"
                return None
            """,
        )
        self.baseline = self.root / GATE.BASELINE_RELATIVE_PATH

    def write(self, path, source):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(source).lstrip())
        return target

    def kinds(self, report):
        return [row["kind"] for row in report["violations"]]

    def test_repository_router_baseline_matches(self):
        from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY

        report = GATE.check(ROOT, BASELINE, registry_names=set(METHOD_REGISTRY.names))
        self.assertEqual(report["status"], "PASS", report)

    def test_a_new_router_branch_is_rejected_unless_declared(self):
        self.assertEqual(
            GATE.write_baseline(self.root, self.baseline)["status"], "PASS"
        )
        self.router.write_text(
            self.router.read_text() + '\nif method == "probe.new":\n    pass\n'
        )
        report = GATE.check(self.root, self.baseline)
        self.assertIn("unregistered-router-method", self.kinds(report), report)
        self.assertEqual(
            GATE.write_baseline(self.root, self.baseline)["status"], "FAIL"
        )

        report = GATE.check(
            self.root, self.baseline, registry_names={"probe.new"}
        )
        self.assertEqual(report["status"], "PASS", report)

    def test_a_new_prefix_and_stale_entries_are_rejected(self):
        GATE.write_baseline(self.root, self.baseline)
        self.router.write_text(
            self.router.read_text() + '\nif method.startswith("other."):\n    pass\n'
        )
        report = GATE.check(self.root, self.baseline)
        self.assertIn("new-router-method-prefix", self.kinds(report), report)

        baseline = json.loads(self.baseline.read_text())
        baseline["methods"].append("probe.removed")
        self.baseline.write_text(json.dumps(baseline))
        report = GATE.check(self.root, self.baseline)
        self.assertIn("stale-router-method-entry", self.kinds(report), report)

    def test_single_word_and_reversed_comparisons_are_seen(self):
        GATE.write_baseline(self.root, self.baseline)
        self.router.write_text(
            self.router.read_text()
            + '\nif method == "newprobe":\n    pass\n'
            + '\nif "probe.reverse" == method:\n    pass\n'
        )
        report = GATE.check(self.root, self.baseline)
        names = {row.get("method") for row in report["violations"]}
        self.assertIn("newprobe", names, report)
        self.assertIn("probe.reverse", names, report)

    def test_empty_ledger_does_not_pass_an_existing_router(self):
        self.baseline.parent.mkdir(parents=True, exist_ok=True)
        self.baseline.write_text(
            json.dumps({"version": 1, "methods": [], "prefixes": []})
        )
        report = GATE.check(self.root, self.baseline)
        self.assertEqual(report["status"], "FAIL", report)
        self.assertGreater(report["routerMethods"], 0)
        self.assertIn("unregistered-router-method", self.kinds(report), report)

    def test_unsupported_dispatch_forms_are_rejected(self):
        GATE.write_baseline(self.root, self.baseline)
        self.router.write_text(
            self.router.read_text()
            + '\nhandlers = {}\nhandlers[method]()\n'
        )
        report = GATE.check(self.root, self.baseline)
        self.assertIn("unsupported-router-method-dispatch", self.kinds(report), report)

    def test_base_ref_is_the_method_growth_ceiling(self):
        def git(*args):
            return subprocess.run(
                ["git", "-C", str(self.root), *args],
                capture_output=True,
                text=True,
                check=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "method-gate",
                    "GIT_AUTHOR_EMAIL": "method-gate@example.invalid",
                    "GIT_COMMITTER_NAME": "method-gate",
                    "GIT_COMMITTER_EMAIL": "method-gate@example.invalid",
                },
            )

        git("init", "-q")
        GATE.write_baseline(self.root, self.baseline)
        git("add", ".")
        git("commit", "-q", "-m", "base")
        self.router.write_text(self.router.read_text() + '\nif method == "probe.new":\n    pass\n')

        report = GATE.check(self.root, self.baseline, base_ref="HEAD")
        self.assertIn("base-router-method-growth", self.kinds(report), report)
        missing = GATE.check(self.root, self.baseline, base_ref="no-such-base")
        self.assertIn("missing-base-ledger", self.kinds(missing), missing)

    def test_unresolvable_comparison_and_prefix_forms_fail_closed(self):
        self.baseline.parent.mkdir(parents=True, exist_ok=True)
        self.baseline.write_text(
            json.dumps({"version": 1, "methods": [], "prefixes": []})
        )
        self.router.write_text(
            "def handle(method):\n"
            "    if method == build_method_name():\n"
            "        return 1\n"
            "    if method.startswith(build_prefix()):\n"
            "        return 2\n"
            "    if method in build_methods():\n"
            "        return 3\n"
            "    return 0\n"
        )
        report = GATE.check(self.root, self.baseline)
        forms = {row.get("form") for row in report["violations"]}
        self.assertIn("unresolved-method-comparison:src/hyprial/daemon/impl/ipc/router.py", forms, report)
        self.assertIn("unresolved-method-prefix:src/hyprial/daemon/impl/ipc/router.py", forms, report)

    def test_relative_imported_constant_is_resolved(self):
        self.write(
            "src/hyprial/daemon/impl/ipc/ports.py",
            'NAME = "probe.new"\n',
        )
        self.router.write_text(
            "from .ports import NAME\n\n"
            "def handle(method):\n"
            "    if method == NAME:\n"
            "        return 1\n"
            "    return 0\n"
        )
        self.baseline.parent.mkdir(parents=True, exist_ok=True)
        self.baseline.write_text(
            json.dumps({"version": 1, "methods": [], "prefixes": []})
        )
        report = GATE.check(self.root, self.baseline)
        self.assertIn("probe.new", {row.get("method") for row in report["violations"]}, report)

    def test_foreign_bindings_and_other_registry_lookalike_fail(self):
        self.baseline.parent.mkdir(parents=True, exist_ok=True)
        self.baseline.write_text(
            json.dumps({"version": 1, "methods": [], "prefixes": []})
        )
        self.router.write_text(
            "def handle(method, other):\n"
            "    if method in other._bindings:\n"
            "        return 1\n"
            "    return 0\n"
        )
        report = GATE.check(self.root, self.baseline)
        self.assertIn("unsupported-router-method-dispatch", self.kinds(report), report)

        self.router.write_text("def handle(method):\n    return 0\n")
        self.write(
            "src/hyprial/daemon/impl/request_catalog/other.py",
            "def handle(method, handlers):\n"
            '    if method == "ping":\n'
            "        return handlers[method]()\n"
            "    return 0\n",
        )
        report = GATE.check(self.root, self.baseline)
        self.assertIn("unsupported-router-method-dispatch", self.kinds(report), report)
