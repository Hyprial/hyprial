"""Periodic-E2E form of the impact check: are the registry assumptions true?

validate-only compares nothing.  It answers the question a scheduled run can
answer without a PR body: does every scenario still name an existing runner and
document, does every covers pattern still match a tracked file, and does every
declared dependency still parse.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from .support import CHECKER, commit, registry, save_registry, seed, write


def validate(repo: Path, *, head: str = "HEAD") -> tuple[subprocess.CompletedProcess, dict]:
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--repo", str(repo), "--head", head, "--validate-only"],
        capture_output=True, text=True, timeout=30,
    )
    try:
        report = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        report = {"stdout": result.stdout, "stderr": result.stderr}
    return result, report


def test_a_true_mapping_holds(tmp_path: Path) -> None:
    repo, _ = seed(tmp_path)
    result, report = validate(repo)
    assert result.returncode == 0, report
    assert report["status"] == "assumptions-hold"
    assert report["mode"] == "validate-only"
    assert report["scenarios"] == 2
    assert report["errors"] == []
    assert report["execution"] == "NOT_EVALUATED"


def test_a_stale_covers_pattern_is_invalid(tmp_path: Path) -> None:
    repo, _ = seed(tmp_path)
    value = registry(repo)
    value["scenarios"][0]["covers"] = ["src/hyprial/daemon/impl/deleted/**"]
    save_registry(repo, value)
    commit(repo)
    result, report = validate(repo)
    assert result.returncode == 1, report
    assert report["status"] == "assumptions-invalid"
    assert any("stale covers pattern" in error for error in report["errors"])


def test_a_missing_runner_is_invalid(tmp_path: Path) -> None:
    repo, _ = seed(tmp_path)
    (repo / "contract/e2e/run-adapter.py").unlink()
    commit(repo)
    result, report = validate(repo)
    assert result.returncode == 1, report
    assert any("missing regular tracked runner" in error for error in report["errors"])


def test_a_missing_document_is_invalid(tmp_path: Path) -> None:
    repo, _ = seed(tmp_path)
    (repo / "contract/e2e/adapter.md").unlink()
    commit(repo)
    result, report = validate(repo)
    assert result.returncode == 1, report
    assert any("missing regular tracked document" in error for error in report["errors"])


def test_a_missing_coverage_schema_is_invalid(tmp_path: Path) -> None:
    repo, _ = seed(tmp_path)
    value = registry(repo)
    value.pop("coverage_schema")
    save_registry(repo, value)
    commit(repo)
    result, report = validate(repo)
    assert result.returncode == 1, report
    assert any("coverage_schema" in error for error in report["errors"])


def test_duplicate_covers_are_invalid(tmp_path: Path) -> None:
    repo, _ = seed(tmp_path)
    value = registry(repo)
    value["scenarios"][0]["covers"] = ["src/hyprial/daemon/impl/adapter/**"] * 2
    save_registry(repo, value)
    commit(repo)
    result, report = validate(repo)
    assert result.returncode == 1, report
    assert any("duplicate covers" in error for error in report["errors"])


def test_an_unparseable_dependency_is_invalid(tmp_path: Path) -> None:
    repo, _ = seed(tmp_path)
    write(repo, "contract/e2e/_shared.py", "def broken(:\n")
    commit(repo)
    result, report = validate(repo)
    assert result.returncode == 1, report
    assert any("cannot inspect scenario/helper dependencies" in error for error in report["errors"])


def test_a_corrupt_registry_json_is_invalid(tmp_path: Path) -> None:
    repo, _ = seed(tmp_path)
    write(repo, "contract/e2e/scenarios.json", "{not json")
    commit(repo)
    result, report = validate(repo)
    assert result.returncode == 1, report
    assert report["status"] == "assumptions-invalid"
    assert any("contract/e2e/scenarios.json" in error for error in report["errors"])


def test_an_unusable_repository_is_an_operational_error(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-repo"
    result, report = validate(missing)
    assert result.returncode == 2, report
    assert report["status"] == "operational-error"
    assert report["errors"]


def test_it_does_not_need_a_base(tmp_path: Path) -> None:
    repo, _ = seed(tmp_path)
    bare = subprocess.run(
        [sys.executable, str(CHECKER), "--repo", str(repo), "--validate-only"],
        capture_output=True, text=True, timeout=30,
    )
    assert bare.returncode == 0, bare.stdout + bare.stderr
    assert json.loads(bare.stdout)["status"] == "assumptions-hold"


def test_the_real_checkout_holds() -> None:
    repo = Path(__file__).resolve().parents[4]
    result, report = validate(repo)
    assert result.returncode == 0, report
    assert report["status"] == "assumptions-hold"
    assert report["scenarios"] >= 25
