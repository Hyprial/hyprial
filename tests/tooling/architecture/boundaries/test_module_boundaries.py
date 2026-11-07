"""Synthetic positive/negative controls for the five-domain CI architecture gate."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

SCRIPT = Path(__file__).resolve().parents[4] / 'scripts/ci/check_module_boundaries.py'
SPEC = importlib.util.spec_from_file_location("module_boundaries", SCRIPT)
GATE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GATE
SPEC.loader.exec_module(GATE)

LIMITS_SCRIPT = Path(__file__).resolve().parents[4] / 'scripts/ci/check_structural_limits.py'
LIMITS_SPEC = importlib.util.spec_from_file_location("structural_limits", LIMITS_SCRIPT)
LIMITS = importlib.util.module_from_spec(LIMITS_SPEC)
sys.modules[LIMITS_SPEC.name] = LIMITS
LIMITS_SPEC.loader.exec_module(LIMITS)


class ModuleBoundaryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="hyprial-module-boundaries-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.write("src/hyprial/__init__.py", '__version__ = "test"\n')
        for domain in GATE.DOMAINS:
            base = f"src/hyprial/{domain}"
            self.write(f"{base}/__init__.py", f"from hyprial.{domain}.interface import __all__ as __all__\nfrom hyprial.{domain}.interface import Thing as Thing\n")
            self.write(f"{base}/interface.py", f"from hyprial.{domain}.impl.thing import Thing\n__all__ = ['Thing']\n")
            self.write(f"{base}/impl/__init__.py", "")
            self.write(f"{base}/impl/thing.py", "class Thing:\n    pass\n")

    def write(self, path, source):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(source))
        return target

    def report(self):
        return GATE.scan(self.root)

    def assert_kind(self, kind, report=None):
        report = report or self.report()
        self.assertEqual(report["status"], "FAIL", report)
        self.assertTrue(any(row["kind"] == kind for row in report["violations"]), report)

    def consumer(self, source, domain="daemon"):
        self.write(f"src/hyprial/{domain}/impl/use.py", source)

    def lazy(self, value="'hyprial.identity.impl.thing'", symbol="name"):
        self.write("src/hyprial/identity/__init__.py", """
            from .interface import __all__ as __all__
            from .interface import __getattr__ as __getattr__
        """)
        self.write("src/hyprial/identity/interface.py", f"""
            _FACADE_EXPORTS = {{'Thing': {value}}}
            def __getattr__(name: str):
                try:
                    module_path = _FACADE_EXPORTS[name]
                except KeyError:
                    raise AttributeError(name) from None
                from importlib import import_module
                export = getattr(import_module(module_path), {symbol})
                globals()[name] = export
                return export
            __all__ = ['Thing']
        """)

    def test_static_faces_relative_child_alias_and_downward_edges_pass(self):
        self.consumer("from ...kernel import Thing as Shared\nfrom hyprial.identity.interface import Thing\n")
        report = self.report()
        self.assertEqual(report["status"], "PASS", report)
        self.assertGreater(report["importStatementsCovered"], 0)
        self.assertEqual(len(report["publicFaces"]), 10)
        self.assertTrue(all("sha256" in row for row in report["inputs"]))

    def test_reverse_edge_in_type_checking_alias_fails(self):
        self.consumer("import typing as t\nif t.TYPE_CHECKING:\n    from hyprial.shell import Thing as View\n", "kernel")
        self.assert_kind("reverse-domain")

    def test_function_local_relative_reverse_edge_fails(self):
        self.consumer("def f():\n    from ...daemon import Thing\n", "identity")
        self.assert_kind("reverse-domain")

    def test_cross_domain_impl_import_and_from_package_child_fail(self):
        for source in ["import hyprial.kernel.impl.thing as private\n", "from hyprial.kernel import impl as private\n", "from ...kernel.impl import thing\n"]:
            with self.subTest(source=source):
                self.consumer(source)
                self.assert_kind("public-face-bypass")

    def test_wildcard_import_fails_even_within_domain(self):
        self.consumer("from hyprial.daemon.interface import *\n")
        self.assert_kind("star-import")

    def test_private_or_unlisted_public_symbol_fails(self):
        self.consumer("from hyprial.kernel import _private\n")
        self.assert_kind("unlisted-public-symbol")

    def test_unknown_product_package_cannot_hide_under_kernel_prefix(self):
        self.write("src/hyprial/old_package/__init__.py", "")
        self.assert_kind("unmapped-product-file")

    def test_only_exact_outer_entry_modules_are_allowed(self):
        self.write("src/hyprial/cli.py", "from hyprial.shell import Thing\n")
        self.write("src/hyprial/gui_build_hook.py", "from hyprial.shell import Thing\n")
        self.assertEqual(self.report()["status"], "PASS")
        self.write("src/hyprial/gui_runtime.py", "")
        self.assert_kind("unmapped-product-file")

    def test_missing_domain_and_invalid_python_fail_closed(self):
        path = self.root / "src/hyprial/biz/interface.py"
        path.unlink()
        self.assert_kind("missing-public-face")
        self.write("src/hyprial/kernel/impl/bad.py", "def :\n")
        self.assert_kind("parse-error")

    def test_source_symlinks_fail_closed(self):
        (self.root / "src/hyprial/kernel/impl/link.py").symlink_to("thing.py")
        self.assert_kind("source-symlink")

    def test_forwarded_all_must_be_same_domain_and_unmodified(self):
        self.write("src/hyprial/daemon/__init__.py", "from hyprial.kernel.interface import __all__\n")
        self.assert_kind("invalid-all-forwarding")
        self.write("src/hyprial/daemon/__init__.py", "from .interface import __all__\n__all__.append('private')\n")
        self.assert_kind("mutated-export-table")

    def test_forwarding_cycle_is_rejected(self):
        self.write("src/hyprial/daemon/interface.py", "from hyprial.daemon import __all__\n")
        self.assert_kind("invalid-all-forwarding")

    def test_all_must_be_literal_unique_and_provider_backed(self):
        for source, kind in [
            ("__all__ = compute()\n", "nonliteral-all"),
            ("__all__ = ['Thing', 'Thing']\n", "duplicate-export"),
            ("__all__ = ['Missing']\n", "missing-export-provider"),
        ]:
            with self.subTest(source=source):
                self.write("src/hyprial/daemon/interface.py", source)
                self.assert_kind(kind)

    def test_missing_static_provider_module_or_symbol_fails(self):
        for source, kind in [
            ("from .impl.missing import Thing\n__all__=['Thing']\n", "missing-internal-module"),
            ("from .impl.thing import Missing as Thing\n__all__=['Thing']\n", "missing-export-provider"),
        ]:
            with self.subTest(source=source):
                self.write("src/hyprial/daemon/interface.py", source)
                self.assert_kind(kind)

    def test_exact_lazy_map_and_forwarding_pass(self):
        self.lazy()
        self.consumer("from hyprial.identity import Thing\n")
        report = self.report()
        self.assertEqual(report["status"], "PASS", report)
        self.assertEqual(report["dynamicCallsResolved"], 1)
        self.assertTrue(any(row["kind"] == "lazy-provider" for row in report["edges"]))

    def test_lazy_semantic_symbol_alias_is_supported(self):
        self.lazy("('hyprial.identity.impl.thing', 'Thing')", "symbol_name")
        path = self.root / "src/hyprial/identity/interface.py"
        path.write_text(path.read_text().replace("module_path = _FACADE_EXPORTS[name]", "module_path, symbol_name = _FACADE_EXPORTS[name]"))
        report = self.report()
        self.assertEqual(report["status"], "PASS", report)
        self.write("src/hyprial/identity/impl/thing.py", "class Different:\n    pass\n")
        self.assert_kind("missing-export-provider")

    def test_lazy_map_keys_must_match_all(self):
        self.lazy()
        path = self.root / "src/hyprial/identity/interface.py"
        path.write_text(path.read_text().replace("__all__ = ['Thing']", "__all__ = ['Other']"))
        self.assert_kind("lazy-map-all-mismatch")

    def test_duplicate_or_computed_lazy_provider_is_not_waived(self):
        self.lazy("compute()")
        self.assert_kind("invalid-lazy-provider-map")
        self.lazy()
        path = self.root / "src/hyprial/identity/interface.py"
        path.write_text(path.read_text().replace("{'Thing': 'hyprial.identity.impl.thing'}", "{'Thing': 'hyprial.identity.impl.thing', 'Thing': 'hyprial.identity.impl.thing'}"))
        self.assert_kind("invalid-lazy-provider-map")

    def test_lazy_foreign_impl_or_higher_provider_is_an_ordinary_edge(self):
        self.lazy("'hyprial.kernel.impl.thing'")
        self.assert_kind("public-face-bypass")
        self.lazy("'hyprial.daemon.impl.thing'")
        self.assert_kind("reverse-domain")

    def test_lazy_missing_provider_fails(self):
        self.lazy("'hyprial.identity.impl.missing'")
        self.assert_kind("missing-internal-module")

    def test_changed_lazy_call_shape_stays_blocked(self):
        self.lazy(symbol="'Different'")
        report = self.report()
        self.assertTrue(report["unknownDynamicCalls"], report)
        self.assertNotEqual(report["status"], "PASS")

    def test_additional_dynamic_call_is_not_blanket_exempted(self):
        self.lazy()
        path = self.root / "src/hyprial/identity/interface.py"
        path.write_text(path.read_text() + "\ndef extra(target):\n    from importlib import import_module\n    return import_module(target)\n")
        report = self.report()
        self.assertEqual(report["status"], "BLOCKED", report)
        self.assertEqual(report["dynamicCallsResolved"], 1)
        self.assertEqual(len(report["unknownDynamicCalls"]), 1)
        self.assertIn("identity/interface.py", report["unknownDynamicCalls"][0]["site"])

    def test_mutating_lazy_table_is_rejected(self):
        self.lazy()
        path = self.root / "src/hyprial/identity/interface.py"
        path.write_text(path.read_text() + "\n_FACADE_EXPORTS['Thing'] = 'hyprial.shell.impl.thing'\n")
        self.assert_kind("mutated-export-table")

    def test_lazy_provider_table_without_getattr_cannot_claim_exports(self):
        self.write("src/hyprial/identity/interface.py", "_FACADE_EXPORTS={'Thing':'hyprial.identity.impl.thing'}\n__all__=['Thing']\n")
        self.assert_kind("missing-lazy-facade")

    def test_forwarding_all_alone_does_not_forward_public_attributes(self):
        self.write("src/hyprial/daemon/__init__.py", "from .interface import __all__\n")
        self.assert_kind("missing-export-provider")

    def test_lazy_table_cannot_escape_the_audited_lookup(self):
        self.lazy()
        path = self.root / "src/hyprial/identity/interface.py"
        path.write_text(path.read_text() + "\nescaped = _FACADE_EXPORTS\n")
        self.assert_kind("escaped-export-table")

    def test_builtin_dynamic_fromlist_child_is_not_a_face_bypass(self):
        self.consumer("__import__('hyprial.kernel', fromlist=['impl'])\n")
        self.assert_kind("public-face-bypass")

    def test_narrow_root_version_metadata_is_supported(self):
        self.consumer("from hyprial import __version__\n")
        self.assertEqual(self.report()["status"], "PASS")
        self.consumer("from hyprial import unknown_metadata\n")
        self.assert_kind("public-face-bypass")

    def test_build_entry_literal_product_source_path_is_audited(self):
        self.write("src/hyprial/gui_build_hook.py", """
            from pathlib import Path
            import runpy
            bundle = Path(__file__).parent / 'shell' / 'impl' / 'thing.py'
            runpy.run_path(str(bundle))
        """)
        report = self.report()
        self.assertEqual(report["status"], "PASS", report)
        self.assertTrue(any(edge["kind"] == "dynamic-source-path" for edge in report["edges"]))

    def test_dynamic_source_path_obeys_domain_and_unknown_path_blocks(self):
        self.consumer("from pathlib import Path\nimport runpy\nrunpy.run_path(str(Path(__file__).parent.parent.parent / 'shell' / 'impl' / 'thing.py'))\n", "kernel")
        self.assert_kind("reverse-domain")
        self.consumer("import runpy\nrunpy.run_path(unknown_path)\n", "kernel")
        self.assertEqual(self.report()["status"], "BLOCKED")

    def test_local_path_parameter_does_not_inherit_global_literal(self):
        self.write("src/hyprial/gui_build_hook.py", """
            from pathlib import Path
            import runpy
            bundle = Path(__file__).parent / 'shell' / 'impl' / 'thing.py'
            def load(bundle):
                return runpy.run_path(str(bundle))
        """)
        self.assertEqual(self.report()["status"], "BLOCKED")

    def test_static_public_alias_reports_canonical_symbol(self):
        self.write("src/hyprial/daemon/interface.py", "from .impl.thing import Thing as Alias\n__all__=['Alias']\n")
        self.write("src/hyprial/daemon/__init__.py", "from .interface import __all__\nfrom .interface import Alias\n")
        report = self.report()
        self.assertEqual(report["status"], "PASS", report)
        face = next(row for row in report["publicFaces"] if row["module"] == "hyprial.daemon")
        self.assertEqual(face["canonicalExports"]["Alias"], {"module": "hyprial.daemon.impl.thing", "symbol": "Thing"})

    def test_literal_module_object_alias_has_exact_provider_handler(self):
        self.write("src/hyprial/identity/interface.py", """
            _FACADE_EXPORTS = {'Thing': ('hyprial.identity.impl.thing', None)}
            def __getattr__(name):
                try:
                    module_path, symbol_name = _FACADE_EXPORTS[name]
                except KeyError:
                    raise AttributeError(name) from None
                from importlib import import_module
                provider = import_module(module_path)
                export = provider if symbol_name is None else getattr(provider, symbol_name)
                globals()[name] = export
                return export
            __all__ = ['Thing']
        """)
        self.write("src/hyprial/identity/__init__.py", "from .interface import __all__, __getattr__\n")
        report = self.report()
        self.assertEqual(report["status"], "PASS", {"status": report["status"], "violations": report["violations"], "unknown": report["unknownDynamicCalls"]})
        face = next(row for row in report["publicFaces"] if row["module"] == "hyprial.identity")
        self.assertEqual(face["canonicalExports"]["Thing"], {"module": "hyprial.identity.impl.thing", "symbol": None})

    def test_literal_and_aliased_dynamic_internal_edges_obey_direction(self):
        self.consumer("from importlib import import_module as load\nload('hyprial.shell')\n", "kernel")
        self.assert_kind("reverse-domain")
        self.consumer("import importlib as il\nil.import_module('hyprial.kernel.impl.thing')\n")
        self.assert_kind("public-face-bypass")

    def test_literal_relative_dynamic_import_is_resolved(self):
        self.consumer("import importlib\nimportlib.import_module('...kernel', __package__)\n")
        self.assertEqual(self.report()["status"], "PASS")

    def test_external_dynamic_literal_and_unrelated_callable_do_not_block(self):
        self.consumer("import importlib\nimportlib.import_module('json')\nclass Tool:\n    def import_module(self, x): pass\nTool().import_module(unknown)\n")
        self.assertEqual(self.report()["status"], "PASS")

    def test_type_checking_third_party_class_names_are_not_internal_modules(self):
        self.consumer("from typing import TYPE_CHECKING as TC\nif TC:\n    from collections.abc import Iterator\n    from hyprial.kernel import Thing as Alias\n")
        self.assertEqual(self.report()["status"], "PASS")

    def test_product_only_scan_ignores_private_test_imports(self):
        self.write("tests/private.py", "from hyprial.kernel.impl.thing import Thing\n")
        self.assertEqual(self.report()["status"], "PASS")

    def test_unknown_call_has_hash_and_qualified_stable_identity(self):
        self.consumer("def load(target):\n    import importlib\n    return importlib.import_module(target)\n")
        report = self.report()
        self.assertEqual(report["status"], "BLOCKED", report)
        unknown = report["unknownDynamicCalls"][0]
        self.assertEqual(unknown["owner"], "load")
        self.assertEqual(len(unknown["sha256"]), 64)
        self.assertEqual(unknown["site"], self.report()["unknownDynamicCalls"][0]["site"])

    def test_cli_nonzero_for_failure_and_json_output(self):
        self.consumer("from hyprial.shell import Thing\n", "kernel")
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------------------
# `src/hyprial/skills/**` IS DATA, NOT A SIXTH DOMAIN.
#
# `ste100/SKILL.md` documents its linter by a path RELATIVE to the skill directory, so the
# payload must ship inside the wheel; the five-domain rule is derived from the path and
# therefore rejects any `.py` there by construction.  The gate exempts the data area from the
# DOMAIN judgement ONLY -- the files are still parsed, still registered, and still checked for
# parse errors, symlinks and duplicate modules, because they are exactly the files that go
# into the package.  The invariant is enforced in BOTH directions and these tests pin every
# part of it, including the parts that must NOT relax.
# ---------------------------------------------------------------------------------------


class DataAreaTests(ModuleBoundaryTests):
    """The data-area exemption, its two hard failures, and what must NOT be relaxed."""

    def test_data_area_is_exempt_from_the_domain_rule(self):
        self.write("src/hyprial/skills/ste100/scripts/ste-lint.py", "VALUE = 1\n")
        report = self.report()
        self.assertEqual(report["status"], "PASS", report)
        self.assertFalse(
            [row for row in report["violations"] if row["kind"] == "unmapped-product-file"],
            report,
        )

    def test_a_sibling_directory_is_not_swallowed_by_the_data_area(self):
        """Segment comparison, not string prefix: `skills-extra/` is NOT the data area."""

        self.write("src/hyprial/skills-extra/thing.py", "VALUE = 1\n")
        self.assert_kind("unmapped-product-file")

    def test_product_code_import_of_data_area_fails(self):
        self.write("src/hyprial/skills/stc/tools/cli.py", "VALUE = 1\n")
        self.consumer("from hyprial.skills.stc.tools import cli\n")
        self.assert_kind("data-area-import")

    def test_data_area_import_of_product_code_fails(self):
        """The hole this closes: before the change this import reported NOTHING."""

        self.write(
            "src/hyprial/skills/stc/tools/cli.py",
            "from hyprial.daemon.impl.thing import Thing\n",
        )
        self.assert_kind("data-area-imports-product-code")

    def test_data_area_internal_import_is_not_a_false_positive(self):
        self.write("src/hyprial/skills/stc/tools/core.py", "VALUE = 1\n")
        self.write(
            "src/hyprial/skills/stc/tools/cli.py",
            "from hyprial.skills.stc.tools import core\n",
        )
        self.assertEqual(self.report()["status"], "PASS", self.report())

    def test_data_area_still_reports_a_symlink(self):
        """The payload ships; a link crossing the package boundary is still a defect."""

        target = self.write("src/hyprial/skills/stc/tools/real.py", "VALUE = 1\n")
        link = self.root / "src/hyprial/skills/stc/tools/link.py"
        link.symlink_to(target)
        self.assert_kind("source-symlink")

    def test_data_area_still_reports_a_parse_error(self):
        self.write("src/hyprial/skills/stc/tools/broken.py", "def broken(:\n")
        self.assert_kind("parse-error")

    def test_data_area_dynamic_import_of_product_code_fails(self):
        """The STATIC rule must not be bypassable through the reflective route.

        hot-fixer's review point: `dynamic_imports()` resolves through the same `edge()`, so
        this path IS blocked today -- but nothing pinned it, and a later refactor of
        `dynamic_imports` would reopen the hole WITHOUT going red.  This test is that pin.
        """

        self.write(
            "src/hyprial/skills/stc/tools/cli.py",
            "import importlib\n"
            "MODULE = importlib.import_module(\"hyprial.daemon.impl.thing\")\n",
        )
        self.assert_kind("data-area-imports-product-code")

    def test_data_area_importing_the_package_root_fails(self):
        """`hyprial` itself is not a data-area module, and that is deliberate.

        Direction 2 fails for ANY non-data-area internal target, including the package root:
        the invariant is "the payload must not import product code", and the root is product
        code.  Pinned so it is read as intent rather than as a false positive.
        """

        self.write("src/hyprial/skills/stc/tools/cli.py", "import hyprial\n")
        self.assert_kind("data-area-imports-product-code")

    def test_structural_limits_still_governs_the_data_area(self):
        """The OTHER script in the same CI step must NOT be relaxed with this one.

        It measures file and line budgets rather than domains, so the data area is still a
        set of module directories to it.  This test exists so that a later "consistency
        cleanup" cannot exempt both checks at once.
        """

        for index in range(8):
            self.write(f"src/hyprial/skills/payload/mod_{index}.py", "VALUE = 1\n")
        report = LIMITS.scan(self.root)
        kinds = [row["kind"] for row in report["violations"]]
        self.assertIn("module-file-count", kinds, report)
