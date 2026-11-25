"""Exercise the actual tag-publication shell against disposable Git remotes."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from tests.support.ci.ci_workflow_policy import internal_publication_condition

ROOT = Path(__file__).resolve().parents[4]


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


@pytest.fixture
def history(tmp_path):
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "init", str(work))
    git(work, "config", "user.name", "CI fixture")
    git(work, "config", "user.email", "ci@example.invalid")
    commits = []
    for message in ("old", "tested", "newer"):
        git(work, "commit", "--allow-empty", "-m", message)
        commits.append(git(work, "rev-parse", "HEAD"))
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "origin", f"{commits[1]}:refs/heads/dev", f"{commits[0]}:refs/tags/internal", f"{commits[2]}:refs/heads/fixture-newer")
    (work / "scripts/ci").mkdir(parents=True)
    (work / "scripts/ci/ci-conditions.sh").write_text('printf "%s\\n" "$1" > marker\n')
    return work, remote, commits


def publish(
    work,
    candidate,
    env=None,
    *,
    unit_selected="true",
    job_status="success",
):
    workflow = yaml.safe_load((ROOT / ".forgejo/workflows/gate-python.yml").read_text())
    steps = workflow["jobs"]["unit"]["steps"]
    step = next(
        item
        for item in steps
        if item["name"] == "Move the track tags to the unit-green dev head"
    )
    mirror = next(
        item
        for item in steps
        if item["name"] == "Dispatch the GitHub publication of the moved internal tag"
    )
    # Check runner admission before exercising the shell body in isolation.
    assert internal_publication_condition(step)
    assert internal_publication_condition(mirror)
    run = step["run"].replace(
        "${{ steps.scope.outputs.unit }}", unit_selected
    ).replace("${{ job.status }}", job_status)
    return subprocess.run(
        ["bash", "-c", run],
        cwd=work,
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "GITHUB_SHA": candidate,
            "GITHUB_SERVER_URL": "https://fixture.invalid",
            "GITHUB_TOKEN": "not-a-credential",
            **(env or {}),
        },
    )


@pytest.mark.parametrize("tag_exists", [True, False])
def test_current_green_head_publishes_and_records_after_push(history, tag_exists):
    work, remote, (_, candidate, _) = history
    if not tag_exists:
        git(remote, "update-ref", "-d", "refs/tags/internal")
    result = publish(work, candidate)
    assert result.returncode == 0, result.stderr
    assert git(remote, "rev-parse", "refs/tags/internal") == candidate
    assert git(remote, "tag", "--list").splitlines() == ["internal"]
    assert (work / "marker").read_text() == "mover\n"


@pytest.mark.parametrize("status", ["success", "failure", "cancelled"])
@pytest.mark.parametrize("event", ["push", "pull_request", "workflow_dispatch"])
@pytest.mark.parametrize("ref", ["refs/heads/dev", "refs/heads/main", "refs/heads/release/v0.5.5.2"])
@pytest.mark.parametrize("enabled", ["true", "false", ""])
def test_publication_condition_allows_only_enabled_green_dev_push(status, event, ref, enabled):
    workflow = yaml.safe_load((ROOT / ".forgejo/workflows/gate-python.yml").read_text())
    steps = workflow["jobs"]["unit"]["steps"]
    for step in steps:
        if step.get("id") != "internal_move" and not step["name"].startswith("Dispatch the GitHub"):
            continue
        assert internal_publication_condition(step)
        expression = step["if"].replace("success()", str(status == "success")).replace(
            "github.event_name", repr(event)
        ).replace("github.ref", repr(ref)).replace(
            "env.INTERNAL_PUBLICATION_ENABLED", repr(enabled)
        ).replace("&&", "and")
        # Only the canonical, closed expression above reaches this evaluator.
        assert eval(expression, {"__builtins__": {}}) is (
            enabled == "true" and status == "success" and event == "push"
            and ref == "refs/heads/dev"
        )


def test_internal_publication_is_frozen_during_dev_first_convergence():
    workflow = yaml.safe_load((ROOT / ".forgejo/workflows/gate-python.yml").read_text())
    assert workflow["jobs"]["unit"].get("env", {}).get("INTERNAL_PUBLICATION_ENABLED") == "false"


def test_late_old_green_job_cannot_move_internal_backwards(history):
    work, remote, (_, candidate, newer) = history
    git(remote, "update-ref", "refs/heads/dev", newer)
    git(remote, "update-ref", "refs/tags/internal", newer)
    result = publish(work, candidate)
    assert result.returncode == 0, result.stderr
    assert git(remote, "rev-parse", "refs/tags/internal") == newer
    assert not (work / "marker").exists()
    assert "superseded" in result.stdout


def test_green_ancestor_advances_while_newer_dev_is_still_unvalidated(history):
    work, remote, (_, candidate, newer) = history
    git(remote, "update-ref", "refs/heads/dev", newer)
    result = publish(work, candidate)
    assert result.returncode == 0, result.stderr
    assert git(remote, "rev-parse", "refs/tags/internal") == candidate
    assert (work / "marker").read_text() == "mover\n"


@pytest.mark.parametrize("tag_exists", [True, False])
def test_concurrent_new_publisher_invalidates_old_lease(history, tmp_path, tag_exists):
    work, remote, (_, candidate, newer) = history
    if not tag_exists:
        git(remote, "update-ref", "-d", "refs/tags/internal")
    shim = tmp_path / "bin"
    shim.mkdir()
    real_git = shutil.which("git")
    script = shim / "git"
    script.write_text('#!/bin/sh\nif [ "$1" = push ]; then\n'
                      '  "$REAL_GIT" --git-dir="$REMOTE" update-ref refs/heads/dev "$NEWER"\n'
                      '  "$REAL_GIT" --git-dir="$REMOTE" update-ref refs/tags/internal "$NEWER"\n'
                      'fi\nexec "$REAL_GIT" "$@"\n')
    script.chmod(0o700)
    result = publish(work, candidate, {"PATH": f"{shim}{os.pathsep}{os.environ['PATH']}",
                                        "REAL_GIT": real_git, "REMOTE": str(remote), "NEWER": newer})
    assert result.returncode != 0
    assert git(remote, "rev-parse", "refs/tags/internal") == newer
    assert not (work / "marker").exists()


def test_missing_dev_ref_refuses_publication(history):
    work, remote, (old, candidate, _) = history
    git(remote, "update-ref", "-d", "refs/heads/dev")
    result = publish(work, candidate)
    assert result.returncode != 0
    assert git(remote, "rev-parse", "refs/tags/internal") == old
    assert not (work / "marker").exists()


@pytest.mark.parametrize("outside", ["candidate", "internal"])
def test_unrelated_history_refuses_publication(history, outside):
    work, remote, (old, candidate, _) = history
    tree = git(work, "rev-parse", f"{candidate}^{{tree}}")
    orphan = git(work, "commit-tree", tree, "-m", "unrelated history")
    if outside == "internal":
        git(work, "push", "--force", "origin", f"{orphan}:refs/tags/internal")
        expected = orphan
    else:
        candidate = orphan
        expected = old
    result = publish(work, candidate)
    assert result.returncode != 0
    assert git(remote, "rev-parse", "refs/tags/internal") == expected
    assert not (work / "marker").exists()


def test_shallow_ci_checkout_fetches_the_ancestry_proof(history, tmp_path):
    _, remote, (_, candidate, _) = history
    shallow = tmp_path / "shallow"
    git(tmp_path, "clone", "--depth=1", "--branch=dev", remote.as_uri(), str(shallow))
    assert git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    (shallow / "scripts/ci").mkdir(parents=True)
    (shallow / "scripts/ci/ci-conditions.sh").write_text('printf "%s\\n" "$1" > marker\n')
    result = publish(shallow, candidate)
    assert result.returncode == 0, result.stderr
    assert git(remote, "rev-parse", "refs/tags/internal") == candidate
    assert (shallow / "marker").read_text() == "mover\n"
