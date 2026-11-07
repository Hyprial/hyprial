"""Binding-aware import rewrite tests (success and refusal cases)."""

from __future__ import annotations

import subprocess
import sys
import unittest

from tests.tooling.refactor.support._helpers import GIT_ENV, RepoFixture, standard_repo

from scripts.refactor.transforms import imports
from scripts.refactor.transforms.common import Rule


def make_rule(**kwargs) -> dict:
    base = {
        "rule_id": "t.import",
        "kind": "import_symbol",
        "stage": "S1",
        "status": "approved",
        "expected_matches": 1,
        "reason": "test",
    }
    base.update(kwargs)
    return Rule(base)


class ImportsTransformTests(RepoFixture):
    def setUp(self):
        super().setUp()
        standard_repo(self.root)

    def outcome(self, **kwargs):
        rule = make_rule(**kwargs)
        ws, ctx = self.build_workspace()
        outcome = imports.apply_rule(rule, ctx)
        if outcome.status != "refused":
            ws.materialize()
        return outcome, ws, ctx

    # -- success cases ------------------------------------------------------
    def test_from_import_unaliased_usage_rewritten(self):
        self.write("src/hyprial/agents/w.py", "from hyprial.daemon.impl.core import Pump\n\n\ndef go():\n    return Pump()\n")
        outcome, ws, _ = self.outcome(
            old_module="hyprial.daemon.impl.core",
            old_symbol="Pump",
            new_module="hyprial.kernel.contracts",
            new_symbol="Pump",
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        self.assertEqual(
            self.assertRead("src/hyprial/agents/w.py"),
            "from hyprial.kernel.contracts import Pump\n\n\ndef go():\n    return Pump()\n",
        )

    def test_alias_keeps_local_name_and_renames_symbol(self):
        self.write("src/hyprial/agents/w.py", "from hyprial.daemon.impl.core import Pump as P\n\nx = P\n")
        outcome, _, _ = self.outcome(
            old_module="hyprial.daemon.impl.core",
            old_symbol="Pump",
            new_module="hyprial.kernel.contracts",
            new_symbol="Pump",
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        self.assertEqual(self.assertRead("src/hyprial/agents/w.py"), "from hyprial.kernel.contracts import Pump as P\n\nx = P\n")

    def test_alias_renamed_symbol_rewrites_usages(self):
        self.write("src/hyprial/agents/w.py", "from hyprial.daemon.impl.core import Pump as P\n\nx = P\n")
        outcome, _, _ = self.outcome(
            old_module="hyprial.daemon.impl.core",
            old_symbol="Pump",
            new_module="hyprial.kernel.contracts",
            new_symbol="HPump",
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        self.assertEqual(self.assertRead("src/hyprial/agents/w.py"), "from hyprial.kernel.contracts import HPump as P\n\nx = P\n")

    def test_module_object_chain_usage_and_import_insertion(self):
        self.write("src/hyprial/agents/w.py", "import hyprial.daemon.impl.core\n\n\ndef go():\n    return hyprial.daemon.impl.core.Pump()\n")
        outcome, _, _ = self.outcome(
            old_module="hyprial.daemon.impl.core",
            old_symbol="Pump",
            new_module="hyprial.kernel.contracts",
            new_symbol="Pump",
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        text = self.assertRead("src/hyprial/agents/w.py")
        self.assertIn("import hyprial.kernel.contracts", text)
        self.assertIn("hyprial.kernel.contracts.Pump()", text)

    def test_mixed_from_import_splits_across_two_rules(self):
        self.write("src/hyprial/agents/w.py", "from hyprial.daemon.impl.core import Pump, Sink\n\nx = Pump\ny = Sink\n")
        first, ws, ctx = self.outcome(
            old_module="hyprial.daemon.impl.core",
            old_symbol="Pump",
            new_module="hyprial.kernel.contracts",
            new_symbol="Pump",
        )
        self.assertEqual(first.status, "applied", first.diagnostics)
        second = imports.apply_rule(
            make_rule(
                rule_id="t.import.2",
                old_module="hyprial.daemon.impl.core",
                old_symbol="Sink",
                new_module="hyprial.daemon.impl.io",
                new_symbol="Sink",
            ),
            ctx,
        )
        self.assertEqual(second.status, "applied", second.diagnostics)
        ws.materialize()
        text = self.assertRead("src/hyprial/agents/w.py")
        self.assertIn("from hyprial.kernel.contracts import Pump\n", text)
        self.assertIn("from hyprial.daemon.impl.io import Sink\n", text)
        self.assertIn("x = Pump\ny = Sink\n", text)

    def test_multilayer_relative_import_rewrite(self):
        self.write("src/hyprial/daemon/sub/deep.py", "")
        self.write(
            "src/hyprial/daemon/sub/user.py",
            "from ...daemon.core import Pump\n\nx = Pump\n",
        )
        outcome, _, _ = self.outcome(
            old_module="hyprial.daemon.core",
            old_symbol="Pump",
            new_module="hyprial.kernel.contracts",
            new_symbol="Pump",
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        self.assertEqual(
            self.assertRead("src/hyprial/daemon/sub/user.py"),
            "from ...kernel.contracts import Pump\n\nx = Pump\n",
        )

    def test_from_package_import_child_submodule(self):
        self.write("src/hyprial/kernel/helpers.py", "VALUE = 1\n")
        self.write("src/hyprial/agents/w.py", "from hyprial.kernel import helpers\n\nx = helpers.VALUE\n")
        outcome, _, _ = self.outcome(
            old_module="hyprial.kernel",
            old_symbol="helpers",
            new_module="hyprial.daemon",
            new_symbol="helpers",
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        self.assertEqual(self.assertRead("src/hyprial/agents/w.py"), "from hyprial.daemon import helpers\n\nx = helpers.VALUE\n")

    def test_namespace_package_target_resolves(self):
        # kernel.nsp has no __init__.py: namespace package
        self.write("src/hyprial/kernel/nsp/leaf.py", "LEAF = 1\n")
        self.write("src/hyprial/agents/w.py", "from hyprial.kernel.nsp.leaf import LEAF\n\nz = LEAF\n")
        outcome, _, _ = self.outcome(
            old_module="hyprial.kernel.nsp.leaf",
            old_symbol="LEAF",
            new_module="hyprial.kernel.leaf2",
            new_symbol="LEAF",
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        self.assertEqual(self.assertRead("src/hyprial/agents/w.py"), "from hyprial.kernel.leaf2 import LEAF\n\nz = LEAF\n")

    def test_module_rule_rewrites_import_from_and_chains(self):
        self.write("src/hyprial/agents/w.py", "import hyprial.daemon.impl.io as dio\n\nx = dio.Sink\ny = dio.Sink()\n")
        outcome, _, _ = self.outcome(
            kind="import_module",
            old_module="hyprial.daemon.impl.io",
            new_module="hyprial.kernel.io2",
            old_symbol=None,
            new_symbol=None,
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        self.assertEqual(
            self.assertRead("src/hyprial/agents/w.py"),
            "import hyprial.kernel.io2 as dio\n\nx = dio.Sink\ny = dio.Sink()\n",
        )

    def test_module_rule_without_alias_rewrites_qualified_chains(self):
        self.write("src/hyprial/agents/w.py", "import hyprial.daemon.impl.io\n\nx = hyprial.daemon.impl.io.Sink\n")
        outcome, _, _ = self.outcome(
            expected_matches=2,
            kind="import_module",
            old_module="hyprial.daemon.impl.io",
            new_module="hyprial.kernel.io2",
            old_symbol=None,
            new_symbol=None,
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        self.assertEqual(
            self.assertRead("src/hyprial/agents/w.py"),
            "import hyprial.kernel.io2\n\nx = hyprial.kernel.io2.Sink\n",
        )

    def test_type_checking_guard_and_else_branch(self):
        self.write(
            "src/hyprial/agents/w.py",
            "from typing import TYPE_CHECKING as TC\nimport typing as t\n"
            "if TC:\n    from hyprial.daemon.impl.core import Pump\nelse:\n"
            "    from hyprial.daemon.impl.core import Sink\n"
            "if t.TYPE_CHECKING:\n    from hyprial.daemon.impl.core import Hose\n",
        )
        outcome, _, _ = self.outcome(
            old_module="hyprial.daemon.impl.core",
            old_symbol="Pump",
            new_module="hyprial.kernel.contracts",
            new_symbol="Pump",
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        text = self.assertRead("src/hyprial/agents/w.py")
        self.assertIn("if TC:\n    from hyprial.kernel.contracts import Pump", text)
        self.assertIn("else:\n    from hyprial.daemon.impl.core import Sink", text)
        self.assertIn("if t.TYPE_CHECKING:\n    from hyprial.daemon.impl.core import Hose", text)

    def test_function_local_import_scoped_rewrite(self):
        self.write(
            "src/hyprial/agents/w.py",
            "from hyprial.daemon.impl.core import Pump\n\n\ndef make():\n    from hyprial.daemon.impl.core import Pump as Local\n    return Local()\n\n\ndef use():\n    return Pump()\n",
        )
        outcome, _, _ = self.outcome(
            expected_matches=2,
            old_module="hyprial.daemon.impl.core",
            old_symbol="Pump",
            new_module="hyprial.kernel.contracts",
            new_symbol="Pump",
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        text = self.assertRead("src/hyprial/agents/w.py")
        self.assertIn("from hyprial.kernel.contracts import Pump as Local", text)
        self.assertIn("from hyprial.kernel.contracts import Pump\n", text)

    def test_encoding_and_crlf_preserved(self):
        self.write(
            "src/hyprial/agents/w.py",
            b"# coding: latin-1\nfrom hyprial.daemon.impl.core import Pump\r\n\r\nX = 'caf\xe9'\r\nx = Pump\r\n",
        )
        outcome, _, _ = self.outcome(
            old_module="hyprial.daemon.impl.core",
            old_symbol="Pump",
            new_module="hyprial.kernel.contracts",
            new_symbol="Pump",
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        data = (self.root / "src/hyprial/agents/w.py").read_bytes()
        self.assertIn(b"from hyprial.kernel.contracts import Pump\r\n", data)
        self.assertIn(b"caf\xe9", data)
        self.assertTrue(data.endswith(b"x = Pump\r\n"))

    def test_comment_and_parenthesized_layout_preserved_for_remaining_names(self):
        self.write(
            "src/hyprial/agents/w.py",
            "from hyprial.daemon.impl.core import (\n    Pump,\n    Sink,  # trailing note\n)\n\nx = Sink\n",
        )
        outcome, _, _ = self.outcome(
            old_module="hyprial.daemon.impl.core",
            old_symbol="Pump",
            new_module="hyprial.kernel.contracts",
            new_symbol="Pump",
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        text = self.assertRead("src/hyprial/agents/w.py")
        self.assertIn("from hyprial.kernel.contracts import Pump\n", text)
        self.assertIn("Sink,  # trailing note", text)

    def test_getattr_with_constant_string_rewritten(self):
        self.write(
            "src/hyprial/agents/w.py",
            "from hyprial.daemon.impl.core import Pump\n\nx = getattr(Pump, 'name')\n",
        )
        outcome, _, _ = self.outcome(
            old_module="hyprial.daemon.impl.core",
            old_symbol="Pump",
            new_module="hyprial.kernel.contracts",
            new_symbol="HPump",
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        text = self.assertRead("src/hyprial/agents/w.py")
        self.assertIn("from hyprial.kernel.contracts import HPump", text)
        self.assertIn("getattr(HPump, 'name')", text)

    def test_already_done_when_new_form_present(self):
        self.write("src/hyprial/agents/w.py", "from hyprial.kernel.contracts import Pump\n\nx = Pump\n")
        outcome, _, _ = self.outcome(
            old_module="hyprial.daemon.impl.core",
            old_symbol="Pump",
            new_module="hyprial.kernel.contracts",
            new_symbol="Pump",
        )
        self.assertEqual(outcome.status, "already_done")

    # -- refusal cases -------------------------------------------------------
    def test_local_shadowing_refused(self):
        # 模块层 if 分支重绑定同一名字：使用处 referents > 1 -> 拒绝
        self.write(
            "src/hyprial/agents/w.py",
            "from hyprial.daemon.impl.core import Pump\n"
            "flag = True\n"
            "if flag:\n"
            "    Pump = 3\n"
            "x = Pump\n",
        )
        outcome, ws, _ = self.outcome(
            old_module="hyprial.daemon.impl.core",
            old_symbol="Pump",
            new_module="hyprial.kernel.contracts",
            new_symbol="Pump",
        )
        self.assertRefused(outcome, "AMBIGUOUS-BINDING")

    def test_chain_root_shadowed_refused(self):
        self.write(
            "src/hyprial/agents/w.py",
            "import hyprial.daemon.impl.core\n\n\ndef go():\n    hyprial = object()\n    return hyprial.daemon.impl.core\n",
        )
        outcome, _, _ = self.outcome(
            kind="import_module",
            old_module="hyprial.daemon.impl.core",
            new_module="hyprial.kernel.io2",
            old_symbol=None,
            new_symbol=None,
        )
        self.assertRefused(outcome, "SHADOWED-BINDING")

    def test_star_import_of_moved_symbol_refused(self):
        self.write("src/hyprial/agents/w.py", "from hyprial.daemon.impl.core import *\n\nx = Pump\n")
        outcome, _, _ = self.outcome(
            old_module="hyprial.daemon.impl.core",
            old_symbol="Pump",
            new_module="hyprial.kernel.contracts",
            new_symbol="Pump",
        )
        self.assertRefused(outcome, "STAR-IMPORT-SYMBOL")

    def test_dynamic_getattr_refused(self):
        self.write(
            "src/hyprial/agents/w.py",
            "from hyprial.daemon.impl.core import Pump\n\nname = dyn()\nx = getattr(Pump, name)\n",
        )
        outcome, _, _ = self.outcome(
            old_module="hyprial.daemon.impl.core",
            old_symbol="Pump",
            new_module="hyprial.kernel.contracts",
            new_symbol="HPump",
        )
        self.assertRefused(outcome, "REFLECT-ACCESS")

    def test_dangling_root_refused(self):
        self.write(
            "src/hyprial/agents/w.py",
            "import hyprial.daemon.impl.core\n\nx = hyprial.daemon.impl.other\n",
        )
        outcome, _, _ = self.outcome(
            kind="import_module",
            old_module="hyprial.daemon.impl.core",
            new_module="otherpkg.io2",
            old_symbol=None,
            new_symbol=None,
        )
        self.assertRefused(outcome, "DANGLING-ROOT")

    def test_comment_only_removal_refused(self):
        self.write(
            "src/hyprial/agents/w.py",
            "from hyprial.daemon.impl.core import (\n    Pump,\n    # keep-me comment\n)\n",
        )
        outcome, _, _ = self.outcome(
            old_module="hyprial.daemon.impl.core",
            old_symbol="Pump",
            new_module="hyprial.kernel.contracts",
            new_symbol="Pump",
        )
        self.assertRefused(outcome, "COMMENT-ONLY-REMOVAL-UNSUPPORTED")

    def test_match_drift_refused(self):
        self.write("src/hyprial/agents/w.py", "from hyprial.daemon.impl.core import Pump\nfrom hyprial.daemon.impl.core import Pump as P2\nx = P2\n")
        outcome, _, _ = self.outcome(
            expected_matches=1,
            old_module="hyprial.daemon.impl.core",
            old_symbol="Pump",
            new_module="hyprial.kernel.contracts",
            new_symbol="Pump",
        )
        self.assertRefused(outcome, "MATCH-DRIFT")

    def test_expected_matches_zero_without_history_refused(self):
        self.write("src/hyprial/agents/w.py", "x = 1\n")
        outcome, _, _ = self.outcome(
            old_module="hyprial.daemon.impl.core",
            old_symbol="Pump",
            new_module="hyprial.kernel.contracts",
            new_symbol="Pump",
        )
        self.assertRefused(outcome, "EXPECTED-MATCHES")

    def test_parse_error_file_refused(self):
        self.write("src/hyprial/agents/broken.py", "def nope(:\n")
        outcome, _, _ = self.outcome(
            old_module="hyprial.daemon.impl.core",
            old_symbol="Pump",
            new_module="hyprial.kernel.contracts",
            new_symbol="Pump",
        )
        self.assertRefused(outcome)

    def test_rewritten_fixture_still_imports_at_runtime(self):
        self.write("src/hyprial/kernel/contracts.py", "class Pump:\n    pass\n")
        self.write("src/hyprial/agents/w.py", "from hyprial.daemon.impl.core import Pump\n\n\ndef make():\n    return Pump()\n")
        # daemon.core still exists for the BEFORE state; simulate the move
        self.write("src/hyprial/daemon/impl/__init__.py", "")
        (self.root / "src/hyprial/daemon/impl/core.py").write_text("class Pump:\n    pass\n")
        outcome, ws, _ = self.outcome(
            old_module="hyprial.daemon.impl.core",
            old_symbol="Pump",
            new_module="hyprial.kernel.contracts",
            new_symbol="Pump",
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        proc = subprocess.run(
            [sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); import hyprial.agents.w; print(hyprial.agents.w.make())"],
            cwd=self.root,
            capture_output=True,
            text=True,
            env=GIT_ENV,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Pump", proc.stdout)

    def test_nonparen_from_import_drop_last_symbol_stays_valid(self):
        # Regression: removing the LAST alias of a non-parenthesized multi-symbol
        # ``from`` import must not leave a trailing comma (invalid syntax).
        self.write(
            "src/hyprial/agents/multi.py",
            "from hyprial.daemon.impl.core import Alpha, Beta, Gamma\n\n\ndef names():\n"
            "    return (Alpha.__name__, Beta.__name__)\n",
        )
        self.write("src/hyprial/daemon/impl/__init__.py", "")
        self.write("src/hyprial/daemon/impl/core.py", "class Alpha:\n    pass\n\n\nclass Beta:\n    pass\n\n\nclass Gamma:\n    pass\n")
        self.write("src/hyprial/kernel/contracts.py", "class Gamma:\n    pass\n")
        outcome, ws, _ = self.outcome(
            old_module="hyprial.daemon.impl.core",
            old_symbol="Gamma",
            new_module="hyprial.kernel.contracts",
            new_symbol="Gamma",
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        rewritten = ws.read("src/hyprial/agents/multi.py").decode()
        import libcst as cst

        cst.parse_module(rewritten)  # must not raise: rewritten source stays valid Python
        proc = subprocess.run(
            [sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); import hyprial.agents.multi; print(hyprial.agents.multi.names())"],
            cwd=self.root,
            capture_output=True,
            text=True,
            env=GIT_ENV,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Alpha", proc.stdout)

    def test_paren_from_import_drop_symbol_keeps_trailing_comment(self):
        self.write(
            "src/hyprial/agents/paren.py",
            "from hyprial.daemon.impl.core import (\n    Alpha,\n    Gamma,  # moved to kernel\n)\n\n\ndef names():\n    return Alpha.__name__\n",
        )
        self.write("src/hyprial/daemon/impl/__init__.py", "")
        self.write("src/hyprial/daemon/impl/core.py", "class Alpha:\n    pass\n\n\nclass Gamma:\n    pass\n")
        self.write("src/hyprial/kernel/contracts.py", "class Gamma:\n    pass\n")
        outcome, ws, _ = self.outcome(
            old_module="hyprial.daemon.impl.core",
            old_symbol="Gamma",
            new_module="hyprial.kernel.contracts",
            new_symbol="Gamma",
        )
        self.assertEqual(outcome.status, "applied", outcome.diagnostics)
        rewritten = ws.read("src/hyprial/agents/paren.py").decode()
        import libcst as cst

        cst.parse_module(rewritten)
        self.assertIn("# moved to kernel", rewritten)  # trailing comment carried on the comma


if __name__ == "__main__":
    unittest.main()
