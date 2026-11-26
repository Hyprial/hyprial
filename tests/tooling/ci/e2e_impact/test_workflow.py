"""The E2E impact checker stays in the tree although no workflow calls it.

Owner decision, 2026-11-10: the PR-side E2E impact declaration step was removed
from the PR hygiene workflow.  ``scripts/ci/check_e2e_impact.py`` itself stays,
with its parser, coverage registry and its own tests, because a periodic E2E run
is where this checker will be reconnected (that wiring is a separate follow-up
task).  The assertions on the PR hygiene workflow's own steps (the base-ref
copy of the gate script, the merge-tree / registry-union / submitted-by rules)
were retired with that workflow file.
"""

from .support import ROOT


def test_the_checker_is_kept_for_the_periodic_e2e_run() -> None:
    assert (ROOT / "scripts/ci/check_e2e_impact.py").is_file()
    assert (ROOT / "contract/e2e/scenarios.json").is_file()
    assert (ROOT / "docs/guides/e2e-impact.md").is_file()
