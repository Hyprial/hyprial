"""Allen 2026-10-26: real, isolated repositories, no production remote."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / "scripts/ci"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GH_PUBLISH_RETRY_BACKOFF", "0,0")
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(work)], check=True, capture_output=True)

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=work, text=True).strip()

    git("config", "user.name", "Track Test")
    git("config", "user.email", "track@example.invalid")
    commits = []
    for version in ("0.5.5", "0.5.5.1", "0.5.6"):
        (work / "VERSION").write_text(version + "\n")
        git("add", "VERSION")
        git("commit", "-qm", version)
        commits.append(git("rev-parse", "HEAD"))
        git("tag", "-a", f"v{version}", "-m", version)
    git("tag", "stable", commits[0])
    git("tag", "nightly", commits[0])
    git("tag", "internal", commits[2])
    git("push", "origin", "HEAD:refs/heads/dev", "--tags")
    return work, git, commits


def run(repo, script="release_tracks.py", *args):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args], cwd=repo[0],
        text=True, capture_output=True, timeout=30,
    )


def remote(repo, tag):
    rows = repo[1]("ls-remote", "--refs", "origin", f"refs/tags/{tag}")
    return rows.split()[0] if rows else ""


def move(repo, tag, sha):
    repo[1]("push", "--force", "origin", f"{sha}:refs/tags/{tag}")


def test_release_moves_stable_and_lagging_nightly_without_writing_internal(repo):
    result = run(repo, "release_tracks.py", "--release", "v0.5.5.1")
    assert result.returncode == 0, result.stderr
    assert remote(repo, "stable") == repo[2][1]
    assert remote(repo, "nightly") == repo[2][1]
    assert remote(repo, "internal") == repo[2][2]


def test_backwards_release_is_refused(repo):
    move(repo, "stable", repo[2][2])
    result = run(repo, "release_tracks.py", "--release", "v0.5.5.1")
    assert result.returncode != 0
    assert "backwards" in result.stderr
    assert remote(repo, "stable") == repo[2][2]


def test_release_preserves_newer_double_green_nightly(repo):
    move(repo, "nightly", repo[2][2])
    result = run(repo, "release_tracks.py", "--release", "v0.5.5.1")
    assert result.returncode == 0, result.stderr
    assert remote(repo, "stable") == repo[2][1]
    assert remote(repo, "nightly") == repo[2][2]


@pytest.mark.parametrize("missing", [False, True])
def test_nightly_run_catches_up_without_green_markers(repo, missing):
    move(repo, "stable", repo[2][1])
    if missing:
        repo[1]("push", "origin", ":refs/tags/nightly")
    result = run(repo, "release_tracks.py", "--nightly")
    assert result.returncode == 0, result.stderr
    assert remote(repo, "nightly") == repo[2][1]


def test_old_green_candidate_cannot_undo_stable_or_newer_nightly(repo):
    move(repo, "stable", repo[2][1])
    move(repo, "nightly", repo[2][2])
    result = run(repo, "release_tracks.py", "--nightly", repo[2][0])
    assert result.returncode == 0, result.stderr
    assert remote(repo, "nightly") == repo[2][2]


def test_fresh_green_candidate_advances_nightly(repo):
    result = run(repo, "release_tracks.py", "--nightly", repo[2][2])
    assert result.returncode == 0, result.stderr
    assert remote(repo, "nightly") == repo[2][2]
    assert remote(repo, "stable") == repo[2][0]


@pytest.mark.parametrize("stable,nightly,ok", [(0, 0, False), (1, 0, False), (0, 2, False), (1, 1, True), (1, 2, True)])
def test_release_readback_requires_both_tracks(repo, stable, nightly, ok):
    move(repo, "stable", repo[2][stable])
    move(repo, "nightly", repo[2][nightly])
    result = run(repo, "check-release-tracks.py", "0.5.5.1")
    assert (result.returncode == 0) == ok, result.stderr
    for tag in ("stable", "nightly", "internal"):
        assert f"{tag}=" in result.stdout
    assert "stable_reached=" in result.stdout
    assert "nightly_reached=" in result.stdout


def test_missing_stable_is_created(repo):
    repo[1]("push", "origin", ":refs/tags/stable")
    result = run(repo, "release_tracks.py", "--release", "v0.5.5.1")
    assert result.returncode == 0, result.stderr
    assert remote(repo, "stable") == repo[2][1]


def test_two_releases_in_succession_never_allow_the_older_one_back(repo):
    for tag in ("v0.5.5.1", "v0.5.6"):
        result = run(repo, "release_tracks.py", "--release", tag)
        assert result.returncode == 0, result.stderr
    result = run(repo, "release_tracks.py", "--release", "v0.5.5.1")
    assert result.returncode != 0
    assert remote(repo, "stable") == remote(repo, "nightly") == repo[2][2]


def test_wrong_event_sha_is_rejected_before_tracks_move(repo):
    result = run(repo, "release_tracks.py", "--release", "v0.5.5.1", "--expected-sha", repo[2][0])
    assert result.returncode != 0
    assert "event SHA" in result.stderr
    assert remote(repo, "stable") == remote(repo, "nightly") == repo[2][0]


def test_shallow_release_checkout_gets_history_before_ancestry_check(repo, tmp_path):
    runner = tmp_path / "shallow"
    subprocess.run(["git", "clone", "--depth=1", "--branch", "v0.5.5.1", (tmp_path / "remote.git").as_uri(), str(runner)], check=True, capture_output=True)
    result = run((runner, repo[1], repo[2]), "release_tracks.py", "--release", "v0.5.5.1")
    assert result.returncode == 0, result.stderr
    assert remote(repo, "stable") == remote(repo, "nightly") == repo[2][1]
