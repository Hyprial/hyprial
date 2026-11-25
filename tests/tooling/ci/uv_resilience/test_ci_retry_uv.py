"""ci_retry_uv: bounded, visible retry for uv environment creation (2026-11-17).

Measured 2026-11-17 03:10-04:30 UTC: 57 jobs failed in their Python environment
step because the runner's egress to the package index and to GitHub release
downloads failed for longer than uv's own 3 retries (about 29 s). The helper
re-runs one idempotent uv command at most 3 times. These tests drive it with a
fake ``uv`` on PATH, as the Rust fetch retry is tested in
tests/tooling/ci/rustify/test_e2e_rust_runtime.py.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
RETRY = ROOT / "scripts/ci/ci-retry.sh"

NETWORK_FAILURE = "error sending request for url (https://pypi.org/simple/hatchling/)\n"
LOCK_FAILURE = "The lockfile at `uv.lock` needs to be updated, but `--locked` was provided.\n"


def _fake_bin(tmp_path: Path, *, fail_first: int, rc: int = 1, message: str = NETWORK_FAILURE) -> tuple[Path, Path, Path]:
    commands = tmp_path / "bin"
    commands.mkdir()
    calls = tmp_path / "uv.calls"
    sleeps = tmp_path / "sleep.calls"
    uv = commands / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> '{calls}'\n"
        f"n=$(wc -l < '{calls}')\n"
        f"if [ \"$n\" -le {fail_first} ]; then printf '%s' '{message}' >&2; exit {rc}; fi\n"
        "exit 0\n"
    )
    uv.chmod(0o755)
    sleep = commands / "sleep"
    sleep.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$1\" >> '{sleeps}'\n")
    sleep.chmod(0o755)
    return commands, calls, sleeps


def _run(tmp_path: Path, commands: Path, snippet: str, **env: str) -> subprocess.CompletedProcess[str]:
    full = {"PATH": f"{commands}:{os.environ['PATH']}", "HOME": str(tmp_path), **env}
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail; source {RETRY}; {snippet}"],
        env=full,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _lines(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def test_first_try_success_is_silent_and_passes_arguments_through_unchanged(tmp_path: Path) -> None:
    commands, calls, sleeps = _fake_bin(tmp_path, fail_first=0)
    result = _run(tmp_path, commands, "ci_retry_uv uv-sync uv sync --locked --python 3.12 --extra test")
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert _lines(calls) == ["sync --locked --python 3.12 --extra test"]
    assert _lines(sleeps) == []


def test_transient_failure_twice_then_success_passes_and_reports_each_attempt(tmp_path: Path) -> None:
    commands, calls, _sleeps = _fake_bin(tmp_path, fail_first=2)
    result = _run(tmp_path, commands, "ci_retry_uv uv-sync uv sync --locked", CI_RETRY_BACKOFF="0 0")
    assert result.returncode == 0, result.stderr
    assert len(_lines(calls)) == 3
    assert result.stderr.count("CI_RETRY label=uv-sync attempt=1/3 status=failed rc=1") == 1
    assert result.stderr.count("CI_RETRY label=uv-sync attempt=2/3 status=failed rc=1") == 1
    assert "CI_RETRY label=uv-sync attempt=3/3 status=ok" in result.stderr
    assert "exhausted" not in result.stderr


def test_persistent_failure_keeps_the_original_exit_code_after_three_attempts(tmp_path: Path) -> None:
    commands, calls, _sleeps = _fake_bin(tmp_path, fail_first=99, rc=7)
    result = _run(tmp_path, commands, "ci_retry_uv uv-sync uv sync --locked", CI_RETRY_BACKOFF="0 0")
    assert result.returncode == 7
    assert len(_lines(calls)) == 3
    assert "CI_RETRY label=uv-sync exhausted attempts=3 rc=7" in result.stderr
    assert NETWORK_FAILURE.strip() in result.stderr


def test_non_network_failure_is_retried_only_the_bounded_number_of_times_and_reported(tmp_path: Path) -> None:
    commands, calls, _sleeps = _fake_bin(tmp_path, fail_first=99, rc=2, message=LOCK_FAILURE)
    result = _run(tmp_path, commands, "ci_retry_uv uv-sync uv sync --locked", CI_RETRY_BACKOFF="0 0")
    assert result.returncode == 2
    assert len(_lines(calls)) == 3
    assert result.stderr.count("status=failed rc=2") == 2
    assert "CI_RETRY label=uv-sync exhausted attempts=3 rc=2" in result.stderr
    assert "needs to be updated" in result.stderr


def test_default_backoff_is_ten_then_thirty_seconds(tmp_path: Path) -> None:
    commands, _calls, sleeps = _fake_bin(tmp_path, fail_first=99)
    result = _run(tmp_path, commands, "ci_retry_uv uv-sync uv sync --locked")
    assert result.returncode == 1
    assert _lines(sleeps) == ["10", "30"]
    assert "next_in=10s" in result.stderr
    assert "next_in=30s" in result.stderr


def test_backoff_override_is_respected(tmp_path: Path) -> None:
    commands, _calls, sleeps = _fake_bin(tmp_path, fail_first=99)
    result = _run(tmp_path, commands, "ci_retry_uv uv-sync uv sync --locked", CI_RETRY_BACKOFF="1 2")
    assert result.returncode == 1
    assert _lines(sleeps) == ["1", "2"]


def test_the_plain_helper_keeps_its_short_default_backoff(tmp_path: Path) -> None:
    commands, _calls, sleeps = _fake_bin(tmp_path, fail_first=99)
    result = _run(tmp_path, commands, "ci_retry plain uv sync")
    assert result.returncode == 1
    assert _lines(sleeps) == ["2", "5"]


def test_the_wrapped_command_runs_in_the_callers_directory_and_subshell(tmp_path: Path) -> None:
    commands, calls, _sleeps = _fake_bin(tmp_path, fail_first=0)
    sub = tmp_path / "old"
    sub.mkdir()
    result = _run(tmp_path, commands, f"(cd '{sub}' && ci_retry_uv uv-sync-old uv sync --locked)")
    assert result.returncode == 0, result.stderr
    assert _lines(calls) == ["sync --locked"]
