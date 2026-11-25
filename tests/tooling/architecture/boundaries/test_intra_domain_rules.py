"""Synthetic red controls and the real-tree pass for the gate's INTRA_DOMAIN_RULES table."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[4]
SCRIPT = REPO / "scripts/ci/check_module_boundaries.py"
SPEC = importlib.util.spec_from_file_location("module_boundaries_intra", SCRIPT)
GATE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GATE
SPEC.loader.exec_module(GATE)

IMPL = "hyprial.daemon.impl"
PACKAGES = (
    "network", "delivery", "threads", "conversations", "orgfs", "cells", "cells/params",
    "cells/runtime", "composition", "inbox", "application", "application/actors", "links", "helper",
)
MESSAGING = f"{IMPL}.application.messaging.delivery"


class IntraDomainRuleTests(unittest.TestCase):
    """Every rule row goes red on a synthetic violating import; neutral trees stay green."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="hyprial-intra-domain-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        patch = mock.patch.object(GATE, "DIRTY_DELIVERY", ())
        patch.start()
        self.addCleanup(patch.stop)
        self.write("src/hyprial/__init__.py", '__version__ = "test"\n')
        for domain in GATE.DOMAINS:
            base = f"src/hyprial/{domain}"
            self.write(f"{base}/__init__.py", f"from hyprial.{domain}.interface import __all__ as __all__\nfrom hyprial.{domain}.interface import Thing as Thing\n")
            self.write(f"{base}/interface.py", f"from hyprial.{domain}.impl.thing import Thing\n__all__ = ['Thing']\n")
            self.write(f"{base}/impl/__init__.py", "")
            self.write(f"{base}/impl/thing.py", "class Thing:\n    pass\n")
        for package in PACKAGES:
            self.write(f"src/hyprial/daemon/impl/{package}/__init__.py", "")
            self.write(f"src/hyprial/daemon/impl/{package}/leaf.py", "VALUE = 1\n")
        for package in ("application/messaging", "application/messaging/delivery"):
            self.write(f"src/hyprial/daemon/impl/{package}/__init__.py", "")

    def write(self, path, source):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(source))
        return target

    def module(self, package, source, name="use"):
        self.write(f"src/hyprial/daemon/impl/{package}/{name}.py", source)

    def intra(self, report=None):
        report = report or GATE.scan(self.root)
        return report, [row for row in report["violations"] if row["kind"] == "intra-domain-import"]

    def assert_rule(self, rule_id, forbidden, report=None):
        report, rows = self.intra(report)
        self.assertEqual(report["status"], "FAIL", report)
        self.assertTrue(any(row["rule"] == rule_id and row["forbidden"] == f"{IMPL}.{forbidden}" for row in rows), rows or report)
        return [row for row in rows if row["rule"] == rule_id]

    def assert_clean(self):
        report, rows = self.intra()
        self.assertEqual(rows, [])
        self.assertEqual(report["status"], "PASS", report)

    def test_neutral_synthetic_tree_passes(self):
        self.module("delivery", f"from {IMPL}.helper.leaf import VALUE\n")
        self.module("threads", f"from {IMPL}.links.leaf import VALUE\nfrom {IMPL}.delivery.leaf import VALUE as OTHER\n")
        self.module("conversations", f"from {IMPL}.cells.leaf import VALUE\nfrom {IMPL}.orgfs.leaf import VALUE as OTHER\n")
        self.assert_clean()

    def test_network_direct_imports_of_each_forbidden_package_fail(self):
        for forbidden in ("orgfs", "cells", "conversations"):
            with self.subTest(forbidden=forbidden):
                self.module("network", f"from {IMPL}.{forbidden}.leaf import VALUE\n")
                rows = self.assert_rule("intra/network-isolation", forbidden)
                self.assertEqual(rows[0]["via"], [])
                self.assertEqual(rows[0]["path"], "src/hyprial/daemon/impl/network/use.py")

    def test_delivery_direct_imports_of_each_forbidden_package_fail(self):
        for forbidden in ("orgfs", "cells", "conversations"):
            with self.subTest(forbidden=forbidden):
                self.module("delivery", f"import {IMPL}.{forbidden}.leaf\n")
                self.assert_rule("intra/delivery-isolation", forbidden)

    def test_threads_direct_imports_of_each_forbidden_package_fail(self):
        for forbidden, statement in (
            ("conversations", f"from {IMPL}.conversations import leaf\n"),
            ("orgfs", f"from {IMPL}.orgfs.leaf import VALUE\n"),
            ("cells", f"from {IMPL}.cells.runtime.leaf import VALUE\n"),
        ):
            with self.subTest(forbidden=forbidden):
                self.module("threads", statement)
                self.assert_rule("intra/threads-isolation", forbidden)

    def test_conversations_direct_imports_of_each_forbidden_package_fail(self):
        for forbidden in ("inbox", "application", "network", "composition", "links", "threads"):
            with self.subTest(forbidden=forbidden):
                self.module("conversations", f"from {IMPL}.{forbidden}.leaf import VALUE\n")
                self.assert_rule("intra/conversations-isolation", forbidden)

    def test_inbox_direct_imports_of_each_forbidden_package_fail(self):
        for forbidden in ("orgfs", "cells", "conversations", "threads"):
            with self.subTest(forbidden=forbidden):
                self.module("inbox", f"from {IMPL}.{forbidden}.leaf import VALUE\n")
                rows = self.assert_rule("intra/inbox-isolation", forbidden)
                self.assertEqual(rows[0]["path"], "src/hyprial/daemon/impl/inbox/use.py")

    def test_inbox_transitive_import_through_a_neutral_module_fails(self):
        self.module("helper", f"from {IMPL}.threads.leaf import VALUE\n", name="bridge")
        self.module("inbox", f"from {IMPL}.helper.bridge import VALUE\n")
        rows = self.assert_rule("intra/inbox-isolation", "threads")
        self.assertEqual(rows[0]["via"], [f"{IMPL}.helper.bridge", f"{IMPL}.threads.leaf"])

    def test_messaging_delivery_direct_imports_of_each_forbidden_package_fail(self):
        for forbidden in ("orgfs", "cells", "conversations"):
            with self.subTest(forbidden=forbidden):
                self.module("application/messaging/delivery", f"from {IMPL}.{forbidden}.leaf import VALUE\n")
                rows = self.assert_rule("intra/messaging-delivery-isolation", forbidden)
                self.assertEqual(rows[0]["from"], f"{MESSAGING}.use")

    def test_messaging_delivery_may_load_delivery_and_threads(self):
        self.module("application/messaging/delivery", f"from {IMPL}.delivery.leaf import VALUE\nfrom {IMPL}.threads.leaf import VALUE as OTHER\n")
        self.assert_clean()

    def test_messaging_delivery_reaching_orgfs_through_the_application_package_fails(self):
        self.write("src/hyprial/daemon/impl/application/__init__.py", f"from {IMPL}.orgfs import leaf\n")
        self.module("application/messaging/delivery", "VALUE = 1\n")
        rows = self.assert_rule("intra/messaging-delivery-isolation", "orgfs")
        self.assertEqual(rows[0]["via"][0], f"{IMPL}.application")

    def test_relative_and_function_local_imports_are_direct_violations(self):
        self.module("network", "def later():\n    from ..cells import leaf\n    return leaf\n")
        self.assert_rule("intra/network-isolation", "cells")

    def test_type_checking_import_is_still_a_direct_violation(self):
        self.module("delivery", f"from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from {IMPL}.cells.leaf import VALUE\n")
        self.assert_rule("intra/delivery-isolation", "cells")

    def test_package_init_of_a_rule_source_is_checked(self):
        self.write("src/hyprial/daemon/impl/delivery/__init__.py", f"from {IMPL}.conversations.leaf import VALUE\n")
        rows = self.assert_rule("intra/delivery-isolation", "conversations")
        self.assertEqual(rows[0]["from"], f"{IMPL}.delivery")

    def test_transitive_import_through_a_neutral_module_fails_with_the_chain(self):
        self.module("helper", f"from {IMPL}.orgfs.leaf import VALUE\n", name="bridge")
        self.module("delivery", f"from {IMPL}.helper.bridge import VALUE\n")
        rows = self.assert_rule("intra/delivery-isolation", "orgfs")
        self.assertEqual(rows[0]["via"], [f"{IMPL}.helper.bridge", f"{IMPL}.orgfs.leaf"])
        self.assertEqual(rows[0]["path"], "src/hyprial/daemon/impl/delivery/use.py")

    def test_transitive_import_through_a_package_init_fails(self):
        self.write("src/hyprial/daemon/impl/helper/__init__.py", f"from {IMPL}.cells import leaf\n")
        self.module("network", f"from {IMPL}.helper.leaf import VALUE\n")
        rows = self.assert_rule("intra/network-isolation", "cells")
        self.assertIn(f"{IMPL}.helper", rows[0]["via"])

    def test_transitive_composition_reexport_fails(self):
        self.module("composition", f"from {IMPL}.cells.params.leaf import VALUE as cell_params\n", name="glue")
        self.write("src/hyprial/daemon/impl/composition/__init__.py", "from .glue import cell_params\n")
        self.module("delivery", f"from {IMPL}.composition import cell_params\n")
        self.assert_rule("intra/delivery-isolation", "cells")

    def test_lazy_facade_reexport_is_resolved_to_its_provider(self):
        self.write("src/hyprial/daemon/interface.py", """
            _FACADE_EXPORTS = {'Thing': 'hyprial.daemon.impl.thing', 'CellThing': 'hyprial.daemon.impl.cells.leaf'}
            def __getattr__(name: str):
                try:
                    module_path = _FACADE_EXPORTS[name]
                except KeyError:
                    raise AttributeError(name) from None
                from importlib import import_module
                export = getattr(import_module(module_path), name)
                globals()[name] = export
                return export
            __all__ = ['Thing', 'CellThing']
        """)
        self.write("src/hyprial/daemon/__init__.py", "from .interface import __all__ as __all__\nfrom .interface import __getattr__ as __getattr__\n")
        self.write("src/hyprial/daemon/impl/cells/leaf.py", "class CellThing:\n    pass\n")
        self.module("network", "from hyprial.daemon import CellThing\n")
        rows = self.assert_rule("intra/network-isolation", "cells")
        self.assertEqual(rows[0]["via"][-1], f"{IMPL}.cells.leaf")

    def test_function_local_hop_still_loads_the_target_import_time_closure(self):
        self.module("helper", f"from {IMPL}.conversations.leaf import VALUE\n", name="bridge")
        self.module("network", f"def later():\n    from {IMPL}.helper import bridge\n    return bridge\n")
        self.assert_rule("intra/network-isolation", "conversations")

    def test_deferred_imports_deeper_in_the_chain_are_not_followed(self):
        self.module("helper", f"def later():\n    from {IMPL}.cells import leaf\n    return leaf\n", name="bridge")
        self.module("helper", f"from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from {IMPL}.orgfs import leaf\n", name="typed")
        self.module("delivery", f"from {IMPL}.helper import bridge, typed\n")
        self.assert_clean()

    def test_threads_may_import_cells_params_and_what_it_imports_within_cells(self):
        with mock.patch.object(
            GATE,
            "INTRA_DOMAIN_ALLOWANCES",
            (GATE.IntraAllowance(f"{IMPL}.threads", f"{IMPL}.cells.params"),),
        ):
            self.write("src/hyprial/daemon/impl/cells/params/__init__.py", f"from {IMPL}.cells.leaf import VALUE\n")
            self.module("threads", f"from {IMPL}.cells.params import VALUE\n")
            self.assert_clean()

    def test_the_cells_params_allowance_does_not_open_other_forbidden_packages(self):
        with mock.patch.object(
            GATE,
            "INTRA_DOMAIN_ALLOWANCES",
            (GATE.IntraAllowance(f"{IMPL}.threads", f"{IMPL}.cells.params"),),
        ):
            self.write("src/hyprial/daemon/impl/cells/params/__init__.py", f"from {IMPL}.orgfs.leaf import VALUE\n")
            self.module("threads", f"from {IMPL}.cells.params import VALUE\n")
            rows = self.assert_rule("intra/threads-isolation", "orgfs")
            self.assertEqual(rows[0]["via"][0], f"{IMPL}.cells.params")

    def test_the_cells_params_allowance_does_not_open_the_cells_package_init(self):
        with mock.patch.object(
            GATE,
            "INTRA_DOMAIN_ALLOWANCES",
            (GATE.IntraAllowance(f"{IMPL}.threads", f"{IMPL}.cells.params"),),
        ):
            self.write("src/hyprial/daemon/impl/cells/__init__.py", f"from {IMPL}.cells.runtime import leaf\n")
            self.module("threads", f"from {IMPL}.cells.params import leaf\n")
            self.assert_rule("intra/threads-isolation", "cells")

    def test_the_cells_params_allowance_is_only_for_threads(self):
        self.module("delivery", f"from {IMPL}.cells.params import leaf\n")
        self.assert_rule("intra/delivery-isolation", "cells")

    def dirty(self, *entries):
        rows = tuple(GATE.IntraDirty("intra/messaging-delivery-isolation", f"{MESSAGING}.{source}".rstrip("."), witness,
                                     tuple(f"{IMPL}.{name}" for name in forbidden), "synthetic")
                     for source, witness, forbidden in entries)
        patch = mock.patch.object(GATE, "DIRTY_DELIVERY", rows)
        patch.start()
        self.addCleanup(patch.stop)

    def test_cli_reports_a_stale_dirty_delivery_entry(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("stale DIRTY_DELIVERY entry", result.stderr)

    def test_dirty_delivery_excuses_only_the_listed_witness_and_package(self):
        self.write("src/hyprial/daemon/impl/application/__init__.py", f"from {IMPL}.orgfs import leaf\n")
        self.module("application/messaging/delivery", "VALUE = 1\n", name="sends")
        self.dirty(("", f"{IMPL}.application", ("orgfs",)), ("sends", f"{IMPL}.application", ("orgfs",)))
        self.assert_clean()

    def test_dirty_delivery_does_not_cover_another_forbidden_package_on_the_same_witness(self):
        self.write("src/hyprial/daemon/impl/application/__init__.py", f"from {IMPL}.orgfs import leaf\nfrom {IMPL}.cells import leaf as other\n")
        self.module("application/messaging/delivery", "VALUE = 1\n", name="sends")
        self.dirty(("", f"{IMPL}.application", ("orgfs",)), ("sends", f"{IMPL}.application", ("orgfs",)))
        rows = self.assert_rule("intra/messaging-delivery-isolation", "cells")
        self.assertEqual({(row["from"], row["forbidden"]) for row in rows}, {(MESSAGING, f"{IMPL}.cells"), (f"{MESSAGING}.sends", f"{IMPL}.cells")})

    def test_dirty_delivery_does_not_cover_a_second_path_through_another_first_hop(self):
        self.module("composition", f"from {IMPL}.cells.leaf import VALUE\n", name="hub")
        self.module("helper", f"from {IMPL}.composition.hub import VALUE\n", name="bridge")
        self.module("application/messaging/delivery", f"from {IMPL}.composition.hub import VALUE\nfrom {IMPL}.helper.bridge import VALUE as OTHER\n", name="lark")
        self.dirty(("lark", f"{IMPL}.composition.hub", ("cells",)))
        rows = self.assert_rule("intra/messaging-delivery-isolation", "cells")
        self.assertEqual(rows[0]["via"][0], f"{IMPL}.helper.bridge")

    def test_dirty_delivery_is_per_module(self):
        self.write("src/hyprial/daemon/impl/application/__init__.py", f"from {IMPL}.orgfs import leaf\n")
        self.module("application/messaging/delivery", "VALUE = 1\n", name="sends")
        self.module("application/messaging/delivery", "VALUE = 1\n", name="fresh")
        self.dirty(("sends", f"{IMPL}.application", ("orgfs",)))
        rows = self.assert_rule("intra/messaging-delivery-isolation", "orgfs")
        self.assertEqual({row["from"] for row in rows}, {f"{MESSAGING}.fresh", MESSAGING})

    def test_dirty_delivery_never_excuses_a_literal_import(self):
        self.module("application/messaging/delivery", f"from {IMPL}.orgfs.leaf import VALUE\n", name="sends")
        self.dirty(("sends", f"{IMPL}.orgfs.leaf", ("orgfs",)))
        rows = self.assert_rule("intra/messaging-delivery-isolation", "orgfs")
        self.assertEqual(rows[0]["via"], [])

    def test_stale_dirty_delivery_entry_fails(self):
        self.module("application/messaging/delivery", "VALUE = 1\n", name="sends")
        self.dirty(("sends", f"{IMPL}.application", ("orgfs",)))
        report = GATE.scan(self.root)
        self.assertEqual(report["status"], "FAIL")
        stale = [row for row in report["violations"] if row["kind"] == "stale-intra-dirty-allowance"]
        self.assertEqual([(row["from"], row["witness"], row["forbidden"]) for row in stale],
                         [(f"{MESSAGING}.sends", f"{IMPL}.application", f"{IMPL}.orgfs")])

    def test_cli_names_the_rule_on_stderr(self):
        self.module("network", f"from {IMPL}.orgfs.leaf import VALUE\n")
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("intra/network-isolation", result.stderr)
        self.assertIn(f"{IMPL}.network.use", result.stderr)

    def test_eager_public_face_reexport_still_fails_existing_intra_rules(self):
        self.write("src/hyprial/daemon/impl/cells/leaf.py", "class CellThing:\n    pass\n")
        self.write(
            "src/hyprial/daemon/interface.py",
            "from hyprial.daemon.impl.thing import Thing\nfrom hyprial.daemon.impl.cells.leaf import CellThing\n__all__ = ['Thing', 'CellThing']\n",
        )
        self.write(
            "src/hyprial/daemon/__init__.py",
            "from hyprial.daemon.interface import __all__ as __all__\nfrom hyprial.daemon.interface import Thing as Thing\nfrom hyprial.daemon.interface import CellThing as CellThing\n",
        )
        self.module("network", "VALUE = 1\n")
        rows = self.assert_rule("intra/network-isolation", "cells")
        self.assertTrue(any("hyprial.daemon" in row["via"] for row in rows), rows)
        self.module("delivery", "from hyprial.daemon import CellThing\n")
        delivery_rows = self.assert_rule("intra/delivery-isolation", "cells")
        self.assertTrue(any(row["via"][-1] == f"{IMPL}.cells.leaf" for row in delivery_rows), delivery_rows)

    def test_manysys_internal_imports_stay_clean_despite_eager_kernel_face(self):
        self.write("src/hyprial/kernel/impl/manysys/__init__.py", "from .ipc import VALUE as VALUE\n")
        self.write("src/hyprial/kernel/impl/manysys/ipc.py", "VALUE = 1\n")
        self.assert_clean()

    def test_manysys_dynamically_rejects_unlisted_future_kernel_impl_subpackages(self):
        self.write("src/hyprial/kernel/impl/manysys/__init__.py", "")
        self.write("src/hyprial/kernel/impl/future_pkg/__init__.py", "")
        self.write("src/hyprial/kernel/impl/future_pkg/leaf.py", "VALUE = 1\n")
        self.write("src/hyprial/kernel/impl/future_mod.py", "VALUE = 2\n")
        self.assertNotIn("future_pkg", GATE.STATIC_KERNEL_SIBLINGS)
        self.assertNotIn("future_mod", GATE.STATIC_KERNEL_SIBLINGS)
        for import_stmt, expected in (
            ("from hyprial.kernel.impl.future_pkg.leaf import VALUE\n", "hyprial.kernel.impl.future_pkg"),
            ("from hyprial.kernel.impl.future_mod import VALUE\n", "hyprial.kernel.impl.future_mod"),
        ):
            with self.subTest(expected=expected):
                self.write("src/hyprial/kernel/impl/manysys/use.py", import_stmt)
                report, rows = self.intra()
                self.assertEqual(report["status"], "FAIL", report)
                self.assertTrue(
                    any(
                        row["rule"] == "intra/manysys-leaf"
                        and row["forbidden"] == expected
                        for row in rows
                    ),
                    rows or report,
                )

    def test_manysys_rejects_kernel_siblings_interface_and_upper_domains(self):
        self.write("src/hyprial/error/__init__.py", "CODE = 'E'\n")
        self.write("src/hyprial/kernel/impl/manysys/__init__.py", "")
        for target, expected in (
            ("from hyprial.kernel.impl.actor_runtime.leaf import VALUE\n", "hyprial.kernel.impl.actor_runtime"),
            ("from hyprial.kernel.impl.contracts.leaf import VALUE\n", "hyprial.kernel.impl.contracts"),
            ("from hyprial.kernel.impl.log.leaf import VALUE\n", "hyprial.kernel.impl.log"),
            ("from hyprial.kernel.interface import Thing\n", "hyprial.kernel.interface"),
            ("from hyprial.kernel import Thing\n", "hyprial.kernel.interface"),
            ("from hyprial.error import CODE\n", "hyprial.error"),
        ):
            with self.subTest(expected=expected):
                for sibling in ("actor_runtime", "contracts", "log"):
                    self.write(f"src/hyprial/kernel/impl/{sibling}/__init__.py", "")
                    self.write(f"src/hyprial/kernel/impl/{sibling}/leaf.py", "VALUE = 1\n")
                self.write("src/hyprial/kernel/impl/manysys/use.py", target)
                report, rows = self.intra()
                self.assertEqual(report["status"], "FAIL", report)
                self.assertTrue(
                    any(row["rule"] == "intra/manysys-leaf" and row["forbidden"] == expected for row in rows),
                    rows or report,
                )


class IntraDomainRuleTableTests(unittest.TestCase):
    """The table rows are pinned so a silent widening shows up in review."""

    def test_rule_rows_are_pinned(self):
        rows = {rule.rule_id: (rule.sources, rule.forbidden) for rule in GATE.INTRA_DOMAIN_RULES}
        isolated = (f"{IMPL}.orgfs", f"{IMPL}.cells", f"{IMPL}.conversations")
        self.assertEqual(rows["intra/manysys-leaf"], ((f"{GATE.KERNEL_IMPL}.manysys",), GATE.KERNEL_NON_MANYSYS))
        self.assertEqual(rows["intra/network-isolation"], ((f"{IMPL}.network",), isolated))
        self.assertEqual(rows["intra/delivery-isolation"], ((f"{IMPL}.delivery",), isolated))
        self.assertEqual(rows["intra/inbox-isolation"], ((f"{IMPL}.inbox",), (*isolated, f"{IMPL}.threads")))
        self.assertEqual(rows["intra/messaging-delivery-isolation"], ((MESSAGING,), isolated))
        self.assertEqual(rows["intra/threads-isolation"], ((f"{IMPL}.threads",), (f"{IMPL}.conversations", f"{IMPL}.orgfs", f"{IMPL}.cells")))
        self.assertEqual(rows["intra/conversations-isolation"], ((f"{IMPL}.conversations",), tuple(
            f"{IMPL}.{name}" for name in ("inbox", "application", "network", "composition", "links", "threads"))))
        self.assertEqual(rows["intra/user-delivery-isolation"], ((f"{IMPL}.adapters.user_delivery",), (*isolated, f"{IMPL}.threads")))
        self.assertEqual(len(rows), 8)

    def test_allowances_are_pinned(self):
        self.assertEqual({(row.source, row.target) for row in GATE.INTRA_DOMAIN_ALLOWANCES}, set())


    def test_dirty_delivery_only_shrinks(self):
        both = ("orgfs", "cells")
        measured = [
            *((suffix, "application", both) for suffix in ("", ".lark", ".sends", ".status_queries", ".wake")),
            (".lark", "composition", ("cells",)),
            (".sends", "composition", ("cells",)),
            (".sends", "runtime", ("cells",)),
            (".sends", "squire", ("cells",)),
            (".status_queries", "application.actors.agents_admin", both),
            (".status_queries", "application.actors.agents_admin.registry_ops", both),
        ]
        known = {(f"{MESSAGING}{suffix}", f"{IMPL}.{witness}", f"{IMPL}.{forbidden}")
                 for suffix, witness, forbidden_packages in measured for forbidden in forbidden_packages}
        actual = {(row.source, row.witness, forbidden) for row in GATE.DIRTY_DELIVERY for forbidden in row.forbidden}
        self.assertLessEqual(actual, known)
        self.assertTrue(all(row.rule_id == "intra/messaging-delivery-isolation" and row.reason for row in GATE.DIRTY_DELIVERY))


class RealTreeIntraDomainTests(unittest.TestCase):
    """The checked-in product tree satisfies every row, and every allowance is still in use."""

    @classmethod
    def setUpClass(cls):
        cls.report = GATE.scan(REPO)

    def test_real_tree_has_no_intra_domain_violation(self):
        rows = [row for row in self.report["violations"] if row["kind"] == "intra-domain-import"]
        self.assertEqual(rows, [])
        self.assertEqual(self.report["status"], "PASS", self.report["violations"][:5])

    def test_every_dirty_delivery_entry_is_still_needed(self):
        self.assertEqual([row for row in self.report["violations"] if row["kind"] == "stale-intra-dirty-allowance"], [])
        self.assertTrue(GATE.DIRTY_DELIVERY)

    def test_every_allowance_matches_a_real_edge(self):
        edges = {(edge["from"], edge["to"]) for edge in self.report["edges"]}
        for allowance in GATE.INTRA_DOMAIN_ALLOWANCES:
            with self.subTest(allowance=allowance):
                self.assertTrue(any(
                    GATE.under(source, allowance.source) and GATE.under(target, allowance.target)
                    for source, target in edges))

    def test_manysys_leaf_rule_covers_every_sibling_kernel_impl_package(self):
        kernel_impl = REPO / "src/hyprial/kernel/impl"
        siblings = {
            f"hyprial.kernel.impl.{entry.name}"
            for entry in kernel_impl.iterdir()
            if entry.is_dir() and entry.name not in {"manysys", "__pycache__"}
        }
        self.assertTrue(siblings.issubset(set(GATE.KERNEL_NON_MANYSYS)), siblings - set(GATE.KERNEL_NON_MANYSYS))


if __name__ == "__main__":
    unittest.main()
