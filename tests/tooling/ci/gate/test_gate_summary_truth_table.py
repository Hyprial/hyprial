"""Truth-table and structural guards for the ``gate`` summary job in ``gate.yml``.

The ``gate`` job is the single required status check. Its shell script is
executed verbatim under ``bash -eo pipefail`` across the full Cartesian product
of upstream job results and trigger events, and pure structural readers guard
the job wiring (``if: always()``, ``needs``, ``env``, ``fail-fast: false``,
trigger types/branches, and absence of path filters) with mutation controls.
"""

from __future__ import annotations

import copy
import itertools
import os
import shutil
import subprocess
from typing import Any

import pytest

import tests.support.ci_download.github_gate_policy as policy

GATE_TEXT = policy.gate_path().read_text(encoding="utf-8")
GATE = policy.load(GATE_TEXT)

JOB_RESULTS: tuple[str | None, ...] = (
    "success",
    "failure",
    "cancelled",
    "timed_out",
    "skipped",
    None,
)
EVENTS: tuple[str, ...] = ("pull_request", "push")
EXPECTED_PASSING: frozenset[tuple[str | None, str | None, str | None, str]] = frozenset(
    {
        ("success", "success", "success", "pull_request"),
        ("success", "success", "skipped", "push"),
    }
)
REQUIRED_PR_TYPES = frozenset({"opened", "synchronize", "reopened", "edited"})
REQUIRED_PUSH_BRANCHES = frozenset({"dev", "main"})
REQUIRED_GATE_ENV = {
    "STATIC": "${{ needs.static.result }}",
    "PYTEST": "${{ needs.pytest.result }}",
    "HYGIENE": "${{ needs.pr-hygiene.result }}",
    "EVENT": "${{ github.event_name }}",
}


def gate_summary_script(document: dict[str, Any]) -> str:
    """Return the literal ``run:`` script of the ``gate`` job's first step."""

    gate_job = policy.jobs_of(document).get("gate")
    assert isinstance(gate_job, dict), "gate job missing"
    steps = gate_job.get("steps") or []
    assert steps and isinstance(steps[0], dict), "gate job has no first step"
    script = steps[0].get("run")
    assert isinstance(script, str) and script.strip(), "gate first step has no run script"
    return script


def evaluate_truth_table(script: str) -> list[tuple[tuple[str | None, str | None, str | None, str], int]]:
    """Run ``script`` across all result x event inputs and return mismatches."""

    bash = shutil.which("bash")
    if bash is None:
        pytest.fail("bash is required to execute the gate summary truth table")
    base_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    mismatches: list[tuple[tuple[str | None, str | None, str | None, str], int]] = []
    for static, pytest_res, hygiene, event in itertools.product(
        JOB_RESULTS, JOB_RESULTS, JOB_RESULTS, EVENTS
    ):
        env = dict(base_env)
        if static is not None:
            env["STATIC"] = static
        if pytest_res is not None:
            env["PYTEST"] = pytest_res
        if hygiene is not None:
            env["HYGIENE"] = hygiene
        env["EVENT"] = event
        proc = subprocess.run(
            [bash, "-eo", "pipefail"],
            input=script,
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        combo = (static, pytest_res, hygiene, event)
        expected_ok = combo in EXPECTED_PASSING
        actual_ok = proc.returncode == 0
        if actual_ok != expected_ok:
            mismatches.append((combo, proc.returncode))
    return mismatches


def gate_summary_offenders(document: dict[str, Any]) -> list[str]:
    """Structural invariants for ``gate.yml``'s triggers, pytest matrix and gate job."""

    offenders: list[str] = []
    jobs = policy.jobs_of(document)
    gate_job = jobs.get("gate")
    if not isinstance(gate_job, dict):
        offenders.append("missing 'gate' job")
    else:
        if gate_job.get("if") != "always()":
            offenders.append(f"gate.if must be 'always()', got {gate_job.get('if')!r}")
        other_jobs = set(jobs) - {"gate"}
        declared_needs = set(str(item) for item in policy.as_list(gate_job.get("needs")))
        if not other_jobs or declared_needs != other_jobs:
            offenders.append(
                f"gate.needs {sorted(declared_needs)!r} must equal all other jobs {sorted(other_jobs)!r}"
            )
        steps = gate_job.get("steps") or []
        first_step = steps[0] if steps and isinstance(steps[0], dict) else {}
        step_env = first_step.get("env") if isinstance(first_step.get("env"), dict) else {}
        for var_name, expected_expr in REQUIRED_GATE_ENV.items():
            actual = step_env.get(var_name)
            if actual != expected_expr:
                offenders.append(
                    f"gate step[0].env[{var_name!r}] must be {expected_expr!r}, got {actual!r}"
                )
        wired_results = {str(value).strip() for value in step_env.values()}
        for needed in sorted(declared_needs):
            expected_ref = f"${{{{ needs.{needed}.result }}}}"
            if expected_ref not in wired_results:
                offenders.append(
                    f"gate step[0].env does not wire {expected_ref!r} for needed job {needed!r}"
                )

    pytest_job = jobs.get("pytest")
    if not isinstance(pytest_job, dict):
        offenders.append("missing 'pytest' job")
    else:
        strategy = pytest_job.get("strategy")
        fail_fast = strategy.get("fail-fast") if isinstance(strategy, dict) else None
        if fail_fast is not False:
            offenders.append(f"pytest.strategy.fail-fast must be false, got {fail_fast!r}")

    on_block = policy.triggers(document)
    if not isinstance(on_block, dict):
        offenders.append(f"top-level 'on' must be a mapping, got {on_block!r}")
    else:
        pr_cfg = on_block.get("pull_request")
        if not isinstance(pr_cfg, dict):
            offenders.append("top-level 'on.pull_request' must be a mapping")
        else:
            pr_types = {str(item) for item in policy.as_list(pr_cfg.get("types"))}
            missing_types = sorted(REQUIRED_PR_TYPES - pr_types)
            if missing_types:
                offenders.append(f"on.pull_request.types missing {missing_types!r}")
        push_cfg = on_block.get("push")
        if not isinstance(push_cfg, dict):
            offenders.append("top-level 'on.push' must be a mapping")
        else:
            push_branches = {str(item) for item in policy.as_list(push_cfg.get("branches"))}
            missing_branches = sorted(REQUIRED_PUSH_BRANCHES - push_branches)
            if missing_branches:
                offenders.append(f"on.push.branches missing {missing_branches!r}")
        for trigger_name, cfg in on_block.items():
            if not isinstance(cfg, dict):
                continue
            for filter_key in ("paths", "paths-ignore"):
                if filter_key in cfg:
                    offenders.append(f"on.{trigger_name} must not define {filter_key!r}")

    return offenders


def test_gate_summary_script_matches_exact_truth_table():
    script = gate_summary_script(GATE)
    assert evaluate_truth_table(script) == []


def test_gate_summary_script_rejects_empty_string_values():
    bash = shutil.which("bash")
    if bash is None:
        pytest.fail("bash is required to execute the gate summary truth table")
    script = gate_summary_script(GATE)
    base = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "STATIC": "success",
        "PYTEST": "success",
        "HYGIENE": "success",
        "EVENT": "pull_request",
    }
    for key in ("STATIC", "PYTEST", "HYGIENE", "EVENT"):
        env = dict(base)
        env[key] = ""
        proc = subprocess.run(
            [bash, "-eo", "pipefail"],
            input=script,
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        assert proc.returncode != 0, f"empty {key} unexpectedly passed"


def test_mutated_gate_summary_script_fails_truth_table_g02_and_g03():
    original = gate_summary_script(GATE)
    without_pytest_check = "\n".join(
        line for line in original.splitlines() if 'test "${PYTEST}" = success' not in line
    ) + "\n"
    assert without_pytest_check != original
    mismatches = evaluate_truth_table(without_pytest_check)
    assert any(combo == ("success", "failure", "success", "pull_request") for combo, _ in mismatches)

    relaxed_pytest_check = original.replace(
        'test "${PYTEST}" = success',
        'test "${PYTEST}" = success || true',
    )
    assert relaxed_pytest_check != original
    relaxed_mismatches = evaluate_truth_table(relaxed_pytest_check)
    assert any(
        combo == ("success", "failure", "success", "pull_request")
        for combo, _ in relaxed_mismatches
    )


def test_real_gate_workflow_has_no_structural_summary_offenders():
    assert gate_summary_offenders(GATE) == []


def _mutate_g01_remove_if(doc: dict[str, Any]) -> None:
    doc["jobs"]["gate"].pop("if")


def _mutate_g01_wrong_if(doc: dict[str, Any]) -> None:
    doc["jobs"]["gate"]["if"] = "success()"


def _on_block(doc: dict[str, Any]) -> dict[str, Any]:
    return doc["on"] if "on" in doc else doc[True]


def _mutate_g07_remove_pr_types(doc: dict[str, Any]) -> None:
    _on_block(doc)["pull_request"].pop("types")


def _mutate_g07_drop_edited_type(doc: dict[str, Any]) -> None:
    _on_block(doc)["pull_request"]["types"] = ["opened", "synchronize", "reopened"]


def _mutate_g07_remove_push(doc: dict[str, Any]) -> None:
    _on_block(doc).pop("push")


def _mutate_g07_drop_main_branch(doc: dict[str, Any]) -> None:
    _on_block(doc)["push"]["branches"] = ["dev"]


def _mutate_g11_add_paths_ignore(doc: dict[str, Any]) -> None:
    _on_block(doc)["pull_request"]["paths-ignore"] = ["docs/**"]


def _mutate_g11_add_paths(doc: dict[str, Any]) -> None:
    _on_block(doc)["push"]["paths"] = ["src/**"]


def _mutate_g13_miswire_pytest_env(doc: dict[str, Any]) -> None:
    doc["jobs"]["gate"]["steps"][0]["env"]["PYTEST"] = "${{ needs.static.result }}"


def _mutate_new_job_missing_from_needs(doc: dict[str, Any]) -> None:
    doc["jobs"]["extra"] = {"runs-on": "ubuntu-latest", "steps": [{"run": "echo extra"}]}


def _mutate_new_job_missing_from_gate_env(doc: dict[str, Any]) -> None:
    doc["jobs"]["extra"] = {"runs-on": "ubuntu-latest", "steps": [{"run": "echo extra"}]}
    doc["jobs"]["gate"]["needs"].append("extra")


def _mutate_pytest_fail_fast_true(doc: dict[str, Any]) -> None:
    doc["jobs"]["pytest"]["strategy"]["fail-fast"] = True


def _mutate_pytest_fail_fast_missing(doc: dict[str, Any]) -> None:
    doc["jobs"]["pytest"]["strategy"].pop("fail-fast")


@pytest.mark.parametrize(
    "case_id,mutator",
    [
        ("G01-remove-if", _mutate_g01_remove_if),
        ("G01-wrong-if", _mutate_g01_wrong_if),
        ("G07-remove-pr-types", _mutate_g07_remove_pr_types),
        ("G07-drop-edited-type", _mutate_g07_drop_edited_type),
        ("G07-remove-push", _mutate_g07_remove_push),
        ("G07-drop-main-branch", _mutate_g07_drop_main_branch),
        ("G11-paths-ignore", _mutate_g11_add_paths_ignore),
        ("G11-paths", _mutate_g11_add_paths),
        ("G13-miswire-pytest", _mutate_g13_miswire_pytest_env),
        ("new-job-not-in-needs", _mutate_new_job_missing_from_needs),
        ("new-job-not-in-env", _mutate_new_job_missing_from_gate_env),
        ("pytest-fail-fast-true", _mutate_pytest_fail_fast_true),
        ("pytest-fail-fast-missing", _mutate_pytest_fail_fast_missing),
    ],
)
def test_gate_summary_structural_mutations_are_reported(case_id, mutator):
    mutated = copy.deepcopy(GATE)
    mutator(mutated)
    offenders = gate_summary_offenders(mutated)
    assert offenders, f"mutation {case_id} was not reported"
