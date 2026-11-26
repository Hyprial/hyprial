"""Static policy checks for the Windows smoke workflow.

Decision (2026-11-26, tier 1 smoke).  Enforces the GitHub-native compliance rules
of the Windows smoke workflow, plus workflow coexistence and tier file-list checks.
A file listed in tier1-rejected.tsv must not also be admitted in tier1-tests.txt.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[4]
WORKFLOW = ROOT / ".github/workflows/windows-smoke.yml"
PINS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "astral-sh/setup-uv": "1db35dd8eee11c5894872e0b383ae6c406cf738d",
    "actions/upload-artifact": "cf430e030ddbb5b0abf93d22962f4752f3646cd9",
    "actions/download-artifact": "9000827ccba6bdab643e8b6fd33ac0654aef8333",
}
EXPECTED_CHECKOUT_REF = "${{ github.event.pull_request.head.sha || github.sha }}"
NATIVE = re.compile(r"^(?:uv|rustup|git|\.venv[\\/]Scripts[\\/]python\.exe)\b")
PRIVATE_IP = re.compile(
    r"10\.\d+\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.|192\.168\.|100\.\d+\.\d+\.\d+"
)
EMAIL = re.compile(r"[\w.+-]+@([\w-]+\.[\w.]+)")


def document() -> dict[Any, Any]:
    value = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def trigger(value: dict[Any, Any]) -> dict[str, Any]:
    result = value.get("on") if "on" in value else value.get(True, {})
    return result if isinstance(result, dict) else {}


def jobs(value: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = value["jobs"]
    return {name: job for name, job in result.items() if isinstance(job, dict)}


def steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job.get("steps", []) if isinstance(step, dict)]


def test_action_pins_and_permissions() -> None:
    value = document()
    text = WORKFLOW.read_text(encoding="utf-8")
    found: dict[str, str] = {}
    checkout_count = 0
    for job in jobs(value).values():
        assert "permissions" not in job or job["permissions"] == {"contents": "read"}
        for step in steps(job):
            uses = step.get("uses")
            if not isinstance(uses, str):
                continue
            action, _, sha = uses.partition("@")
            assert re.fullmatch(r"[0-9a-f]{40}", sha), uses
            assert action in PINS, action
            assert sha == PINS[action], uses
            found[action] = sha
            if action == "actions/checkout":
                checkout_count += 1
                with_block = step.get("with", {})
                assert isinstance(with_block, dict)
                assert with_block.get("persist-credentials") is False
                assert with_block.get("ref") == EXPECTED_CHECKOUT_REF
    assert found == PINS
    assert checkout_count == 5
    assert value["permissions"] == {"contents": "read"}
    assert "pull_request_target" not in text
    assert "secrets." not in text


def test_runner_labels_and_job_timeouts() -> None:
    value = document()
    expected = {
        "build-wheel": "windows-latest",
        "w1-x64": "windows-latest",
        "smoke-x64": "windows-latest",
        "smoke-arm": "windows-11-arm",
        "windows-smoke-summary": "ubuntu-latest",
    }
    all_jobs = jobs(value)
    assert set(all_jobs) == set(expected)
    for name, runner in expected.items():
        actual = all_jobs[name]["runs-on"]
        assert isinstance(actual, str) and "${{" not in actual
        assert actual == runner
        assert isinstance(all_jobs[name]["timeout-minutes"], int)
        assert all_jobs[name]["timeout-minutes"] > 0
    summary = all_jobs["windows-smoke-summary"]
    assert summary.get("if") == "always()"
    assert summary.get("needs") == ["build-wheel", "w1-x64", "smoke-x64", "smoke-arm"]
    assert all_jobs["smoke-arm"].get("needs") == ["build-wheel"]
    summary_uses = [step.get("uses", "").split("@", 1)[0] for step in steps(summary)]
    assert "actions/checkout" in summary_uses
    assert "actions/download-artifact" in summary_uses

    step_timeouts = {
        ("w1-x64", "Install the editable test environment"): 20,
        ("smoke-x64", "Run x64 smoke"): 10,
        ("smoke-arm", "Run ARM smoke"): 10,
    }
    for (job_name, step_name), minutes in step_timeouts.items():
        matching = [step for step in steps(all_jobs[job_name]) if step.get("name") == step_name]
        assert len(matching) == 1, (job_name, step_name)
        assert type(matching[0].get("timeout-minutes")) is int
        assert matching[0]["timeout-minutes"] == minutes

    validate_steps = [step for step in steps(summary) if step.get("name") == "Validate all legs"]
    assert len(validate_steps) == 1
    validate_run = str(validate_steps[0].get("run", ""))
    inputs = re.findall(r'--input\s+"([^"=]+)=([^"]+)"', validate_run)
    legs = [leg for _, leg in inputs]
    assert len(legs) == 3 and set(legs) == {"w1", "x64", "arm"}, inputs
    for path, _ in inputs:
        assert re.fullmatch(
            r"\$\{\{\s*runner\.temp\s*\}\}/wci-artifacts/[A-Za-z0-9._-]+", path
        ), path

    w1_run_steps = [
        step for step in steps(all_jobs["w1-x64"]) if step.get("name") == "Run W1 waves"
    ]
    assert len(w1_run_steps) == 1
    w1_run = str(w1_run_steps[0].get("run", ""))
    assert "--tier 1" in w1_run
    assert "--files scripts/ci/windows/tier1-tests.txt" in w1_run

    concurrency = value.get("concurrency")
    assert isinstance(concurrency, dict)
    assert str(concurrency.get("group", "")).startswith(
        "windows-smoke-${{ github.event_name }}-"
    )
    assert concurrency.get("cancel-in-progress") == "${{ github.event_name == 'pull_request' }}"


def test_windows_env_keys_and_proxy_policy() -> None:
    value = document()
    text = WORKFLOW.read_text(encoding="utf-8")
    assert not re.search(r"(?im)^\s*[A-Z_]*(?:HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|NO_PROXY)\s*:", text)
    assert "continue-on-error" not in text
    top_env = [str(key).casefold() for key in value.get("env", {})]
    assert len(top_env) == len(set(top_env))
    for job in jobs(value).values():
        if job.get("runs-on") == "ubuntu-latest":
            continue
        job_keys = [str(key).casefold() for key in job.get("env", {})]
        assert len(job_keys) == len(set(job_keys))
        for step in steps(job):
            step_keys = [str(key).casefold() for key in step.get("env", {})]
            assert len(step_keys) == len(set(step_keys))


def test_upload_steps_have_outcome_followups() -> None:
    value = document()
    for job in jobs(value).values():
        job_steps = steps(job)
        for index, step in enumerate(job_steps):
            if step.get("uses", "").split("@", 1)[0] != "actions/upload-artifact":
                continue
            assert isinstance(step.get("id"), str) and step["id"]
            assert type(step.get("timeout-minutes")) is int and step["timeout-minutes"] == 10
            assert step.get("if") == "always()"
            assert step.get("with", {}).get("if-no-files-found") == "error"
            assert index + 1 < len(job_steps)
            next_run = str(job_steps[index + 1].get("run", ""))
            assert f"steps.{step['id']}.outcome" in next_run


def test_pwsh_runs_have_one_native_command_and_no_start_process() -> None:
    value = document()
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "Start-Process" not in text
    for job in jobs(value).values():
        if job.get("runs-on") == "ubuntu-latest":
            continue
        for step in steps(job):
            body = step.get("run")
            if not isinstance(body, str):
                continue
            lines = [line.strip() for line in body.splitlines() if line.strip()]
            native_indices = [idx for idx, line in enumerate(lines) if NATIVE.match(line)]
            assert len(native_indices) <= 1, body
            if native_indices:
                trailing = lines[native_indices[0] + 1 :]
                assert trailing == ["$code = $LASTEXITCODE", "exit $code"], body


def test_triggers_paths_and_sensitive_text() -> None:
    value = document()
    text = WORKFLOW.read_text(encoding="utf-8")
    event = trigger(value)
    assert "pull_request" in event and "push" in event and "workflow_dispatch" in event
    assert text.index("\nname:") < text.index("\non:") < text.index("\npermissions:")
    assert PRIVATE_IP.search(text) is None
    emails = [match.group(0) for match in EMAIL.finditer(text) if match.group(1) != "example.com"]
    assert emails == []
    assert not (ROOT / ".github/workflows/windows-followup.yml").exists()
    tier = ROOT / "scripts/ci/windows/tier1-tests.txt"
    for path in tier.read_text(encoding="utf-8").splitlines():
        assert (ROOT / path).is_file(), path
    tier_next = ROOT / "scripts/ci/windows/tier1-next.txt"
    for line in tier_next.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            assert (ROOT / stripped).is_file(), stripped
    arm_lines = [
        line.strip()
        for line in (ROOT / "scripts/ci/windows/tier1-arm-tests.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert arm_lines == []


def test_rejected_files_are_not_admitted() -> None:
    admitted = {
        line.strip()
        for line in (ROOT / "scripts/ci/windows/tier1-tests.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    lines = (ROOT / "scripts/ci/windows/tier1-rejected.tsv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "path\treason\trun_id"
    rows = [line.split("\t") for line in lines[1:] if line.strip() and not line.startswith("#")]
    for row in rows:
        assert len(row) == 3 and all(field.strip() for field in row), row
        assert (ROOT / row[0]).is_file(), row[0]
    rejected = [row[0] for row in rows]
    assert len(rejected) == len(set(rejected))
    assert admitted.isdisjoint(rejected), sorted(admitted.intersection(rejected))
