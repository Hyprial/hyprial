"""PR CI no longer requires an E2E disposition; the checker is kept for periodic E2E.

Owner decision, 2026-11-10: the PR-side E2E impact declaration step is removed
from ``gate-pr-hygiene``.  ``scripts/ci/check_e2e_impact.py`` itself stays, with
its parser, coverage registry and its own tests, because the periodic E2E run is
where this checker will be reconnected (that wiring is a separate follow-up task).

These tests therefore assert absence, not execution: the workflow must not call
the checker, and the three base-owned hygiene rules that shared the job must
still be wired.
"""

from .support import ROOT


def test_pr_hygiene_no_longer_calls_the_e2e_impact_checker() -> None:
    text = (ROOT / ".forgejo/workflows/gate-pr-hygiene.yml").read_text()
    assert "check_e2e_impact.py" not in text
    assert "e2e-impact" not in text


def test_the_checker_is_kept_for_the_periodic_e2e_run() -> None:
    assert (ROOT / "scripts/ci/check_e2e_impact.py").is_file()
    assert (ROOT / "contract/e2e/scenarios.json").is_file()
    assert (ROOT / "docs/guides/e2e-impact.md").is_file()


def test_original_base_owned_hygiene_rules_remain() -> None:
    text = (ROOT / ".forgejo/workflows/gate-pr-hygiene.yml").read_text()
    for required in (
        'git show "${ref}:${gate}" >"${gate}"',
        "check_pr_hygiene.py \\\n            submitted-by",
        "check_pr_hygiene.py \\\n            merge-tree",
        "check_pr_hygiene.py \\\n            registry-union",
    ):
        assert required in text
