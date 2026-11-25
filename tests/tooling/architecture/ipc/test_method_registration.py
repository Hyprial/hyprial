"""Legacy-router method-registration gate: new literal branches need a spec."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace

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

    def test_external_api_method_name_is_not_an_ipc_route(self):
        self.write(
            "src/hyprial/daemon/impl/adapters/slack/transport.py",
            '''
            async def call(self, method):
                api_method = method
                if api_method in {"chat.postMessage", "files.upload", "files.uploadV2"}:
                    return True
            ''',
        )
        methods = GATE.collect(self.root)["methods"]
        self.assertNotIn("chat.postMessage", methods)
        self.assertNotIn("files.upload", methods)
        self.assertNotIn("files.uploadV2", methods)
        self.router.write_text(
            'def handle(method):\n'
            '    if method == "chat.postMessage":\n'
            '        return old_handler()\n'
            '    return None\n'
        )
        self.assertIn("chat.postMessage", GATE.collect(self.root)["methods"])

    def test_registered_org_methods_are_not_shadowed_by_legacy_dispatch(self):
        source = (ROOT / GATE.ROUTER).read_text(encoding="utf-8")
        self.assertNotIn("return self._ipc_orgfs(", source)
        self.assertNotIn("return self._ipc_top_snapshot()", source)
        self.assertNotIn("return self._ipc_hosts(params)", source)
        self.assertNotIn('if method.startswith("orgfs.") and method not in METHOD_REGISTRY:', source)

    def test_retired_orgfs_prefix_and_unknown_literals_fail_closed(self):
        self.baseline.parent.mkdir(parents=True, exist_ok=True)
        self.baseline.write_text(json.dumps({"version": 1, "methods": [], "prefixes": ["orgfs."]}))
        self.router.write_text("def handle(method):\n    return None\n")
        report = GATE.check(self.root, self.baseline, registry_names={"orgfs.spaces"})
        self.assertIn("stale-router-prefix-entry", self.kinds(report), report)

        self.baseline.write_text(json.dumps({"version": 1, "methods": [], "prefixes": []}))
        for source in (
            'def handle(method):\n    if method == "orgfs.future":\n        return None\n',
            '_IPC_STATS_METHODS = frozenset({"orgfs.future"})\n',
        ):
            with self.subTest(source=source):
                self.router.write_text(source)
                report = GATE.check(self.root, self.baseline, registry_names={"orgfs.spaces"})
                self.assertIn({"kind": "unregistered-router-method", "method": "orgfs.future"},
                              report["violations"], report)

    def compiled_handle(self):
        from hyprial.daemon.impl.ipc.router import _NAMES_CHECKED_BEFORE_REFUSAL
        from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
        from hyprial.daemon.impl.request_catalog.migration import (
            MIGRATED_FAMILIES,
            MIGRATED_METHODS,
            ROUTER_ONLY_METHODS,
        )
        from hyprial.kernel import (
            DaemonRequestError,
            IPC_MESSAGE_READ_BODY,
            IPC_MESSAGE_SETTLE_HINT,
            ipc_errors,
        )

        class RegistryProbe:
            def __contains__(self, method):
                return method in METHOD_REGISTRY

            def invoke(self, app, method, params, **kwargs):
                return ("registry", method, params, kwargs)

        tree = ast.parse((ROOT / GATE.ROUTER).read_text(encoding="utf-8"))
        function = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "handle"
        )
        module = ast.parse("from __future__ import annotations\n")
        module.body.append(function)
        namespace = {
            "METHOD_REGISTRY": RegistryProbe(),
            "MIGRATED_FAMILIES": MIGRATED_FAMILIES,
            "MIGRATED_METHODS": MIGRATED_METHODS,
            "ROUTER_ONLY_METHODS": ROUTER_ONLY_METHODS,
            "_NAMES_CHECKED_BEFORE_REFUSAL": _NAMES_CHECKED_BEFORE_REFUSAL,
            "DaemonRequestError": DaemonRequestError,
            "ipc_errors": ipc_errors,
            "canonical_user_uri": lambda owner: f"user:{owner}",
            "_required_string": lambda value, name: value,
            "IPC_MESSAGE_READ_BODY": IPC_MESSAGE_READ_BODY,
            "IPC_MESSAGE_SETTLE_HINT": IPC_MESSAGE_SETTLE_HINT,
        }
        exec(compile(module, GATE.ROUTER, "exec"), namespace)
        app = SimpleNamespace(
            _restore_done=SimpleNamespace(is_set=lambda: True),
            _inbox=object(), _harnesses=object(), _presence=object(),
        )
        return namespace["handle"], app

    def test_all_registered_names_return_before_legacy_routes(self):
        from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY

        handle, app = self.compiled_handle()
        for method in METHOD_REGISTRY.names:
            with self.subTest(method=method):
                params = {"probe": method}
                result = handle(app, method, params, _trusted_message_origin="worker")
                self.assertEqual(
                    result,
                    ("registry", method, params, {"trusted_message_origin": "worker"}),
                )

    def test_unregistered_legacy_routes_keep_authorization_and_unknown_rejection(self):
        from hyprial.kernel import DaemonRequestError, ipc_errors

        handle, app = self.compiled_handle()
        app._handle_agent = lambda method, params: ("legacy-agent", method)
        with self.assertRaises(DaemonRequestError) as retired:
            handle(app, "work.unregistered", {})
        self.assertEqual(retired.exception.code, ipc_errors.METHOD_NOT_FOUND)
        self.assertEqual(handle(app, "agent.get", {}), ("legacy-agent", "agent.get"))
        with self.assertRaises(DaemonRequestError) as unlisted:
            handle(app, "agent.unregistered", {})
        self.assertEqual(unlisted.exception.code, ipc_errors.METHOD_NOT_FOUND)
        with self.assertRaises(DaemonRequestError) as unknown:
            handle(app, "orgfs.unregistered", {})
        self.assertEqual(unknown.exception.code, ipc_errors.METHOD_NOT_FOUND)

        app.owner = "owner"
        app._workflow_caller = lambda params: "user:intruder"
        app._routine_service = SimpleNamespace(
            status=lambda **kwargs: {"owner": "user:owner", "actor": "actor"}
        )
        app._routine_caller_is = lambda *args: False
        calls = []
        app._ipc_routine_set = lambda params: calls.append(params) or "updated"
        with self.assertRaises(DaemonRequestError) as denied:
            handle(app, "routine.set", {"name": "routine"})
        self.assertEqual(denied.exception.code, ipc_errors.CALLER_NOT_AUTHORIZED)
        self.assertEqual(calls, [])
        app._workflow_caller = lambda params: "user:owner"
        self.assertEqual(handle(app, "routine.set", {"name": "routine"}), "updated")
        self.assertEqual(calls, [{"name": "routine"}])

    def test_a_registered_router_branch_is_rejected_as_a_shadow(self):
        self.router.write_text("def handle(method):\n    return None\n")
        self.assertEqual(
            GATE.write_baseline(self.root, self.baseline)["status"], "PASS"
        )
        self.router.write_text(
            'def handle(method):\n'
            '    if method == "probe.new":\n'
            '        return old_handler()\n'
            '    return None\n'
        )
        report = GATE.check(self.root, self.baseline)
        self.assertIn("unregistered-router-method", self.kinds(report), report)
        self.assertEqual(
            GATE.write_baseline(self.root, self.baseline)["status"], "FAIL"
        )

        report = GATE.check(
            self.root, self.baseline, registry_names={"probe.new"}
        )
        self.assertEqual(report["status"], "FAIL", report)
        self.assertIn("registered-legacy-method-shadow", self.kinds(report), report)

    def test_registry_exclusion_must_guard_every_route_into_the_branch(self):
        cases = {
            'method == "probe.read" and method not in METHOD_REGISTRY': False,
            'method not in METHOD_REGISTRY and method.startswith("probe.")': False,
            'allowed and (method not in METHOD_REGISTRY and method == "probe.read")': False,
            'method == "probe.read" or method not in METHOD_REGISTRY': True,
            'method.startswith("probe.") and (method not in METHOD_REGISTRY or allowed)': True,
            'method == "probe.read" and method in METHOD_REGISTRY': True,
            'method == "probe.read" and method not in OTHER_REGISTRY': True,
            '(method == "probe.read" and method not in METHOD_REGISTRY) or allowed': True,
        }
        self.baseline.parent.mkdir(parents=True, exist_ok=True)
        self.baseline.write_text(
            json.dumps({"version": 1, "methods": ["probe.read"], "prefixes": ["probe."]})
        )
        for condition, expected_shadow in cases.items():
            with self.subTest(condition=condition):
                self.router.write_text(
                    f"def handle(method, allowed):\n    if {condition}:\n"
                    "        return old_handler()\n    return None\n"
                )
                report = GATE.check(
                    self.root, self.baseline, registry_names={"probe.read"}
                )
                self.assertEqual(
                    "registered-legacy-method-shadow" in self.kinds(report),
                    expected_shadow,
                    report,
                )

    def test_registered_membership_and_imported_constant_shadows_are_rejected(self):
        self.write("src/hyprial/daemon/impl/ipc/ports.py", 'NAME = "probe.read"\n')
        self.router.write_text(
            "from .ports import NAME\n\n"
            "def handle(method):\n"
            "    if method in {NAME, \"probe.write\"}:\n"
            "        return 1\n"
            "    return 0\n"
        )
        GATE.write_baseline(self.root, self.baseline)
        report = GATE.check(
            self.root,
            self.baseline,
            registry_names={"probe.read", "probe.write"},
        )
        shadows = [row for row in report["violations"] if row["kind"] == "registered-legacy-method-shadow"]
        self.assertEqual({row["method"] for row in shadows}, {"probe.read", "probe.write"}, report)

    def test_registry_membership_and_policy_helper_are_not_shadows(self):
        self.router.write_text(
            "def handle(method):\n"
            "    if method in METHOD_REGISTRY:\n"
            "        return METHOD_REGISTRY.invoke(method)\n"
            "    return 0\n\n"
            "def policy_check(method):\n"
            "    if method == \"probe.read\":\n"
            "        return True\n"
            "    return False\n"
        )
        GATE.write_baseline(self.root, self.baseline)
        report = GATE.check(self.root, self.baseline, registry_names={"probe.read"})
        self.assertNotIn("registered-legacy-method-shadow", self.kinds(report), report)

    def test_complete_prior_registry_dispatch_prevents_later_shadows(self):
        self.router.write_text(
            "def handle(method, params):\n"
            "    if method in METHOD_REGISTRY:\n"
            "        return METHOD_REGISTRY.invoke(self, method, params)\n"
            "    if method == 'probe.read':\n"
            "        return old_handler()\n"
        )
        GATE.write_baseline(self.root, self.baseline)
        report = GATE.check(self.root, self.baseline, registry_names={"probe.read"})
        self.assertEqual(report["status"], "PASS", report)

    def test_incomplete_registry_dispatch_does_not_exempt_later_routes(self):
        guards = (
            "    if method in METHOD_REGISTRY:\n"
            "        if allowed:\n"
            "            return METHOD_REGISTRY.invoke(self, method, params)\n",
            "    if method in METHOD_REGISTRY:\n"
            "        return METHOD_REGISTRY.invoke(self, 'other.method', params)\n",
            "    if method in METHOD_REGISTRY:\n"
            "        return METHOD_REGISTRY.invoke(self, method, params)\n"
            "    else:\n"
            "        return None\n",
            "    if allowed and method in METHOD_REGISTRY:\n"
            "        return METHOD_REGISTRY.invoke(self, method, params)\n",
        )
        GATE.write_baseline(self.root, self.baseline)
        for guard in guards:
            with self.subTest(guard=guard):
                self.router.write_text(
                    "def handle(method, params, allowed):\n"
                    + guard
                    + "    if method == 'probe.read':\n"
                    + "        return old_handler()\n"
                )
                report = GATE.check(
                    self.root, self.baseline, registry_names={"probe.read"}
                )
                self.assertIn("registered-legacy-method-shadow", self.kinds(report), report)

    def test_registered_prefix_shadow_requires_a_matching_declared_name(self):
        self.router.write_text(
            "def handle(method):\n"
            '    if method.startswith("probe."):\n'
            "        return 1\n"
            "    return 0\n"
        )
        GATE.write_baseline(self.root, self.baseline)
        report = GATE.check(self.root, self.baseline, registry_names={"probe.read"})
        self.assertIn("registered-legacy-method-shadow", self.kinds(report), report)

        report = GATE.check(self.root, self.baseline, registry_names={"other.read"})
        self.assertNotIn("registered-legacy-method-shadow", self.kinds(report), report)

    def test_write_baseline_cannot_whitewash_a_registered_shadow(self):
        GATE.write_baseline(self.root, self.baseline)
        before = self.baseline.read_text()
        report = GATE.write_baseline(
            self.root,
            self.baseline,
            registry_names={"probe.read"},
        )
        self.assertEqual(report["status"], "FAIL", report)
        self.assertIn("registered-legacy-method-shadow", self.kinds(report), report)
        self.assertEqual(self.baseline.read_text(), before)

    def test_shadow_controls_cover_compound_forwarding_negative_and_renamed_routes(self):
        cases = {
            "compound": (
                'def process_request(method, allowed):\n'
                '    if method == "probe.read" and allowed:\n'
                "        return old_handler()\n"
                "    return None\n",
                True,
            ),
            "forwarding": (
                'def process_request(method):\n'
                '    if method == "probe.read":\n'
                "        return METHOD_REGISTRY.invoke(method)\n"
                "    return None\n",
                False,
            ),
            "negative": (
                'def process_request(method):\n'
                '    if method != "probe.read":\n'
                "        return old_handler()\n"
                "    return METHOD_REGISTRY.invoke(method)\n",
                False,
            ),
            "renamed": (
                'def process_request(method):\n'
                '    if method == "probe.read":\n'
                "        return old_handler()\n"
                "    return None\n",
                True,
            ),
            "negated": (
                'def process_request(method):\n'
                '    if not (method == "probe.read"):\n'
                "        return old_handler()\n"
                "    return METHOD_REGISTRY.invoke(method)\n",
                False,
            ),
            "assigned": (
                'def process_request(method):\n'
                '    if method == "probe.read":\n'
                "        result = old_handler()\n"
                "        return result\n"
                "    return None\n",
                True,
            ),
            "nested": (
                'def process_request(method, allowed):\n'
                '    if method == "probe.read":\n'
                "        if allowed:\n"
                "            return old_handler()\n"
                "    return None\n",
                True,
            ),
            "unrelated_negation": (
                'def process_request(method, allowed):\n'
                '    if method == "probe.read" and not allowed:\n'
                "        return old_handler()\n"
                "    return None\n",
                True,
            ),
            "uninvoked_nested_helper": (
                'def process_request(method):\n'
                '    if method == "probe.read":\n'
                "        def unused():\n"
                "            return old_handler()\n"
                "        return METHOD_REGISTRY.invoke(method)\n"
                "    return None\n",
                False,
            ),
        }
        self.baseline.parent.mkdir(parents=True, exist_ok=True)
        for source, expected in cases.values():
            self.router.write_text(source)
            self.baseline.write_text(
                json.dumps({"version": 1, "methods": ["probe.read"], "prefixes": []})
            )
            report = GATE.check(
                self.root,
                self.baseline,
                registry_names={"probe.read"},
            )
            actual = "registered-legacy-method-shadow" in self.kinds(report)
            self.assertEqual(actual, expected, report)

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
