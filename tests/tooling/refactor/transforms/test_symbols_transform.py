"""Symbol extraction tests: dependencies, registration order, collaborators."""

from __future__ import annotations

import unittest

from tests.tooling.refactor.support._helpers import RepoFixture, standard_repo

from scripts.refactor.transforms import symbols
from scripts.refactor.transforms.common import Rule


def extract_rule(**kwargs) -> dict:
    base = {
        "rule_id": "t.extract",
        "kind": "symbol_extract",
        "stage": "S1",
        "status": "approved",
        "expected_matches": 1,
        "reason": "test",
    }
    base.update(kwargs)
    return base


class SymbolExtractTests(RepoFixture):
    def setUp(self):
        super().setUp()
        standard_repo(self.root)

    def outcome(self, **kwargs):
        ws, ctx = self.build_workspace()
        return symbols.apply_rule(Rule(extract_rule(**kwargs)), ctx), ws, ctx

    def outcome_direct(self, rule: dict, ws, ctx):
        outcome = symbols.apply_rule(Rule(rule), ctx)
        if outcome.status == "applied":
            ws.materialize()
        return outcome

    def test_top_level_class_extraction_with_import_dependency(self):
        self.write("src/hyprial/kernel/backoff.py", "DEFAULT = 3\n")
        self.write(
            "src/hyprial/daemon/desired.py",
            "import hyprial.kernel.backoff as backoff\n\n\nclass HarnessLaunchSpec:\n    retries = backoff.DEFAULT\n",
        )
        rule = self.pinned_source(
            extract_rule(
                symbols=["HarnessLaunchSpec"],
                target_path="src/hyprial/kernel/contracts.py",
                dependencies=[
                    {"name": "backoff", "provision": "import", "module": "hyprial.kernel.backoff"}
                ],
            ),
            "src/hyprial/daemon/desired.py",
        )
        ws, ctx = self.build_workspace()
        outcome = self.outcome_direct(rule, ws, ctx)
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        target = self.assertRead("src/hyprial/kernel/contracts.py")
        self.assertIn("from hyprial.kernel.backoff import backoff", target)
        self.assertIn("class HarnessLaunchSpec:", target)
        source = self.assertRead("src/hyprial/daemon/desired.py")
        self.assertNotIn("class HarnessLaunchSpec", source)
        self.assertIn("import hyprial.kernel.backoff as backoff", source)  # untouched import kept

    def test_uncovered_dependency_refused(self):
        self.write(
            "src/hyprial/daemon/desired.py",
            "import hyprial.kernel.backoff as backoff\n\n\nclass Spec:\n    r = backoff.DEFAULT\n",
        )
        rule = self.pinned_source(
            extract_rule(
                symbols=["Spec"],
                target_path="src/hyprial/kernel/contracts.py",
                dependencies=[],
            ),
            "src/hyprial/daemon/desired.py",
        )
        ws, ctx = self.build_workspace()
        outcome = symbols.apply_rule(Rule(rule), ctx)
        self.assertRefused(outcome, "UNCOVERED-DEPENDENCY")

    def test_unknown_dependency_module_refused(self):
        self.write("src/hyprial/daemon/desired.py", "from hyprial.kernel.backoff import X\n\n\nclass Spec:\n    v = X\n")
        rule = self.pinned_source(
            extract_rule(
                symbols=["Spec"],
                target_path="src/hyprial/kernel/contracts.py",
                dependencies=[{"name": "X", "provision": "import", "module": "hyprial.nonexistent"}],
            ),
            "src/hyprial/daemon/desired.py",
        )
        ws, ctx = self.build_workspace()
        outcome = symbols.apply_rule(Rule(rule), ctx)
        self.assertRefused(outcome, "DEPENDENCY-MODULE-UNKNOWN")

    def test_companion_provision_moves_second_symbol(self):
        self.write(
            "src/hyprial/daemon/desired.py",
            "DEFAULTS = [1]\n\n\nclass Spec:\n    d = DEFAULTS\n",
        )
        rule = self.pinned_source(
            extract_rule(
                symbols=["Spec", "DEFAULTS"],
                target_path="src/hyprial/kernel/contracts.py",
                dependencies=[{"name": "DEFAULTS", "provision": "companion", "symbol": "DEFAULTS"}],
            ),
            "src/hyprial/daemon/desired.py",
        )
        ws, ctx = self.build_workspace()
        outcome = self.outcome_direct(rule, ws, ctx)
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        self.assertIn("DEFAULTS = [1]", self.assertRead("src/hyprial/kernel/contracts.py"))
        self.assertIn("class Spec", self.assertRead("src/hyprial/kernel/contracts.py"))

    def test_registration_wrapper_preserves_order(self):
        self.write(
            "src/hyprial/cli_fam.py",
            "app = object()\n\n\n@app.command('a')\ndef alpha(app):\n    return 'a'\n\n\n@app.command('b')\ndef beta(app):\n    return 'b'\n",
        )
        rule = self.pinned_source(
            extract_rule(
                symbols=["alpha", "beta"],
                target_path="src/hyprial/shell/commands/fam.py",
                dependencies=[{"name": "app", "provision": "wrapper_param"}],
                registration={"wrapper": "register_fam_commands", "parameter": "app"},
            ),
            "src/hyprial/cli_fam.py",
        )
        ws, ctx = self.build_workspace()
        outcome = self.outcome_direct(rule, ws, ctx)
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        target = self.assertRead("src/hyprial/shell/commands/fam.py")
        self.assertIn("def register_fam_commands(app):", target)
        alpha_pos = target.index("def alpha(app):")
        beta_pos = target.index("def beta(app):")
        self.assertLess(alpha_pos, beta_pos, "registration order must be preserved")
        source = self.assertRead("src/hyprial/cli_fam.py")
        self.assertIn("register_fam_commands(app)", source)
        self.assertNotIn("def alpha(", source)

    def test_input_drift_refused(self):
        self.write("src/hyprial/daemon/desired.py", "class Spec:\n    pass\n")
        rule = self.pinned_source(
            extract_rule(
                symbols=["Spec"],
                target_path="src/hyprial/kernel/contracts.py",
                dependencies=[],
            ),
            "src/hyprial/daemon/desired.py",
        )
        self.write("src/hyprial/daemon/desired.py", "class Spec:\n    changed = True\n")
        ws, ctx = self.build_workspace()
        outcome = symbols.apply_rule(Rule(rule), ctx)
        self.assertRefused(outcome, "INPUT-DRIFT")

    def test_missing_symbol_refused(self):
        self.write("src/hyprial/daemon/desired.py", "class Other:\n    pass\n")
        rule = self.pinned_source(
            extract_rule(
                symbols=["Spec"],
                target_path="src/hyprial/kernel/contracts.py",
                dependencies=[],
            ),
            "src/hyprial/daemon/desired.py",
        )
        ws, ctx = self.build_workspace()
        outcome = symbols.apply_rule(Rule(rule), ctx)
        self.assertRefused(outcome, "SYMBOL-NOT-FOUND")

    def test_target_conflict_refused(self):
        self.write("src/hyprial/daemon/desired.py", "class Spec:\n    pass\n")
        self.write("src/hyprial/kernel/contracts.py", "# existing\n")
        rule = self.pinned_source(
            extract_rule(
                symbols=["Spec"],
                target_path="src/hyprial/kernel/contracts.py",
                dependencies=[],
            ),
            "src/hyprial/daemon/desired.py",
        )
        ws, ctx = self.build_workspace()
        outcome = symbols.apply_rule(Rule(rule), ctx)
        self.assertRefused(outcome, "TARGET-CONFLICT")

    def test_already_done_after_extraction(self):
        self.write("src/hyprial/kernel/contracts.py", "class Spec:\n    pass\n")
        self.write("src/hyprial/daemon/desired.py", "# moved away\n")
        rule = self.pinned_source(
            extract_rule(
                symbols=["Spec"],
                target_path="src/hyprial/kernel/contracts.py",
                target_mode="append",
                dependencies=[],
            ),
            "src/hyprial/daemon/desired.py",
        )
        ws, ctx = self.build_workspace()
        outcome = symbols.apply_rule(Rule(rule), ctx)
        self.assertEqual(outcome.status, "already_done", outcome.diagnostics)

    def test_stdlib_import_dependency_accepted_and_written(self):
        # Regression: extracted symbols may bind stdlib names (e.g. enum.StrEnum);
        # provision="import" must accept stdlib modules and write the import.
        self.write(
            "src/hyprial/agents/kinds.py",
            "from enum import StrEnum\n\n\nclass Mode(StrEnum):\n    A = \"a\"\n",
        )
        rule = extract_rule(
            symbols=["Mode"],
            target_path="src/hyprial/kernel/kinds.py",
            dependencies=[
                {"name": "StrEnum", "provision": "import", "module": "enum", "symbol": "StrEnum"}
            ],
        )
        rule = self.pinned_source(rule, "src/hyprial/agents/kinds.py")
        ws, ctx = self.build_workspace()
        outcome = self.outcome_direct(rule, ws, ctx)
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        target = self.assertRead("src/hyprial/kernel/kinds.py")
        self.assertIn("from enum import StrEnum", target)
        self.assertIn("class Mode(StrEnum):", target)


class ClassMemberExtractTests(RepoFixture):
    def setUp(self):
        super().setUp()
        standard_repo(self.root)
        self.write("src/hyprial/kernel/log.py", "def note(msg):\n    pass\n")

    def outcome_direct(self, rule: dict, ws, ctx):
        outcome = symbols.apply_rule(Rule(rule), ctx)
        if outcome.status == "applied":
            ws.materialize()
        return outcome

    def member_rule(self, **kwargs) -> dict:
        base = {
            "rule_id": "t.collab",
            "kind": "class_member_extract",
            "stage": "S4",
            "status": "approved",
            "expected_matches": 2,
            "reason": "test",
        }
        base.update(kwargs)
        return base

    def _write_host(self) -> None:
        self.write(
            "src/hyprial/daemon/application.py",
            (
                "import hyprial.kernel.log as log\n\n\n"
                "class DaemonApplication:\n"
                "    def __init__(self, inbox):\n"
                "        self.inbox = inbox\n"
                "        self.counter = 0\n\n"
                "    def _serve_ipc(self, stream):\n"
                "        self.counter += 1\n"
                "        log.note('serve')\n"
                "        return stream\n\n"
                "    def _close_ipc(self):\n"
                "        self.counter = 0\n\n"
                "    def _tick(self):\n"
                "        return self._serve_ipc('x')\n"
            ),
        )

    def _rule(self, **overrides) -> dict:
        rule = self.member_rule(
            class_name="DaemonApplication",
            symbols=["_serve_ipc", "_close_ipc"],
            new_class_name="IpcServer",
            target_path="src/hyprial/daemon/_ipc_server.py",
            host_attribute="_ipc",
            dependencies=[
                {"name": "log", "provision": "import", "module": "hyprial.kernel.log"},
                {"name": "self.counter", "provision": "field"},
            ],
            wiring={
                "method": "__init__",
                "code": "self._ipc = IpcServer(host=self, counter=self.counter)",
                "imports": ["from hyprial.daemon.impl._ipc_server import IpcServer"],
            },
        )
        rule.update(overrides)
        return self.pinned_source(rule, "src/hyprial/daemon/application.py")

    def test_collaborator_extraction_with_shim_and_wiring(self):
        self._write_host()
        ws, ctx = self.build_workspace()
        outcome = self.outcome_direct(self._rule(), ws, ctx)
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        collab = self.assertRead("src/hyprial/daemon/_ipc_server.py")
        self.assertIn("class IpcServer:", collab)
        self.assertIn("def __init__(self, host, counter):", collab)
        self.assertIn("self.counter = counter", collab)
        self.assertIn("log.note('serve')", collab)
        source = self.assertRead("src/hyprial/daemon/application.py")
        self.assertIn("self._ipc = IpcServer(host=self, counter=self.counter)", source)
        self.assertIn("from hyprial.daemon.impl._ipc_server import IpcServer", source)
        # the moved method body must be gone from the host; what remains is a
        # delegation shim for the remaining host caller _tick
        self.assertNotIn("self.counter += 1", source)
        self.assertNotIn("log.note('serve')", source)
        self.assertIn("def _serve_ipc(self, stream):", source)
        self.assertIn("return self._ipc._serve_ipc(stream)", source)
        import libcst as cst

        cst.parse_module(source)  # host file must stay syntactically valid
        cst.parse_module(collab)

    def test_uncovered_self_attribute_refused(self):
        self._write_host()
        rule = self._rule(
            dependencies=[{"name": "log", "provision": "import", "module": "hyprial.kernel.log"}]
        )
        ws, ctx = self.build_workspace()
        outcome = symbols.apply_rule(Rule(rule), ctx)
        self.assertRefused(outcome, "UNCOVERED-SELF-ATTRIBUTE")

    def test_zero_arg_super_refused(self):
        self.write(
            "src/hyprial/daemon/application.py",
            "class Base:\n    pass\n\n\nclass DaemonApplication(Base):\n    def _m(self):\n        return super()._m()\n",
        )
        rule = self.member_rule(
            class_name="DaemonApplication",
            symbols=["_m"],
            new_class_name="Collab",
            target_path="src/hyprial/daemon/_collab.py",
            dependencies=[],
            wiring={"method": "__init__", "code": "pass"},
        )
        rule = self.pinned_source(rule, "src/hyprial/daemon/application.py")
        ws, ctx = self.build_workspace()
        outcome = symbols.apply_rule(Rule(rule), ctx)
        self.assertRefused(outcome, "SUPER-CELL-UNSUPPORTED")

    def test_wiring_method_missing_refused(self):
        self._write_host()
        rule = self._rule(wiring={"method": "__missing__", "code": "pass"})
        ws, ctx = self.build_workspace()
        outcome = symbols.apply_rule(Rule(rule), ctx)
        self.assertRefused(outcome, "WIRING-METHOD-MISSING")



if __name__ == "__main__":
    unittest.main()
