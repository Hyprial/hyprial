"""Negative tests for the product and test structure completion/CI gate."""

import importlib.util
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[4] / 'scripts/ci/check_structural_limits.py'
SPEC = importlib.util.spec_from_file_location("structural_limits", SCRIPT)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)
NATIVE_SHELL_ROOT = "clients/native"


class StructuralLimitsTests(unittest.TestCase):
    def tree(self):
        directory = tempfile.TemporaryDirectory(prefix="hyprial-structure-gate-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for path in GATE.PRODUCT_ROOTS:
            (root / path).mkdir(parents=True, exist_ok=True)
        (root / "tests").mkdir()
        (root / "src/hyprial/__init__.py").write_text("\n")
        return root

    def test_optional_roots_may_be_absent(self):
        directory = tempfile.TemporaryDirectory(prefix="hyprial-structure-gate-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "src/hyprial").mkdir(parents=True)
        (root / "src/hyprial/__init__.py").write_text("\n")
        (root / "tests").mkdir()
        self.assertEqual(GATE.scan(root)["violations"], [])

    def test_exact_limits_pass(self):
        root = self.tree()
        module = root / "src/hyprial/kernel"
        module.mkdir()
        for index in range(7):
            (module / f"part{index}.py").write_text("# line\n" * 1000)
        self.assertEqual(GATE.scan(root)["violations"], [])

    def test_eighth_direct_file_fails_even_if_it_is_readme(self):
        root = self.tree()
        module = root / "src/hyprial/studio"
        module.mkdir(parents=True)
        for index in range(7):
            (module / f"part{index}.mjs").write_text("export {};\n")
        (module / "README.md").write_text("Module documentation\n")
        self.assertTrue(any(row["kind"] == "module-file-count" for row in GATE.scan(root)["violations"]))

    def test_1001_lines_fails_without_final_newline(self):
        root = self.tree()
        (root / "src/hyprial/main.cjs").write_text("// line\n" * 1000 + "// last")
        self.assertTrue(any(row["kind"] == "code-line-count" for row in GATE.scan(root)["violations"]))

    def test_directory_count_is_direct_not_recursive(self):
        root = self.tree()
        for index in range(9):
            module = root / f"src/hyprial/group{index}"
            module.mkdir()
            (module / "__init__.py").write_text("\n")
        self.assertEqual(GATE.scan(root)["violations"], [])

    def test_data_only_directory_is_not_a_code_module(self):
        root = self.tree()
        fixtures = root / "src/hyprial/session/fixtures"
        fixtures.mkdir(parents=True)
        for index in range(10):
            (fixtures / f"case{index}.json").write_text("{}\n")
        self.assertEqual(GATE.scan(root)["violations"], [])

    def test_product_roots_and_missing_sources_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="hyprial-empty-structure-") as raw:
            self.assertTrue(GATE.scan(Path(raw))["violations"])

    def test_native_shell_is_the_only_optional_product_root(self):
        root = self.tree()
        report = GATE.scan(root)
        self.assertEqual(report["violations"], [], report)
        self.assertEqual(report["productRoots"], ("src/hyprial",), report)
        self.assertEqual(
            set(report["optionalProductRoots"]),
            {NATIVE_SHELL_ROOT},
            report,
        )
        self.assertNotIn("legacyProductRoots", report)

    def test_missing_src_hyprial_still_fails_with_only_the_native_root_optional(self):
        root = self.tree()
        (root / "src/hyprial").rename(root / "src/hyprial-moved")
        self.assertTrue(any(
            row["kind"] == "missing-product-root" and row["path"] == "src/hyprial"
            for row in GATE.scan(root)["violations"]
        ))

    def test_symlink_product_code_is_rejected(self):
        root = self.tree()
        (root / "src/hyprial/linked.cjs").symlink_to(root / "src/hyprial/not-found.py")
        self.assertTrue(any(row["kind"] == "source-symlink" for row in GATE.scan(root)["violations"]))

    def test_renaming_first_party_module_vendor_does_not_hide_it(self):
        root = self.tree()
        module = root / "src/hyprial/kernel/vendor"
        module.mkdir(parents=True)
        (module / "oversized.py").write_text("# line\n" * 1001)
        self.assertTrue(any(row["kind"] == "code-line-count" for row in GATE.scan(root)["violations"]))

    def test_optional_tauri_rust_code_is_scanned(self):
        root = self.tree()
        module = root / "clients/native/src-tauri/src/runtime"
        module.mkdir(parents=True)
        (module / "gui.rs").write_text("// line\n" * 1001)
        self.assertTrue(any(row["path"].endswith("gui.rs") and row["kind"] == "code-line-count" for row in GATE.scan(root)["violations"]))

    def test_tauri_build_script_is_not_outside_the_gate(self):
        root = self.tree()
        module = root / "clients/native/src-tauri"
        module.mkdir(parents=True)
        (module / "build.rs").write_text("// line\n" * 1001)
        self.assertTrue(any(row["path"].endswith("build.rs") and row["kind"] == "code-line-count" for row in GATE.scan(root)["violations"]))

    def test_tauri_rust_module_counts_all_direct_regular_files(self):
        root = self.tree()
        module = root / "clients/native/src-tauri/src"
        module.mkdir(parents=True)
        for index in range(7):
            (module / f"part{index}.rs").write_text("fn main() {}\n")
        (module / "README.md").write_text("Documentation\n")
        self.assertTrue(any(row["path"] == "clients/native/src-tauri/src" and row["kind"] == "module-file-count" for row in GATE.scan(root)["violations"]))

    def test_only_pinned_tauri_generated_roots_are_excluded(self):
        root = self.tree()
        generated = root / "clients/native/src-tauri/target"
        generated.mkdir(parents=True)
        (generated / "generated.rs").write_text("// line\n" * 1001)
        self.assertEqual(GATE.scan(root)["violations"], [])
        source = root / "clients/native/src-tauri/src/target"
        source.mkdir(parents=True)
        (source / "first_party.rs").write_text("// line\n" * 1001)
        self.assertTrue(any(row["path"].endswith("first_party.rs") for row in GATE.scan(root)["violations"]))

    def test_native_shell_launcher_eighth_file_fails(self):
        root = self.tree()
        module = root / NATIVE_SHELL_ROOT / "launcher/src"
        module.mkdir(parents=True)
        for index in range(8):
            (module / f"part{index}.rs").write_text("fn main() {}\n")
        self.assertTrue(any(row["path"] == "clients/native/launcher/src" and row["kind"] == "module-file-count" for row in GATE.scan(root)["violations"]))

    def test_native_shell_is_optional_and_seven_launcher_files_pass(self):
        root = self.tree()
        self.assertEqual(GATE.scan(root)["violations"], [])
        module = root / NATIVE_SHELL_ROOT / "launcher/src"
        module.mkdir(parents=True)
        for index in range(7):
            (module / f"part{index}.rs").write_text("fn main() {}\n")
        self.assertEqual(GATE.scan(root)["violations"], [])

    def test_native_shell_build_payloads_are_excluded(self):
        root = self.tree()
        for relative in ("target/debug", "src-tauri/target/release", "src-tauri/gen/schemas", "src-tauri/runtime/lib"):
            payload = root / NATIVE_SHELL_ROOT / relative
            payload.mkdir(parents=True)
            (payload / "generated.rs").write_text("// line\n" * 1001)
            for index in range(8):
                (payload / f"part{index}.py").write_text("\n")
        self.assertEqual(GATE.scan(root)["violations"], [])

    def test_native_shell_payload_names_do_not_hide_first_party_code(self):
        root = self.tree()
        for relative in ("src-tauri/src/gen", "launcher/runtime", "launcher/src/target"):
            source = root / NATIVE_SHELL_ROOT / relative
            source.mkdir(parents=True)
            (source / "first_party.rs").write_text("// line\n" * 1001)
        failing = {row["path"] for row in GATE.scan(root)["violations"] if row["kind"] == "code-line-count"}
        self.assertEqual(
            failing,
            {f"{NATIVE_SHELL_ROOT}/{relative}/first_party.rs" for relative in ("src-tauri/src/gen", "launcher/runtime", "launcher/src/target")},
        )

    def test_ci_gate_also_runs_for_gui_and_desktop_only_changes(self):
        workflow = (SCRIPT.parents[2] / ".github/workflows/gate-python.yml").read_text()
        block = workflow.split("- name: Enforce product module and file size limits", 1)[1].split("- name:", 1)[0]
        self.assertNotIn("scope.outputs.python", block)
        self.assertNotIn("scope.outputs.unit", block)
        self.assertIn("uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py", block)
        self.assertIn("uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py", block)
        self.assertIn("uv run --no-project --python 3.12 python scripts/ci/check_network_reachability.py", block)
        self.assertLess(workflow.index("Enforce product module"), workflow.index("Install Python and locked dependencies"))

    def test_test_modules_are_not_outside_the_size_gate(self):
        root = self.tree()
        (root / "tests/test_oversized.py").write_text("# line\n" * 1001)
        self.assertTrue(any(row["path"] == "tests/test_oversized.py" and row["kind"] == "code-line-count" for row in GATE.scan(root)["violations"]))

    def test_test_helpers_are_scanned_as_code(self):
        root = self.tree()
        helpers = root / "tests/support"
        helpers.mkdir()
        (helpers / "helper.py").write_text("# line\n" * 1001)
        self.assertTrue(any(row["path"] == "tests/support/helper.py" and row["kind"] == "code-line-count" for row in GATE.scan(root)["violations"]))

    def test_test_modules_count_readme_and_data_toward_seven(self):
        root = self.tree()
        module = root / "tests/runtime"
        module.mkdir()
        (module / "test_runtime.py").write_text("\n")
        for index in range(7):
            (module / f"fixture{index}.json").write_text("{}\n")
        self.assertTrue(any(row["path"] == "tests/runtime" and row["kind"] == "module-file-count" for row in GATE.scan(root)["violations"]))

    def test_missing_tests_root_fails_closed(self):
        root = self.tree()
        (root / "tests").rmdir()
        self.assertTrue(any(row["path"] == "tests" and row["kind"] == "missing-test-root" for row in GATE.scan(root)["violations"]))

    def test_symlink_tests_root_fails_closed(self):
        root = self.tree()
        (root / "tests").rmdir()
        (root / "tests").symlink_to(root / "src/hyprial", target_is_directory=True)
        self.assertTrue(any(row["path"] == "tests" and row["kind"] == "missing-test-root" for row in GATE.scan(root)["violations"]))

    def test_first_party_go_fixture_is_code(self):
        root = self.tree()
        fixtures = root / "tests/fixtures"
        fixtures.mkdir()
        (fixtures / "worker.go").write_text("// line\n" * 1001)
        self.assertTrue(any(row["path"] == "tests/fixtures/worker.go" and row["kind"] == "code-line-count" for row in GATE.scan(root)["violations"]))

    def test_active_tool_scripts_obey_code_line_limit(self):
        root = self.tree()
        module = root / "scripts/ci"
        module.mkdir(parents=True)
        (module / "tool.py").write_text("# line\n" * 1001)
        self.assertTrue(any(row["path"] == "scripts/ci/tool.py" and row["kind"] == "code-line-count" for row in GATE.scan(root)["violations"]))

    def test_active_contract_runners_obey_code_line_limit(self):
        root = self.tree()
        module = root / "contract/e2e"
        module.mkdir(parents=True)
        (module / "run.sh").write_text("# line\n" * 1001)
        self.assertTrue(any(row["path"] == "contract/e2e/run.sh" and row["kind"] == "code-line-count" for row in GATE.scan(root)["violations"]))

    def test_first_party_sidecar_code_obeys_code_line_limit(self):
        root = self.tree()
        module = root / "sidecar/worker"
        module.mkdir(parents=True)
        (module / "main.go").write_text("// line\n" * 1001)
        self.assertTrue(any(row["path"] == "sidecar/worker/main.go" and row["kind"] == "code-line-count" for row in GATE.scan(root)["violations"]))

    def test_go_sidecar_packages_count_readme_toward_seven(self):
        root = self.tree()
        package = root / "sidecar/hyprial-tailcat/internal/proto"
        package.mkdir(parents=True)
        for index in range(7):
            (package / f"part{index}.go").write_text("package proto\n")
        (package / "README.md").write_text("Protocol documentation\n")
        self.assertTrue(any(row["path"] == "sidecar/hyprial-tailcat/internal/proto"
                            and row["kind"] == "module-file-count"
                            for row in GATE.scan(root)["violations"]))

    def test_go_sidecar_seven_files_pass_and_are_counted_once(self):
        root = self.tree()
        package = root / "sidecar/hyprial-tailcat/cmd/hyprial-tailcat"
        package.mkdir(parents=True)
        (package / "main.go").write_text("package main\n" * 1000)
        for index in range(6):
            (package / f"fixture{index}.json").write_text("{}\n")
        report = GATE.scan(root)
        self.assertEqual(report["violations"], [])
        self.assertEqual(report["modulesChecked"], 2)
        self.assertEqual(report["filesChecked"], 2)

    def test_sidecar_cli_collection_is_not_a_go_package(self):
        root = self.tree()
        runners = root / "sidecar/hyprial-tailcat/e2e"
        runners.mkdir(parents=True)
        for index in range(8):
            (runners / f"run{index}.py").write_text("# independent runner\n")
        self.assertEqual(GATE.scan(root)["violations"], [])

    def test_go_sidecar_package_cannot_hide_under_vendor_name(self):
        root = self.tree()
        package = root / "sidecar/hyprial-tailcat/internal/vendor"
        package.mkdir(parents=True)
        for index in range(8):
            (package / f"part{index}.go").write_text("package vendor\n")
        self.assertTrue(any(row["path"] == "sidecar/hyprial-tailcat/internal/vendor"
                            and row["kind"] == "module-file-count"
                            for row in GATE.scan(root)["violations"]))

    def test_only_exact_historical_script_archive_is_outside_active_gate(self):
        root = self.tree()
        historical = root / "scripts/archived"
        historical.mkdir(parents=True)
        (historical / "old.py").write_text("# line\n" * 1001)
        self.assertEqual(GATE.scan(root)["violations"], [])
        active = root / "scripts/ci/archived"
        active.mkdir(parents=True)
        (active / "new.py").write_text("# line\n" * 1001)
        self.assertTrue(any(row["path"] == "scripts/ci/archived/new.py" and row["kind"] == "code-line-count" for row in GATE.scan(root)["violations"]))

    def test_command_collections_are_not_product_module_directories(self):
        root = self.tree()
        commands = root / "scripts/ci"
        commands.mkdir(parents=True)
        for index in range(8):
            (commands / f"command{index}.py").write_text("# independent CLI entry\n")
        self.assertEqual(GATE.scan(root)["violations"], [])


if __name__ == "__main__":
    unittest.main()
