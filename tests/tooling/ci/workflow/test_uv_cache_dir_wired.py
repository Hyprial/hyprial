"""The pr-hygiene job must point uv at the shared CI cache before any uv run.

Why this exists (measured 2026-10-31, run 42281 / #2210): pr-hygiene's
merge-tree gate ran ``uv run --no-project --with pytest`` with a cold cache and
without a proxy, so every PR re-downloaded pytest and its dependencies, and a
slow path surfaced as ``Failed to extract archive`` / ``network timeout``. #2219
restores the proxy and UV_HTTP_TIMEOUT; this wiring removes the download from
the required check's critical path in the first place.

The setting cannot live in the job's ``env:`` block: CI_CACHE_ROOT only exists
on the DinD runners, so a literal value would point uv at a missing directory
on every other runner. The step therefore writes ``UV_CACHE_DIR`` to
``$GITHUB_ENV`` and falls back to the per-job cache when the shared root is
absent or not writable. It must precede the first step that runs
``check_pr_hygiene.py``, because ``GITHUB_ENV`` only governs later steps.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]
TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "gate-pr-hygiene.yml"
STEP_NAME = "Point the uv cache at the shared CI cache"
GATE_SCRIPT = "check_pr_hygiene.py"


def _pr_hygiene_steps():
    doc = yaml.safe_load(TESTS_WORKFLOW.read_text()) or {}
    return (doc.get("jobs") or {})["pr-hygiene"]["steps"]


def _index_of_cache_step():
    for index, step in enumerate(_pr_hygiene_steps()):
        if step.get("name") == STEP_NAME:
            return index
    return None


def _index_of_first_gate_step():
    """The first step that actually *runs* uv with the gate script.

    A plain substring match is too loose: the preceding step materialises the
    gate from the base ref (``gate="scripts/ci/check_pr_hygiene.py"``) without
    running uv, and matching that step would hide a wrong ordering.
    """

    for index, step in enumerate(_pr_hygiene_steps()):
        run = str(step.get("run", ""))
        if GATE_SCRIPT in run and "uv run" in run:
            return index
    return None


def test_the_cache_step_exists_exactly_once():
    names = [step.get("name") for step in _pr_hygiene_steps()]
    assert names.count(STEP_NAME) == 1, names


def test_the_cache_step_precedes_every_hygiene_gate_run():
    cache_index = _index_of_cache_step()
    gate_index = _index_of_first_gate_step()
    assert cache_index is not None, "the uv cache step is missing"
    assert gate_index is not None, "no step runs check_pr_hygiene.py"
    assert cache_index < gate_index, (
        "the uv cache step must come before the first check_pr_hygiene.py step: "
        f"cache at {cache_index}, first gate at {gate_index}"
    )


def test_the_cache_step_exports_through_github_env_with_a_fallback():
    script = str(_pr_hygiene_steps()[_index_of_cache_step()]["run"])
    assert 'UV_CACHE_DIR=' in script, "the step never exports UV_CACHE_DIR"
    assert '>>"$GITHUB_ENV"' in script, "the export must go through GITHUB_ENV"
    assert "CI_CACHE_ROOT" in script, "the shared root must be the source of the path"
    assert "-w" in script, "the step must check writability before using the shared root"
    assert "per-job" in script, "there must be an explicit fallback path"


def test_the_job_env_does_not_hardcode_a_cache_dir():
    doc = yaml.safe_load(TESTS_WORKFLOW.read_text()) or {}
    job_env = ((doc.get("jobs") or {})["pr-hygiene"].get("env") or {})
    assert "UV_CACHE_DIR" not in job_env, (
        "UV_CACHE_DIR depends on CI_CACHE_ROOT and must not be a literal job env value"
    )
