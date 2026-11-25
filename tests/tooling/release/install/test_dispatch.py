"""Execute the actual internal dispatch block without GitHub or Forgejo access."""
import json
import os
import subprocess

import pytest
import yaml

from test_publish import ROOT


@pytest.mark.parametrize("track,moved", [("internal", True), ("internal", False), ("nightly", True)])
@pytest.mark.parametrize("github_fails", [False, True])
@pytest.mark.parametrize("wheel_fails", [False, True])
def test_dispatches_exactly_one_wheel_build_before_github(tmp_path, track, moved, github_fails, wheel_fails):
    workflow, job = ("gate-python", "unit") if track == "internal" else ("release-nightly-promote", "promote")
    doc = yaml.safe_load((ROOT / f".github/workflows/{workflow}.yml").read_text())
    step = next(s for s in doc["jobs"][job]["steps"] if
                s["name"] == f"Dispatch the GitHub publication of the moved {track} tag")
    sha = "a" * 40
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").write_text(f'#!/bin/sh\nprintf "{sha}\\trefs/tags/{track}\\n"\n')
    (bin_dir / "curl").write_text(
        '#!/bin/sh\nwhile IFS= read -r ignored; do :; done\n'
        'printf "%s\\n" "$*" >> "$REQUESTS"\n'
        'case "$*" in *github-publish.yml*) exit "$GH_FAIL";; '
        '*wheel-release.yml*) exit "$WHEEL_FAIL";; esac\n')
    for path in bin_dir.iterdir():
        path.chmod(0o755)
    record = tmp_path / "requests"
    env = {**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path),
           "GITHUB_SHA": sha, "GITHUB_TOKEN": "fake", "GITHUB_SERVER_URL": "https://fixture.invalid",
           "GITHUB_REPOSITORY": "test/repo", "REQUESTS": str(record), "GH_FAIL": "1" if github_fails else "0",
           "WHEEL_FAIL": "1" if wheel_fails else "0", "PROMOTED": sha, "MOVED_INTERNAL": sha if moved else ""}
    result = subprocess.run(["bash", "-c", step["run"]], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == (1 if github_fails or (moved and wheel_fails) else 0), result.stderr
    calls = record.read_text().splitlines()
    if not moved:
        assert len(calls) == 1 and "github-publish.yml/dispatches" in calls[0]
        return
    assert len(calls) == 2
    assert "wheel-release.yml/dispatches" in calls[0]
    assert "github-publish.yml/dispatches" in calls[1]
    body = calls[0].split("--data ", 1)[1].split(" https://", 1)[0]
    assert json.loads(body) == {"ref": track, "inputs": {"track": track, "source_sha": sha}}
    github = (ROOT / ".github/workflows/github-publish.yml").read_text()
    assert "Dispatch internal wheel build for this exact source" not in github


def test_internal_noop_does_not_rebuild_wheels(tmp_path):
    doc = yaml.safe_load((ROOT / ".github/workflows/gate-python.yml").read_text())
    steps = doc["jobs"]["unit"]["steps"]
    mover = next(s for s in steps if s["name"] == "Move the track tags to the unit-green dev head")
    dispatch = next(s for s in steps if s["name"] == "Dispatch the GitHub publication of the moved internal tag")
    assert mover["id"] == "internal_move"
    assert mover["run"].index("git push") < mover["run"].index("promoted=")
    assert dispatch["env"]["MOVED_INTERNAL"] == "${{ steps.internal_move.outputs.promoted }}"
    assert 'if [[ "${MOVED_INTERNAL:-}" = "${GITHUB_SHA}" ]]' in dispatch["run"]
    # Nightly already exports promoted only after a real move; an unchanged
    # pointer has an empty output and the existing job condition skips dispatch.
    nightly = yaml.safe_load((ROOT / ".github/workflows/release-nightly-promote.yml").read_text())
    step = next(s for s in nightly["jobs"]["promote"]["steps"] if s["name"].startswith("Dispatch the GitHub"))
    assert step["if"] == "success() && env.NIGHTLY_PUBLICATION_ENABLED == 'true' && steps.move.outputs.promoted != ''"


def test_manual_wheel_dispatch_accepts_all_tracks():
    text = (ROOT / ".github/workflows/wheel-release.yml").read_text()
    for track in ("stable", "nightly", "internal"):
        assert f"workflow_dispatch:{track}" in text
