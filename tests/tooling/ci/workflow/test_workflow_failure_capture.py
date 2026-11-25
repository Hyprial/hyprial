'Workflow failure-capture shape, pinned as tests (#143).\n\nEvery scenario step in the controlled gate runs its runner through a\n``uv run … | tee diag/<step>.log`` pipeline under the runner\'s default\n``bash -eo pipefail``.  Without a ``set +e`` guard, errexit kills the shell\nat a failing pipeline *before* ``PIPESTATUS`` is read, so the step dies\nwithout writing its ``results/<id>.code`` or its ``diag/steps.tsv`` row —\nand the verdict then misreports "ran and failed" as "MISSING RECORD"\n(runs #496/#499/#505).  A sibling trap: the row must be written from\n``$code``, not ``$rc`` — the NOT-RUN branch never assigns ``rc``.\n\nThe isolated gate (e2e-macos-isolated.yml) moved its scenario loop into the gate driver\n(spec-e2e001 追加 2), so its capture contract is pinned in the driver\'s\nnew shape: the single gate step runs the driver with NO shell pipeline\n(nothing can swallow an exit code that never passes through a pipe), and\nthe driver itself writes each scenario\'s diag log and steps.tsv row from\nthe child\'s captured returncode (behavior pinned in\ntests/tooling/isolation/test_isolated_gate_driver.py; the wiring literals are pinned in\ntests/tooling/ci/runner/test_ci_e2e_baseline.py).\n\nThese tests parse the workflow files and pin the shape so a future edit\ncannot silently drop the guard again.\n'

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = [
    ROOT / ".forgejo" / "workflows" / "e2e-macos-isolated.yml",
    ROOT / ".forgejo" / "workflows" / "e2e-macos-credentialed.yml",
]

SCENARIO_RE = re.compile(r"contract/e2e/run-[a-z0-9-]+\.py")


def _scenario_steps(path: Path) -> list[tuple[str, str]]:
    """Steps under test: any step whose run body invokes a scenario runner
    OR reads PIPESTATUS[0].

    Enumeration is by the pipeline-capture marker, NOT by scenario name:
    a step whose invocation form diverges (E2E-008 takes extra args, a future
    step may rename things) must not be able to hide from these checks
    (enumeration-by-name missed exactly that shape once).
    """

    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    steps = []
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            body = step.get("run", "")
            if SCENARIO_RE.search(body) or "PIPESTATUS[0]" in body:
                steps.append((step.get("name", "?"), body))
    return steps


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_every_scenario_step_exists(workflow: Path) -> None:
    assert _scenario_steps(workflow), f"{workflow.name}: no scenario steps found"


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_scenario_steps_disarm_errexit_before_pipeline(workflow: Path) -> None:
    if workflow.name == "e2e-macos-isolated.yml":
        # New shape: the loop lives in the driver.  Two rules: (1) any step
        # that still pipes its runner through `| tee` keeps needing the
        # `set +e` disarm (validate does exactly that); (2) the gate step
        # that invokes the driver must have NO pipeline at all — an exit
        # code that never passes through a pipe cannot be swallowed by one.
        saw_gate_step = False
        for name, body in _scenario_steps(workflow):
            if "run-isolated-gate.py" in body:
                saw_gate_step = True
                assert "| tee" not in body, (
                    f"{workflow.name} step {name!r}: the gate step must not "
                    "pipe the driver's output — a pipeline between the driver "
                    "and the step exit is where a child rc gets swallowed"
                )
            elif "| tee " in body:
                pipeline_at = body.index("| tee ")
                assert "set +e" in body[:pipeline_at], (
                    f"{workflow.name} step {name!r}: the tee pipeline runs "
                    "without a preceding `set +e` (#143)"
                )
        assert saw_gate_step, "e2e-macos-isolated.yml no longer invokes the gate driver"
        return
    for name, body in _scenario_steps(workflow):
        pipeline_at = body.index("| tee ")
        prefix = body[:pipeline_at]
        assert "set +e" in prefix, (
            f"{workflow.name} step {name!r}: the tee pipeline runs without a "
            "preceding `set +e` — under bash -eo pipefail a failing scenario "
            "kills the shell before PIPESTATUS is read (#143)"
        )


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_scenario_steps_record_code_file_and_complete_row(workflow: Path) -> None:
    if workflow.name == "e2e-macos-isolated.yml":
        # New shape: the per-scenario record (diag log + steps.tsv row with
        # the captured rc) is written by the driver from the child's
        # returncode; the wiring literals are pinned here so a driver
        # refactor cannot drop them.  Behavior (a failing child still gets
        # its row, the driver still exits nonzero) is pinned in
        # tests/test_isolated_gate_driver.py.
        source = (ROOT / "contract" / "e2e" / "run-isolated-gate.py").read_text(encoding="utf-8")
        assert 'diag / f"{scenario}.log"' in source
        assert "steps.append(scenario, returncode" in source
        return
    for name, body in _scenario_steps(workflow):
        is_scenario = bool(SCENARIO_RE.search(body))
        assert "PIPESTATUS[0]" in body, f"{workflow.name} {name!r}: no PIPESTATUS read"
        # The per-scenario results/<id>.code file is the controlled gate's
        # verdict input; e2e-macos-isolated.yml has no results/ mechanism, and non-scenario
        # steps captured by the PIPESTATUS union (gate-verdict) read codes
        # instead of writing them.
        if is_scenario and workflow.name == "e2e-macos-credentialed.yml":
            assert re.search(r'echo "\$code" > "\$\{HYPRIAL_E2E_ROOT\}/results/', body), (
                f"{workflow.name} {name!r}: no results code file write"
            )
        # Row must be built from $code, never bare $rc, wherever a step has
        # a branch that never assigns rc (e2e-controlled's NOT-RUN branch,
        # code=3).  e2e-macos-isolated.yml steps have no such branch — rc is always
        # assigned there, so $rc stays legal in that file.
        has_notrun_branch = "NOT-RUN" in body
        row_lines = [ln for ln in body.splitlines() if "steps.tsv" in ln or ("row=" in ln)]
        for ln in row_lines:
            if 'printf' in ln and '\\t' in ln and has_notrun_branch:
                assert '"$rc"' not in ln, (
                    f"{workflow.name} {name!r}: steps.tsv row uses $rc — the "
                    "NOT-RUN branch (code=3) never assigns rc and would emit an "
                    "empty rc column (#143)"
                )
        # Exit the captured status explicitly.  e2e-controlled steps (with a
        # NOT-RUN branch) must exit $code; e2e-macos-isolated.yml steps exit $rc (no NOT-RUN
        # branch exists there, rc is always assigned).
        if has_notrun_branch:
            assert 'exit "$code"' in body, (
                f"{workflow.name} {name!r}: step must exit $code"
            )
        else:
            assert 'exit "$rc"' in body or 'exit "$code"' in body, (
                f"{workflow.name} {name!r}: step must exit the captured status"
            )
