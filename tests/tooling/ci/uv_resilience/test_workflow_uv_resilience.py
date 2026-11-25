"""Every workflow job that runs uv survives a few minutes of index trouble.

Measured 2026-11-17 03:10-04:30 UTC: 57 jobs failed creating their Python
environment (pypi.org simple index, wheel downloads, python-build-standalone
downloads from github.com). The fix has three parts, each pinned here:

* UV_HTTP_RETRIES is set next to UV_HTTP_TIMEOUT in every job that runs uv
  (macOS host-mode jobs included; it is only a read-only setting there);
* the commands that only fetch and materialise an environment are wrapped in
  ``ci_retry_uv``, and nothing that verifies anything is;
* free-threaded CPython comes from the pinned file:// mirror on Linux runners
  instead of an ``unset UV_PYTHON_INSTALL_MIRROR`` followed by a live download.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = ROOT / ".forgejo" / "workflows"

UV_USE = re.compile(r"(^|[\s(])uv (sync|run|build|python|pip|venv)|scripts/build/package-macos-shell\.sh", re.M)
FETCH_COMMANDS = re.compile(r"^\s*(?:\(cd \"[^\"]+\" && )?uv (sync|python install|pip install)\b")
ALLOWED_WRAPPED = re.compile(
    r"^ci_retry_uv [a-z0-9-]+ uv "
    r"(sync --locked --python 3\.12( --extra test)?"
    r"|python install (3\.12|3\.14t|3\.15t)"
    r"|pip install --python \"\$\{venv\}/bin/python\" 'pytest>=8\.4,<9'"
    r"|run --locked --no-sync --python 3\.12 --with libcst==1\.9\.0 python -c 'import libcst'"
    r"|run --no-project --python 3\.12 python -c 'pass'"
    r"|run --no-project --with pytest python -c 'import pytest')$"
)
MACOS_EXEMPT_SYNC = ("e2e-macos-credentialed.yml", "e2e-macos-isolated.yml")
HOST_WITHOUT_UV_ENV = {("e2e-provider-calibration.yml", "calibrate")}


def _workflows():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        yield path.name, yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _jobs():
    for name, doc in _workflows():
        for job_id, job in (doc.get("jobs") or {}).items():
            yield name, job_id, job


def _runs(job: dict) -> list[tuple[dict, str]]:
    return [(step, str(step.get("run", ""))) for step in job.get("steps", []) if isinstance(step, dict)]


def test_every_job_that_runs_uv_sets_ten_http_retries_beside_the_timeout() -> None:
    checked = 0
    for name, job_id, job in _jobs():
        if (name, job_id) in HOST_WITHOUT_UV_ENV:
            continue
        if not any(UV_USE.search(run) for _step, run in _runs(job)):
            continue
        env = job.get("env") or {}
        assert env.get("UV_HTTP_RETRIES") == "10", (name, job_id, env)
        assert "UV_HTTP_TIMEOUT" in env, (name, job_id, env)
        checked += 1
    assert checked >= 24, checked


def test_every_fetching_uv_command_is_wrapped_except_the_macos_host_syncs() -> None:
    wrapped = 0
    for name, job_id, job in _jobs():
        for step, run in _runs(job):
            for line in run.splitlines():
                if not FETCH_COMMANDS.match(line) or "--no-deps" in line:
                    continue
                if name in MACOS_EXEMPT_SYNC:
                    continue
                raise AssertionError(f"{name}:{job_id}:{step.get('name')}: unwrapped fetch command: {line.strip()}")
            for line in run.splitlines():
                stripped = line.strip()
                if "ci_retry_uv " in stripped and not stripped.startswith("#"):
                    wrapped += 1
    assert wrapped >= 18, wrapped


def test_only_fetch_and_warm_commands_are_ever_wrapped() -> None:
    for name, job_id, job in _jobs():
        for step, run in _runs(job):
            for line in run.splitlines():
                stripped = line.strip().removeprefix('(cd "${AT04_CI_ROOT}/old" && ').removesuffix(")")
                if not stripped.startswith("ci_retry_uv "):
                    continue
                assert ALLOWED_WRAPPED.match(stripped), f"{name}:{job_id}:{step.get('name')}: {stripped}"


def test_a_step_that_calls_ci_retry_uv_sources_the_helper_first() -> None:
    for name, job_id, job in _jobs():
        for step, run in _runs(job):
            if "ci_retry_uv " not in run:
                continue
            source = run.find("source scripts/ci/ci-retry.sh")
            use = run.find("ci_retry_uv ")
            assert 0 <= source < use, f"{name}:{job_id}:{step.get('name')}"
            assert step.get("timeout-minutes"), f"{name}:{job_id}:{step.get('name')}: wrapped step needs its own cap"


def test_wrapped_steps_have_room_for_every_attempt_and_stay_inside_the_job_timeout() -> None:
    """Worst case with a fast-failing index: 3 attempts x (about 65 s of uv backoff
    for 10 retries plus per-request time) plus 40 s of ci_retry backoff is roughly
    4 to 7 minutes, so a wrapped step needs a cap of at least 8 minutes; with a
    hanging index the step cap, not the retries, ends the step."""

    for name, job_id, job in _jobs():
        for step, run in _runs(job):
            if "ci_retry_uv " not in run:
                continue
            cap = int(step["timeout-minutes"])
            assert 8 <= cap <= int(job["timeout-minutes"]), (name, job_id, step.get("name"), cap)


def test_free_threaded_installs_use_the_pinned_mirror_except_on_the_macos_host() -> None:
    doc = dict(_workflows())["native-conformance.yml"]
    seen = {}
    for job_id, job in doc["jobs"].items():
        for step, run in _runs(job):
            for version in ("3.14t", "3.15t"):
                if f"uv python install {version}" in run:
                    seen[job_id] = (version, step, run)
    assert set(seen) == {"native-linux-amd64", "native-linux-arm64", "native-darwin-arm64", "free-threaded-3-15t"}
    for job_id in ("native-linux-amd64", "native-linux-arm64"):
        version, _step, run = seen[job_id]
        assert version == "3.14t"
        assert "unset UV_PYTHON_INSTALL_MIRROR" not in run
        assert run.index("ci-ensure-cpython-mirror.sh 3.14t") < run.index("uv python install 3.14t")
    version, _step, run = seen["native-darwin-arm64"]
    assert "unset UV_PYTHON_INSTALL_MIRROR" in run
    assert "ci-ensure-cpython-mirror.sh" not in run


def test_the_315t_job_stages_its_mirror_in_a_step_before_the_install_step() -> None:
    job = dict(_workflows())["native-conformance.yml"]["jobs"]["free-threaded-3-15t"]
    runs = [run for _step, run in _runs(job)]
    stage = next(i for i, run in enumerate(runs) if "ci-ensure-cpython-mirror.sh 3.15t" in run)
    install = next(i for i, run in enumerate(runs) if "uv python install 3.15t" in run)
    assert stage < install
    assert "unset UV_PYTHON_INSTALL_MIRROR" not in runs[install]
    assert "ci-uv-cache.sh" in runs[stage]


def test_cli_inventory_and_gui_jobs_use_the_persistent_uv_cache() -> None:
    docs = dict(_workflows())
    for name, job_id in (("gate-python.yml", "cli-help-inventory"), ("gate-gui.yml", "gui-unit")):
        job = docs[name]["jobs"][job_id]
        runs = [run for _step, run in _runs(job)]
        cache = next(i for i, run in enumerate(runs) if "ci-uv-cache.sh" in run)
        sync = next(i for i, run in enumerate(runs) if "uv sync" in run)
        assert cache < sync, (name, job_id)


def test_pr_hygiene_warms_python_and_the_pytest_overlay_before_the_gate() -> None:
    job = dict(_workflows())["gate-pr-hygiene.yml"]["jobs"]["pr-hygiene"]
    runs = [run for _step, run in _runs(job)]
    warm = next(i for i, run in enumerate(runs) if "ci_retry_uv uv-overlay" in run)
    gate = next(i for i, run in enumerate(runs) if "check_pr_hygiene.py" in run and "uv run" in run)
    assert warm < gate
