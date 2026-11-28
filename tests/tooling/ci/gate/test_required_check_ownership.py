"""Who may report a required check, and how ``pr-hygiene`` reads the body it judges.

A-S3 (gate-scope critic, plan A): ``pr-hygiene`` reads the pull request body
and ``created_at`` from the API when it runs. Read from the event payload, a
re-run of an old run would judge an old, compliant body and post a newer green
check over a body that has since been broken. The step script is executed here
with stand-ins for ``gh`` and ``uv`` to show that it reads the live object,
ignores an event file whose body differs, treats a null body as empty, and
fails closed when the API call or the check itself fails. Statically, the
hygiene file reaches the event only as ``github.event.pull_request.number``.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

import tests.support.ci_download.gate_scope_policy as scope
import tests.support.ci_download.github_gate_policy as policy

TREE = scope.tree_documents()
HYGIENE = policy.PR_HYGIENE_FILE
HYGIENE_TEXT = policy.pr_hygiene_path().read_text(encoding="utf-8")
CHECK_STEP = "Require the Submitted-by identity line"
CREATED_AT = "2026-11-27T01:02:03Z"


def hygiene_tree(mutate) -> dict:
    tree = copy.deepcopy(TREE)
    mutate(tree[HYGIENE]["jobs"]["pr-hygiene"], tree)
    assert tree != TREE, "the mutation did not apply"
    return tree


def check_step(job: dict) -> dict:
    return next(step for step in job["steps"] if step.get("name") == CHECK_STEP)


def test_the_owning_file_of_each_required_check_is_fixed():
    assert policy.REQUIRED_CHECK_OWNERS == {"gate": "gate.yml", "pr-hygiene": "pr-hygiene.yml"}
    assert policy.STRICT_FILES == {"gate.yml", "pr-hygiene.yml"}
    assert policy.pr_hygiene_path().is_file() and policy.gate_path().is_file()


def test_only_the_gate_workflow_defines_a_gate_job():
    for path in scope.workflow_files():
        document = scope.load(path.read_text(encoding="utf-8"))
        assert scope.foreign_required_check_offenders(document, path.name) == []


def test_a_second_workflow_defining_a_gate_job_is_reported():
    document = {"jobs": {"gate": {"runs-on": "windows-latest", "steps": []}}}
    found = scope.foreign_required_check_offenders(document, "windows-followup.yml")
    assert len(found) == 1 and found[0].file == "windows-followup.yml"


def test_a_second_workflow_naming_a_job_gate_is_also_reported():
    document = {"jobs": {"shadow": {"name": "gate", "runs-on": "windows-latest", "steps": []}}}
    found = scope.foreign_required_check_offenders(document, "windows-followup.yml")
    assert len(found) == 1 and found[0].file == "windows-followup.yml"


@pytest.mark.parametrize(
    "job_id,display_name,file_name,claimed",
    [
        ("gate", None, "extra.yml", "gate"),
        ("Gate", None, "extra.yml", "gate"),
        ("shadow", " GATE ", "extra.yml", "gate"),
        ("pr-hygiene", None, "extra.yml", "pr-hygiene"),
        ("shadow", "PR-Hygiene", "gate.yml", "pr-hygiene"),
        ("gate", None, "pr-hygiene.yml", "gate"),
    ],
)
def test_a_required_check_name_outside_its_owning_file_is_reported(job_id, display_name, file_name, claimed):
    job = {"runs-on": "ubuntu-latest", "steps": []}
    if display_name is not None:
        job["name"] = display_name
    found = scope.foreign_required_check_offenders({"jobs": {job_id: job}}, file_name)
    assert len(found) == 1 and f"reserved check name {claimed!r}" in found[0].message, found


def test_a_job_whose_name_only_contains_a_check_name_is_not_a_claim():
    jobs = {"gate-preview": {"name": "pr-hygiene helper"}, "lint": {"name": "gatekeeper"}}
    assert scope.foreign_required_check_offenders({"jobs": jobs}, "extra.yml") == []


def test_two_jobs_folding_to_one_required_id_are_reported():
    document = copy.deepcopy(TREE[HYGIENE])
    document["jobs"]["PR-Hygiene"] = copy.deepcopy(document["jobs"]["pr-hygiene"])
    found = scope.owned_job_offenders(document, HYGIENE)
    assert any("defines 2 jobs with id 'pr-hygiene'" in item.message for item in found), found


def test_a_relaxed_job_or_step_in_the_hygiene_file_is_reported():
    assert policy.relaxed_gate_surfaces(HYGIENE_TEXT, HYGIENE) == []
    job_relaxed = yaml.safe_load(HYGIENE_TEXT)
    job_relaxed["jobs"]["pr-hygiene"]["continue-on-error"] = True
    found = policy.relaxed_gate_surfaces(yaml.safe_dump(job_relaxed), HYGIENE)
    assert len(found) == 1 and "job pr-hygiene is relaxed" in found[0].message
    step_relaxed = yaml.safe_load(HYGIENE_TEXT)
    check_step(step_relaxed["jobs"]["pr-hygiene"]).update({"continue-on-error": True, "if": "always()"})
    assert len(policy.relaxed_gate_surfaces(yaml.safe_dump(step_relaxed), HYGIENE)) == 1


@pytest.mark.parametrize(
    "permissions",
    [{"contents": "read"}, {"pull-requests": "read"}, {"contents": "read", "pull-requests": "read"}],
)
def test_a_job_token_reading_contents_and_pull_requests_is_allowed(permissions):
    document = {"jobs": {"job": {"permissions": permissions}}}
    assert scope.job_permission_offenders(document, "wf.yml") == []


@pytest.mark.parametrize(
    "permissions",
    [
        {"pull-requests": "write"},
        {"contents": "write"},
        {"issues": "read"},
        {"contents": "read", "actions": "read"},
        {"contents": "none"},
        "read-all",
        "write-all",
    ],
)
def test_a_job_token_outside_the_whitelist_is_reported(permissions):
    document = {"jobs": {"job": {"permissions": permissions}}}
    assert scope.job_permission_offenders(document, "wf.yml")


def test_every_job_token_in_the_tree_is_on_the_whitelist():
    for name, document in TREE.items():
        assert scope.job_permission_offenders(document, name) == []
    assert TREE[HYGIENE]["jobs"]["pr-hygiene"]["permissions"] == {"contents": "read", "pull-requests": "read"}


def test_a_s3_no_workflow_reads_the_body_from_the_event_and_hygiene_reads_it_live():
    for name, document in TREE.items():
        assert scope.event_body_offenders(document, name) == []
    assert scope.live_body_offenders(TREE[HYGIENE], HYGIENE) == []
    step = check_step(TREE[HYGIENE]["jobs"]["pr-hygiene"])
    assert step["env"]["GH_TOKEN"] == scope.RUN_TOKEN
    assert "PR_BODY" not in step["env"] and "PR_CREATED_AT" not in step["env"]


def test_the_event_body_scan_reads_values_not_comments():
    text = "# never read github.event.pull_request.body here\non: pull_request\njobs: {}\n"
    assert scope.event_body_offenders(policy.load(text), "wf.yml") == []


@pytest.mark.parametrize(
    "case_id,mutate,fragment",
    [
        ("AS3-event-body-env", lambda job, _t: check_step(job)["env"].update(PR_BODY="${{ github.event.pull_request.body }}"), "event payload"),
        ("AS3-event-body-brackets", lambda job, _t: check_step(job)["env"].update(B="${{ github.event['pull_request']['body'] }}"), "event payload"),
        ("AS3-event-object-json", lambda job, _t: check_step(job)["env"].update(E="${{ toJSON(github.event.pull_request) }}"), "event payload"),
        ("AS3-gate-reads-event-body", lambda _job, t: t["gate.yml"]["jobs"]["static"]["steps"][-1].update(env={"B": "${{ github.event.pull_request.body }}"}), "gate.yml:0: reads the pull request body"),
        ("AS3-secret-token", lambda job, _t: check_step(job)["env"].update(GH_TOKEN="${{ secrets.GITHUB_TOKEN }}"), "references a secret"),
        ("AS3-no-api-read", lambda job, _t: check_step(job).update(run="set -euo pipefail\nprintf '' > b\nuv run python scripts/ci/check_pr_hygiene.py submitted-by --body-file b"), "not exactly one"),
        ("AS3-pull-requests-write", lambda job, _t: job["permissions"].update({"pull-requests": "write"}), "only contents and pull-requests may be read"),
        ("AS3-no-pull-requests-scope", lambda job, _t: job["permissions"].pop("pull-requests"), "must read contents and pull-requests"),
        ("S2-gate-reads-body-via-github-brackets", lambda _job, t: t["gate.yml"]["jobs"]["static"]["steps"][-1].update(env={"B": "${{ github['event']['pull_request']['body'] }}"}), "gate.yml:0: reads the pull request body"),
        ("S2-gate-dumps-github-context", lambda _job, t: t["gate.yml"]["jobs"]["static"]["steps"][-1].update(env={"B": "${{ toJSON(github) }}"}), "gate.yml:0: reads the pull request body"),
    ],
)
def test_a_s3_mutations_are_reported(case_id, mutate, fragment):
    messages = [str(item) for item in scope.gate_scope_offenders(hygiene_tree(mutate))]
    assert any(fragment in message for message in messages), (case_id, messages)


WHITELIST = "reaches the event outside github.event.pull_request.number"


@pytest.mark.parametrize(
    "case_id,value",
    [
        ("S2-github-brackets", "${{ github['event']['pull_request']['body'] }}"),
        ("S2-tojson-github", "${{ toJSON(github) }}"),
        ("S2-join-wildcard", "${{ join(github.event.*.body, '') }}"),
        ("S2-changes-body-from", "${{ github.event.changes.body.from }}"),
        ("S2-event-path-context", "${{ github.event_path }}"),
        ("S2-event-upper-case", "${{ GITHUB.EVENT.PULL_REQUEST.BODY }}"),
        ("S2-number-then-more", "${{ github.event.pull_request.number }}${{ github.event.pull_request.title }}"),
    ],
)
def test_s2_the_hygiene_file_reaches_the_event_only_as_the_pull_request_number(case_id, value):
    tree = hygiene_tree(lambda job, _t: check_step(job)["env"].update(X=value))
    messages = [str(item) for item in scope.hygiene_event_offenders(tree[HYGIENE], HYGIENE)]
    assert messages and all(WHITELIST in message for message in messages), (case_id, messages)
    assert any(WHITELIST in str(item) for item in scope.gate_scope_offenders(tree)), case_id


def test_s2_the_hygiene_script_may_not_read_the_event_file():
    def mutate(job, _t):
        step = check_step(job)
        step["run"] = step["run"].replace(
            'jq -j \'.body // ""\' "${RUNNER_TEMP}/pr.json"', 'jq -j \'.pull_request.body // ""\' "${GITHUB_EVENT_PATH}"'
        )

    tree = hygiene_tree(mutate)
    messages = [str(item) for item in scope.hygiene_event_offenders(tree[HYGIENE], HYGIENE)]
    assert any("'GITHUB_EVENT_PATH'" in message for message in messages), messages


def test_s2_the_real_hygiene_file_reaches_the_event_only_as_the_pull_request_number():
    assert scope.hygiene_event_offenders(TREE[HYGIENE], HYGIENE) == []
    references = [text for text in scope.strings_of(TREE[HYGIENE]) if "github.event" in text]
    assert references and all("github.event.pull_request.number" in text for text in references)


def run_check_step(
    tmp_path: Path,
    gh_stdout: str,
    gh_exit: int = 0,
    *,
    uv_exit: int = 0,
    extra_env: dict[str, str] | None = None,
    script: str | None = None,
) -> tuple[subprocess.CompletedProcess, Path]:
    """Run the real step script (or ``script``) with ``gh`` and ``uv`` replaced by recorders."""

    bash, jq = shutil.which("bash"), shutil.which("jq")
    if bash is None or jq is None:
        pytest.fail("bash and jq are required to run the pr-hygiene step (both are on GitHub's ubuntu image)")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    (tmp_path / "gh.json").write_text(gh_stdout, encoding="utf-8")
    (stubs / "gh").write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > "{tmp_path}/gh.args"\ncat "{tmp_path}/gh.json"\nexit {gh_exit}\n'
    )
    (stubs / "uv").write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{tmp_path}/uv.args"\nexit {uv_exit}\n')
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    temp = tmp_path / "runner-temp"
    temp.mkdir()
    env = {
        "PATH": os.pathsep.join([str(stubs), str(Path(jq).parent), "/usr/bin", "/bin"]),
        "GITHUB_REPOSITORY": "example/repo",
        "PR_NUMBER": "7",
        "RUNNER_TEMP": str(temp),
        "PYTHON_VERSION": "3.12",
        "GH_TOKEN": "token-stand-in",
        **(extra_env or {}),
    }
    if script is None:
        script = check_step(TREE[HYGIENE]["jobs"]["pr-hygiene"])["run"]
    proc = subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail"],
        input=script, text=True, capture_output=True, env=env, cwd=tmp_path, check=False,
    )
    return proc, temp


def test_a_s3_the_step_reads_the_live_pull_request_and_passes_its_body(tmp_path):
    body = "Submitted-by: someone\n\nline two\n"
    proc, temp = run_check_step(tmp_path, json.dumps({"body": body, "created_at": CREATED_AT}))
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "gh.args").read_text().split("\n")[:2] == ["api", "repos/example/repo/pulls/7"]
    assert (temp / "pr-body.txt").read_bytes() == body.encode()
    uv_args = (tmp_path / "uv.args").read_text().splitlines()
    assert uv_args[uv_args.index("--created-at") + 1] == CREATED_AT
    assert uv_args[uv_args.index("--pr-number") + 1] == "7"
    assert "submitted-by" in uv_args


def test_s2_an_event_file_with_another_body_is_ignored(tmp_path):
    live = "Submitted-by: agent:live\n"
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"body": "Submitted-by: agent:stale\n", "number": 7}}), encoding="utf-8")
    proc, temp = run_check_step(
        tmp_path, json.dumps({"body": live, "created_at": CREATED_AT}), extra_env={"GITHUB_EVENT_PATH": str(event)}
    )
    assert proc.returncode == 0, proc.stderr
    assert (temp / "pr-body.txt").read_bytes() == live.encode()


def check_failure_fails_the_step(tmp_path: Path, script: str | None = None) -> bool:
    proc, _temp = run_check_step(
        tmp_path, json.dumps({"body": "x", "created_at": CREATED_AT}), uv_exit=1, script=script
    )
    assert (tmp_path / "uv.args").is_file()
    return proc.returncode != 0


def test_s3_a_failing_check_fails_the_step(tmp_path):
    assert check_failure_fails_the_step(tmp_path)


def test_s3_a_swallowed_check_exit_code_is_caught(tmp_path):
    script = check_step(TREE[HYGIENE]["jobs"]["pr-hygiene"])["run"]
    swallowed = script.rstrip("\n") + " || true\n"
    assert swallowed != script
    assert not check_failure_fails_the_step(tmp_path, swallowed)


def test_a_s3_a_null_body_is_judged_as_empty(tmp_path):
    proc, temp = run_check_step(tmp_path, '{"body": null, "created_at": "%s"}' % CREATED_AT)
    assert proc.returncode == 0, proc.stderr
    assert (temp / "pr-body.txt").read_bytes() == b""
    assert (tmp_path / "uv.args").is_file()


@pytest.mark.parametrize(
    "case_id,stdout,exit_code",
    [
        ("api-error", '{"message": "Bad credentials"}', 1),
        ("not-json", "<html>502</html>", 0),
        ("no-created-at", '{"body": "Submitted-by: x"}', 0),
    ],
)
def test_a_s3_the_step_fails_closed_without_a_live_pull_request(tmp_path, case_id, stdout, exit_code):
    proc, _temp = run_check_step(tmp_path, stdout, exit_code)
    assert proc.returncode != 0, case_id
    assert not (tmp_path / "uv.args").exists(), case_id
