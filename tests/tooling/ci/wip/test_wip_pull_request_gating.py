"""WIP pull requests run only the light checks; the WIP rule lives in one place.

Owner 2026-11-15 (CI load). The decision is `wipDecision` in
scripts/ci/ci-plan.mjs (unit-tested in scripts/ci/ci-plan.test.mjs). These
tests pin how the workflows consume it: the `edited` trigger that lets the full
set run when the prefix is removed, the job conditions that skip the heavy jobs
on a WIP plan without ever skipping them when the plan failed, and the absence
of a second copy of the rule in YAML.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = ROOT / ".github/workflows"
WIP_WORKFLOWS = ("gate-python", "gate-gui", "native-conformance", "mock-model-harness-e2e")
NOT_WIP = "needs.cli-help-inventory.outputs.wip != 'true'"


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / f"{name}.yml").read_text(encoding="utf-8"))


def triggers(document: dict) -> dict:
    return document.get("on", document.get(True))


@pytest.mark.parametrize("name", WIP_WORKFLOWS)
def test_title_edits_start_the_workflow_so_removing_wip_needs_no_push(name: str) -> None:
    types = triggers(load(name))["pull_request"]["types"]
    assert {"opened", "synchronize", "reopened", "edited"} <= set(types)
    assert "ready_for_review" not in types and "converted_to_draft" not in types


def test_gate_python_skips_checks_and_unit_on_a_wip_plan_but_not_on_a_failed_plan() -> None:
    jobs = load("gate-python")["jobs"]
    expected = f"always() && {NOT_WIP}"
    assert jobs["checks"]["if"] == expected
    assert jobs["unit"]["if"] == expected
    assert jobs["cli-help-inventory"]["outputs"]["wip"] == "${{ steps.scope.outputs.wip }}"
    for name in ("pytest-shard", "tailcat-go"):
        assert "outputs.wip" not in jobs[name]["if"]
        assert jobs[name]["if"].startswith("always() && needs.cli-help-inventory.result == 'success'")


def test_gate_python_light_run_and_full_run_share_one_cancelling_group() -> None:
    concurrency = load("gate-python")["concurrency"]
    assert "github.event.action" not in concurrency["group"]
    assert "github.event.pull_request.number" in concurrency["group"]
    assert "github.event_name == 'pull_request'" in concurrency["cancel-in-progress"]


def test_mock_model_harness_needs_a_wip_plan_job_and_skips_when_it_says_wip() -> None:
    jobs = load("mock-model-harness-e2e")["jobs"]
    assert set(jobs) == {"plan", "hosted"}
    assert jobs["plan"]["outputs"] == {"wip": "${{ steps.wip.outputs.wip }}"}
    decide = next(step for step in jobs["plan"]["steps"] if step.get("id") == "wip")
    assert decide["run"] == "node scripts/ci/ci-plan.mjs --wip-only"
    assert jobs["hosted"]["needs"] == ["plan"]
    assert jobs["hosted"]["if"] == "needs.plan.outputs.wip != 'true'"


def test_native_conformance_plan_exposes_wip_to_the_summary() -> None:
    jobs = load("native-conformance")["jobs"]
    assert jobs["plan"]["outputs"]["wip"] == "${{ steps.scope.outputs.wip }}"
    summary = next(step for step in jobs["native-summary"]["steps"] if "env" in step)
    assert summary["env"]["WIP_SELECTED"] == "${{ needs.plan.outputs.wip }}"


def test_the_wip_rule_is_not_copied_into_workflow_yaml() -> None:
    for path in sorted(WORKFLOWS.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        assert "pull_request.title" not in text, path.name
        assert "'WIP:'" not in text and '"WIP:"' not in text, path.name
    planner = (ROOT / "scripts/ci/ci-plan.mjs").read_text(encoding="utf-8")
    assert planner.count("wipPrefix = 'WIP:'") == 1
