"""Closed E2E release inventory and CI wiring contracts.

The mutation checks are deliberate: they prove these guards reject the two
regressions they exist to catch instead of merely asserting the current text.

All registered release scenarios are partitioned completely and disjointly
across ISOLATED_IDS, CONTROLLED_IDS, and GUI_ACCEPTANCE_IDS. E2E-032 runs in
gate-gui.yml's gui-acceptance job (with built GUI, pinned Chromium, native
actors on schedule, and reference-ABI actors on pull requests).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
SCENARIOS = ROOT / "contract" / "e2e"
VALIDATE = SCENARIOS / "validate.py"
RUN_015 = SCENARIOS / "run-pac-workflow-unified.py"
GATE_DRIVER = SCENARIOS / "run-isolated-gate.py"
TESTS_WORKFLOW = ROOT / ".forgejo" / "workflows" / "gate-python.yml"
ISOLATED_WORKFLOW = ROOT / ".forgejo" / "workflows" / "e2e-macos-isolated.yml"
CONTROLLED_WORKFLOW = ROOT / ".forgejo" / "workflows" / "e2e-macos-credentialed.yml"
GUI_WORKFLOW = ROOT / ".forgejo" / "workflows" / "gate-gui.yml"

EXPECTED_IDS = (
    {f"E2E-{number:03d}" for number in range(1, 19) if number != 14}
) | {
    "E2E-020",
    "E2E-021",
    "E2E-024",
    "E2E-025",
    "E2E-028",
    "E2E-029",
    "E2E-030",
    "E2E-031",
    "E2E-032",
}
GUI_ACCEPTANCE_IDS = {
    "E2E-032",
}
GUI_ACCEPTANCE_SETUP_STEPS = (
    "Sync Python environment for GUI acceptance tests",
    "Install locked Node dependencies",
    "Provision pinned Chromium for the browser gates",
    "Ensure a CJK font and fontconfig for the browser screenshots",
    "Build independent GUI",
    "Run browser acceptance",
)
ISOLATED_IDS = {
    "E2E-001",
    "E2E-002",
    "E2E-003",
    "E2E-005",
    "E2E-007",
    "E2E-009",
    "E2E-010",
    "E2E-011",
    "E2E-015",
    "E2E-016",
    "E2E-017",
    "E2E-018",
    "E2E-020",
    "E2E-021",
    "E2E-024",
    "E2E-028",
    "E2E-029",
    "E2E-030",
    "E2E-031",
}
CONTROLLED_IDS = EXPECTED_IDS - ISOLATED_IDS - GUI_ACCEPTANCE_IDS
COMPILEALL_COMMAND = "python -m compileall -q src tests contract"


def _load_validate(name: str):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, VALIDATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_gate_driver():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("e2e_gate_driver_wiring", GATE_DRIVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _scenario_steps(path: Path) -> dict[str, str]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    result: dict[str, str] = {}
    for job in document["jobs"].values():
        for step in job.get("steps", []):
            name = str(step.get("name", ""))
            if name.startswith("E2E-"):
                scenario_id = name.split(maxsplit=1)[0]
                result[scenario_id] = str(step.get("run", ""))
    return result


def _gui_acceptance_steps(workflow_text: str | None = None) -> dict[str, dict]:
    text = (
        workflow_text
        if workflow_text is not None
        else GUI_WORKFLOW.read_text(encoding="utf-8")
    )
    document = yaml.safe_load(text)
    job = document["jobs"]["gui-acceptance"]
    assert int(job.get("timeout-minutes", 0)) >= 45
    steps = list(job["steps"])
    names = [str(step.get("name", "")) for step in steps]
    setup_indices = [names.index(setup_name) for setup_name in GUI_ACCEPTANCE_SETUP_STEPS]
    e2e_index = next(
        (
            index
            for index, step in enumerate(steps)
            if str(step.get("name", "")).startswith("E2E-032 ")
        ),
        None,
    )
    assert e2e_index is not None
    end_conditions_index = names.index("Record end CI conditions")
    assert all(index < e2e_index for index in setup_indices)
    assert e2e_index < end_conditions_index
    browser_acceptance = steps[names.index("Run browser acceptance")]
    assert browser_acceptance.get("if") == (
        "success() && steps.scope.outputs.gui == 'true'"
    )
    assert not browser_acceptance.get("continue-on-error", False)
    assert (browser_acceptance.get("env") or {}).get(
        "HYPRIAL_ACCEPTANCE_ACTOR_BACKEND"
    ) == (
        "${{ github.event_name == 'schedule' && 'native' || 'reference-abi' }}"
    )
    result: dict[str, dict] = {}
    for index, step in enumerate(steps):
        name = str(step.get("name", ""))
        if name.startswith("E2E-"):
            scenario_id = name.split(maxsplit=1)[0]
            assert scenario_id not in result, f"duplicate GUI acceptance step {scenario_id}"
            result[scenario_id] = step
    return result


def _isolated_wiring() -> dict[str, str]:
    """The isolated gate's scenario wiring.  The loop lives in the gate
    driver (spec-e2e001 追加 2: one script step, not twelve workflow steps),
    so the wiring table comes from the driver, not from workflow step
    names."""

    return dict(_load_gate_driver().SCENARIOS)


def _assert_wiring(
    isolated: dict[str, str],
    controlled: dict[str, str],
    gui_acceptance: dict[str, dict] | None = None,
    *,
    driver_source: str | None = None,
    workflow_text: str | None = None,
    gui_workflow_text: str | None = None,
) -> None:
    resolved_gui = (
        gui_acceptance
        if gui_acceptance is not None
        else _gui_acceptance_steps(gui_workflow_text)
    )
    assert set(isolated) == ISOLATED_IDS
    assert set(controlled) == CONTROLLED_IDS
    assert set(resolved_gui) == GUI_ACCEPTANCE_IDS
    assert not (set(isolated) & set(controlled))
    assert not (set(isolated) & set(resolved_gui))
    assert not (set(controlled) & set(resolved_gui))
    assert set(isolated) | set(controlled) | set(resolved_gui) == EXPECTED_IDS
    inventory = {
        row["id"]: row["runner"]
        for row in json.loads(
            (SCENARIOS / "scenarios.json").read_text(encoding="utf-8")
        )["scenarios"]
    }
    for scenario_id, runner in isolated.items():
        # The driver must run the inventory's runner for every isolated
        # scenario, and that runner must exist on disk.
        assert inventory[scenario_id] == runner, scenario_id
        assert (SCENARIOS / runner).is_file(), runner
    for scenario_id, body in controlled.items():
        assert f"contract/e2e/{inventory[scenario_id]}" in body
        assert f"diag/{scenario_id}.log" in body
        assert "PIPESTATUS[0]" in body
    for scenario_id, step in resolved_gui.items():
        runner = inventory[scenario_id]
        assert (SCENARIOS / runner).is_file(), runner
        run = str(step.get("run", ""))
        assert f"contract/e2e/{runner}" in run
        assert "set -euo pipefail" in run
        assert "uv run --locked --no-sync --python 3.12 python" in run
        assert "|| status=$?" in run
        assert 'exit "$status"' in run
        assert "| tee" not in run
        assert "|| true" not in run
        assert "|| :" not in run
        assert step.get("if") == "success() && steps.scope.outputs.gui == 'true'"
        assert not step.get("continue-on-error", False)
        assert (step.get("env") or {}).get("HYPRIAL_ACTOR_BACKEND") == (
            "${{ github.event_name == 'schedule' && 'native' || 'reference-abi' }}"
        )
    # The per-scenario evidence the isolated workflow steps used to carry
    # (diag log, captured rc) now lives in the driver; pinned on its source
    # so a refactor cannot drop either silently.
    source = (
        driver_source
        if driver_source is not None
        else GATE_DRIVER.read_text(encoding="utf-8")
    )
    assert 'diag / f"{scenario}.log"' in source, "driver lost per-scenario diag logs"
    assert "steps.append(scenario, returncode" in source, "driver lost per-scenario rc rows"
    # And the workflow must hand the gate to the driver exactly once, with
    # nothing piped between the driver's exit code and the step's own.
    text = (
        workflow_text
        if workflow_text is not None
        else ISOLATED_WORKFLOW.read_text(encoding="utf-8")
    )
    document = yaml.safe_load(text)
    gate_bodies = [
        str(step.get("run", ""))
        for job in document["jobs"].values()
        for step in job.get("steps", [])
        if "run-isolated-gate.py" in str(step.get("run", ""))
    ]
    assert len(gate_bodies) == 1, "the isolated gate must invoke the driver exactly once"
    assert "| tee" not in gate_bodies[0], "nothing may swallow the driver's exit code"


def _assert_compileall(workflow_text: str) -> None:
    document = yaml.safe_load(workflow_text)
    # PyYAML 1.1 parses the key `on` as True. The exact candidate must remain
    # manually dispatchable while it lives on an unmerged integration branch.
    triggers = document[True]
    assert "workflow_dispatch" in triggers
    bodies = [
        str(step.get("run", ""))
        for job in document["jobs"].values()
        for step in job.get("steps", [])
    ]
    matches = [body for body in bodies if COMPILEALL_COMMAND in body]
    assert len(matches) == 1
    assert "uv run --locked --python 3.12" in matches[0]


def _assert_e2e015_home_guard(source: str) -> None:
    refusal = source.index('refused.get("code") == "HYPRIAL_HOME_NOT_INITIALIZED"')
    untouched = source.index('assert not (root / "home").exists()')
    initialise = source.index('result = daemon.command("init"')
    successful_plan = source.index('plan = daemon.command("workflow", "plan"')
    assert refusal < untouched < initialise < successful_plan


def test_inventory_is_exactly_the_registered_release_set() -> None:
    module = _load_validate("e2e_inventory_exact")
    assert module.REQUIRED_IDS == EXPECTED_IDS
    assert module.main() == 0


def test_inventory_omission_mutation_is_rejected(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    module = _load_validate("e2e_inventory_omission_mutation")
    document = json.loads((SCENARIOS / "scenarios.json").read_text(encoding="utf-8"))
    document["scenarios"] = [
        row for row in document["scenarios"] if row["id"] != "E2E-017"
    ]
    (tmp_path / "scenarios.json").write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    with pytest.raises(AssertionError, match="missing.*E2E-017"):
        module.main()


def test_every_scenario_is_wired_once_to_its_dependency_gate() -> None:
    _assert_wiring(
        _isolated_wiring(),
        _scenario_steps(CONTROLLED_WORKFLOW),
        _gui_acceptance_steps(),
    )


def _mutate_gui_acceptance(mutator) -> str:
    document = yaml.safe_load(GUI_WORKFLOW.read_text(encoding="utf-8"))
    mutator(document["jobs"]["gui-acceptance"]["steps"])
    return yaml.safe_dump(document, sort_keys=False)


def _e2e032_step_index(steps: list[dict]) -> int:
    return next(
        index
        for index, step in enumerate(steps)
        if str(step.get("name", "")).startswith("E2E-032 ")
    )


def test_gui_acceptance_omission_mutation_is_rejected() -> None:
    mutated = _mutate_gui_acceptance(lambda steps: steps.pop(_e2e032_step_index(steps)))
    with pytest.raises(AssertionError):
        _assert_wiring(
            _isolated_wiring(),
            _scenario_steps(CONTROLLED_WORKFLOW),
            gui_workflow_text=mutated,
        )


def test_gui_acceptance_continue_on_error_mutation_is_rejected() -> None:
    mutated = _mutate_gui_acceptance(
        lambda steps: steps[_e2e032_step_index(steps)].__setitem__(
            "continue-on-error", True
        )
    )
    with pytest.raises(AssertionError):
        _assert_wiring(
            _isolated_wiring(),
            _scenario_steps(CONTROLLED_WORKFLOW),
            gui_workflow_text=mutated,
        )


def test_gui_acceptance_tee_pipeline_mutation_is_rejected() -> None:
    def add_tee(steps: list[dict]) -> None:
        step = steps[_e2e032_step_index(steps)]
        step["run"] = f"{step['run']}\necho ok | tee /tmp/x.log"

    with pytest.raises(AssertionError):
        _assert_wiring(
            _isolated_wiring(),
            _scenario_steps(CONTROLLED_WORKFLOW),
            gui_workflow_text=_mutate_gui_acceptance(add_tee),
        )


@pytest.mark.parametrize("mutation", ["true", "colon", "exit"])
def test_gui_acceptance_status_handling_mutations_are_rejected(mutation: str) -> None:
    def mutate_status(steps: list[dict]) -> None:
        step = steps[_e2e032_step_index(steps)]
        if mutation == "true":
            step["run"] = f'{step["run"]}\necho ignored || true'
        elif mutation == "colon":
            step["run"] = f'{step["run"]}\necho ignored || :'
        else:
            step["run"] = step["run"].replace('exit "$status"', "", 1)

    with pytest.raises(AssertionError):
        _assert_wiring(
            _isolated_wiring(),
            _scenario_steps(CONTROLLED_WORKFLOW),
            gui_workflow_text=_mutate_gui_acceptance(mutate_status),
        )


@pytest.mark.parametrize("backend", [None, "native", "reference-abi"])
def test_gui_acceptance_actor_backend_mutation_is_rejected(
    backend: str | None,
) -> None:
    def mutate_backend(steps: list[dict]) -> None:
        env = dict(steps[_e2e032_step_index(steps)].get("env") or {})
        if backend is None:
            env.pop("HYPRIAL_ACTOR_BACKEND", None)
        else:
            env["HYPRIAL_ACTOR_BACKEND"] = backend
        steps[_e2e032_step_index(steps)]["env"] = env

    with pytest.raises(AssertionError):
        _assert_wiring(
            _isolated_wiring(),
            _scenario_steps(CONTROLLED_WORKFLOW),
            gui_workflow_text=_mutate_gui_acceptance(mutate_backend),
        )


def test_gui_acceptance_weakened_if_mutation_is_rejected() -> None:
    mutated = _mutate_gui_acceptance(
        lambda steps: steps[_e2e032_step_index(steps)].__setitem__("if", "always()")
    )
    with pytest.raises(AssertionError):
        _assert_wiring(
            _isolated_wiring(),
            _scenario_steps(CONTROLLED_WORKFLOW),
            gui_workflow_text=mutated,
        )


@pytest.mark.parametrize(
    "setup_name",
    [
        "Provision pinned Chromium for the browser gates",
        "Ensure a CJK font and fontconfig for the browser screenshots",
        "Build independent GUI",
    ],
)
def test_gui_acceptance_early_step_order_mutation_is_rejected(
    setup_name: str,
) -> None:
    def move_before(steps: list[dict]) -> None:
        step = steps.pop(_e2e032_step_index(steps))
        target = next(
            index
            for index, candidate in enumerate(steps)
            if candidate.get("name") == setup_name
        )
        steps.insert(target, step)

    with pytest.raises(AssertionError):
        _assert_wiring(
            _isolated_wiring(),
            _scenario_steps(CONTROLLED_WORKFLOW),
            gui_workflow_text=_mutate_gui_acceptance(move_before),
        )


def test_gui_acceptance_after_condition_tail_mutation_is_rejected() -> None:
    def move_after_tail(steps: list[dict]) -> None:
        step = steps.pop(_e2e032_step_index(steps))
        end_index = next(
            index
            for index, candidate in enumerate(steps)
            if candidate.get("name") == "Record end CI conditions"
        )
        steps.insert(end_index + 1, step)

    with pytest.raises(AssertionError):
        _assert_wiring(
            _isolated_wiring(),
            _scenario_steps(CONTROLLED_WORKFLOW),
            gui_workflow_text=_mutate_gui_acceptance(move_after_tail),
        )


@pytest.mark.parametrize("mutation", ["rename", "driver"])
def test_gui_acceptance_browser_mutations_are_rejected(mutation: str) -> None:
    def mutate_browser_acceptance(steps: list[dict]) -> None:
        index = next(
            index
            for index, step in enumerate(steps)
            if step.get("name") == "Run browser acceptance"
        )
        if mutation == "rename":
            steps[index]["name"] = "Run Contract Mock browser acceptance"
        else:
            steps[index]["env"]["HYPRIAL_ACCEPTANCE_ACTOR_BACKEND"] = "native"

    with pytest.raises((AssertionError, ValueError)):
        _assert_wiring(
            _isolated_wiring(),
            _scenario_steps(CONTROLLED_WORKFLOW),
            gui_workflow_text=_mutate_gui_acceptance(mutate_browser_acceptance),
        )


def test_gui_acceptance_timeout_mutation_is_rejected() -> None:
    document = yaml.safe_load(GUI_WORKFLOW.read_text(encoding="utf-8"))
    document["jobs"]["gui-acceptance"]["timeout-minutes"] = 25
    with pytest.raises(AssertionError):
        _assert_wiring(
            _isolated_wiring(),
            _scenario_steps(CONTROLLED_WORKFLOW),
            gui_workflow_text=yaml.safe_dump(document, sort_keys=False),
        )


def test_workflow_omission_mutation_is_rejected() -> None:
    isolated = _isolated_wiring()
    controlled = _scenario_steps(CONTROLLED_WORKFLOW)
    del isolated["E2E-017"]
    with pytest.raises(AssertionError):
        _assert_wiring(isolated, controlled)


def test_gate_driver_wiring_mutations_are_rejected() -> None:
    """Removing the loop's protections in the DRIVER must redden the
    wiring guard (the loop's new home — the same rule the omission
    mutation has always enforced for the workflow)."""

    isolated = _isolated_wiring()
    controlled = _scenario_steps(CONTROLLED_WORKFLOW)
    source = GATE_DRIVER.read_text(encoding="utf-8")
    with pytest.raises(AssertionError):
        _assert_wiring(
            isolated,
            controlled,
            driver_source=source.replace(
                "steps.append(scenario, returncode", "steps.append(scenario, 0,"
            ),
        )
    with pytest.raises(AssertionError):
        _assert_wiring(
            isolated,
            controlled,
            driver_source=source.replace(
                'diag / f"{scenario}.log"', 'diag / "gate.log"'
            ),
        )


def test_gate_step_pipeline_mutation_is_rejected() -> None:
    """Reintroducing a shell pipeline on the gate step must redden the
    wiring guard: a `| tee` between the driver and the step exit is the
    new shape of the rc-swallowing the old PIPESTATUS checks caught."""

    text = ISOLATED_WORKFLOW.read_text(encoding="utf-8")
    mutated = text.replace(
        "run-isolated-gate.py",
        "run-isolated-gate.py 2>&1 | tee /tmp/gate.log",
        1,
    )
    with pytest.raises(AssertionError):
        _assert_wiring(
            _isolated_wiring(), _scenario_steps(CONTROLLED_WORKFLOW), workflow_text=mutated
        )


def test_unit_ci_runs_compileall_over_every_python_tree() -> None:
    _assert_compileall(TESTS_WORKFLOW.read_text(encoding="utf-8"))


def test_compileall_wiring_mutation_is_rejected() -> None:
    text = TESTS_WORKFLOW.read_text(encoding="utf-8")
    mutated = text.replace(COMPILEALL_COMMAND, "python -m compileall -q src tests")
    with pytest.raises(AssertionError):
        _assert_compileall(mutated)


def test_e2e015_runner_refuses_then_prepares_disposable_home() -> None:
    """The actual runner proves the guard before it creates its own home.

    This is deliberately an ordering contract: weakening the product guard
    or moving the mkdir before the refusal probe makes the real E2E go red.
    """

    _assert_e2e015_home_guard(RUN_015.read_text(encoding="utf-8"))


def test_e2e015_home_guard_mutation_is_rejected() -> None:
    source = RUN_015.read_text(encoding="utf-8")
    mutated = source.replace("HYPRIAL_HOME_NOT_INITIALIZED", "WRONG_CODE", 1)
    with pytest.raises(ValueError):
        _assert_e2e015_home_guard(mutated)


# --- unit gate triggers: intg PRs run, intg pushes must not move the tag ----
#
# A PR based on an integration branch used to get only the declaration gate,
# so #343 waited 40 minutes for a unit run that was never going to start --
# and the declaration gate's green made it look like CI was in progress. The
# fix adds `intg/**` to `pull_request.branches`.


def _tests_workflow_triggers() -> dict[str, object]:
    document = yaml.safe_load(TESTS_WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML resolves the bare key `on` to True (YAML 1.1 boolean), so accept
    # either spelling rather than pinning today's parser behaviour.
    return document.get("on") or document[True]


def test_pull_requests_into_integration_branches_run_the_unit_gate() -> None:
    branches = _tests_workflow_triggers()["pull_request"]["branches"]

    assert "dev" in branches and "main" in branches
    assert "intg/**" in branches, (
        "a PR based on an integration branch would get no unit run, and the "
        "declaration gate.s green would look like CI was running"
    )
    assert "release/**" in branches, "a release-line PR must run the same unit gate"


def test_component_integration_pushes_validate_without_publishing_internal() -> None:
    """Validation triggers and publication authority are separate boundaries."""

    push_branches = _tests_workflow_triggers()["push"]["branches"]

    assert "intg/**" not in push_branches
    assert {"intg/gui**", "intg/sidecar**"} <= set(push_branches)
    document = yaml.safe_load(TESTS_WORKFLOW.read_text())
    unit_steps = document["jobs"]["unit"]["steps"]
    publication_names = {
        "Move the track tags to the unit-green dev head",
        "Dispatch the GitHub publication of the moved internal tag",
    }
    publication_steps = [
        step for step in unit_steps if step["name"] in publication_names
    ]
    from tests.support.ci.ci_workflow_policy import internal_publication_condition

    assert {step["name"] for step in publication_steps} == publication_names
    assert all(internal_publication_condition(step) for step in publication_steps)


def test_the_dev_tag_step_still_keys_on_a_push_to_dev() -> None:
    """The trigger change must not have disturbed what moves the tag.

    Pins the *step* that pushes the dev track tag and its trigger, not the
    literal command: the force flag is a separate property with its own test
    (``test_the_dev_tag_push_is_forced``), so changing a flag reds the test
    named for that flag rather than this one.
    """

    step = _mover_step(TESTS_WORKFLOW.read_text(encoding="utf-8"))
    condition = str(step.get("if", ""))
    assert "github.event_name == 'push'" in condition
    assert "github.ref == 'refs/heads/dev'" in condition
    assert _pushes_the_dev_tag(str(step.get("run", "")))


# --- the mover's ordering key is a PROPERTY, never a step position ----------
#
# The audit that decides where `refs/tags/internal` should point used to read a
# dev-push task's terminal time (`updated_at`) as the moment the tag was actually
# pushed, and ordered the green dev-push runs by it.  That reading is correct
# only while the mover is the unit job's LAST step: add a step after it (cache,
# upload, notification) and the ordering silently absorbs that step's duration,
# so the computed tag can be wrong while every input still looks right.
#
# 2026-09-21: the condition tail (added 2026-09-14, `if: always()`) is exactly
# such a step, and the tail is intentional -- so the answer is the one this
# file's own docstring asked for: "first ask whether a step was *intentionally*
# added after the mover (then the ordering key needs rethinking, not the
# guard)".  The key was re-thought: the mover records its OWN push time (see
# `_assert_the_dev_tag_mover_records_its_own_push_time`), so nothing here reads a
# step index any more.  The old guard read `movers == [len(steps) - 1]`, which is
# a position, not a property.

# Identity is semantic, not a literal command line: the mover *pushes the dev
# tag*. Pinning `git push --force origin refs/tags/internal` verbatim would go red on
# an unrelated flag change (dropping --force, switching to a token variable)
# while the contract is intact; pinning only the step's `name` would miss a
# semantic move.  The ref is matched as a whitespace TOKEN, so a renamed
# `refs/tags/internal-2` cannot match it by prefix.


def _pushes_the_dev_tag(run: str) -> bool:
    return "git push" in run and "refs/tags/internal" in run.split()


def _unit_job_steps(text: str) -> list[dict]:
    return list(yaml.safe_load(text)["jobs"]["unit"]["steps"])


def _dev_tag_mover_steps(steps: list[dict]) -> list[int]:
    return [
        index
        for index, step in enumerate(steps)
        if _pushes_the_dev_tag(str(step.get("run", "")))
    ]


def _mover_step(text: str) -> dict:
    """The one unit step that pushes the dev track tag."""

    steps = _unit_job_steps(text)
    movers = _dev_tag_mover_steps(steps)
    assert len(movers) == 1, (
        f"expected exactly one unit step that pushes refs/tags/internal; got {movers}"
    )
    return steps[movers[0]]


def _assert_the_dev_tag_push_is_forced(text: str) -> None:
    run = str(_mover_step(text).get("run", ""))
    assert "--force" in run or "--force-with-lease" in run, (
        "updating an existing refs/tags/internal needs a forced push; a plain `git "
        "push` is rejected, so the track would silently stop moving while the "
        "job still looks green"
    )


def test_the_dev_tag_push_is_forced() -> None:
    """Force is a property of the contract, not an implementation detail.

    Kept separate from the push test on purpose: this way a removed flag reds
    the test whose name says *forced*, instead of a test named after the push.
    """

    _assert_the_dev_tag_push_is_forced(TESTS_WORKFLOW.read_text(encoding="utf-8"))


def test_force_flag_mutation_is_rejected() -> None:
    with pytest.raises(AssertionError, match="forced push"):
        _assert_the_dev_tag_push_is_forced(_synthetic_unit_job(force=False))


def _assert_green_dev_push(text: str) -> None:
    from tests.support.ci.ci_workflow_policy import internal_publication_condition

    assert internal_publication_condition(_mover_step(text)), "requires green dev push only"


def test_the_dev_tag_mover_requires_green_dev_push() -> None:
    """Allen 2026-10-28 supersedes the warning-only 10-20 policy."""

    _assert_green_dev_push(TESTS_WORKFLOW.read_text(encoding="utf-8"))


@pytest.mark.parametrize("replacement", ["always()", "failure()", "cancelled()"])
def test_non_green_mover_mutation_is_rejected(replacement) -> None:
    document = yaml.safe_load(_synthetic_unit_job())
    mover = document["jobs"]["unit"]["steps"][2]
    mover["if"] = mover["if"].replace("success()", replacement)
    with pytest.raises(AssertionError, match="green dev push"):
        _assert_green_dev_push(yaml.safe_dump(document, sort_keys=False))


def _assert_the_dev_tag_mover_records_its_own_push_time(text: str) -> None:
    run = str(_mover_step(text).get("run", ""))
    push_at = run.find("git push")
    record_at = run.find("ci-conditions.sh mover")
    assert push_at != -1, "the mover step no longer pushes anything"
    assert record_at != -1, (
        "the mover must record its OWN push time via `ci-conditions.sh mover`; "
        "otherwise the audit falls back to a dev-push task's `updated_at`, which "
        "is the mover's time only while the mover is the job's LAST step -- a "
        "position nothing guarantees, since the condition tail sits after it"
    )
    assert record_at > push_at, (
        "the push-time record must come AFTER the push: recording first stamps an "
        "intent, not a push"
    )


def test_the_dev_tag_mover_records_its_own_push_time() -> None:
    'The new ordering key: a record written by the push itself.\n\n    The property kept is the one the old guard protected ("the tag time is the\n    push time"), without the step-position dependency that made it fragile.\n    The *missing-value* half is a protocol rule and is pinned where the protocol\n    lives (scripts/ci/ci-conditions.sh), see\n    tests/tooling/ci/execution/test_ci_conditions.py::test_the_missing_value_semantics_are_written_into_the_protocol_not_only_here.\n    '

    _assert_the_dev_tag_mover_records_its_own_push_time(
        TESTS_WORKFLOW.read_text(encoding="utf-8")
    )


def _assert_the_dev_tag_mover_is_unique(text: str, label: str) -> None:
    movers = _dev_tag_mover_steps(_unit_job_steps(text))
    assert len(movers) == 1, (
        f"{label}: expected exactly one unit step that pushes refs/tags/internal; "
        f"got {movers}. A second mover makes 'the tag time' ambiguous -- two "
        "pushes, two times -- while both steps look plausible"
    )


def test_the_dev_tag_mover_is_unique_by_identity_not_by_position() -> None:
    """Uniqueness is the property; the step index is not.

    Repo-wide on purpose: `_dev_tag_mover_steps` only saw the unit job before,
    so a second mover added to any other workflow kept this guard green.
    """

    text = TESTS_WORKFLOW.read_text(encoding="utf-8")
    _assert_the_dev_tag_mover_is_unique(text, "real file")
    assert len(_dev_tag_mover_steps(_unit_job_steps(text))) == 1
    from tests.support.ci.ci_workflow_policy import internal_tag_movers

    assert len(internal_tag_movers()) == 1, (
        "exactly one step in the whole tree may push refs/tags/internal; got "
        + repr([(key, index) for key, index, _ in internal_tag_movers()])
    )


def test_a_second_mover_mutation_is_rejected() -> None:
    document = yaml.safe_load(_synthetic_unit_job())
    steps = document["jobs"]["unit"]["steps"]
    steps.append({"name": "another mover", "run": "git push --force origin refs/tags/internal"})
    with pytest.raises(AssertionError, match="exactly one unit step"):
        _assert_the_dev_tag_mover_is_unique(yaml.safe_dump(document, sort_keys=False), "synthetic")


def test_a_mover_without_the_push_time_record_is_rejected() -> None:
    document = yaml.safe_load(_synthetic_unit_job())
    steps = document["jobs"]["unit"]["steps"]
    mover = steps[_dev_tag_mover_steps(steps)[0]]
    mover["run"] = str(mover["run"]).replace("bash scripts/ci/ci-conditions.sh mover\n", "")
    with pytest.raises(AssertionError, match="record its OWN push time"):
        _assert_the_dev_tag_mover_records_its_own_push_time(
            yaml.safe_dump(document, sort_keys=False)
        )


def _synthetic_unit_job(
    *,
    trailing_step: bool = False,
    target: str = "refs/tags/internal",
    force: bool = True,
    record_push_time: bool = True,
) -> str:
    """A known-good unit job, so the mutation checks do not depend on the real
    file's current state: each one mutates THIS shape and asks the assertion to
    accept or reject it. The real file is covered by the guard test above."""

    from tests.support.ci.ci_workflow_policy import INTERNAL_MOVE_CONDITION

    mover_if = INTERNAL_MOVE_CONDITION
    steps: list[dict] = [
        {"name": "Check out commit", "run": "git init ."},
        {"name": "Run unit tests", "run": "pytest -q"},
        {
            "name": "Move the dev track tag",
            "if": mover_if,
            "run": (
                f'git tag -f dev "$GITHUB_SHA"\ngit push'
                f'{" --force" if force else ""} origin {target}\n'
                + ("bash scripts/ci/ci-conditions.sh mover\n" if record_push_time else "")
            ),
        },
    ]
    if trailing_step:
        steps.append({"name": "upload diagnostics", "run": "echo after the mover"})
    return yaml.safe_dump({"jobs": {"unit": {"steps": steps}}})


def test_dispatch_trigger_addition_mutation_is_rejected() -> None:
    """Manual dispatch cannot authorize an internal tag move."""

    document = yaml.safe_load(_synthetic_unit_job())
    steps = document["jobs"]["unit"]["steps"]
    mover = steps[_dev_tag_mover_steps(steps)[0]]
    mover["if"] = str(mover["if"]).replace(
        "github.event_name == 'push'",
        "(github.event_name == 'push' || github.event_name == 'workflow_dispatch')",
    )
    with pytest.raises(AssertionError, match="green dev push"):
        _assert_green_dev_push(yaml.safe_dump(document, sort_keys=False))


def test_a_step_after_the_mover_no_longer_reds_the_ordering_key() -> None:
    """The old guard's trigger, kept as the negative of the new key.

    Appending a step after the mover used to go red on a step INDEX. It must no
    longer do so -- that coupling was the defect (the intentional `if: always()`
    tail is exactly such a step) -- while the push-time record still has to be
    there. Keeping this case means the change is a *narrowing of what is read*,
    not a guard that was simply dropped.
    """

    text = _synthetic_unit_job(trailing_step=True)
    _assert_the_dev_tag_mover_records_its_own_push_time(text)
    _assert_the_dev_tag_mover_is_unique(text, "trailing step")


def test_dev_tag_target_mutation_is_rejected() -> None:
    """A renamed target must be caught, not swallowed by prefix matching."""

    with pytest.raises(AssertionError, match="exactly one unit step"):
        _assert_the_dev_tag_mover_is_unique(
            _synthetic_unit_job(target="refs/tags/devtrack"), "renamed target"
        )


def test_unrelated_unit_step_changes_do_not_red_the_key() -> None:
    """The control: changes that keep the contract must not fire the guard.

    Inserting a step *before* the mover and renaming an unrelated step both keep
    "one mover records the push time" true; a guard that fired here would be
    measuring the workflow's text rather than the contract.
    """

    document = yaml.safe_load(_synthetic_unit_job())
    steps = document["jobs"]["unit"]["steps"]
    steps.insert(
        _dev_tag_mover_steps(steps)[0],
        {"name": "cache warmup", "run": "echo warm"},
    )
    steps[0]["name"] = "Check out the commit (renamed)"
    text = yaml.safe_dump(document, sort_keys=False)
    _assert_the_dev_tag_mover_records_its_own_push_time(text)
    _assert_the_dev_tag_mover_is_unique(text, "renamed neighbour")


def test_gate_precompiles_the_tree_and_the_hyprial_venv_before_any_scenario(
    tmp_path, monkeypatch
) -> None:
    """E2E-002 (2026-09-27, task 35833) was the first daemon start from a CI
    venv built without bytecode, so it compiled everything inside `hyprial
    init`'s 15 s readiness wait.  The gate compiles once up front instead."""

    gate = _load_gate_driver()
    venv = tmp_path / "venv"
    site = venv / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (site / "sample_dep.py").write_text("VALUE = 1\n")
    (venv / "bin").mkdir()
    monkeypatch.setenv("HYPRIAL_BIN", str(venv / "bin" / "hyprial"))

    targets = gate.precompile_bytecode_targets()
    assert site.resolve() in [target.resolve() for target in targets]
    assert gate.ROOT / "src" in targets

    monkeypatch.setattr(gate, "precompile_bytecode_targets", lambda: [site])
    lines: list[str] = []
    gate.precompile_bytecode(lines.append)
    assert any((site / "__pycache__").glob("sample_dep.*.pyc"))
    assert lines and lines[0].startswith("RESULT NOTE gate precompile ok")

    def broken() -> list:
        raise OSError("disk gone")

    monkeypatch.setattr(gate, "precompile_bytecode_targets", broken)
    lines.clear()
    gate.precompile_bytecode(lines.append)  # must not raise
    assert lines == ["RESULT NOTE gate precompile skipped: OSError: disk gone\n"]
