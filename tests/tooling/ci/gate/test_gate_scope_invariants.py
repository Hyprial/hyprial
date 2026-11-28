"""The six gate-scope invariants (owner ruling 2026-11-27, plan A), each with mutations that go red.

``gate`` (``gate.yml``) and ``pr-hygiene`` (``pr-hygiene.yml``) are the two
required checks of dev and main. Every test asserts the property on the real
tree and then applies mutations to a deep copy of the loaded tree; each
mutation must be reported by the reader named for it, recognised by a message
fragment, so a mutation caught only by an unrelated rule does not count.

1. edited cannot turn a red gate green, nor wash out a green one.
2. edited cannot cancel the full run in progress.
3. A required check can never be left pending for ever.
4. Pure-documentation detection is the second step's work. Until it exists,
   every change runs the full suite. The guard here: the shards take every
   tracked test file, the pytest job and its steps carry no condition, and
   the "Run the shard" command deselects nothing (no ``-k``, ``--ignore``,
   ``--deselect`` or ``PYTEST_ADDOPTS``; ``-m`` is only
   ``"not gui_acceptance"``).
5. The full-suite backstop: push runs on dev and main are never interrupted
   once running (a newer push may still replace a queued run) and pytest is
   not conditioned on the event.
6. The gate tells an intended skip from a failure or a cancel: only
   success/success passes (truth table in ``test_gate_summary_truth_table``);
   here, the gate's one step cannot be skipped or run under another shell.
"""

from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import tests.support.ci_download.gate_scope_policy as scope
import tests.support.ci_download.github_gate_policy as policy

ROOT = Path(__file__).resolve().parents[4]
TREE = scope.tree_documents()
GATE = policy.GATE_FILE
HYGIENE = policy.PR_HYGIENE_FILE
SELECT_STEP = "Select this shard's test files"
RUN_STEP = "Run the shard"
ONLY_MARKER = '"not gui_acceptance"'
DESELECTING = re.compile(r"(?:^|\s)(-k\S*|--keyword\S*|--ignore\S*|--deselect\S*)")
MARKER = re.compile(r"(?:^|\s)(?:-m|--markexpr)(?:=|\s+)?(\"[^\"]*\"|'[^']*'|\S+)")
SHARDS = 4

Mutator = Callable[[dict[str, dict[str, Any]]], None]


def on(document: dict[str, Any]) -> dict[str, Any]:
    return document["on"] if "on" in document else document[True]


def hygiene_job(tree: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return tree[HYGIENE]["jobs"]["pr-hygiene"]


def gate_job(tree: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return tree[GATE]["jobs"]["gate"]


def full_suite_offenders(document: dict[str, Any]) -> list[str]:
    """Anything in ``gate.yml`` that would let a change run less than the full suite."""

    found: list[str] = []
    pytest_job = policy.jobs_of(document).get("pytest") or {}
    if "if" in pytest_job:
        found.append(f"pytest job is conditioned: if {pytest_job['if']!r}")
    steps = [step for step in pytest_job.get("steps") or [] if isinstance(step, dict)]
    for index, step in enumerate(steps):
        if "if" in step and not policy.is_instrumentation(step):
            found.append(f"pytest step[{index}] is conditioned: if {step['if']!r}")
        if "github.event" in str(step.get("if") or ""):
            found.append(f"pytest step[{index}] depends on the event")
    select = [step for step in steps if step.get("name") == SELECT_STEP]
    if len(select) != 1:
        found.append(f"{len(select)} steps named {SELECT_STEP!r}, not exactly one")
    elif "scripts/ci/pytest_impact.py" not in str(select[0].get("run")) or "--emit-shard" not in str(select[0].get("run")):
        found.append("the shard input does not use the shared selector")
    run = [step for step in steps if step.get("name") == RUN_STEP]
    if len(run) != 1:
        found.append(f"{len(run)} steps named {RUN_STEP!r}, not exactly one")
    else:
        command = str(run[0].get("run"))
        found.extend(f"{RUN_STEP!r} deselects tests with {match}" for match in DESELECTING.findall(command))
        if MARKER.findall(command) != [ONLY_MARKER]:
            found.append(f"{RUN_STEP!r} marker selection is {MARKER.findall(command)}, must be only [{ONLY_MARKER}]")
        if "PYTEST_ADDOPTS" in str(run[0].get("env") or {}) or "PYTEST_ADDOPTS" in command:
            found.append(f"{RUN_STEP!r} sets PYTEST_ADDOPTS")
    for trigger, config in (policy.triggers(document) or {}).items():
        if isinstance(config, dict) and ({"paths", "paths-ignore"} & set(config)):
            found.append(f"on.{trigger} has a path filter")
    return found


def mutated(mutator: Mutator) -> dict[str, dict[str, Any]]:
    tree = copy.deepcopy(TREE)
    mutator(tree)
    assert tree != TREE, "the mutation did not apply"
    return tree


def assert_reported(mutator: Mutator, fragment: str) -> None:
    messages = [str(item) for item in scope.gate_scope_offenders(mutated(mutator))]
    assert any(fragment in message for message in messages), (fragment, messages)


def test_the_real_tree_has_no_gate_scope_offender():
    assert set(policy.REQUIRED_CHECK_OWNERS.values()) <= set(TREE)
    assert scope.gate_scope_offenders(TREE) == []
    assert full_suite_offenders(TREE[GATE]) == []


def test_invariant_1_only_gate_yml_reports_gate_and_it_ignores_edited():
    assert scope.trigger_values(TREE[GATE], "pull_request", "types") == {"opened", "synchronize", "reopened"}
    owners = {
        check: sorted(name for name, document in TREE.items() if check in {
            claimed for job_name, job in policy.jobs_of(document).items()
            for claimed in scope.reserved_names_of(job_name, job)
        })
        for check in policy.REQUIRED_CHECK_OWNERS
    }
    assert owners == {"gate": [GATE], "pr-hygiene": [HYGIENE]}


@pytest.mark.parametrize(
    "case_id,mutator,fragment",
    [
        ("I1-gate-listens-to-edited", lambda t: on(t[GATE])["pull_request"]["types"].append("edited"), "must be exactly"),
        ("I1-hygiene-defines-gate", lambda t: t[HYGIENE]["jobs"].update(gate=copy.deepcopy(gate_job(t))), "reserved check name 'gate'"),
        ("I1-hygiene-job-named-gate", lambda t: hygiene_job(t).update(name=" Gate "), "reserved check name 'gate'"),
        ("I1-another-workflow-reports-gate", lambda t: t.update({"extra.yml": {"on": "pull_request", "jobs": {"GATE": {"runs-on": "ubuntu-latest", "steps": []}}}}), "reserved check name 'gate'"),
        ("I1-gate-yml-regains-pr-hygiene", lambda t: t[GATE]["jobs"].update({"pr-hygiene": copy.deepcopy(hygiene_job(t))}), "reserved check name 'pr-hygiene'"),
    ],
)
def test_invariant_1_mutations_are_reported(case_id, mutator, fragment):
    """Invariant 1: an edited run must not be able to replace or wash out a gate result."""

    assert_reported(mutator, fragment)


def test_invariant_2_the_two_checks_never_share_a_concurrency_group():
    gate_group = scope.concurrency_group(TREE[GATE]["concurrency"])
    hygiene_group = scope.concurrency_group(TREE[HYGIENE]["concurrency"])
    assert gate_group == scope.REQUIRED_CONCURRENCY_GROUPS[GATE] == "gate-${{ github.event.pull_request.number || github.ref }}"
    assert hygiene_group == scope.REQUIRED_CONCURRENCY_GROUPS[HYGIENE] == "pr-hygiene-${{ github.event.pull_request.number }}"
    assert TREE[HYGIENE]["concurrency"]["cancel-in-progress"] is True
    for name, document in TREE.items():
        for job in policy.jobs_of(document).values():
            assert "concurrency" not in job or name not in policy.STRICT_FILES, name


@pytest.mark.parametrize(
    "case_id,mutator,fragment",
    [
        ("I2-hygiene-joins-gate-group", lambda t: t[HYGIENE]["concurrency"].update(group="gate-${{ github.event.pull_request.number }}"), "pr-hygiene.yml:0: concurrency group is 'gate-"),
        ("I2-gate-joins-hygiene-group", lambda t: t[GATE]["concurrency"].update(group="pr-hygiene-${{ github.ref }}"), "gate.yml:0: concurrency group is 'pr-hygiene-"),
        ("S4-gate-shared", lambda t: t[GATE]["concurrency"].update(group="gate-shared"), "concurrency group is 'gate-shared', must be exactly"),
        ("S4-pr-hygiene-all", lambda t: t[HYGIENE]["concurrency"].update(group="pr-hygiene-all"), "concurrency group is 'pr-hygiene-all', must be exactly"),
        ("S4-gate-group-without-ref", lambda t: t[GATE]["concurrency"].update(group="gate-${{ github.event.pull_request.number }}"), "gate.yml:0: concurrency group is 'gate-${{ github.event.pull_request.number }}'"),
        ("I2-gate-job-level-group-uses-hygiene-prefix", lambda t: t[GATE]["jobs"]["static"].update(concurrency="pr-hygiene-x"), "job static concurrency group 'pr-hygiene-x' uses pr-hygiene.yml's prefix"),
        ("I2-other-workflow-uses-gate-prefix", lambda t: t["windows-smoke.yml"]["concurrency"].update(group="Gate-${{ github.ref }}"), "uses gate.yml's prefix"),
        ("I2-job-level-group-uses-gate-prefix", lambda t: t["windows-smoke.yml"]["jobs"]["build-wheel"].update(concurrency="gate-x"), "job build-wheel concurrency group"),
        ("I2-hygiene-job-level-concurrency", lambda t: hygiene_job(t).update(concurrency={"group": "pr-hygiene-x"}), "job-level concurrency"),
    ],
)
def test_invariant_2_mutations_are_reported(case_id, mutator, fragment):
    """Invariant 2: an edited run must not cancel a full run in progress."""

    assert_reported(mutator, fragment)


def test_invariant_3_both_required_checks_always_appear():
    for name in policy.STRICT_FILES:
        assert scope.trigger_filter_offenders(TREE[name], name) == []
    assert scope.trigger_values(TREE[HYGIENE], "pull_request", "types") == scope.PR_HYGIENE_PR_TYPES
    assert scope.trigger_values(TREE[HYGIENE], "pull_request", "branches") == scope.trigger_values(
        TREE[GATE], "pull_request", "branches"
    )
    assert {"if", "strategy", "name", "needs", "environment"}.isdisjoint(hygiene_job(TREE))
    assert list(TREE[HYGIENE]["jobs"]) == ["pr-hygiene"]
    assert gate_job(TREE)["if"] == "always()"


def add_prep_job(needs: bool) -> Mutator:
    """critic2 S1: a ``prep`` job skipped on edited, which ``pr-hygiene`` may depend on."""

    def mutate(tree: dict[str, dict[str, Any]]) -> None:
        tree[HYGIENE]["jobs"]["prep"] = {
            "if": "github.event.action != 'edited'",
            "runs-on": "ubuntu-latest",
            "steps": [{"run": "true"}],
        }
        if needs:
            hygiene_job(tree)["needs"] = ["prep"]

    return mutate


@pytest.mark.parametrize(
    "case_id,mutator,fragment",
    [
        ("I3-hygiene-job-if", lambda t: hygiene_job(t).update({"if": "github.event.action == 'edited'"}), "job pr-hygiene if"),
        ("I3-hygiene-matrix", lambda t: hygiene_job(t).update(strategy={"matrix": {"x": [1]}}), "has a strategy"),
        ("I3-hygiene-name", lambda t: hygiene_job(t).update(name="Submitted-by"), "overrides its check name"),
        ("I3-hygiene-step-if", lambda t: hygiene_job(t)["steps"][-1].update({"if": "false"}), "step-level if"),
        ("I3-gate-if-success", lambda t: gate_job(t).update({"if": "success()"}), "must be 'always()'"),
        ("I3-hygiene-paths-ignore", lambda t: on(t[HYGIENE])["pull_request"].update({"paths-ignore": ["docs/**"]}), "defines 'paths-ignore'"),
        ("I3-hygiene-branches-ignore", lambda t: on(t[HYGIENE])["pull_request"].update({"branches-ignore": ["x"]}), "defines 'branches-ignore'"),
        ("I3-gate-paths", lambda t: on(t[GATE])["push"].update(paths=["src/**"]), "defines 'paths'"),
        ("I3-hygiene-drops-edited", lambda t: on(t[HYGIENE])["pull_request"].update(types=["opened", "synchronize", "reopened"]), "pr-hygiene.yml:0: on.pull_request.types"),
        ("I3-hygiene-branches-differ", lambda t: on(t[HYGIENE])["pull_request"].update(branches=["dev"]), "differ from gate.yml"),
        ("I3-hygiene-on-push", lambda t: on(t[HYGIENE]).update(push={"branches": ["dev"]}), "must be only pull_request"),
        ("I3-hygiene-file-missing", lambda t: t.pop(HYGIENE), "owning file of a required check is missing"),
        ("S1-prep-and-needs", add_prep_job(needs=True), "job pr-hygiene has needs: ['prep']"),
        ("S1-prep-and-needs-second-job", add_prep_job(needs=True), "not exactly one job"),
        ("S1-second-job-alone", add_prep_job(needs=False), "defines 2 jobs ['pr-hygiene', 'prep'], not exactly one job"),
        ("S1-needs-alone", lambda t: hygiene_job(t).update(needs=[]), "job pr-hygiene has needs: []"),
        ("N5-hygiene-environment", lambda t: hygiene_job(t).update(environment="review"), "job pr-hygiene has environment: 'review'"),
        ("N5-gate-environment", lambda t: gate_job(t).update(environment={"name": "review"}), "job gate has environment"),
    ],
)
def test_invariant_3_mutations_are_reported(case_id, mutator, fragment):
    """Invariant 3: a skipped job reports Success and a filtered workflow stays pending."""

    assert_reported(mutator, fragment)


def shard_files(script: str, shard: int, runner_temp: Path) -> list[str]:
    bash = shutil.which("bash")
    if bash is None:
        pytest.fail("bash is required to run the shard selection step")
    proc = subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail"],
        input=script.replace("${{ matrix.shard }}", str(shard)),
        text=True,
        capture_output=True,
        cwd=ROOT,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHON_VERSION": "3.12",
            "RUNNER_TEMP": str(runner_temp),
        },
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return (runner_temp / "shard-files.txt").read_text(encoding="utf-8").split()


def test_invariant_4_uses_the_shared_selector_and_four_shards():
    steps = policy.jobs_of(TREE[GATE])["pytest"]["steps"]
    script = next(step["run"] for step in steps if step.get("name") == SELECT_STEP)
    run_script = str(next(step["run"] for step in steps if step.get("name") == RUN_STEP))
    assert "scripts/ci/pytest_impact.py" in script
    assert "--emit-shard" in script
    assert "git ls-files -z -- 'tests/**/test_*.py' 'tests/test_*.py' 'tests/**/*_test.py' 'tests/*_test.py'" in run_script
    assert "tr -dc '\\0' | wc -c" in run_script
    assert policy.jobs_of(TREE[GATE])["pytest"]["strategy"]["matrix"]["shard"] == list(range(1, SHARDS + 1))


@pytest.mark.parametrize(
    "case_id,mutate",
    [
        ("I4-pytest-job-if", lambda d: d["jobs"]["pytest"].update({"if": "needs.classify.outputs.docs_only != 'true'"})),
        ("I4-select-tooling-only", lambda d: next(s for s in d["jobs"]["pytest"]["steps"] if s.get("name") == SELECT_STEP).update(run="git ls-files 'tests/tooling/**/test_*.py' > x")),
        ("I4-run-shard-step-if", lambda d: d["jobs"]["pytest"]["steps"][-2].update({"if": "github.event_name == 'push'"})),
        ("I4-run-shard-always-false", lambda d: d["jobs"]["pytest"]["steps"][-2].update({"if": "always() && false"})),
        ("I4-run-shard-always-or-false", lambda d: d["jobs"]["pytest"]["steps"][-2].update({"if": "always() || false"})),
        ("I4-run-shard-expression-always-false", lambda d: d["jobs"]["pytest"]["steps"][-2].update({"if": "${{ always() && false }}"})),
        ("I4-gate-paths-ignore", lambda d: on(d)["pull_request"].update({"paths-ignore": ["docs/**"]})),
        ("N3-ignore", lambda d: run_step(d).update(run=run_step(d)["run"].replace("-p no:cacheprovider", "--ignore=tests/tooling -p no:cacheprovider"))),
        ("N3-keyword", lambda d: run_step(d).update(run=run_step(d)["run"].replace("-p no:cacheprovider", '-k "not daemon" -p no:cacheprovider'))),
        ("N3-deselect", lambda d: run_step(d).update(run=run_step(d)["run"].replace("-p no:cacheprovider", "--deselect tests/x.py -p no:cacheprovider"))),
        ("N3-other-marker", lambda d: run_step(d).update(run=run_step(d)["run"].replace(ONLY_MARKER, '"not gui_acceptance and not slow"'))),
        ("N3-second-marker", lambda d: run_step(d).update(run=run_step(d)["run"].replace("-p no:cacheprovider", "-m slow -p no:cacheprovider"))),
        ("N3-addopts", lambda d: run_step(d)["env"].update(PYTEST_ADDOPTS="-k smoke")),
    ],
)
def test_invariant_4_guard_mutations_are_reported(case_id, mutate):
    document = copy.deepcopy(TREE[GATE])
    mutate(document)
    assert document != TREE[GATE]
    assert full_suite_offenders(document), case_id


def run_step(document: dict[str, Any]) -> dict[str, Any]:
    return next(step for step in document["jobs"]["pytest"]["steps"] if step.get("name") == RUN_STEP)


def test_invariant_4_the_shard_command_deselects_nothing():
    command = run_step(TREE[GATE])["run"]
    assert MARKER.findall(command) == [ONLY_MARKER]
    assert DESELECTING.findall(command) == []


def test_invariant_4_non_pr_metadata_must_be_full(tmp_path):
    metadata = "PLAN_MODE=selected\nPLAN_KIND=docs-only\nPLAN_TOTAL=7\nSHARD_INDEX=1\nSHARD_COUNT=7\nPLAN_DIGEST=" + "a" * 64 + "\n"
    result = run_empty_shard(tmp_path, metadata, event_name="push")
    assert result.returncode != 0
    assert not (tmp_path / "runner-temp" / "uv-called").exists()


def run_empty_shard(tmp_path: Path, metadata: str, event_name: str = "pull_request") -> subprocess.CompletedProcess:
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    (runner_temp / "pytest-plan.env").write_text(metadata, encoding="utf-8")
    (runner_temp / "shard-files.txt").write_text("", encoding="utf-8")
    stub = tmp_path / "uv"
    stub.write_text("#!/bin/sh\ntouch \"$RUNNER_TEMP/uv-called\"\nexit 99\n", encoding="utf-8")
    stub.chmod(0o755)
    command = run_step(TREE[GATE])["run"].replace("${{ matrix.shard }}", "2")
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail"],
        input=command,
        text=True,
        capture_output=True,
        cwd=ROOT,
        env={
            "PATH": f"{tmp_path}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_EVENT_NAME": event_name,
        },
        check=False,
    )


@pytest.mark.parametrize(
    "metadata,expected",
    [
        ("PLAN_MODE=selected\nPLAN_KIND=docs-only\nPLAN_TOTAL=7\nSHARD_INDEX=2\nSHARD_COUNT=0\nPLAN_DIGEST=" + "a" * 64 + "\n", True),
        ("PLAN_MODE=full\nPLAN_KIND=full\nPLAN_TOTAL=7\nSHARD_INDEX=2\nSHARD_COUNT=0\nPLAN_DIGEST=" + "a" * 64 + "\n", False),
        ("PLAN_MODE=selected\nPLAN_KIND=docs-only\nPLAN_TOTAL=0\nSHARD_INDEX=2\nSHARD_COUNT=0\nPLAN_DIGEST=" + "a" * 64 + "\n", False),
        ("PLAN_MODE=selected\nPLAN_KIND=docs-only\nPLAN_TOTAL=17\nSHARD_INDEX=2\nSHARD_COUNT=0\nPLAN_DIGEST=" + "a" * 64 + "\n", False),
        ("PLAN_MODE=selected\nPLAN_KIND=docs-only\nPLAN_TOTAL=7\nSHARD_INDEX=1\nSHARD_COUNT=0\nPLAN_DIGEST=" + "a" * 64 + "\n", False),
    ],
)
def test_invariant_4_empty_shard_contract(metadata, expected, tmp_path):
    result = run_empty_shard(tmp_path, metadata)
    assert (result.returncode == 0) is expected, result.stderr
    if expected:
        assert "PYTEST_SHARD_EMPTY" in result.stdout
        assert not (tmp_path / "runner-temp" / "uv-called").exists()
    else:
        assert "PYTEST_SHARD_EMPTY" not in result.stdout


@pytest.mark.parametrize(
    "metadata",
    [
        "PLAN_MODE=selected\nPLAN_MODE=full\nPLAN_KIND=selected\nPLAN_TOTAL=7\nSHARD_INDEX=1\nSHARD_COUNT=1\nPLAN_DIGEST=" + "a" * 64 + "\n",
        "PLAN_MODE=selected\nPLAN_KIND=selected\nPLAN_TOTAL=7\nSHARD_INDEX=1\nSHARD_COUNT=1\nPLAN_DIGEST=short\n",
        "PLAN_MODE=selected\nPLAN_KIND=selected\nPLAN_TOTAL=7\nSHARD_INDEX=1\nSHARD_COUNT=1\nEXTRA=value\nPLAN_DIGEST=" + "a" * 64 + "\n",
    ],
)
def test_invariant_4_metadata_requires_unique_controlled_keys_and_digest(metadata, tmp_path):
    result = run_empty_shard(tmp_path, metadata)
    assert result.returncode != 0
    assert not (tmp_path / "runner-temp" / "uv-called").exists()


def test_invariant_5_push_runs_are_a_full_backstop_never_interrupted_once_running():
    assert TREE[GATE]["concurrency"]["cancel-in-progress"] == scope.PR_ONLY_CANCEL
    assert scope.trigger_values(TREE[GATE], "push", "branches") == scope.BACKSTOP_BRANCHES == {"dev", "main"}
    assert "if" not in policy.jobs_of(TREE[GATE])["pytest"]


@pytest.mark.parametrize(
    "case_id,mutator,fragment",
    [
        ("I5-cancel-true", lambda t: t[GATE]["concurrency"].update({"cancel-in-progress": True}), "cancel-in-progress is True"),
        ("I5-cancel-push-too", lambda t: t[GATE]["concurrency"].update({"cancel-in-progress": "${{ github.event_name != 'workflow_dispatch' }}"}), "cancel-in-progress is"),
        ("I5-push-drops-main", lambda t: on(t[GATE])["push"].update(branches=["dev"]), "on.push.branches"),
        ("I5-push-removed", lambda t: on(t[GATE]).pop("push"), "on.push.branches"),
        ("N1-push-negates-main", lambda t: on(t[GATE])["push"]["branches"].append("!main"), "on.push.branches is ['!main', 'dev', 'main'], must be exactly"),
    ],
)
def test_invariant_5_mutations_are_reported(case_id, mutator, fragment):
    """Invariant 5: the push backstop exists for dev and main and is not interrupted."""

    assert_reported(mutator, fragment)


def test_invariant_5_pytest_conditioned_on_the_event_is_reported():
    document = copy.deepcopy(TREE[GATE])
    document["jobs"]["pytest"]["if"] = "github.event_name == 'push'"
    assert any("pytest job is conditioned" in item for item in full_suite_offenders(document))


def test_invariant_6_the_gate_has_one_unconditional_step():
    steps = gate_job(TREE)["steps"]
    assert len(steps) == 1 and "if" not in steps[0] and "shell" not in steps[0]
    assert steps[0]["run"].startswith(scope.STRICT_PREAMBLE)


@pytest.mark.parametrize(
    "case_id,mutator,fragment",
    [
        ("I6-gate-step-if", lambda t: gate_job(t)["steps"][0].update({"if": "github.event_name == 'push'"}), "step-level if"),
        ("I6-gate-second-step", lambda t: gate_job(t)["steps"].append({"run": "set -euo pipefail\necho"}), "not exactly one"),
        ("I6-gate-step-shell", lambda t: gate_job(t)["steps"][0].update(shell="sh"), "overrides the shell"),
        ("I6-gate-job-default-shell", lambda t: gate_job(t).update(defaults={"run": {"shell": "sh"}}), "job defaults.run.shell"),
        ("I6-gate-workflow-default-shell", lambda t: t[GATE].update(defaults={"run": {"shell": "sh"}}), "workflow defaults.run.shell"),
        ("I6-gate-loses-strict-mode", lambda t: gate_job(t)["steps"][0].update(run="test \"${STATIC}\" = success"), "does not start with 'set -euo pipefail'"),
    ],
)
def test_invariant_6_mutations_are_reported(case_id, mutator, fragment):
    """Invariant 6: a skipped or reshelled verdict step would report Success."""

    assert_reported(mutator, fragment)
