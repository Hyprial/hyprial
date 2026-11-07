"""Facade generation tests: lazy behavior, merge, conflicts."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest

from tests.tooling.refactor.support._helpers import GIT_ENV, RepoFixture, standard_repo

from scripts.refactor.transforms import facades
from scripts.refactor.transforms.common import Rule


def facade_rule(**kwargs) -> Rule:
    base = {
        "rule_id": "t.facade",
        "kind": "facade_export",
        "stage": "S2",
        "status": "approved",
        "expected_matches": 1,
        "reason": "test",
    }
    base.update(kwargs)
    return Rule(base)


class FacadeTests(RepoFixture):
    def setUp(self):
        super().setUp()
        standard_repo(self.root)

    def outcome(self, **kwargs):
        ws, ctx = self.build_workspace()
        outcome = facades.apply_rule(facade_rule(**kwargs), ctx)
        if outcome.status == "applied":
            ws.materialize()
        return outcome, ws, ctx

    def test_creates_lazy_facade(self):
        outcome, _, _ = self.outcome(
            facade_path="src/hyprial/kernel/interface.py",
            facade_module="hyprial.kernel",
            provider_module="hyprial.kernel.impl.contracts",
            symbol="HarnessLaunchSpec",
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        text = self.assertRead("src/hyprial/kernel/interface.py")
        self.assertIn("_FACADE_EXPORTS", text)
        self.assertIn("__getattr__", text)
        self.assertIn("from hyprial.kernel.impl.contracts import HarnessLaunchSpec", text)
        self.assertIn("if TYPE_CHECKING:", text)
        self.assertNotIn("import *", text)
        # the runtime import of the provider must NOT appear outside TYPE_CHECKING
        runtime_body = text.split("if TYPE_CHECKING:")[0] + text.split("def __getattr__")[1]
        self.assertNotIn("from hyprial.kernel.impl.contracts import", runtime_body)

    def test_lazy_behavior_no_eager_import(self):
        outcome, _, _ = self.outcome(
            facade_path="src/hyprial/kernel/interface.py",
            facade_module="hyprial.kernel",
            provider_module="hyprial.kernel.impl.contracts",
            symbol="HARNESS",
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        self.write(
            "src/hyprial/kernel/impl/contracts.py",
            "IMPL_IMPORTED = True\nHARNESS = 1\n",
        )
        self.write("src/hyprial/kernel/impl/__init__.py", "")
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                textwrap.dedent(
                    """
                    import sys
                    sys.path.insert(0, 'src')
                    import hyprial.kernel.interface as iface
                    assert 'hyprial.kernel.impl.contracts' not in sys.modules, 'eager import happened'
                    assert iface.HARNESS == 1
                    assert 'hyprial.kernel.impl.contracts' in sys.modules, 'lazy forward failed'
                    try:
                        iface.Missing
                    except AttributeError:
                        pass
                    else:
                        raise AssertionError('missing attribute did not raise')
                    print('lazy-ok')
                    """
                ),
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            env=GIT_ENV,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("lazy-ok", proc.stdout)

    def test_merge_into_managed_facade(self):
        outcome, _, _ = self.outcome(
            facade_path="src/hyprial/kernel/interface.py",
            facade_module="hyprial.kernel",
            provider_module="hyprial.kernel.impl.contracts",
            symbol="HarnessLaunchSpec",
        )
        self.assertEqual(outcome.status, "applied")
        second, _, _ = self.outcome(
            rule_id="t.facade.2",
            facade_path="src/hyprial/kernel/interface.py",
            facade_module="hyprial.kernel",
            provider_module="hyprial.kernel.impl.ports",
            symbol="RegistryPort",
        )
        self.assertEqual(second.status, "applied", second.diagnostics)
        text = self.assertRead("src/hyprial/kernel/interface.py")
        self.assertIn('"HarnessLaunchSpec"', text)
        self.assertIn('"RegistryPort"', text)

    def test_conflicting_provider_refused(self):
        first, _, _ = self.outcome(
            facade_path="src/hyprial/kernel/interface.py",
            facade_module="hyprial.kernel",
            provider_module="hyprial.kernel.impl.contracts",
            symbol="HarnessLaunchSpec",
        )
        self.assertEqual(first.status, "applied")
        conflict, _, _ = self.outcome(
            rule_id="t.facade.2",
            facade_path="src/hyprial/kernel/interface.py",
            facade_module="hyprial.kernel",
            provider_module="hyprial.kernel.impl.other",
            symbol="HarnessLaunchSpec",
        )
        self.assertRefused(conflict, "EXPORT-CONFLICT")

    def test_same_export_is_already_done(self):
        first, _, _ = self.outcome(
            facade_path="src/hyprial/kernel/interface.py",
            facade_module="hyprial.kernel",
            provider_module="hyprial.kernel.impl.contracts",
            symbol="HarnessLaunchSpec",
        )
        self.assertEqual(first.status, "applied")
        again, _, _ = self.outcome(
            rule_id="t.facade.2",
            facade_path="src/hyprial/kernel/interface.py",
            facade_module="hyprial.kernel",
            provider_module="hyprial.kernel.impl.contracts",
            symbol="HarnessLaunchSpec",
        )
        self.assertEqual(again.status, "already_done", again.diagnostics)

    def test_non_managed_existing_file_refused(self):
        self.write("src/hyprial/kernel/interface.py", "VERSION = '1.0'\n")
        outcome, _, _ = self.outcome(
            facade_path="src/hyprial/kernel/interface.py",
            facade_module="hyprial.kernel",
            provider_module="hyprial.kernel.impl.contracts",
            symbol="HarnessLaunchSpec",
        )
        self.assertRefused(outcome, "FACADE-NOT-MANAGED")

    def test_symbol_already_bound_refused(self):
        self.write("src/hyprial/kernel/interface.py", "def HarnessLaunchSpec():\n    pass\n")
        outcome, _, _ = self.outcome(
            facade_path="src/hyprial/kernel/interface.py",
            facade_module="hyprial.kernel",
            provider_module="hyprial.kernel.impl.contracts",
            symbol="HarnessLaunchSpec",
        )
        self.assertRefused(outcome, "EXPORT-CONFLICT")

    def test_eager_append_into_existing_file(self):
        self.write("src/hyprial/kernel/interface.py", "VERSION = '1.0'\n")
        outcome, _, _ = self.outcome(
            facade_path="src/hyprial/kernel/interface.py",
            facade_module="hyprial.kernel",
            provider_module="hyprial.kernel.version",
            symbol="VERSION",
            mode="eager",
        )
        # symbol VERSION already bound -> conflict on eager too
        self.assertRefused(outcome, "EXPORT-CONFLICT")
        second, _, _ = self.outcome(
            rule_id="t.facade.2",
            facade_path="src/hyprial/kernel/interface.py",
            facade_module="hyprial.kernel",
            provider_module="hyprial.kernel.contracts",
            symbol="NEWTHING",
        )
        # lazy create on existing non-managed file still refused
        self.assertRefused(second, "FACADE-NOT-MANAGED")
        third, _, _ = self.outcome(
            rule_id="t.facade.3",
            facade_path="src/hyprial/kernel/interface.py",
            facade_module="hyprial.kernel",
            provider_module="hyprial.kernel.info",
            symbol="PRODUCT",
            mode="eager",
        )
        self.assertEqual(third.status, "applied", third.diagnostics)
        self.assertEqual(
            self.assertRead("src/hyprial/kernel/interface.py"),
            "VERSION = '1.0'\nfrom hyprial.kernel.info import PRODUCT\n",
        )


if __name__ == "__main__":
    unittest.main()
