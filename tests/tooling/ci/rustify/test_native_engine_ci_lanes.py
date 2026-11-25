"""Every gate-python lane that starts real daemons stages the native engine.

#2726 shipped native as the default actor backend, and an editable checkout
imports no compiled module, so a lane whose pytest step starts real daemons
fails closed with ActorBackendUnavailableError when the extension is missing.
The dev red baseline of 2026-11-15 (R3 / KP-067) recorded exactly that failure
in gate-python / checks: "Run Python GUI integration for audited GUI changes"
ran the daemon-backed CLI files while only pytest-shard carried the three
staging steps (pinned Rust toolchain, build and stage, import proof). Each
staging step must exist in every such lane, sit before the pytest consumer,
carry the shard lane's timeouts, and run under exactly the consumer's own
condition -- a staging step skipped where its consumer runs would re-create
the red, and an unconditional one would build for lanes that never start a
daemon.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = ROOT / ".forgejo/workflows/gate-python.yml"

STAGES = (
    ("Ensure the pinned Rust toolchain for the editable native engine", 12, "scripts/ci/ci-ensure-rust.sh"),
    ("Build and stage the editable native engine", 15, "scripts/build/build_native_engine.sh"),
    ("Verify the staged native engine imports", 5, "import hyprial._core.actors"),
)
LANES = (
    ("pytest-shard", "Run unit tests shard (excludes E2E)"),
    ("checks", "Run Python GUI integration for audited GUI changes"),
)


def _staging_problems(text: str) -> list[str]:
    jobs = yaml.safe_load(text)["jobs"]
    problems: list[str] = []
    for job_name, consumer in LANES:
        steps = jobs[job_name]["steps"]
        names = [str(step.get("name", "")) for step in steps]
        if consumer not in names:
            problems.append(f"{job_name}: daemon-backed consumer {consumer!r} is missing")
            continue
        consumer_index = names.index(consumer)
        consumer_condition = steps[consumer_index].get("if")
        for stage, timeout, marker in STAGES:
            label = f"{job_name}/{stage}"
            if stage not in names:
                problems.append(f"{label}: staging step is missing")
                continue
            step = steps[names.index(stage)]
            if names.index(stage) > consumer_index:
                problems.append(f"{label}: staging step must precede the pytest consumer")
            if step.get("if") != consumer_condition:
                problems.append(
                    f"{label}: runs under {step.get('if')!r} while the consumer "
                    f"runs under {consumer_condition!r}"
                )
            if step.get("timeout-minutes") != timeout:
                problems.append(
                    f"{label}: timeout-minutes is {step.get('timeout-minutes')!r}, expected {timeout}"
                )
            if step.get("continue-on-error"):
                problems.append(f"{label}: relaxed with continue-on-error")
            run = str(step.get("run", ""))
            if marker not in run:
                problems.append(f"{label}: does not run {marker}")
            if stage == STAGES[0][0] and "HYPRIAL_E2E_ROOT=" not in run:
                problems.append(f"{label}: has no isolated HYPRIAL_E2E_ROOT run root")
    return problems


def test_every_daemon_backed_lane_stages_the_native_engine_first() -> None:
    assert _staging_problems(WORKFLOW.read_text(encoding="utf-8")) == []


def test_mutation_checks_lane_without_the_stages_is_rejected() -> None:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    document["jobs"]["checks"]["steps"] = [
        step for step in document["jobs"]["checks"]["steps"]
        if step.get("name") not in {stage for stage, _, _ in STAGES}
    ]
    problems = _staging_problems(yaml.safe_dump(document))
    assert {problem.split(": staging")[0] for problem in problems} == {
        f"checks/{stage}" for stage, _, _ in STAGES
    }


def test_mutation_stage_condition_drift_from_its_consumer_is_rejected() -> None:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in document["jobs"]["checks"]["steps"]:
        if step.get("name") == STAGES[0][0]:
            step.pop("if")
    problems = _staging_problems(yaml.safe_dump(document))
    assert any(
        problem.startswith("checks/Ensure the pinned Rust toolchain") and "runs under" in problem
        for problem in problems
    )
