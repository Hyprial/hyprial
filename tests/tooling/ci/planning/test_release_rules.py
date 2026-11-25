"""Release policy and offline execution of the actual workflow shell blocks."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest
import yaml

from tests.support.ci.ci_workflow_policy import (
    main_release_policy_errors, runner_jobs, wheel_publication_policy_errors,
)

ROOT = Path(__file__).resolve().parents[4]


def workflow(name):
    return yaml.safe_load((ROOT / f".github/workflows/{name}.yml").read_text())


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                          capture_output=True).stdout.strip()


def test_retired_main_listener_is_dispatch_only_and_cannot_publish():
    doc = workflow("release-main-dispatch")
    assert main_release_policy_errors(doc) == []
    assert doc.get("on", doc.get(True)) == {"workflow_dispatch": None}
    for branch in ("dev", "release/*", "main"):
        changed = {**doc, "on": {"push": {"branches": [branch]}}}
        assert main_release_policy_errors(changed)
    for mutation in ("git push origin refs/tags/v1", "curl -X POST /dispatches", "release_tracks.py"):
        changed = deepcopy(doc)
        changed["jobs"]["dispatch"]["steps"][1]["run"] = mutation
        assert main_release_policy_errors(changed)
    assert set(workflow("release-tag").get(True, {})) == {"workflow_dispatch"}


@pytest.fixture
def repository(tmp_path):
    work = tmp_path / "work"
    remote = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "init", "-b", "dev", str(work))
    git(work, "config", "user.name", "Release fixture")
    git(work, "config", "user.email", "ci@example.invalid")
    (work / "VERSION").write_text("0.5.5.2\n")
    (work / "CHANGELOG.md").write_text("## [0.5.5.2]\n\n- Release fixture.\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "base")
    git(work, "branch", "main")
    git(work, "commit", "--allow-empty", "-m", "dev change")
    dev = git(work, "rev-parse", "HEAD")
    git(work, "checkout", "main")
    git(work, "merge", "--no-ff", "dev", "-m", "dev into main")
    pushed = git(work, "rev-parse", "HEAD")
    assert pushed != dev
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "origin", "main", "dev")
    git(work, "fetch", "origin")
    # Advance the checkout without changing the event SHA: VERSION must be read
    # from the pushed object, never the checkout or the latest branch head.
    (work / "VERSION").write_text("9.9.9\n")
    git(work, "add", "VERSION")
    git(work, "commit", "-m", "later version")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text('#!/bin/sh\ncat >/dev/null\nprintf "%s\\n" "$@" >> "$CALLS"\n'
                    'printf "%s" "${HTTP_CODE:-204}"\nexit "${CURL_RC:-0}"\n')
    curl.chmod(0o700)
    env = {
        **os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_SHA": pushed, "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main",
        "GITHUB_SERVER_URL": "https://fixture.invalid", "GITHUB_REPOSITORY": "org/repo",
        "GITHUB_TOKEN": "fixture-not-secret", "CALLS": str(tmp_path / "calls"),
        "RUNNER_TEMP": str(tmp_path), "GITHUB_RUN_ID": "fixture", "NO_PROXY": "localhost",
    }
    return work, remote, pushed, env


def dispatch(repository, **overrides):
    work, _, _, env = repository
    step = next(s for s in workflow("release-main-dispatch")["jobs"]["dispatch"]["steps"]
                if s.get("id") == "dispatch")
    return subprocess.run(["bash", "-c", step["run"]], cwd=work,
                          env={**env, **overrides}, capture_output=True, text=True)


@pytest.mark.parametrize("event,ref", [
    ("workflow_dispatch", "refs/heads/main"),
    ("push", "refs/heads/main"),
    ("push", "refs/heads/dev"),
])
def test_retired_main_dispatch_shell_never_writes(repository, event, ref):
    work, remote, _, env = repository
    result = dispatch(repository, GITHUB_EVENT_NAME=event, GITHUB_REF=ref)
    assert result.returncode == 0, result.stderr
    assert "retired" in result.stdout
    assert "release-tag" in result.stdout
    assert not Path(env["CALLS"]).exists()
    assert git(work, "tag", "--list") == ""
    assert git(remote, "tag", "--list") == ""


@pytest.mark.parametrize("target,ok,gate", [
    ("dev-tip", True, ""), ("dev-ancestor", True, ""),
    ("main-only", False, "gate 3 FAILED"),
    ("missing-notes", False, "gate 4 FAILED"),
    ("existing-tag", False, "gate 1 FAILED"),
    ("wrong-version", False, "gate 2 FAILED"),
    ("lookup-failure", False, "gate 1 FAILED"),
])
def test_release_anchor_gate_accepts_dev_history_and_preserves_guards(repository, target, ok, gate):
    work, remote, main_merge, env = repository
    candidate = git(work, "rev-parse", "refs/remotes/origin/dev")
    tag = "v0.5.5.2"
    if target == "dev-ancestor":
        candidate = git(work, "rev-parse", candidate + "^")
    elif target == "main-only":
        candidate = main_merge
    elif target == "missing-notes":
        git(work, "checkout", "dev")
        (work / "CHANGELOG.md").write_text("# No version notes\n")
        git(work, "add", "CHANGELOG.md")
        git(work, "commit", "-m", target)
        candidate = git(work, "rev-parse", "HEAD")
        git(work, "update-ref", "refs/remotes/origin/dev", candidate)
    elif target == "existing-tag":
        git(work, "tag", tag, candidate)
        git(work, "push", "origin", f"refs/tags/{tag}")
    elif target == "wrong-version":
        tag = "v9.9.9"
    elif target == "lookup-failure":
        git(work, "remote", "set-url", "origin", str(work / "absent.git"))
    tools = work / "scripts/ci"
    tools.mkdir(parents=True)
    (tools / "ci-step.sh").write_text(":\n")
    shutil.copy(ROOT / "scripts/ci/check-changelog.mjs", tools)
    step = next(s for s in workflow("release-tag")["jobs"]["tag"]["steps"]
                if s["name"].startswith("Gates ("))
    askpass_path = work / "askpass-path"
    askpass_path.write_text("/nonexistent-fixture-askpass")
    run = step["run"].replace("/tmp/reltag-askpass-path", str(askpass_path))
    result = subprocess.run(["bash", "-c", run], cwd=work, capture_output=True, text=True,
                            env={**env, "IN_TAG": tag, "IN_SHA": candidate,
                                 "IN_DRY": "true", "GITHUB_ENV": str(work / "env")}, timeout=10)
    assert (result.returncode == 0) is ok, result.stdout + result.stderr
    assert (work / "env").exists() is ok
    if gate:
        assert gate in result.stdout
    assert git(remote, "tag", "--list") == (tag if target == "existing-tag" else "")


@pytest.mark.parametrize("enabled", [None, "false", "TRUE", "unexpected"])
def test_formal_publication_hold_refuses_tag_creation(repository, enabled):
    work, remote, _, env = repository
    candidate = git(work, "rev-parse", "refs/remotes/origin/dev")
    step = next(s for s in workflow("release-tag")["jobs"]["tag"]["steps"]
                if s["name"] == "Create and push the tag")
    variables = {**env, "TAG_NAME": "v0.5.5.2", "TAG_SHA": candidate, "DRY": "false",
                 "GITHUB_STEP_SUMMARY": str(work / "summary")}
    variables.pop("FORMAL_PUBLICATION_ENABLED", None)
    if enabled is not None:
        variables["FORMAL_PUBLICATION_ENABLED"] = enabled
    result = subprocess.run(["bash", "-c", step["run"]], cwd=work, capture_output=True,
                            text=True, env=variables, timeout=10)
    assert result.returncode != 0
    assert "formal publication is held" in result.stdout + result.stderr
    assert git(work, "tag", "--list") == ""
    assert git(remote, "tag", "--list") == ""
    assert not Path(env["CALLS"]).exists()


def test_formal_hold_allows_dry_run_after_gates(repository):
    work, remote, _, env = repository
    candidate = git(work, "rev-parse", "refs/remotes/origin/dev")
    step = next(s for s in workflow("release-tag")["jobs"]["tag"]["steps"]
                if s["name"] == "Create and push the tag")
    result = subprocess.run(["bash", "-c", step["run"]], cwd=work, capture_output=True,
                            text=True, timeout=10, env={**env, "TAG_NAME": "v0.5.5.2",
                            "TAG_SHA": candidate, "DRY": "true", "FORMAL_PUBLICATION_ENABLED": "false",
                            "GITHUB_STEP_SUMMARY": str(work / "summary")})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "dry-run" in result.stdout
    assert git(work, "tag", "--list") == ""
    assert git(remote, "tag", "--list") == ""
    assert not Path(env["CALLS"]).exists()


def test_enabled_formal_publication_creates_only_the_exact_candidate_tag(repository):
    work, remote, _, env = repository
    candidate = git(work, "rev-parse", "refs/remotes/origin/dev")
    step = next(s for s in workflow("release-tag")["jobs"]["tag"]["steps"]
                if s["name"] == "Create and push the tag")
    askpass_path = work / "askpass-path"
    askpass_path.write_text("/nonexistent-fixture-askpass")
    run = step["run"].replace("/tmp/reltag-askpass-path", str(askpass_path))
    result = subprocess.run(["bash", "-c", run], cwd=work, capture_output=True, text=True,
                            timeout=10, env={**env, "TAG_NAME": "v0.5.5.2", "TAG_SHA": candidate,
                            "DRY": "false", "FORMAL_PUBLICATION_ENABLED": "true",
                            "GITHUB_STEP_SUMMARY": str(work / "summary")})
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(remote, "rev-parse", "refs/tags/v0.5.5.2^{}") == candidate
    assert git(remote, "tag", "--list") == "v0.5.5.2"
    assert not Path(env["CALLS"]).exists()


def homepage_dispatch_step():
    return next(s for s in workflow("github-publish")["jobs"]["publish"]["steps"]
                if s["name"].startswith("Dispatch homepage changelog sync"))


def test_homepage_dispatch_uses_job_token_not_a_single_workflow_secret():
    step = homepage_dispatch_step()
    assert step["env"] == {"GITHUB_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}
    assert "HOMEPAGE_SOURCE_DISPATCH_TOKEN" not in step["run"]
    assert "may dispatch only this workflow" not in step["run"]


@pytest.mark.parametrize("tag,dry,token,code,ok,called", [
    ("v0.5.5.2", "false", "fixture-job-token", "204", True, True),
    ("v0.5.5.2", "false", "fixture-job-token", "200", True, True),
    ("v0.5.5.2", "false", "fixture-job-token", "403", False, True),
    ("v0.5.5.2", "false", "", "204", False, False),
    ("v0.5.5.2", "true", "", "204", True, False),
    ("internal", "false", "", "204", True, False),
    ("nightly", "false", "", "204", True, False),
    ("stable", "false", "", "204", True, False),
])
def test_homepage_job_token_dispatch_offline(repository, tag, dry, token, code, ok, called):
    work, _, pushed, env = repository
    curl = Path(env["PATH"].split(os.pathsep)[0]) / "curl"
    curl.write_text('#!/bin/sh\ncat > "$CALLS.headers"\n'
                    'printf "%s\\n" "$@" > "$CALLS"\nprintf "%s" "$HTTP_CODE"\n')
    # No real HTTP or shared /tmp artifact, even on a failed dispatch.
    run = homepage_dispatch_step()["run"].replace(
        "/tmp/changelog-sync-dispatch.out", str(work / "dispatch.out"))
    result = subprocess.run(["bash", "-c", run], cwd=work, capture_output=True, text=True,
                            env={**env, "PLAN_TAG": tag, "PLAN_DRY": dry,
                                 "GITHUB_TOKEN": token, "HTTP_CODE": code,
                                 "HOMEPAGE_SOURCE_DISPATCH_TOKEN": ""})
    assert (result.returncode == 0) is ok, result.stdout + result.stderr
    assert Path(env["CALLS"]).exists() is called
    if called:
        calls = Path(env["CALLS"]).read_text().splitlines()
        assert Path(env["CALLS"] + ".headers").read_text() == (
            'header = "Authorization: token fixture-job-token"\n')
        assert calls[-1].endswith("/homepage-changelog-sync.yml/dispatches")
        body = json.loads(calls[calls.index("--data") + 1])
        assert body["inputs"]["source_tag"] == tag
        assert body["inputs"]["source_sha"] == pushed
        assert body["inputs"]["dry_run"] == "false"
        assert token not in " ".join(calls) + result.stdout + result.stderr


def test_all_three_distribution_writes_are_mandatory():
    assert wheel_publication_policy_errors(runner_jobs()) == []


@pytest.mark.parametrize("script,switch", [
    ("attach_wheel_assets.py", "ATTACH_WHEEL_ASSETS"),
    ("publish_wheels.py", "MIRROR_WHEELS_TO_R2"),
])
def test_optional_distribution_switch_mutation_is_rejected(script, switch):
    jobs = deepcopy(runner_jobs())
    for job in jobs:
        for step in job.steps:
            if script in step.get("run", ""):
                step["if"] = "${{ vars." + switch + " == '1' }}"
    assert any(script in e for e in wheel_publication_policy_errors(jobs))


@pytest.mark.parametrize("track", ["stable", "nightly", "internal"])
@pytest.mark.parametrize("attach_rc", ["0", "2"])
def test_track_asset_attachment_runs_without_switches_and_fails_loudly(repository, track, attach_rc):
    work, _, _, env = repository
    tools = work / "scripts/ci"
    tools.mkdir(parents=True)
    (tools / "ci-step.sh").write_text(":\n")
    bindir = Path(env["PATH"].split(os.pathsep)[0])
    shim = bindir / "python3"
    shim.write_text('#!/bin/sh\ncase "$1" in\n'
                    ' *github_publish.py) printf "0.5.5.2\\n" ;;\n'
                    ' *attach_wheel_assets.py) printf "%s\\n" "$@" > "$CALLS"; exit "$ATTACH_RC" ;;\n'
                    ' *) exit 99 ;;\nesac\n')
    shim.chmod(0o700)
    step = next(s for s in workflow("github-publish")["jobs"]["publish"]["steps"]
                if s["name"] == "Attach wheels to the public release")
    assert step.get("if") in (None, "success()")
    result = subprocess.run(["bash", "-c", step["run"]], cwd=work, capture_output=True, text=True,
                            env={**env, "PLAN_TAG": track, "PLAN_DRY": "false",
                                 "READINGS": str(work / "readings"), "ATTACH_RC": attach_rc,
                                 "PKG_PUBLISH_USER": "", "PKG_PUBLISH_TOKEN": "",
                                 "GH_PUBLISH_TOKEN": "",
                                 "JOB_START_EPOCH": str(int(time.time()))})
    assert result.returncode == (0 if attach_rc == "0" else 1), result.stdout + result.stderr
    calls = Path(env["CALLS"]).read_text().splitlines()
    assert calls[calls.index("--tag") + 1] == track
    assert calls[calls.index("--wait-for-registry") + 1] == "2400"
    if attach_rc == "2":
        assert "NOT EVALUABLE" in result.stderr
