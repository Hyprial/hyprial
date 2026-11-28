"""Contracts for the fail-closed pytest impact selector and shard planner."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
GUARDS = (
    "tests/kernel/language/test_exception_handler_bindings.py",
    "tests/daemon/harnesses/actors/test_harness_restore_admission.py",
    "tests/shell/compatibility/test_unreachable_surface_retirement.py",
    "tests/tooling/architecture/rename/test_hyprial_rename_contract.py",
    "tests/tooling/isolation/windows/test_windows_suite_harness.py",
    "tests/tooling/refactor/scanning/test_c0_scan.py",
)


def _impact():
    path = ROOT / "scripts" / "ci" / "pytest_impact.py"
    spec = importlib.util.spec_from_file_location("pytest_impact_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()


def write(root: Path, path: str, content: str = "def test_fixture(): pass\n") -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def commit(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-m", message)
    return git(root, "rev-parse", "HEAD")


def merge_fixture(
    tmp_path: Path,
    head_files: dict[str, str] | None = None,
    base_files: dict[str, str] | None = None,
) -> tuple[Path, str, str]:
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    git(tmp_path, "config", "user.name", "Impact fixture")
    write(tmp_path, "tests/tooling/test_existing.py")
    write(tmp_path, "pyproject.toml", "[project]\nname = 'fixture'\n")
    write(tmp_path, "GLOSSARY.md", "# Fixture\n")
    for guard in GUARDS:
        write(tmp_path, guard)
    for path, content in (base_files or {}).items():
        write(tmp_path, path, content)
    base = commit(tmp_path, "base")
    git(tmp_path, "checkout", "-b", "pull-request")
    for path, content in (head_files or {"tests/tooling/test_changed.py": "def test_changed(): pass\n"}).items():
        write(tmp_path, path, content)
    head = commit(tmp_path, "head")
    git(tmp_path, "checkout", "main")
    git(tmp_path, "merge", "--no-ff", "pull-request", "-m", "merge")
    return tmp_path, base, head


def event(head: str) -> dict[str, object]:
    return {
        "pull_request": {
            "base": {"ref": "dev"},
            "head": {"sha": head},
        }
    }


def test_pull_request_diff_requires_the_checked_out_two_parent_merge(tmp_path):
    impact = _impact()
    root, _base, head = merge_fixture(tmp_path)
    comparison = impact.docs_only.pull_request_diff(root, event(head), "pull_request")
    assert comparison.head_sha == head
    assert comparison.paths == ("tests/tooling/test_changed.py",)
    assert comparison.parent_sha != comparison.head_sha


def test_source_changes_run_the_full_suite(tmp_path):
    impact = _impact()
    root, _base, head = merge_fixture(
        tmp_path, {"src/hyprial/example.py": "VALUE = 1\n"}
    )
    result = impact.plan(root, event(head), "pull_request")
    assert result["mode"] == "full"
    assert "product" in " ".join(result["reasons"])


def test_unreferenced_docs_select_tests_tooling(tmp_path):
    impact = _impact()
    root, _base, head = merge_fixture(
        tmp_path, {"docs/guides/new-page.md": "new page\n"}
    )
    result = impact.plan(root, event(head), "pull_request")
    assert result["mode"] == "selected"
    assert result["kind"] == "docs-only"
    assert "tests/tooling/test_existing.py" in result["tests"]
    assert all(path.startswith("tests/tooling/") for path in result["tests"])


def test_referenced_docs_fail_closed_to_full(tmp_path):
    impact = _impact()
    root, _base, head = merge_fixture(
        tmp_path,
        {
            "tests/tooling/test_existing.py": "DOC = 'docs/guides/used.md'\n",
            "docs/guides/used.md": "used\n",
        },
    )
    result = impact.plan(root, event(head), "pull_request")
    assert result["mode"] == "full"
    assert "runtime reference" in " ".join(result["reasons"])


def test_all_python_changes_add_the_six_guard_files(tmp_path):
    impact = _impact()
    root, _base, head = merge_fixture(
        tmp_path, {"tests/tooling/test_changed.py": "VALUE = 2\n"}
    )
    result = impact.plan(root, event(head), "pull_request")
    assert result["mode"] == "selected"
    assert set(GUARDS) <= set(result["tests"])


def test_changed_test_helper_selects_cross_domain_reverse_references(tmp_path):
    impact = _impact()
    root, _base, head = merge_fixture(
        tmp_path,
        {
            "tests/biz/routines/teleport/support.py": "VALUE = 2\n",
            "tests/daemon/teleport/wiring/test_routine_wiring.py": (
                "from tests.biz.routines.teleport import support\n"
                "def test_wiring(): assert support.VALUE == 2\n"
            ),
        },
    )
    result = impact.plan(root, event(head), "pull_request")
    assert result["mode"] == "selected"
    assert "tests/daemon/teleport/wiring/test_routine_wiring.py" in result["tests"]


def test_changed_test_data_selects_cross_domain_reverse_references(tmp_path):
    impact = _impact()
    root, _base, head = merge_fixture(
        tmp_path,
        {
            "tests/biz/routines/teleport/data.json": '{"value": 2}\n',
            "tests/daemon/teleport/wiring/reader.py": (
                "from pathlib import Path\n"
                "def read_data(): return Path('tests/biz/routines/teleport/data.json').is_file()\n"
            ),
            "tests/shell/teleport/test_routine_wiring.py": (
                "from tests.daemon.teleport.wiring.reader import read_data\n"
                "def test_wiring(): assert read_data()\n"
            ),
        },
    )
    result = impact.plan(root, event(head), "pull_request")
    assert result["mode"] == "selected"
    assert "tests/shell/teleport/test_routine_wiring.py" in result["tests"]


def test_changed_helper_closes_over_real_callers_across_multiple_domains(tmp_path):
    impact = _impact()
    root, _base, head = merge_fixture(
        tmp_path,
        {"tests/biz/shared/support.py": "VALUE = 2\n"},
        {
            "tests/biz/shared/support.py": "VALUE = 1\n",
            "tests/daemon/shared/adapter.py": (
                "from tests.biz.shared.support import VALUE\n"
                "def read_value(): return VALUE\n"
            ),
            "tests/shell/real/test_consumer.py": (
                "from tests.daemon.shared.adapter import read_value\n"
                "def test_consumer(): assert read_value() == 2\n"
            ),
        },
    )
    result = impact.plan(root, event(head), "pull_request")
    assert result["mode"] == "selected"
    assert "tests/shell/real/test_consumer.py" in result["tests"]


@pytest.mark.parametrize(
    "helper",
    [
        "VALUE = __import__(name)\n",
        "def broken(:\n",
    ],
)
def test_unresolvable_changed_python_input_fails_closed(tmp_path, helper):
    impact = _impact()
    root, _base, head = merge_fixture(
        tmp_path,
        {"tests/biz/shared/support.py": helper},
        {"tests/biz/shared/support.py": "VALUE = 1\n"},
    )
    result = impact.plan(root, event(head), "pull_request")
    assert result["mode"] == "full"
    assert "comparison unavailable" in " ".join(result["reasons"])


def test_unreadable_changed_python_input_fails_closed(tmp_path, monkeypatch):
    impact = _impact()
    root, _base, head = merge_fixture(
        tmp_path,
        {"tests/biz/shared/support.py": "VALUE = 2\n"},
        {"tests/biz/shared/support.py": "VALUE = 1\n"},
    )
    original_read_text = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == root / "tests" / "tooling" / "test_existing.py":
            raise OSError("fixture is unreadable")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    result = impact.plan(root, event(head), "pull_request")
    assert result["mode"] == "full"
    assert "comparison unavailable" in " ".join(result["reasons"])


def test_invalid_event_and_non_pr_events_run_full(tmp_path):
    impact = _impact()
    root, _base, _head = merge_fixture(tmp_path)
    assert impact.plan(root, {}, "pull_request")["mode"] == "full"
    assert impact.plan(root, event("0" * 40), "pull_request")["mode"] == "full"
    assert impact.plan(root, event("f" * 40), "push")["mode"] == "full"


def test_shard_planner_puts_small_selected_sets_in_shard_one():
    impact = _impact()
    selected = [f"tests/tooling/test_{index}.py" for index in range(16)]
    assert impact._shard_paths(selected, 1, 4, "selected") == selected
    assert impact._shard_paths(selected, 2, 4, "selected") == []


def test_shard_planner_uses_zero_based_round_robin_for_large_sets():
    impact = _impact()
    selected = [f"tests/tooling/test_{index}.py" for index in range(17)]
    assert impact._shard_paths(selected, 1, 4, "selected") == selected[0::4]
    assert impact._shard_paths(selected, 4, 4, "selected") == selected[3::4]


def test_emit_shard_preserves_empty_selected_shards(tmp_path):
    impact = _impact()
    output = tmp_path / "shard.txt"
    meta = tmp_path / "meta.env"
    decision = {
        "mode": "selected",
        "kind": "docs-only",
        "tests": ["tests/tooling/test_one.py"],
    }
    assert impact.emit_shard(tmp_path, decision, 2, 4, output, meta) == 0
    assert output.read_text(encoding="utf-8") == ""
    assert "SHARD_COUNT=0" in meta.read_text(encoding="utf-8")


def test_parse_shard_rejects_invalid_ranges():
    impact = _impact()
    for value in ("0/4", "2/1", "1/17", "one/4"):
        with pytest.raises(Exception):
            impact.parse_shard(value)


def test_full_metadata_rejects_an_empty_shard(tmp_path):
    impact = _impact()
    git(tmp_path, "init")
    decision = {"mode": "full", "kind": "full", "tests": ["tests/"]}
    with pytest.raises(ValueError, match="selected test set is empty"):
        impact.emit_shard(tmp_path, decision, 1, 4, tmp_path / "out", tmp_path / "meta")
