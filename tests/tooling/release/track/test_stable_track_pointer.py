"""Track writer policy after Allen's 2026-10-26 ruling.

This explicitly replaces the 2026-09-18 'stable follows main' assertions.
Stable now follows formal releases. Gate-relaxation controls and the internal
mover now requires a green dev push (2026-10-28); behavioral moves use real git fixtures
in test_release_tracks.py rather than the old always-successful git stub.
"""
from pathlib import Path

import yaml

from tests.support.ci.ci_workflow_policy import (
    gate_steps, internal_publication_condition, relaxed_gate_surfaces,
)

ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = ROOT / ".github/workflows/gate-python.yml"


def test_only_formal_publisher_can_move_stable():
    writers = []
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        doc = yaml.safe_load(path.read_text())
        for name, job in doc.get("jobs", {}).items():
            for step in job.get("steps", []):
                script = step.get("run", "")
                if "release_tracks.py --release" in script or "tag -f stable" in script:
                    writers.append(f"{path.name}::{name}")
    assert writers == ["github-publish.yml::publish"]


def test_track_updates_never_delete_refs_or_write_internal():
    text = (ROOT / "scripts/ci/release_tracks.py").read_text()
    assert 'self.move("stable", target, previous)' in text
    assert 'self.move("nightly", target, previous)' in text
    assert 'self.move("internal"' not in text
    assert '"--delete"' not in text and '"tag", "-d"' not in text
    retry = (ROOT / "scripts/ci/github_publish_policy/push.py").read_text()
    assert '--force-with-lease={ref}:{previous}' in retry
    assert 'f"{intended}:{ref}"' in retry


def test_gate_relaxation_policy_is_not_weakened():
    text = WORKFLOW.read_text()
    assert relaxed_gate_surfaces(text, WORKFLOW.name) == []
    assert gate_steps(yaml.safe_load(text)["jobs"]["unit"])
    mutated = text.replace(
        "      - name: Accept the plan, the checks job and every selected pytest shard\n",
        "      - name: Accept the plan, the checks job and every selected pytest shard\n"
        "        continue-on-error: true\n", 1,
    )
    assert mutated != text
    assert relaxed_gate_surfaces(mutated, "mutated")
    assert "continue-on-error: true" in text  # instrumentation remains allowed


def test_internal_mover_and_mirror_require_green_dev_push():
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["unit"]["steps"]
    names = {
        "Move the track tags to the unit-green dev head",
        "Dispatch the GitHub publication of the moved internal tag",
    }
    publication_steps = [step for step in steps if step["name"] in names]
    assert {step["name"] for step in publication_steps} == names
    for step in publication_steps:
        assert internal_publication_condition(step)
        assert "steps.scope.outputs.unit == 'true'" not in step["if"]
