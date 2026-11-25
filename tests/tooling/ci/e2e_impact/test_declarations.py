"""Impact disposition is explicit and never stands in for execution evidence."""

from pathlib import Path

import pytest

from .support import check, commit, registry, save_registry, seed, write

UNCHANGED = "unchanged: reason=the public reply envelope is identical; validation=run the original scenario on the candidate SHA"


def product_change(repo: Path) -> None:
    write(repo, "src/hyprial/daemon/impl/adapter/interface.py", "def invoke(): return 2\n")
    commit(repo)


def test_missing_declaration_fails_and_concrete_unchanged_disposition_passes(tmp_path: Path) -> None:
    repo, base = seed(tmp_path)
    product_change(repo)
    red, report = check(repo, base)
    assert red.returncode == 1
    assert any("E2E-001" in error for error in report["errors"])
    green, report = check(repo, base, f"E2E-Impact: E2E-001 {UNCHANGED}\n")
    assert green.returncode == 0, report
    assert report["status"] == "declaration-complete"
    assert report["execution"] == "NOT_EVALUATED"


def test_updated_requires_a_corresponding_scenario_change(tmp_path: Path) -> None:
    repo, base = seed(tmp_path)
    product_change(repo)
    body = "E2E-Impact: E2E-001 updated: align the CLI assertion\n"
    red, report = check(repo, base, body)
    assert red.returncode == 1 and any("corresponding" in error for error in report["errors"])
    write(repo, "contract/e2e/run-adapter.py", "from _shared import invoke\nassert invoke() == 2\n")
    commit(repo)
    green, report = check(repo, base, body)
    assert green.returncode == 0, report


def test_changing_only_covers_does_not_count_as_updated(tmp_path: Path) -> None:
    repo, base = seed(tmp_path)
    value = registry(repo)
    value["scenarios"][0]["covers"].append("contract/e2e/_shared.py")
    save_registry(repo, value)
    commit(repo)
    result, report = check(repo, base, "E2E-Impact: E2E-001 updated: refreshed covers\n")
    assert result.returncode == 1
    assert any("corresponding" in error for error in report["errors"])


def test_unaffected_scenario_cannot_claim_an_unsupported_update(tmp_path: Path) -> None:
    repo, base = seed(tmp_path)
    write(repo, "README.md", "documentation only\n")
    commit(repo)
    result, report = check(repo, base, "E2E-Impact: E2E-002 updated: refreshed scenario\n")
    assert result.returncode == 1
    assert any("corresponding" in error for error in report["errors"])


@pytest.mark.parametrize("declaration", [
    "unchanged: no changes",
    "unchanged: reason=same contract",
    "unchanged: validation=will run original",
    "unchanged: reason=same; validation=PASS https://example.invalid/green",
    "unchanged: reason=same; validation=https://example.invalid/run",
    "not-applicable: internal helper only",
    "updated:",
])
def test_incomplete_or_self_reported_verdict_is_not_validation(tmp_path: Path, declaration: str) -> None:
    repo, base = seed(tmp_path)
    product_change(repo)
    result, report = check(repo, base, f"E2E-Impact: E2E-001 {declaration}\n")
    assert result.returncode == 1, report


def test_not_applicable_is_an_explicit_review_item(tmp_path: Path) -> None:
    repo, base = seed(tmp_path)
    product_change(repo)
    result, report = check(repo, base, "E2E-Impact: E2E-001 not-applicable: error-only diagnostic; requires-review\n")
    assert result.returncode == 0, report
    assert report["review_required"] == ["E2E-001"]
    assert report["execution"] == "NOT_EVALUATED"


def test_unknown_requires_exact_path_and_review_reason(tmp_path: Path) -> None:
    repo, base = seed(tmp_path)
    path = "src/hyprial/new_protocol.py"
    write(repo, path, "VALUE = 1\n")
    commit(repo)
    for body in ("E2E-Unknown: *: no scenarios; requires-review", f"E2E-Unknown: {path}: no scenarios"):
        red, report = check(repo, base, body)
        assert red.returncode == 1, report
    green, report = check(repo, base, f"E2E-Unknown: {path}: no existing protocol scenario covers this new endpoint; requires-review\n")
    assert green.returncode == 0, report
    assert report["review_required"] == [path]


@pytest.mark.parametrize("kind", ["duplicate", "unknown-id", "commented-out", "in-code-block"])
def test_unusable_declarations_do_not_satisfy_gate(tmp_path: Path, kind: str) -> None:
    repo, base = seed(tmp_path)
    product_change(repo)
    valid = f"E2E-Impact: E2E-001 {UNCHANGED}"
    bodies = {
        "duplicate": valid + "\n" + valid,
        "unknown-id": valid.replace("E2E-001", "E2E-999"),
        "commented-out": f"<!--\n{valid}\n-->",
        "in-code-block": f"```text\n{valid}\n```",
    }
    result, report = check(repo, base, bodies[kind])
    assert result.returncode == 1, report
