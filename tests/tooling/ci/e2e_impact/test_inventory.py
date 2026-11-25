"""The repository registry maps stable boundaries without selecting everything."""

from fnmatch import fnmatchcase
import importlib.util
import json

from .support import CHECKER, REGISTRY, ROOT


def checker():
    spec = importlib.util.spec_from_file_location("e2e_impact_inventory", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_real_scenario_has_valid_coverage_and_automatic_dependencies() -> None:
    module = checker()
    snapshot = module.Snapshot(ROOT, "HEAD")
    value = json.loads((ROOT / REGISTRY).read_text())
    mapping = module.coverage(snapshot, value)
    assert len(mapping) == 26
    for identifier, entry in mapping.items():
        row = entry["row"]
        assert f"contract/e2e/{row['runner']}" in entry["dependencies"], identifier
        assert f"contract/e2e/{row['document']}" in entry["dependencies"], identifier
        assert not any(pattern in {"src/**", "src/hyprial/**", "contract/**", "**"} for pattern in entry["patterns"])
    assert "contract/e2e/_harness_lifecycle/scenario.py" in mapping["E2E-013"]["dependencies"]
    assert "contract/e2e/run-pac-workflow-unified.py" in mapping["E2E-015"]["dependencies"]
    assert "contract/e2e/run-pac-public-events.py" in mapping["E2E-020"]["dependencies"]
    assert "contract/e2e/_gui_browser/session.py" in mapping["E2E-032"]["dependencies"]


def test_real_boundary_selection_is_specific() -> None:
    rows = json.loads((ROOT / REGISTRY).read_text())["scenarios"]
    examples = {
        "src/hyprial/shell/impl/cli/commands/message/outbox.py": {"E2E-001", "E2E-017"},
        "src/hyprial/biz/impl/routine/execution/service.py": {"E2E-016"},
        "src/hyprial/daemon/impl/pac/views/changed.py": {"E2E-015", "E2E-016", "E2E-018", "E2E-020"},
        "sidecar/hyprial-tailcat/internal/membership/runtime.go": {"E2E-001", "E2E-025", "E2E-029", "E2E-030"},
    }
    for path, expected in examples.items():
        actual = {row["id"] for row in rows if any(fnmatchcase(path, pattern) for pattern in row["covers"])}
        assert actual == expected, (path, actual)
