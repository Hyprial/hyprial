"""Execute real workflow blocks with real Git and isolated, local-only remotes."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

import pytest
import yaml

from .test_release_tracks import ROOT, SCRIPTS, move, remote
from .test_release_tracks import repo as repo


def stage_tools(repo):
    work, git, _ = repo
    tools = work / "scripts/ci"
    tools.mkdir(parents=True)
    for name in ("release_tracks.py", "check-release-tracks.py"):
        shutil.copy(SCRIPTS / name, tools / name)
    package = tools / "github_publish_policy"
    package.mkdir()
    for name in ("__init__.py", "netretry.py", "push.py"):
        shutil.copy(SCRIPTS / "github_publish_policy" / name, package / name)
    git("add", "scripts")
    git("commit", "-qm", "CI tools")
    git("push", "origin", "HEAD:refs/heads/dev")
    return git("rev-parse", "HEAD")


def env_for(repo, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(sys.executable)
    curl = bin_dir / "curl"
    curl.write_text('#!/bin/sh\ncat >/dev/null\nprintf "%s\\n" "$*" >> "$DISPATCHES"\n'
                    'case "$*" in *github-publish.yml*) exit "${GH_FAIL:-0}";; esac\n')
    curl.chmod(0o755)
    return {
        **os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GITHUB_SERVER_URL": tmp_path.as_uri(), "GITHUB_REPOSITORY": "remote",
        "GITHUB_TOKEN": "fixture-only", "GITHUB_OUTPUT": str(tmp_path / "output"),
        "FRESHNESS_SECONDS": "14400", "READINGS": str(tmp_path / "readings"),
        "DISPATCHES": str(tmp_path / "dispatches"),
    }


def steps(workflow, job):
    return yaml.safe_load((ROOT / f".forgejo/workflows/{workflow}.yml").read_text())["jobs"][job]["steps"]


def shell(script, cwd, env):
    result = subprocess.run(["bash", "-c", script], cwd=cwd, env=env, text=True, capture_output=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


@pytest.mark.parametrize("markers", ["absent-branch", "absent-file", "stale", "different", "green"])
def test_real_nightly_workflow_keeps_floor_on_every_gate_path(repo, tmp_path, markers):
    head = stage_tools(repo)
    work, git, commits = repo
    move(repo, "stable", commits[1])
    if markers != "absent-branch":
        epoch = int(time.time()) - (90000 if markers == "stale" else 0)
        (work / "gate-e2e.json").write_text(json.dumps({"sha": head, "epoch": epoch}, separators=(",", ":")))
        if markers != "absent-file":
            other = commits[0] if markers == "different" else head
            (work / "gate-e2e-controlled.json").write_text(json.dumps({"sha": other, "epoch": epoch}, separators=(",", ":")))
        git("add", ".")
        git("commit", "-qm", "markers")
        git("push", "origin", "HEAD:refs/heads/ci/gate-status")
    env = env_for(repo, tmp_path)
    env["GITHUB_SHA"] = head
    runner = tmp_path / "runner"
    runner.mkdir()
    workflow = steps("release-nightly-promote", "promote")
    gate = next(s for s in workflow if s.get("id") == "candidate")
    reconcile = next(s for s in workflow if s.get("id") == "move")
    assert reconcile["if"] == "success() && env.NIGHTLY_PUBLICATION_ENABLED == 'true'"
    env["NIGHTLY_PUBLICATION_ENABLED"] = "true"
    shell(gate["run"], runner, env)
    output = tmp_path / "output"
    selected = dict(line.split("=", 1) for line in output.read_text().splitlines()) if output.exists() else {}
    env["CANDIDATE"] = selected.get("candidate", "")
    assert bool(env["CANDIDATE"]) == (markers == "green")
    shell(reconcile["run"], runner, env)
    assert remote(repo, "nightly") == (head if markers == "green" else commits[1])
    assert remote(repo, "stable") == commits[1]
    assert remote(repo, "internal") == commits[2]
    assert "promoted=" in output.read_text()


@pytest.mark.parametrize("github_fails", [False, True])
def test_real_formal_release_step_moves_checks_and_dispatches_tracks(repo, tmp_path, github_fails):
    stage_tools(repo)
    env = env_for(repo, tmp_path)
    env.update(PLAN_TAG="v0.5.5.1", PLAN_DRY="false", GITHUB_SHA=repo[2][1])
    script = next(s["run"] for s in steps("github-publish", "publish") if "release_tracks.py --release" in s.get("run", ""))
    env["GH_FAIL"] = "1" if github_fails else "0"
    result = subprocess.run(["bash", "-c", script], cwd=repo[0], env=env, capture_output=True, text=True, timeout=40)
    assert result.returncode == (1 if github_fails else 0), result.stdout + result.stderr
    assert remote(repo, "stable") == remote(repo, "nightly") == repo[2][1]
    readings = (tmp_path / "readings").read_text()
    assert "stable_reached=true" in readings and "nightly_reached=true" in readings
    dispatches = (tmp_path / "dispatches").read_text().splitlines()
    assert len(dispatches) == 4
    for index, track in ((0, "stable"), (2, "nightly")):
        assert "wheel-release.yml/dispatches" in dispatches[index]
        assert f'"track":"{track}"' in dispatches[index]
        assert f'"source_sha":"{repo[2][1]}"' in dispatches[index]
        assert "github-publish.yml/dispatches" in dispatches[index + 1]
        assert f'"ref":"{track}"' in dispatches[index + 1]


@pytest.mark.parametrize("nightly_ahead", [False, True])
def test_release_noop_does_not_dispatch_wheel_builds(repo, tmp_path, nightly_ahead):
    stage_tools(repo)
    move(repo, "stable", repo[2][1])
    move(repo, "nightly", repo[2][2] if nightly_ahead else repo[2][1])
    env = env_for(repo, tmp_path)
    env.update(PLAN_TAG="v0.5.5.1", PLAN_DRY="false", GITHUB_SHA=repo[2][1])
    script = next(s["run"] for s in steps("github-publish", "publish") if "release_tracks.py --release" in s.get("run", ""))
    shell(script, repo[0], env)
    dispatches = (tmp_path / "dispatches").read_text().splitlines()
    assert len(dispatches) == 2
    assert all("github-publish.yml/dispatches" in call for call in dispatches)


def test_release_job_fails_if_track_mover_did_not_update_remote(repo, tmp_path):
    stage_tools(repo)
    # Mutation control only in the isolated candidate: a mover that reports
    # success but changes nothing must be caught by the real readback step.
    (repo[0] / "scripts/ci/release_tracks.py").write_text(
        (SCRIPTS / "release_tracks.py").read_text().replace(
            "tracks.release(args.release, args.expected_sha)", "pass"
        )
    )
    env = env_for(repo, tmp_path)
    env.update(PLAN_TAG="v0.5.5.1", PLAN_DRY="false", GITHUB_SHA=repo[2][1])
    script = next(s["run"] for s in steps("github-publish", "publish") if "release_tracks.py --release" in s.get("run", ""))
    result = subprocess.run(["bash", "-c", script], cwd=repo[0], env=env, text=True, capture_output=True, timeout=40)
    assert result.returncode != 0
    assert "stable_reached=false" in result.stdout
    assert not (tmp_path / "dispatches").exists()


def test_stale_lease_cannot_overwrite_a_concurrent_release(repo, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from release_tracks import TrackError, Tracks

    # Tracks uses cwd for the push, so construct and read in the fixture repo.
    monkeypatch.chdir(repo[0])
    tracks = Tracks()
    previous, _ = tracks.tag("stable")
    move(repo, "stable", repo[2][2])
    with pytest.raises(TrackError, match="push of refs/tags/stable failed"):
        tracks.move("stable", repo[2][1], previous)
    assert remote(repo, "stable") == repo[2][2]


@pytest.mark.parametrize("stable,competitor", [(2, 1), (1, 2)])
def test_concurrent_nightly_move_rechecks_floor_and_preserves_newest(repo, monkeypatch, stable, competitor):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    monkeypatch.chdir(repo[0])
    from release_tracks import Tracks

    move(repo, "stable", repo[2][stable])
    tracks = Tracks()
    original = tracks.move
    raced = False

    def race(tag, intended, previous):
        nonlocal raced
        if not raced:
            raced = True
            move(repo, "nightly", repo[2][competitor])
        original(tag, intended, previous)

    monkeypatch.setattr(tracks, "move", race)
    tracks.nightly()
    assert raced
    assert remote(repo, "nightly") == repo[2][2]


def test_release_racing_newer_nightly_cannot_roll_it_back(repo, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    monkeypatch.chdir(repo[0])
    from release_tracks import TrackError, Tracks

    tracks = Tracks()
    previous, _ = tracks.tag("nightly")
    move(repo, "nightly", repo[2][2])
    with pytest.raises(TrackError, match="push of refs/tags/nightly failed"):
        tracks.move("nightly", repo[2][1], previous)
    assert remote(repo, "nightly") == repo[2][2]
