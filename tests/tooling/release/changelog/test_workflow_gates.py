"""Execute the release guard from each real workflow without publishing anything."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from tests.support.ci.ci_workflow_policy import is_gate_step, relaxed_gate_surfaces

ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = ROOT / ".github/workflows"


def doc(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


@pytest.mark.parametrize("name,job,before", [
    ("release-tag.yml", "tag", "Create and push the tag"),
    ("github-publish.yml", "publish", "Build denylist-filtered snapshot tree"),
    ("wheel-release.yml", "build-publish", "Stamp local version segment and build wheel"),
])
def test_guard_is_hard_and_precedes_publication(name, job, before):
    steps = doc(name)["jobs"][job]["steps"]
    guards = [(i, s) for i, s in enumerate(steps) if "check-changelog.mjs" in s.get("run", "")]
    assert len(guards) == 1, f"{name} must execute the changelog checker once"
    index, guard = guards[0]
    assert is_gate_step(guard) and not guard.get("continue-on-error")
    assert index < next(i for i, s in enumerate(steps) if s["name"] == before)
    assert relaxed_gate_surfaces((WORKFLOWS / name).read_text()) == []
    run = guard["run"]
    assert "--tag" in run
    if name == "release-tag.yml":
        assert '--rev "${IN_SHA}"' in run
        # The always() create step must not see the success token on failure.
        assert run.index("check-changelog.mjs") < run.index('echo "TAG_NAME=')
    else:
        assert 'v*)' in run and 'case ' in run
        assert "|| true" not in run


@pytest.mark.parametrize("name,job,var", [
    ("github-publish.yml", "publish", "PLAN_TAG"),
    ("wheel-release.yml", "build-publish", "WHEEL_TRACK"),
])
@pytest.mark.parametrize("tag", ["v0.5.5.1", "v0.5.5", "internal", "nightly", "stable"])
@pytest.mark.parametrize("notes,version_ok", [
    ("## [Unreleased]\n- Not released.\n", False),
    ("## [0.5.5.1] - 2026-10-26\n### Fixed\n- Released.\n", True),
])
def test_publish_guard_shell_blocks_formal_releases_only(
    tmp_path, name, job, var, tag, notes, version_ok,
):
    steps = doc(name)["jobs"][job]["steps"]
    guard = next(s for s in steps if "check-changelog.mjs" in s.get("run", ""))
    target = tmp_path / "scripts/ci"
    target.mkdir(parents=True)
    shutil.copy(ROOT / "scripts/ci/check-changelog.mjs", target)
    (tmp_path / "VERSION").write_text("0.5.5.1\n")
    (tmp_path / "CHANGELOG.md").write_text(notes)
    result = subprocess.run(
        ["bash", "-c", guard["run"]], cwd=tmp_path,
        env={**os.environ, var: tag}, capture_output=True, text=True, timeout=10,
    )
    blocked = tag.startswith("v") and not (tag == "v0.5.5.1" and version_ok)
    assert result.returncode == int(blocked), result.stderr
    if tag == "v0.5.5":
        assert "tag/VERSION mismatch" in result.stderr


@pytest.mark.parametrize("notes,tag,passes", [
    ("## [Unreleased]\n- pending\n", "v0.5.5.1", False),
    ("## [0.5.5.1]\n### Fixed\n", "v0.5.5.1", False),
    ("## [0.5.5.10]\n- collision\n", "v0.5.5.1", False),
    ("## [0.5.5]\n- wrong\n", "v0.5.5", False),
    ("## [0.5.5.1]\n- actual\n", "v0.5.5.1", True),
])
def test_actual_tag_gates_never_arm_push_without_notes(tmp_path, notes, tag, passes):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (tmp_path / "VERSION").write_text("0.5.5.1\n")
    (tmp_path / "CHANGELOG.md").write_text(notes)
    git("add", ".")
    git("commit", "-qm", "target")
    sha = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/dev", sha)
    git("remote", "add", "origin", str(tmp_path))
    scripts = tmp_path / "scripts/ci"
    scripts.mkdir(parents=True)
    for file in ("check-changelog.mjs", "ci-step.sh"):
        source = ROOT / "scripts/ci" / file
        if source.exists():  # Also run this regression against the pre-gate tree.
            shutil.copy(source, scripts / file)
    askpass_path = tmp_path / "askpass-path"
    askpass_path.write_text("/unused\n")
    env_file = tmp_path / "env"
    steps = doc("release-tag.yml")["jobs"]["tag"]["steps"]
    gate = next(s for s in steps if s["name"].startswith("Gates ("))
    run = gate["run"].replace("/tmp/reltag-askpass-path", str(askpass_path))
    result = subprocess.run(
        ["bash", "-c", run], cwd=tmp_path, capture_output=True, text=True, timeout=10,
        env={**os.environ, "IN_SHA": sha, "IN_TAG": tag, "IN_DRY": "true",
             "GITHUB_SERVER_URL": "https://example.invalid", "NO_PROXY": "",
             "GITHUB_ENV": str(env_file)},
    )
    assert result.returncode == (0 if passes else 1), result.stdout + result.stderr
    armed = env_file.exists() and "TAG_NAME=" in env_file.read_text()
    assert armed == passes
    assert git("tag", "--list") == ""


def test_wheel_lane_remains_track_only():
    workflow = doc("wheel-release.yml")
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["push"]["tags"] == ["nightly", "stable"]
    run = workflow["jobs"]["build-publish"]["steps"][0]["run"]
    assert "workflow_dispatch:internal|workflow_dispatch:nightly|workflow_dispatch:stable|push:nightly|push:stable)" in run
    assert '*) echo "refusing wheel invocation' in run
