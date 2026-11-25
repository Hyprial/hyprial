"""The release chain must not go red for reasons that leave distribution intact.

Field record (2026-11-07/08, v0.5.5.3.4):
    * run 51727 (nightly): ``uv publish`` got 409 "package file already exists"
      because stable had already published the byte-identical
      ``0.5.5.3.4+gc3a1857`` (stable floor puts nightly on the stable SHA).
    * runs 51240/51376/51622: R2 and the registry were written, then the
      convenience workflow-artifact copy hit Forgejo 502 and the report step
      turned the job red although nothing consumes that artifact.
    * run 51606: the first (cold) target took 25 min to compile the Tailcat
      sidecar; the 35-min job cap killed it before publishing, and
      github-publish 51607 then waited until the runner killed it too.

Each check below runs the real step script where possible (fake ``uv`` on
PATH), so it pins behaviour, not wording.  What must stay red stays red:
a registry file with a different sha256, and an artifact failure when the
registry publish did not succeed.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]
WHEEL = ROOT / ".github/workflows/wheel-release.yml"
PUBLISH = ROOT / ".github/workflows/github-publish.yml"


def _job(path: Path, name: str) -> dict:
    return (yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"])[name]


def _step(job: dict, name: str) -> dict:
    return next(s for s in job["steps"] if s.get("name") == name)


def _run_step(script: str, tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    summary = tmp_path / "summary"
    summary.touch()
    return subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env={"PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "GITHUB_STEP_SUMMARY": str(summary), **env},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_registry_publish_skips_identical_files_without_leaking_credentials(tmp_path: Path) -> None:
    step = _step(_job(WHEEL, "build-publish"), "Publish wheel to Forgejo PyPI registry")
    assert step.get("id") == "pypi_registry"
    assert "continue-on-error" not in step and "if" not in step, "a real registry failure must red the job"
    record = tmp_path / "uv-call"
    fake = tmp_path / "bin" / "uv"
    fake.parent.mkdir()
    fake.write_text(
        '#!/usr/bin/env bash\n{ printf "%s\\n" "$@"; echo "--- netrc"; cat "$NETRC"; } > "$RECORD"\n',
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "a.whl").touch()
    result = _run_step(
        str(step["run"]),
        tmp_path,
        {
            "GITHUB_SERVER_URL": "http://forgejo-tailscale:3000/",
            "UV_PUBLISH_USERNAME": "bot",
            "UV_PUBLISH_PASSWORD": "s3cret",
            "RECORD": str(record),
        },
    )
    assert result.returncode == 0, result.stderr
    argv, netrc = record.read_text(encoding="utf-8").split("--- netrc\n")
    assert argv.splitlines() == [
        "publish",
        "dist/a.whl",
        "--check-url",
        "http://forgejo-tailscale:3000/api/packages/HyprialOS/pypi/simple/",
    ]
    assert "s3cret" not in argv, "credentials must never reach argv"
    assert netrc.strip() == "machine forgejo-tailscale login bot password s3cret"


def _report_step() -> dict:
    return _step(_job(WHEEL, "build-publish"), "Report wheel artifact upload failure")


def test_artifact_failure_is_advisory_only_after_a_successful_registry_publish(tmp_path: Path) -> None:
    step = _report_step()
    assert "continue-on-error" not in step, "the not-in-registry branch must be able to red the job"
    ok = _run_step(str(step["run"]), tmp_path, {"PYPI_REGISTRY_OUTCOME": "success", "WHEEL_ARTIFACT_NAME": "wheels-x-1"})
    assert ok.returncode == 0, ok.stderr
    assert "::warning title=wheel artifact not saved::" in ok.stdout
    assert "ARTIFACT_UPLOAD_RETRY artifact=wheels-x-1" in ok.stdout
    assert "artifact upload failed/timeout" in (tmp_path / "summary").read_text(encoding="utf-8")
    for outcome in ("skipped", "failure", "cancelled", ""):
        bad = _run_step(str(step["run"]), tmp_path, {"PYPI_REGISTRY_OUTCOME": outcome, "WHEEL_ARTIFACT_NAME": "w"})
        assert bad.returncode == 1, f"outcome={outcome!r} must stay red"
        assert "did NOT reach the registry" in bad.stderr


def test_mutation_unconditional_success_is_detected(tmp_path: Path) -> None:
    """Self-check: dropping the success guard must make the same probe fail."""
    run = str(_report_step()["run"])
    mutated = re.sub(r'if \[ "\$\{PYPI_REGISTRY_OUTCOME:-\}" = "success" \]; then', "if true; then", run)
    assert mutated != run
    bad = _run_step(mutated, tmp_path, {"PYPI_REGISTRY_OUTCOME": "skipped", "WHEEL_ARTIFACT_NAME": "w"})
    assert bad.returncode == 0, "the probe above would now catch this regression"


def test_cold_build_is_cached_and_capped_inside_the_job_budget() -> None:
    job = _job(WHEEL, "build-publish")
    names = [s.get("name") for s in job["steps"]]
    cache = "Persist Go, npm and uv caches; stage the pinned CPython"
    build = "Stamp local version segment and build wheel"
    assert names.index(cache) < names.index(build)
    cache_run = str(_step(job, cache)["run"])
    for needle in ("GOMODCACHE=", "GOCACHE=", "npm_config_cache=", "ci-uv-cache.sh", "ci-ensure-cpython-mirror.sh"):
        assert needle in cache_run, needle
    build_step = _step(job, build)
    assert 0 < int(build_step["timeout-minutes"]) <= int(job["timeout-minutes"]) - 15, (
        "the build cap must kill a slow build BEFORE publish starts, leaving room to publish"
    )
    builds = re.findall(r"uv build [^\n]*", str(build_step["run"]))
    assert builds and all("--python 3.12" in b for b in builds), "the file mirror only holds 3.12"


def test_publish_registry_wait_is_bounded_by_the_job_budget() -> None:
    job = _job(PUBLISH, "publish")
    preflight = str(_step(job, "Preflight (publish secret present)")["run"])
    assert 'JOB_START_EPOCH=$(date +%s)" >> "$GITHUB_ENV"' in preflight
    attach = next(str(s["run"]) for s in job["steps"] if "attach_wheel_assets.py" in str(s.get("run", "")))
    minutes = re.search(r"budget=\$\(\( (\d+)\*60 ", attach)
    assert minutes and int(minutes.group(1)) == int(job["timeout-minutes"]), "keep in sync with timeout-minutes"
    assert '--wait-for-registry "${wait}"' in attach
    assert "${JOB_START_EPOCH:?" in attach, "a missing start epoch must fail, not fall back silently"
    gate = next(str(s["run"]) for s in job["steps"] if "uv build --wheel" in str(s.get("run", "")))
    assert "uv build --wheel --python 3.12" in gate
