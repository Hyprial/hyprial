"""CI diagnostics must not turn a failed command into a passing gate."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[4] / 'scripts/ci/ci-step.sh'


@pytest.mark.parametrize("command,code", [("true", 0), ("exit 7", 7), ("false | cat", 1)])
def test_timing_preserves_command_failure(tmp_path: Path, command: str, code: int) -> None:
    summary = tmp_path / "summary.md"
    result = subprocess.run(
        ["bash", "-c", 'source "$1" sample; ' + command, "ci-test", str(SCRIPT)],
        env={**os.environ, "GITHUB_STEP_SUMMARY": str(summary)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == code
    assert "CI_TIMING stage=sample seconds=" in result.stdout
    assert f"exit={code}" in result.stdout
    assert f"exit {code}" in summary.read_text()


def _run_step(tmp_path: Path, command: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", 'source "$1" sample; ' + command, "ci-test", str(SCRIPT)],
        env={**os.environ, "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"), **env},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("command,code", [("sleep 1", 0), ("sleep 1; exit 7", 7)])
def test_ram_disk_peak_is_reported_without_changing_the_exit_code(tmp_path: Path, command: str, code: int) -> None:
    disk = tmp_path / "ram"
    disk.mkdir()
    result = _run_step(tmp_path, command, CI_TMPFS_PATH=str(disk), CI_TMPFS_INTERVAL="0.2")
    assert result.returncode == code
    line = next(x for x in result.stdout.splitlines() if x.startswith("CI_TMPFS "))
    assert f"stage=sample path={disk} peak_kb=" in line
    fields = dict(part.split("=", 1) for part in line.split()[1:])
    assert int(fields["size_kb"]) > 0 and int(fields["samples"]) >= 2


def test_no_ram_disk_means_no_sampler_and_no_report(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "CI_TMPFS_PATH"}
    result = subprocess.run(
        ["bash", "-c", 'source "$1" sample; true', "ci-test", str(SCRIPT)],
        env={**env, "CI_TMPFS_PATH": "", "GITHUB_STEP_SUMMARY": str(tmp_path / "s.md")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    if not Path("/ci-tmpfs").is_mount():
        assert "CI_TMPFS " not in result.stdout


def test_an_unwritable_tmpdir_does_not_fail_the_measured_step(tmp_path: Path) -> None:
    disk = tmp_path / "ram"
    disk.mkdir()
    result = _run_step(tmp_path, "true", CI_TMPFS_PATH=str(disk), TMPDIR=str(tmp_path / "missing"))
    assert result.returncode == 0
    assert "CI_TIMING stage=sample" in result.stdout
    assert "CI_TMPFS " not in result.stdout


def _memory_lines(result: subprocess.CompletedProcess[str]) -> dict[str, dict[str, str]]:
    lines = [x for x in result.stdout.splitlines() if x.startswith("CI_MEMORY ")]
    return {
        fields["phase"]: fields
        for fields in (dict(part.split("=", 1) for part in line.split()[1:]) for line in lines)
    }


def _cgroup_v2(root: Path, **files: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (root / name.replace("_", ".", 1)).write_text(text)
    return root


def test_memory_limit_usage_and_oom_kills_are_reported_at_start_and_end(tmp_path: Path) -> None:
    cgroup = _cgroup_v2(
        tmp_path / "cg",
        memory_max="4294967296\n",
        memory_current="1048576\n",
        memory_peak="2097152\n",
        memory_events="low 0\nhigh 0\nmax 0\noom 1\noom_kill 3\n",
    )
    bump = f"printf 'low 0\\noom_kill 5\\n' > {cgroup}/memory.events"
    result = _run_step(tmp_path, bump, CI_CGROUP_ROOT=str(cgroup))
    assert result.returncode == 0
    memory = _memory_lines(result)
    assert memory["start"] == {
        "stage": "sample",
        "phase": "start",
        "max": "4294967296",
        "current": "1048576",
        "peak": "2097152",
        "oom_kill": "3",
    }
    assert memory["end"]["oom_kill"] == "5"
    assert memory["end"]["oom_kill_delta"] == "2"


def test_cgroup_v1_files_are_used_when_v2_files_are_absent(tmp_path: Path) -> None:
    cgroup = tmp_path / "cg"
    (cgroup / "memory").mkdir(parents=True)
    (cgroup / "memory/memory.limit_in_bytes").write_text("4294967296\n")
    (cgroup / "memory/memory.usage_in_bytes").write_text("512\n")
    (cgroup / "memory/memory.max_usage_in_bytes").write_text("1024\n")
    (cgroup / "memory/memory.oom_control").write_text("oom_kill_disable 0\nunder_oom 0\noom_kill 2\n")
    result = _run_step(tmp_path, "true", CI_CGROUP_ROOT=str(cgroup))
    assert _memory_lines(result)["start"] == {
        "stage": "sample",
        "phase": "start",
        "max": "4294967296",
        "current": "512",
        "peak": "1024",
        "oom_kill": "2",
    }


def test_missing_cgroup_files_print_nothing_and_do_not_fail_the_step(tmp_path: Path) -> None:
    result = _run_step(tmp_path, "exit 7", CI_CGROUP_ROOT=str(tmp_path / "absent"))
    assert result.returncode == 7
    assert "CI_MEMORY " not in result.stdout
    assert "CI_TIMING stage=sample" in result.stdout


@pytest.mark.parametrize(
    "events",
    ["oom_kill notanumber\n", "oom_kill\n", "oom_kill -4\n", "oom_kill 1 extra\n", "\x00\x01garbage", ""],
)
def test_malformed_cgroup_files_report_dashes_and_do_not_fail_the_step(tmp_path: Path, events: str) -> None:
    cgroup = _cgroup_v2(
        tmp_path / "cg",
        memory_max="not-a-limit\n",
        memory_current="12\n",
        memory_events=events,
    )
    result = _run_step(tmp_path, "true", CI_CGROUP_ROOT=str(cgroup))
    assert result.returncode == 0
    memory = _memory_lines(result)
    assert memory["start"]["max"] == "-"
    assert memory["start"]["current"] == "12"
    assert memory["start"]["oom_kill"] == "-"
    assert "oom_kill_delta" not in memory["end"]


def test_unbounded_memory_max_is_kept_verbatim(tmp_path: Path) -> None:
    cgroup = _cgroup_v2(tmp_path / "cg", memory_max="max\n")
    result = _run_step(tmp_path, "true", CI_CGROUP_ROOT=str(cgroup))
    assert _memory_lines(result)["start"]["max"] == "max"
