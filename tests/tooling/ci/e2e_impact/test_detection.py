"""Implementation, helper and registry changes cannot disappear from the plan."""

from pathlib import Path

import pytest

from .support import check, commit, git, registry, save_registry, seed, write


def test_shared_contract_change_selects_multiple_scenarios(tmp_path: Path) -> None:
    repo, base = seed(tmp_path)
    write(repo, "contract/cli/shared.md", "new published behavior\n")
    commit(repo)
    result, report = check(repo, base, plan=True)
    assert result.returncode == 0, report
    assert set(report["affected"]) == {"E2E-001", "E2E-002"}
    assert report["execution"] == "NOT_EVALUATED"
    assert report["merge_base"] == base


def test_product_change_selects_only_declared_consumer(tmp_path: Path) -> None:
    repo, base = seed(tmp_path)
    write(repo, "src/hyprial/daemon/impl/adapter/interface.py", "def invoke(): return 2\n")
    commit(repo)
    result, report = check(repo, base, plan=True)
    assert result.returncode == 0, report
    assert list(report["affected"]) == ["E2E-001"]


@pytest.mark.parametrize("path", ["contract/e2e/run-adapter.py", "contract/e2e/adapter.md", "contract/e2e/_shared.py"])
def test_own_runner_document_and_imported_helper_are_dependencies(tmp_path: Path, path: str) -> None:
    repo, base = seed(tmp_path)
    write(repo, path, (repo / path).read_text() + "\n")
    commit(repo)
    result, report = check(repo, base, plan=True)
    assert result.returncode == 0, report
    expected = {"E2E-001", "E2E-002"} if "_shared" in path else {"E2E-001"}
    assert set(report["affected"]) == expected


def test_transitive_and_dynamic_dedicated_helpers_are_dependencies(tmp_path: Path) -> None:
    repo, _ = seed(tmp_path)
    write(repo, "contract/e2e/run-adapter.py", 'import runpy\nrunpy.run_path("delegate.py")\n')
    write(repo, "contract/e2e/delegate.py", "from _shared import invoke\n")
    base = commit(repo)
    write(repo, "contract/e2e/_shared.py", "def invoke(): return 2\n")
    commit(repo)
    result, report = check(repo, base, plan=True)
    assert result.returncode == 0, report
    assert set(report["affected"]) == {"E2E-001", "E2E-002"}


def test_head_mapping_cannot_erase_base_consumer(tmp_path: Path) -> None:
    repo, base = seed(tmp_path)
    value = registry(repo)
    value["scenarios"][0]["covers"] = ["contract/cli/shared.md"]
    save_registry(repo, value)
    write(repo, "src/hyprial/daemon/impl/adapter/interface.py", "def invoke(): return 2\n")
    commit(repo)
    result, report = check(repo, base, plan=True)
    assert result.returncode == 0, report
    assert "src/hyprial/daemon/impl/adapter/interface.py" in report["affected"]["E2E-001"]["paths"]


@pytest.mark.parametrize("mutation", ["missing", "empty", "stale", "escape", "duplicate", "missing-runner"])
def test_invalid_coverage_mapping_cannot_report_no_impact(tmp_path: Path, mutation: str) -> None:
    repo, base = seed(tmp_path)
    value = registry(repo)
    row = value["scenarios"][0]
    if mutation == "missing":
        del row["covers"]
    elif mutation == "empty":
        row["covers"] = []
    elif mutation == "stale":
        row["covers"] = ["src/hyprial/retired/**"]
    elif mutation == "escape":
        row["covers"] = ["../outside/**"]
    elif mutation == "duplicate":
        value["scenarios"].append(dict(row))
    else:
        (repo / "contract/e2e/run-adapter.py").unlink()
    save_registry(repo, value)
    commit(repo)
    result, report = check(repo, base, plan=True)
    assert result.returncode != 0
    assert report["status"] == "invalid-mapping", report
    assert report["errors"]


def test_diff_uses_complete_branch_range_and_both_rename_paths(tmp_path: Path) -> None:
    repo, base = seed(tmp_path)
    git(repo, "mv", "src/hyprial/daemon/impl/adapter/interface.py", "src/hyprial/daemon/impl/adapter/renamed.py")
    commit(repo)
    write(repo, "README.md", "latest commit only docs\n")
    commit(repo)
    result, report = check(repo, base, plan=True)
    assert result.returncode == 0, report
    paths = report["affected"]["E2E-001"]["paths"]
    assert "src/hyprial/daemon/impl/adapter/interface.py" in paths
    assert "src/hyprial/daemon/impl/adapter/renamed.py" in paths


@pytest.mark.parametrize("path", [
    "src/hyprial/new_protocol.py", ".forgejo/git-flow.json", "scripts/ci/ci-plan.mjs",
    ".forgejo/workflows/gate-gui.yml", "crates/new/src/lib.rs",
])
def test_unknown_product_path_is_not_silently_none(tmp_path: Path, path: str) -> None:
    repo, base = seed(tmp_path)
    write(repo, path, "VALUE = 1\n")
    commit(repo)
    result, report = check(repo, base)
    assert result.returncode != 0
    assert report["unknown"] == [path]
    assert any("E2E-Unknown" in error for error in report["errors"])


def test_bootstrap_is_not_falsely_green(tmp_path: Path) -> None:
    repo, _ = seed(tmp_path)
    value = registry(repo)
    del value["coverage_schema"]
    for row in value["scenarios"]:
        del row["covers"]
    save_registry(repo, value)
    base = commit(repo)
    write(repo, "README.md", "first adoption\n")
    commit(repo)
    result, report = check(repo, base)
    assert result.returncode == 2
    assert report["status"] == "BOOTSTRAP_REVIEW_REQUIRED"
    assert report["execution"] == "NOT_EVALUATED"


def test_documentation_only_change_needs_no_invented_scenario(tmp_path: Path) -> None:
    repo, base = seed(tmp_path)
    write(repo, "docs/guides/readme.md", "prose\n")
    commit(repo)
    result, report = check(repo, base)
    assert result.returncode == 0, report
    assert report["affected"] == {} and report["unknown"] == []
    assert report["status"] == "declaration-complete"
