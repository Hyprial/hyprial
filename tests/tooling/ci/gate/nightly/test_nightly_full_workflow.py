"""Mutation coverage for the independent nightly full-suite workflow."""

import json
import subprocess
from pathlib import Path

import pytest

import tests.support.ci_download.gate_scope_policy as policy

ROOT = Path(__file__).resolve().parents[5]


def documents() -> dict[str, dict]:
    return policy.tree_documents()


def assert_clean(tree: dict[str, dict]) -> None:
    assert not policy.nightly_workflow_offenders(tree), policy.nightly_workflow_offenders(tree)


def test_nightly_workflow_is_clean():
    assert_clean(documents())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: policy.triggers(document).pop("schedule"),
        lambda document: policy.triggers(document).update(workflow_dispatch={"paths": ["docs/**"]}),
        lambda document: document["concurrency"].update({"cancel-in-progress": True}),
        lambda document: document["jobs"].update({"gate": {"runs-on": "ubuntu-latest"}}),
        lambda document: document["jobs"]["nightly-pytest"].update({"if": "false"}),
        lambda document: document["jobs"]["nightly-pytest"]["steps"].append({"run": "echo", "continue-on-error": True}),
        lambda document: next(step for step in document["jobs"]["nightly-pytest"]["steps"] if "upload-artifact" in step.get("uses", "")).pop("timeout-minutes"),
        lambda document: document["jobs"]["nightly-summary"].update({"if": "success()"}),
        lambda document: document["jobs"]["nightly-pytest"].update({"runs-on": "self-hosted"}),
        lambda document: next(step for step in document["jobs"]["nightly-pytest"]["steps"] if step.get("name") == "Run the shard").update(run="pytest -q --ignore tests/daemon"),
        lambda document: next(step for step in document["jobs"]["nightly-pytest"]["steps"] if step.get("name") == "Run the shard").update(run="pytest -q --deselect tests/x.py"),
        lambda document: next(step for step in document["jobs"]["nightly-pytest"]["steps"] if step.get("name") == "Run the shard").setdefault("env", {}).update(PYTEST_ADDOPTS="-k smoke"),
        lambda document: document["jobs"]["nightly-pytest"]["steps"].insert(0, {"name": "Install the pinned Pi SDK", "run": "false"}),
        lambda document: document["jobs"]["nightly-pytest"]["steps"].append({"uses": "actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1", "with": {"node-version": "24"}}),
    ],
)
def test_nightly_mutations_are_reported(mutate):
    tree = documents()
    nightly = tree["nightly-full.yml"]
    mutate(nightly)
    assert policy.nightly_workflow_offenders(tree)


def test_nightly_does_not_share_gate_concurrency_prefix():
    tree = documents()
    nightly = tree["nightly-full.yml"]
    nightly["concurrency"]["group"] = "gate-${{ github.ref }}"
    found = policy.nightly_workflow_offenders(tree)
    assert any("nightly concurrency group" in item.message for item in found)


def test_nightly_summary_requires_both_success_markers():
    tree = documents()
    nightly = tree["nightly-full.yml"]
    summary = nightly["jobs"]["nightly-summary"]["steps"][0]
    summary["run"] = 'test "STATIC=${STATIC}" = STATIC=success'
    found = policy.nightly_workflow_offenders(tree)
    assert any("require success" in item.message for item in found)


def test_fixed_selector_cohort_replays_through_the_final_selector():
    cohort_path = ROOT / "scripts" / "ci" / "pytest-selector-cohort.json"
    replay_path = ROOT / "scripts" / "ci" / "replay_pytest_selector.py"
    cohort = json.loads(cohort_path.read_text(encoding="utf-8"))
    result = subprocess.run(
        ["uv", "run", "--no-project", "--python", "3.12", "python", str(replay_path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["snapshot"] == cohort["snapshot"]
    assert report["testCount"] > 0
    assert {item["name"] for item in report["cohorts"]} == {item["name"] for item in cohort["cohort"]}
    assert {item["commitCount"] for item in report["cohorts"]} == {239, 39, 7}
    assert all(item["commits"] for item in report["cohorts"])
