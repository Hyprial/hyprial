"""Static and fail-closed contracts for the six Python guard tests."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EXPECTED_GUARDS = {
    "tests/kernel/language/test_exception_handler_bindings.py",
    "tests/daemon/harnesses/actors/test_harness_restore_admission.py",
    "tests/shell/compatibility/test_unreachable_surface_retirement.py",
    "tests/tooling/architecture/rename/test_hyprial_rename_contract.py",
    "tests/tooling/isolation/windows/test_windows_suite_harness.py",
    "tests/tooling/refactor/scanning/test_c0_scan.py",
}


def _impact():
    spec = importlib.util.spec_from_file_location(
        "pytest_impact_guards", ROOT / "scripts/ci/pytest_impact.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_guard_constant_is_exact_and_all_files_exist():
    impact = _impact()
    assert set(impact.ALL_PYTHON_GUARDS) == EXPECTED_GUARDS
    assert all((ROOT / path).is_file() for path in impact.ALL_PYTHON_GUARDS)


def test_missing_guard_fails_closed():
    impact = _impact()
    selected, reasons = impact._guard_selection(
        Path("/tmp/selector-guard-fixture"), ["tests/tooling/test_changed.py"], set()
    )
    assert selected == set()
    assert reasons and reasons[0].startswith("Python guard missing:")
