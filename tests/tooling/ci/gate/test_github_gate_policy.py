"""The GitHub merge gate's workflow-tree policies, each with a control that must go red.

Every rule is enforced by one reader in ``github_gate_policy``. A reader that
finds nothing proves only that it found nothing, so each tree-level test pairs
a positive assertion on the real workflow with a synthetic workflow that breaks
exactly the property under test. The synthetic ones are written into a
``tmp_path`` workflow directory; the real tree is only read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import tests.support.ci_download.github_gate_policy as policy

PINNED = "actions/checkout@" + "a" * 40
GATE_TEXT = policy.gate_path().read_text(encoding="utf-8")
GATE = policy.load(GATE_TEXT)


def write(tmp_path: Path, name: str, document: dict | str) -> None:
    directory = tmp_path / "workflows"
    directory.mkdir(exist_ok=True)
    text = document if isinstance(document, str) else yaml.safe_dump(document, sort_keys=False)
    (directory / name).write_text(text, encoding="utf-8")


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """Point every tree reader at a synthetic workflow directory."""

    directory = tmp_path / "workflows"
    directory.mkdir()
    monkeypatch.setattr(policy, "WORKFLOW_DIR", directory)
    write(tmp_path, "gate.yml", GATE_TEXT)
    return directory


def test_discovery_covers_yml_and_yaml_and_nothing_else(tmp_path, monkeypatch):
    for name in ("a.yml", "b.yaml", "c.txt", "d.yml.bak"):
        (tmp_path / name).write_text("jobs: {}\n")
    monkeypatch.setattr(policy, "WORKFLOW_DIR", tmp_path)
    assert sorted(path.name for path in policy.workflow_files()) == ["a.yml", "b.yaml"]


def test_a_second_workflow_is_checked_alongside_the_gate(tree):
    """The gate is the merge gate today; the reader must not depend on it being alone."""

    write(
        tree.parent,
        "extra.yaml",
        "on: pull_request_target\njobs: {}\n",
    )
    assert policy.workflow_files() == [tree / "gate.yml", tree / "extra.yaml"]
    document = policy.load((tree / "extra.yaml").read_text())
    assert policy.pull_request_target_offenders(document, "extra.yaml")


def test_the_gate_is_present_and_is_the_merge_gate():
    assert policy.gate_path().is_file()
    assert GATE["name"] == "gate"
    assert set(policy.jobs_of(GATE)) == {"static", "pytest", "pr-hygiene", "gate"}
    assert policy.jobs_of(GATE)["gate"]["needs"] == ["static", "pytest", "pr-hygiene"]


def test_the_gate_sets_no_env_name_twice_in_another_case():
    assert policy.case_duplicate_env_offenders(GATE_TEXT, "gate.yml") == []


@pytest.mark.parametrize(
    "block",
    [
        "env:\n  Path: /a\n  PATH: /b\n",
        "env:\n  http_proxy: ''\n  HTTP_PROXY: ''\n",
    ],
)
def test_a_case_folded_duplicate_in_the_workflow_env_is_reported(block):
    offenders = policy.case_duplicate_env_offenders(block, "wf.yml")
    assert len(offenders) == 1
    assert offenders[0].line == 3


def test_a_case_folded_duplicate_is_found_at_job_and_step_level():
    text = (
        "on: push\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    env:\n"
        "      NO_COLOR: '1'\n"
        "    steps:\n"
        "      - run: echo\n"
        "        env:\n"
        "          Foo: a\n"
        "          FOO: b\n"
        "      - run: echo\n"
        "        env:\n"
        "          BAR: c\n"
        "          bar: d\n"
    )
    offenders = policy.case_duplicate_env_offenders(text, "wf.yml")
    assert [(item.line, item.message.split("'")[1]) for item in offenders] == [
        (11, "Foo"),
        (15, "BAR"),
    ]


def test_a_step_overriding_a_workflow_variable_is_not_a_duplicate():
    """The same name at two levels is an override, which is how ``env`` is meant to work."""

    text = (
        "env:\n"
        "  PYTHON_VERSION: '3.12'\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    env:\n"
        "      PYTHON_VERSION: '3.13'\n"
        "    steps:\n"
        "      - run: echo\n"
        "        env:\n"
        "          PYTHON_VERSION: '3.14'\n"
    )
    assert policy.case_duplicate_env_offenders(text, "wf.yml") == []


def test_the_duplicate_reader_sees_what_a_plain_load_hides():
    """``yaml.safe_load`` keeps only the last of two equal keys; compose keeps both."""

    text = "env:\n  A: first\n  A: second\n"
    assert policy.load(text)["env"] == {"A": "second"}
    offenders = policy.case_duplicate_env_offenders(text, "wf.yml")
    assert len(offenders) == 1 and "A" in offenders[0].message


def test_distinct_names_are_not_duplicates():
    text = "env:\n  PATH: /a\n  PAT: /b\n  HOME: /c\n  HOMEDRIVE: d\n"
    assert policy.case_duplicate_env_offenders(text, "wf.yml") == []


def test_no_artifact_action_older_than_v4_remains():
    for name in policy.workflow_files():
        assert policy.retired_artifact_offenders(name.read_text(), name.name) == []


@pytest.mark.parametrize("uses", ["actions/upload-artifact@v3", "actions/upload-artifact@v2", "actions/download-artifact@v1", "actions/upload-artifact@V3"])
def test_a_retired_artifact_action_is_reported_with_its_line(uses):
    text = (
        "on: push\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo\n"
        "      - uses: " + uses + "\n"
    )
    offenders = policy.retired_artifact_offenders(text, "wf.yml")
    assert len(offenders) == 1
    assert offenders[0].line == 7 and uses in offenders[0].message


@pytest.mark.parametrize("uses", ["actions/upload-artifact@v4", "actions/upload-artifact@v7.0.2", "actions/upload-artifact@" + "b" * 40, "actions/cache@v3", "./local"])
def test_a_current_artifact_action_or_another_action_is_not_reported(uses):
    text = "jobs:\n  build:\n    steps:\n      - uses: " + uses + "\n"
    assert policy.retired_artifact_offenders(text, "wf.yml") == []


def test_the_major_version_reader_distinguishes_a_sha_pin_from_a_version():
    assert policy.artifact_action_major("actions/upload-artifact@v3") == 3
    assert policy.artifact_action_major("actions/upload-artifact@v7.0.2") == 7
    assert policy.artifact_action_major("actions/upload-artifact@" + "c" * 40) == 0
    assert policy.artifact_action_major("actions/checkout@v5") is None


def test_every_action_in_the_tree_is_pinned_to_a_full_sha():
    used = []
    for name in policy.workflow_files():
        document = policy.load(name.read_text())
        used += policy.all_uses(document)
        assert policy.unpinned_action_offenders(document, name.name) == []
    assert used, "no action is used at all; the pin check proved nothing"
    assert all(len(item.rpartition("@")[2]) == 40 for item in used)


@pytest.mark.parametrize(
    "uses",
    ["actions/checkout@v5", "actions/checkout@main", "actions/checkout", "actions/checkout@" + "a" * 39, "actions/checkout@" + "g" * 40],
)
def test_an_action_that_is_not_pinned_to_40_hex_is_reported(uses):
    document = {"jobs": {"build": {"steps": [{"uses": uses}]}}}
    offenders = policy.unpinned_action_offenders(document, "wf.yml")
    assert len(offenders) == 1 and uses in offenders[0].message


@pytest.mark.parametrize("uses", [PINNED, "./.github/actions/local", "docker://alpine:3.20"])
def test_a_pinned_or_local_action_is_not_reported(uses):
    document = {"jobs": {"build": {"steps": [{"uses": uses}]}}}
    assert policy.unpinned_action_offenders(document, "wf.yml") == []


def test_a_reusable_workflow_call_is_pinned_like_a_step():
    document = {"jobs": {"call": {"uses": "owner/repo/.github/workflows/x.yml@main"}}}
    assert policy.unpinned_action_offenders(document, "wf.yml")
    document["jobs"]["call"]["uses"] = "owner/repo/.github/workflows/x.yml@" + "d" * 40
    assert policy.unpinned_action_offenders(document, "wf.yml") == []


def test_the_gate_declares_exactly_a_read_token():
    assert GATE["permissions"] == policy.MINIMAL_TOKEN
    assert policy.permissions_offenders(GATE, "gate.yml") == []


@pytest.mark.parametrize(
    "document",
    [
        {"jobs": {}},
        {"permissions": "read-all", "jobs": {}},
        {"permissions": "write-all", "jobs": {}},
        {"permissions": {"contents": "write"}, "jobs": {}},
        {"permissions": {"contents": "read", "packages": "write"}, "jobs": {}},
        {"permissions": [1, 2], "jobs": {}},
    ],
)
def test_a_missing_or_wider_token_is_reported(document):
    assert policy.permissions_offenders(document, "wf.yml")


@pytest.mark.parametrize(
    "declared",
    [{"contents": "read"}, {"contents": "none"}, {"contents": "read", "id-token": "none"}],
)
def test_a_minimal_token_passes(declared):
    assert policy.permissions_offenders({"permissions": declared, "jobs": {}}, "wf.yml") == []


def test_a_job_may_widen_its_own_token_without_widening_the_workflow():
    document = {
        "permissions": {"contents": "read"},
        "jobs": {"label": {"permissions": {"pull-requests": "write"}, "steps": []}},
    }
    assert policy.permissions_offenders(document, "wf.yml") == []


def test_no_workflow_uses_pull_request_target():
    for name in policy.workflow_files():
        assert policy.pull_request_target_offenders(policy.load(name.read_text()), name.name) == []


@pytest.mark.parametrize(
    "on",
    ["pull_request_target", ["push", "pull_request_target"], {"pull_request_target": {"branches": ["dev"]}}],
)
def test_pull_request_target_is_reported_in_every_trigger_shape(on):
    assert policy.pull_request_target_offenders({"on": on}, "wf.yml")


def test_a_run_step_that_mentions_the_trigger_is_not_a_trigger():
    text = (
        "on: pull_request\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo 'never use pull_request_target'\n"
    )
    assert policy.pull_request_target_offenders(policy.load(text), "wf.yml") == []


def test_the_gate_runs_pull_request_code_on_hosted_runners():
    assert "pull_request" in policy.trigger_names(GATE)
    assert policy.unhosted_runner_offenders(GATE, "gate.yml") == []


@pytest.mark.parametrize(
    "runs_on,offending",
    [
        ("self-hosted", ["self-hosted"]),
        ("release-ci", ["release-ci"]),
        (["self-hosted", "windows", "x64"], ["self-hosted", "windows", "x64"]),
        ("${{ matrix.runner }}", ["${{ matrix.runner }}"]),
        ({"label": "self-hosted"}, ["label=self-hosted"]),
    ],
)
def test_an_unhosted_or_unresolved_runner_on_a_pull_request_workflow_is_reported(runs_on, offending):
    document = {"on": "pull_request", "jobs": {"build": {"runs-on": runs_on, "steps": []}}}
    offenders = policy.unhosted_runner_offenders(document, "wf.yml")
    assert len(offenders) == len(offending), offenders
    assert all("job build" in item.message for item in offenders)
    for label in offending:
        assert any(repr(label) in item.message for item in offenders), label


def test_a_pull_request_job_with_no_runner_at_all_has_nothing_to_judge():
    """A job with neither ``runs-on`` nor ``uses`` is not a runner job; GitHub refuses
    the file, so there is no label to judge. The reader must not invent one."""

    document = {"on": "pull_request", "jobs": {"build": {"steps": []}}}
    assert policy.unhosted_runner_offenders(document, "wf.yml") == []


@pytest.mark.parametrize(
    "label",
    ["ubuntu-latest", "windows-latest", "windows-2025", "windows-11-arm", "macos-latest", "macos-14"],
)
def test_a_github_hosted_label_is_accepted(label):
    document = {"on": "pull_request", "jobs": {"build": {"runs-on": label, "steps": []}}}
    assert policy.unhosted_runner_offenders(document, "wf.yml") == []


def test_a_push_or_dispatch_workflow_is_not_judged_by_this_rule():
    document = {"on": ["push", "workflow_dispatch"], "jobs": {"build": {"runs-on": "self-hosted", "steps": []}}}
    assert policy.unhosted_runner_offenders(document, "wf.yml") == []


def test_a_job_with_no_runs_on_such_as_a_reusable_call_is_not_reported():
    document = {"on": "pull_request", "jobs": {"call": {"uses": "owner/repo/.github/workflows/x.yml@" + "a" * 40}}}
    assert policy.unhosted_runner_offenders(document, "wf.yml") == []


def test_the_pull_request_check_and_the_acquisition_audit_share_one_hosted_registry():
    """Two lists of hosted labels would drift; the check reads the audit's."""

    from tests.support.ci_download.runners import GITHUB_HOSTED_VM_LABELS, runner_labels

    assert GITHUB_HOSTED_VM_LABELS <= policy.GITHUB_HOSTED_RUNNER_LABELS
    assert {"windows-latest", "windows-2025", "windows-11-arm"} <= GITHUB_HOSTED_VM_LABELS
    for label in sorted(policy.GITHUB_HOSTED_RUNNER_LABELS):
        assert runner_labels(label) == (label,)
    with pytest.raises(ValueError, match="runs-on"):
        runner_labels("self-hosted")


def test_the_gate_sets_no_proxy_so_its_artifact_step_clears_none():
    for job_name in policy.jobs_of(GATE):
        assert not policy.sets_proxy(policy.env_mapping(GATE, job_name))
    steps = list(policy.artifact_steps(GATE_TEXT))
    assert len(steps) == 1 and steps[0][0] == "pytest"
    assert steps[0][2].get("if") == "always()"
    assert "env" not in steps[0][2]
    assert policy.proxy_leaking_artifact_steps(GATE_TEXT, "gate.yml") == []


def unbounded_artifact_steps(text: str) -> list[str]:
    """Artifact steps whose ``timeout-minutes`` is missing, zero or not a number.

    A step without its own cap runs until the job's ``timeout-minutes`` ends
    the job (the 2026-10-29 shape: an upload retrying at 0.0% until the job
    died before any test ran).
    """

    unbounded = []
    for job_name, _line, step in policy.artifact_steps(text):
        timeout = step.get("timeout-minutes")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            unbounded.append(f"{job_name}: {step.get('uses')} timeout-minutes={timeout!r}")
    return unbounded


def test_every_artifact_step_of_the_merge_gate_is_bounded():
    """The gate's artifact step is an upload: it has no ``continue-on-error``
    (rule 8), so an unbounded one could hang a required job."""

    steps = list(policy.artifact_steps(GATE_TEXT))
    assert any(step.get("uses", "").startswith("actions/upload-artifact") for _job, _line, step in steps)
    assert unbounded_artifact_steps(GATE_TEXT) == []


@pytest.mark.parametrize("timeout", [None, 0, -1, "5", True])
def test_an_artifact_step_without_a_positive_integer_cap_is_unbounded(timeout):
    text = artifact_workflow("nowhere", clears=False)
    if timeout is not None:
        text = text + "        timeout-minutes: " + json.dumps(timeout) + "\n"
    assert len(unbounded_artifact_steps(text)) == 1, timeout
    bounded = artifact_workflow("nowhere", clears=False) + "        timeout-minutes: 5\n"
    assert unbounded_artifact_steps(bounded) == []


def artifact_workflow(proxy_at: str, clears: bool, *, proxy_key: str = "HTTPS_PROXY") -> str:
    step_env = {name: "" for name in policy.PROXY_VARIABLES} if clears else {}
    workflow_env = f"env:\n  {proxy_key}: http://proxy.invalid:3128\n" if proxy_at == "workflow" else ""
    job_env = f"    env:\n      {proxy_key}: http://proxy.invalid:3128\n" if proxy_at == "job" else ""
    return (
        "on: push\n" + workflow_env + "jobs:\n  build:\n    runs-on: ubuntu-latest\n" + job_env
        + "    steps:\n      - uses: actions/upload-artifact@" + "e" * 40 + "\n"
        + "        env:\n" + "".join(f"          {k}: '{v}'\n" for k, v in step_env.items())
    )


def test_proxy_variables_holds_the_two_canonical_names():
    assert policy.PROXY_VARIABLES == ("HTTP_PROXY", "HTTPS_PROXY")


@pytest.mark.parametrize("proxy_at", ["workflow", "job"])
def test_an_artifact_step_under_a_proxy_must_clear_both_proxy_variables(proxy_at):
    leaking = policy.proxy_leaking_artifact_steps(artifact_workflow(proxy_at, clears=False), "wf.yml")
    assert len(leaking) == 1
    assert all(name in leaking[0].message for name in policy.PROXY_VARIABLES)
    cleared = artifact_workflow(proxy_at, clears=True)
    assert policy.proxy_leaking_artifact_steps(cleared, "wf.yml") == []
    assert policy.case_duplicate_env_offenders(cleared, "wf.yml") == []


def test_an_artifact_step_with_no_proxy_above_it_needs_no_cleared_keys():
    text = artifact_workflow("nowhere", clears=False)
    assert policy.proxy_leaking_artifact_steps(text, "wf.yml") == []
    assert policy.artifact_steps(text)


def test_a_proxy_on_another_step_does_not_reach_the_artifact_step():
    text = (
        "on: push\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: curl https://example.invalid\n"
        "        env:\n"
        "          HTTPS_PROXY: http://proxy.invalid:3128\n"
        "      - uses: actions/upload-artifact@" + "e" * 40 + "\n"
    )
    assert policy.proxy_leaking_artifact_steps(text, "wf.yml") == []


def test_an_empty_proxy_above_the_artifact_step_is_no_proxy():
    text = artifact_workflow("job", clears=False).replace(
        "HTTPS_PROXY: http://proxy.invalid:3128", "HTTPS_PROXY: ''"
    )
    assert policy.proxy_leaking_artifact_steps(text, "wf.yml") == []


@pytest.mark.parametrize(
    "pair",
    [
        ("HTTP_PROXY", "HTTPS_PROXY"),
        ("http_proxy", "https_proxy"),
        ("Http_Proxy", "Https_Proxy"),
    ],
)
def test_clearing_one_letter_case_per_proxy_variable_passes_both_rules(pair):
    first, second = pair
    text = artifact_workflow("job", clears=False).replace(
        "        env:\n", f"        env:\n          {first}: ''\n          {second}: ''\n"
    )
    assert policy.proxy_leaking_artifact_steps(text, "wf.yml") == []
    assert policy.case_duplicate_env_offenders(text, "wf.yml") == []


def test_clearing_all_four_proxy_keys_trips_the_duplicate_rule():
    text = artifact_workflow("job", clears=False).replace(
        "        env:\n",
        "        env:\n"
        "          HTTP_PROXY: ''\n"
        "          HTTPS_PROXY: ''\n"
        "          http_proxy: ''\n"
        "          https_proxy: ''\n",
    )
    assert len(policy.case_duplicate_env_offenders(text, "wf.yml")) == 2
    assert policy.proxy_leaking_artifact_steps(text, "wf.yml") == []


def test_mixed_case_proxy_above_artifact_step_is_detected():
    text = artifact_workflow("workflow", clears=False, proxy_key="Https_Proxy")
    assert policy.sets_proxy(policy.env_mapping(policy.load(text), "build"))
    offenders = policy.proxy_leaking_artifact_steps(text, "wf.yml")
    assert len(offenders) == 1
    assert "HTTP_PROXY" in offenders[0].message and "HTTPS_PROXY" in offenders[0].message


def test_nothing_in_the_merge_gate_is_relaxed():
    assert policy.relaxed_gate_surfaces(GATE_TEXT, "gate.yml") == []
    for job_name, job in policy.jobs_of(GATE).items():
        assert "continue-on-error" not in job, job_name
        for index, step in enumerate(job.get("steps") or []):
            assert "continue-on-error" not in step, (job_name, index)


@pytest.mark.parametrize("surface", ["job", "gate-step", "always-step", "summary-step"])
def test_a_relaxed_job_or_step_in_the_merge_gate_is_reported(surface):
    document = yaml.safe_load(GATE_TEXT)
    if surface == "job":
        document["jobs"]["static"]["continue-on-error"] = True
    elif surface == "always-step":
        document["jobs"]["pytest"]["steps"][-1]["continue-on-error"] = True
    elif surface == "summary-step":
        document["jobs"]["gate"]["steps"][0]["continue-on-error"] = True
    else:
        document["jobs"]["static"]["steps"][2]["continue-on-error"] = True
    text = yaml.safe_dump(document, sort_keys=False)
    assert text != GATE_TEXT, "the mutation did not apply"
    offenders = policy.relaxed_gate_surfaces(text, policy.GATE_FILE)
    assert len(offenders) == 1, offenders


def relaxed(smoke: dict | None = None, step: dict | None = None) -> str:
    """A Windows smoke job outside the merge gate, optionally with a relaxed step."""

    document = {
        "on": "push",
        "jobs": {
            "windows-smoke": {
                "runs-on": "windows-2025",
                "continue-on-error": True,
                "steps": [{"run": "hyprial --version"}],
            }
        },
    }
    if smoke is not None:
        document["jobs"]["windows-smoke"].update(smoke)
    if step is not None:
        document["jobs"]["windows-smoke"]["steps"][0].update(step)
    return yaml.safe_dump(document, sort_keys=False)


def test_another_workflow_may_relax_a_whole_job():
    assert policy.relaxed_gate_surfaces(relaxed(), "windows-smoke.yml") == []


def test_another_workflow_may_relax_an_instrumentation_step():
    text = relaxed(step={"continue-on-error": True, "if": "always()"})
    assert policy.relaxed_gate_surfaces(text, "windows-smoke.yml") == []


def test_no_other_workflow_relaxes_a_step_that_decides_the_result():
    text = relaxed(step={"continue-on-error": True})
    offenders = policy.relaxed_gate_surfaces(text, "windows-smoke.yml")
    assert len(offenders) == 1 and "step[0]" in offenders[0].message


def test_the_gate_file_name_decides_the_strictness_and_nothing_else():
    """The same relaxed job is refused under the gate's name and allowed under another."""

    text = relaxed()
    gate_named = policy.relaxed_gate_surfaces(text, policy.GATE_FILE)
    assert len(gate_named) == 1 and "job windows-smoke" in gate_named[0].message
    assert policy.relaxed_gate_surfaces(text, "windows-smoke.yml") == []


def test_a_false_flag_is_not_a_relaxation():
    text = relaxed(smoke={"continue-on-error": False}, step={"continue-on-error": False})
    assert policy.relaxed_gate_surfaces(text, policy.GATE_FILE) == []


def test_an_expression_on_a_merge_gate_job_is_reported():
    """A GitHub Actions expression on continue-on-error counts as a relaxation."""

    document = yaml.safe_load(GATE_TEXT)
    document["jobs"]["static"]["continue-on-error"] = "${{ matrix.leg == 'base' }}"
    text = yaml.safe_dump(document, sort_keys=False)
    offenders = policy.relaxed_gate_surfaces(text, policy.GATE_FILE)
    assert len(offenders) == 1 and "job static" in offenders[0].message


def test_an_expression_on_a_merge_gate_step_is_reported():
    """Step-level continue-on-error expressions are rejected in the merge gate."""

    document = yaml.safe_load(GATE_TEXT)
    document["jobs"]["static"]["steps"][2]["continue-on-error"] = "${{ matrix.leg == 'base' }}"
    text = yaml.safe_dump(document, sort_keys=False)
    offenders = policy.relaxed_gate_surfaces(text, policy.GATE_FILE)
    assert len(offenders) == 1 and "job static step[2]" in offenders[0].message


@pytest.mark.parametrize("value", [False, "false", "False", "FALSE"])
def test_literal_false_in_the_merge_gate_is_not_a_relaxation(value):
    """Literal false (boolean or case-insensitive string) is not a relaxation."""

    document = yaml.safe_load(GATE_TEXT)
    document["jobs"]["static"]["continue-on-error"] = value
    document["jobs"]["static"]["steps"][2]["continue-on-error"] = value
    text = yaml.safe_dump(document, sort_keys=False)
    assert policy.relaxed_gate_surfaces(text, policy.GATE_FILE) == []


def test_another_workflow_may_relax_a_job_with_an_expression():
    """Advisory workflows outside the merge gate may use job-level expressions."""

    text = relaxed(smoke={"continue-on-error": "${{ matrix.leg == 'base' }}"})
    assert policy.relaxed_gate_surfaces(text, "windows-followup.yml") == []


def test_no_other_workflow_relaxes_a_deciding_step_with_an_expression():
    """Non-gate workflows still forbid expression-form continue-on-error on non-always steps."""

    text = relaxed(
        smoke={"continue-on-error": "${{ matrix.leg == 'base' }}"},
        step={"continue-on-error": "${{ matrix.leg == 'base' }}"},
    )
    offenders = policy.relaxed_gate_surfaces(text, "windows-followup.yml")
    assert len(offenders) == 1 and "step[0]" in offenders[0].message


def test_another_workflow_may_relax_an_instrumentation_step_with_an_expression():
    """Non-gate workflows keep allowing instrumentation steps with if: always()."""

    text = relaxed(
        smoke={"continue-on-error": "${{ matrix.leg == 'base' }}"},
        step={"continue-on-error": "${{ matrix.leg == 'base' }}", "if": "always()"},
    )
    assert policy.relaxed_gate_surfaces(text, "windows-followup.yml") == []


UNPARSABLE = "on: [pull_request\njobs: {}\n"


@pytest.mark.parametrize(
    "reader",
    [
        policy.case_duplicate_env_offenders,
        policy.retired_artifact_offenders,
        policy.proxy_leaking_artifact_steps,
        policy.relaxed_gate_surfaces,
    ],
)
def test_a_reader_raises_on_a_file_that_does_not_parse(reader):
    """A parse error is raised, never turned into an empty finding list."""

    with pytest.raises(yaml.YAMLError):
        reader(UNPARSABLE, "broken.yml")


@pytest.mark.parametrize(
    "text",
    [
        "on: [pull_request\njobs: {}\n",
        "permissions: [unclosed\njobs: {}\n",
        "jobs:\n  build:\n    steps:\n    - run: echo\n      - run: echo\n",
    ],
)
def test_a_file_that_does_not_parse_cannot_be_loaded(text):
    """The document readers (pin, token, trigger, runner) take a loaded document.

    ``load`` raises on a file that does not parse instead of returning an
    empty document, so no document reader can judge such a file as clean.
    """

    with pytest.raises(yaml.YAMLError):
        policy.load(text)
    with pytest.raises(yaml.YAMLError):
        policy.compose(text)


def _tree_offenders(rule: str) -> list[policy.Finding]:
    """Run one workflow-tree policy reader across every discovered workflow file."""

    found: list[policy.Finding] = []
    for path in policy.workflow_files():
        text = path.read_text(encoding="utf-8")
        if rule == "case_duplicate_env":
            found.extend(policy.case_duplicate_env_offenders(text, path.name))
        elif rule == "permissions":
            found.extend(policy.permissions_offenders(policy.load(text), path.name))
        elif rule == "unhosted_runner":
            found.extend(policy.unhosted_runner_offenders(policy.load(text), path.name))
        elif rule == "proxy_leaking_artifact":
            found.extend(policy.proxy_leaking_artifact_steps(text, path.name))
        elif rule == "relaxed_gate_surfaces":
            found.extend(policy.relaxed_gate_surfaces(text, path.name))
        else:
            raise AssertionError(f"unknown rule {rule!r}")
    return found


TREE_WIDE_RULES = (
    "case_duplicate_env",
    "permissions",
    "unhosted_runner",
    "proxy_leaking_artifact",
    "relaxed_gate_surfaces",
)


@pytest.mark.parametrize("rule", TREE_WIDE_RULES)
def test_every_workflow_in_the_tree_satisfies_policy(rule):
    assert _tree_offenders(rule) == []


CLEAN_SECOND_WORKFLOW = (
    "name: windows-followup\n"
    "on:\n"
    "  pull_request:\n"
    "    branches: [dev]\n"
    "permissions:\n"
    "  contents: read\n"
    "jobs:\n"
    "  smoke:\n"
    "    runs-on: windows-latest\n"
    "    continue-on-error: true\n"
    "    steps:\n"
    "      - uses: actions/upload-artifact@" + "e" * 40 + "\n"
)


@pytest.mark.parametrize("rule", TREE_WIDE_RULES)
def test_a_clean_second_workflow_passes_every_tree_wide_rule(tree, rule):
    write(tree.parent, "windows-followup.yml", CLEAN_SECOND_WORKFLOW)
    assert _tree_offenders(rule) == []


@pytest.mark.parametrize(
    "case_id,rule,text,expected_count",
    [
        (
            "W01",
            "case_duplicate_env",
            "on: pull_request\npermissions:\n  contents: read\nenv:\n  Path: /a\n  PATH: /b\n"
            "jobs:\n  smoke:\n    runs-on: windows-latest\n    steps:\n      - run: echo ok\n",
            1,
        ),
        (
            "W02",
            "case_duplicate_env",
            "on: pull_request\npermissions:\n  contents: read\n"
            "jobs:\n  smoke:\n    runs-on: windows-latest\n    steps:\n      - run: echo ok\n"
            "        env:\n          HTTP_PROXY: ''\n          http_proxy: ''\n",
            1,
        ),
        (
            "W03",
            "permissions",
            "on: pull_request\njobs:\n  smoke:\n    runs-on: windows-latest\n    steps:\n      - run: echo ok\n",
            1,
        ),
        (
            "W04",
            "permissions",
            "on: pull_request\npermissions: write-all\n"
            "jobs:\n  smoke:\n    runs-on: windows-latest\n    steps:\n      - run: echo ok\n",
            1,
        ),
        (
            "W07",
            "relaxed_gate_surfaces",
            "on: pull_request\npermissions:\n  contents: read\n"
            "jobs:\n  smoke:\n    runs-on: windows-latest\n    steps:\n"
            "      - run: echo ok\n        continue-on-error: true\n",
            1,
        ),
        (
            "proxy-leak",
            "proxy_leaking_artifact",
            "on: pull_request\npermissions:\n  contents: read\n"
            "env:\n  HTTPS_PROXY: http://proxy.invalid:3128\n"
            "jobs:\n  smoke:\n    runs-on: windows-latest\n    steps:\n"
            "      - uses: actions/upload-artifact@" + "e" * 40 + "\n",
            1,
        ),
    ],
)
def test_a_defect_in_a_second_workflow_is_caught_by_tree_wide_rules(
    tree, case_id, rule, text, expected_count
):
    write(tree.parent, "windows-followup.yml", text)
    offenders = _tree_offenders(rule)
    assert len(offenders) == expected_count, (case_id, offenders)
    assert all(item.file == "windows-followup.yml" for item in offenders)


def test_w22_four_key_proxy_clear_in_second_workflow_trips_rule_1_only(tree):
    text = (
        "on: pull_request\n"
        "permissions:\n"
        "  contents: read\n"
        "env:\n"
        "  HTTPS_PROXY: http://proxy.invalid:3128\n"
        "jobs:\n"
        "  smoke:\n"
        "    runs-on: windows-latest\n"
        "    steps:\n"
        "      - uses: actions/upload-artifact@" + "e" * 40 + "\n"
        "        env:\n"
        "          HTTP_PROXY: ''\n"
        "          HTTPS_PROXY: ''\n"
        "          http_proxy: ''\n"
        "          https_proxy: ''\n"
    )
    write(tree.parent, "windows-followup.yml", text)
    dup_offenders = _tree_offenders("case_duplicate_env")
    assert len(dup_offenders) == 2
    assert all(item.file == "windows-followup.yml" for item in dup_offenders)
    assert _tree_offenders("proxy_leaking_artifact") == []


@pytest.mark.parametrize("label", ["macos-host", "release-ci", "publish-github"])
def test_registered_self_hosted_runner_in_second_pr_workflow_is_caught(tree, label):
    text = (
        "on: pull_request\n"
        "permissions:\n"
        "  contents: read\n"
        "jobs:\n"
        "  smoke:\n"
        f"    runs-on: {label}\n"
        "    steps:\n"
        "      - run: echo ok\n"
    )
    write(tree.parent, "windows-followup.yml", text)
    offenders = _tree_offenders("unhosted_runner")
    assert len(offenders) == 1
    assert offenders[0].file == "windows-followup.yml"
    assert repr(label) in offenders[0].message


def test_hosted_matrix_runners_on_pull_request_are_accepted():
    document = {
        "on": "pull_request",
        "jobs": {
            "build": {
                "runs-on": "${{ matrix.os }}",
                "strategy": {
                    "matrix": {
                        "os": ["ubuntu-latest", "windows-latest", "windows-2025"],
                        "include": [{"os": "windows-11-arm"}],
                    }
                },
                "steps": [],
            },
            "by-leg": {
                "runs-on": "${{ matrix.os }}",
                "strategy": {
                    "matrix": {
                        "leg": ["base", "head"],
                        "include": [
                            {"leg": "base", "os": "windows-latest"},
                            {"leg": "head", "os": "windows-2025"},
                        ],
                    }
                },
                "steps": [],
            },
        },
    }
    assert policy.unhosted_runner_offenders(document, "wf.yml") == []


@pytest.mark.parametrize(
    "matrix,expected_fragment",
    [
        ({"os": ["windows-latest", "windows-2022"]}, "'windows-2022'"),
        ({"os": ["windows-latest", "macos-26"]}, "'macos-26'"),
        ({"os": ["windows-latest", "macos-host"]}, "'macos-host'"),
        ({"os": ["windows-latest", {"group": "custom"}]}, "'${{ matrix.os }}'"),
        ({"os": ["windows-latest"], "include": [{"os": "windows-2022"}]}, "'windows-2022'"),
        ({"leg": ["a", "b"], "include": [{"leg": "a", "os": "windows-latest"}, {"leg": "b", "os": "macos-26"}]}, "'macos-26'"),
        ({"os": "windows-latest"}, "'${{ matrix.os }}'"),
        ({"other": ["windows-latest"]}, "'${{ matrix.os }}'"),
        ({"os": [123]}, "'${{ matrix.os }}'"),
    ],
)
def test_unhosted_or_unresolvable_matrix_runner_is_reported(matrix, expected_fragment):
    document = {
        "on": "pull_request",
        "jobs": {
            "build": {
                "runs-on": "${{ matrix.os }}",
                "strategy": {"matrix": matrix},
                "steps": [],
            }
        },
    }
    offenders = policy.unhosted_runner_offenders(document, "wf.yml")
    assert len(offenders) == 1, offenders
    assert expected_fragment in offenders[0].message


def test_only_the_gate_workflow_defines_a_gate_job():
    for path in policy.workflow_files():
        document = policy.load(path.read_text(encoding="utf-8"))
        assert policy.foreign_gate_job_offenders(document, path.name) == []


def test_a_second_workflow_defining_a_gate_job_is_reported(tree):
    bad = (
        "name: windows-followup\n"
        "on: pull_request\n"
        "permissions:\n"
        "  contents: read\n"
        "jobs:\n"
        "  gate:\n"
        "    runs-on: windows-latest\n"
        "    continue-on-error: true\n"
        "    steps:\n"
        "      - run: echo ok\n"
    )
    write(tree.parent, "windows-followup.yml", bad)
    found = [
        item
        for path in policy.workflow_files()
        for item in policy.foreign_gate_job_offenders(policy.load(path.read_text()), path.name)
    ]
    assert len(found) == 1
    assert found[0].file == "windows-followup.yml"
    write(tree.parent, "windows-followup.yml", CLEAN_SECOND_WORKFLOW)
    clean_found = [
        item
        for path in policy.workflow_files()
        for item in policy.foreign_gate_job_offenders(policy.load(path.read_text()), path.name)
    ]
    assert clean_found == []


def test_a_second_workflow_naming_a_job_gate_is_also_reported(tree):
    bad = (
        "name: windows-followup\n"
        "on: pull_request\n"
        "permissions:\n"
        "  contents: read\n"
        "jobs:\n"
        "  shadow:\n"
        "    name: gate\n"
        "    runs-on: windows-latest\n"
        "    steps:\n"
        "      - run: echo ok\n"
    )
    write(tree.parent, "windows-followup.yml", bad)
    found = [
        item
        for path in policy.workflow_files()
        for item in policy.foreign_gate_job_offenders(policy.load(path.read_text()), path.name)
    ]
    assert len(found) == 1
    assert found[0].file == "windows-followup.yml"
