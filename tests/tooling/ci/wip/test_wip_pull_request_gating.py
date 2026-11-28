"""WIP pull requests: the WIP rule itself lives in one place, never in YAML.

Owner 2026-11-15 (CI load). The decision is ``wipDecision`` in
scripts/ci/ci-plan.mjs (unit-tested in scripts/ci/ci-plan.test.mjs).  The
workflows that once consumed it under WIP-aware job conditions were retired;
what stays a repository contract is that no workflow grows a second copy of
the rule and that the planner keeps exactly one definition of the prefix.
"""
from __future__ import annotations

from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[4] / ".github/workflows"
PLANNER = WORKFLOWS.parents[1] / "scripts/ci/ci-plan.mjs"


def test_the_wip_rule_is_not_copied_into_workflow_yaml() -> None:
    paths = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    assert (WORKFLOWS / "gate.yml") in paths, paths
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "pull_request.title" not in text, path.name
        assert "'WIP:'" not in text and '"WIP:"' not in text, path.name
    planner = PLANNER.read_text(encoding="utf-8")
    assert planner.count("wipPrefix = 'WIP:'") == 1
