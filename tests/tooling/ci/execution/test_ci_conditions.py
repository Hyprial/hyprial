"""Contract tests for the best-effort CI condition readings."""

from __future__ import annotations

import os
import platform
import pytest
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "ci" / "ci-conditions.sh"


def _run(
    *, phase: str = "start", env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    run_env.update({"GITHUB_JOB": "condition-contract", "GITHUB_RUN_ID": "run-42"})
    if env:
        run_env.update(env)
    return subprocess.run(
        ["bash", str(SCRIPT), phase],
        check=False,
        capture_output=True,
        env=run_env,
        text=True,
    )


def _fields(line: str) -> dict[str, str]:
    tokens = shlex.split(line)
    assert tokens[0] == "CI_CONDITIONS"
    return dict(token.split("=", 1) for token in tokens[1:])


def test_reader_emits_exactly_one_platform_scoped_line(tmp_path: Path) -> None:
    env = {"RUNNER_TEMP": str(tmp_path)}
    result = _run(env=env)
    end_result = _run(phase="end", env=env)

    assert result.returncode == 0
    assert end_result.returncode == 0
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    fields = _fields(lines[0])
    assert fields["phase"] == "start"
    assert fields["job"] == "condition-contract"
    assert fields["run"] == "run-42"
    assert fields["at"].endswith("Z")
    assert fields["loadavg"] != "unavailable"
    assert fields["nproc"].isdigit()

    if platform.system() == "Linux":
        assert fields["scope"] == "vm-container"
        assert fields["instrument"] == "loadavg,psi,meminfo,cgroup"
        assert fields["mem_available_kb"].isdigit()
        assert fields["mem_total_kb"].isdigit()
        assert fields["psi"] in {"available", "unavailable"}
        if fields["psi"] == "available":
            assert fields["psi_cpu_some_avg10"]
            assert fields["psi_cpu_full_avg10"] == "unavailable"
            assert fields["psi_memory_some_avg10"]
            assert fields["psi_memory_full_avg10"]
            assert fields["psi_io_some_avg10"]
            assert fields["psi_io_full_avg10"]
    elif platform.system() == "Darwin":
        assert fields["scope"] == "macos-runner-host"
        assert fields["instrument"] == "vm.loadavg,memory_pressure,hw.memsize"
        assert fields["mem_total_kb"].isdigit()
        assert fields["mem_free_percent"].isdigit()
        assert fields["psi"] == "unavailable"
    else:
        raise AssertionError(f"unsupported test platform: {platform.system()}")

    conditions_file = tmp_path / "ci-conditions-condition-contract.txt"
    assert conditions_file.read_text(encoding="utf-8").splitlines() == [
        result.stdout.rstrip("\n"),
        end_result.stdout.rstrip("\n"),
    ]


def test_linux_proc_instruments_parse_captured_kernel_shapes(tmp_path: Path) -> None:
    """Use real procfs shapes: captured load/mem plus the Linux PSI ABI."""

    proc = tmp_path / "proc"
    pressure = proc / "pressure"
    pressure.mkdir(parents=True)
    (proc / "loadavg").write_text(
        "5.26 5.92 5.47 11/51074 16\n", encoding="utf-8"
    )
    (proc / "meminfo").write_text(
        "MemTotal:       16422316 kB\nMemAvailable:    8004600 kB\n",
        encoding="utf-8",
    )
    (pressure / "cpu").write_text(
        "some avg10=1.25 avg60=2.50 avg300=3.75 total=1234\n",
        encoding="utf-8",
    )
    for resource, base in (("memory", 4), ("io", 6)):
        (pressure / resource).write_text(
            f"some avg10={base}.25 avg60=0.00 avg300=0.00 total=1\n"
            f"full avg10={base + 1}.50 avg60=0.00 avg300=0.00 total=2\n",
            encoding="utf-8",
        )
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cgroup.controllers").write_text("cpu memory io\n", encoding="utf-8")
    (cgroup / "cpu.max").write_text("2800000 100000\n", encoding="utf-8")
    (cgroup / "cpu.stat").write_text(
        "usage_usec 123\nnr_throttled 17\nthrottled_usec 456\n",
        encoding="utf-8",
    )

    result = _run(
        env={
            "CI_CONDITIONS_OS_NAME": "Linux",
            "CI_CONDITIONS_PROC_ROOT": str(proc),
            "CI_CONDITIONS_CGROUP_ROOT": str(cgroup),
            "RUNNER_TEMP": str(tmp_path),
        }
    )

    assert result.returncode == 0
    fields = _fields(result.stdout)
    assert fields["scope"] == "vm-container"
    assert fields["loadavg"] == "5.26 5.92 5.47 11/51074 16"
    assert fields["mem_available_kb"] == "8004600"
    assert fields["mem_total_kb"] == "16422316"
    assert fields["psi"] == "available"
    assert fields["psi_cpu_some_avg10"] == "1.25"
    assert fields["psi_cpu_full_avg10"] == "unavailable"
    assert fields["psi_memory_some_avg10"] == "4.25"
    assert fields["psi_memory_full_avg10"] == "5.50"
    assert fields["psi_io_some_avg10"] == "6.25"
    assert fields["psi_io_full_avg10"] == "7.50"
    assert fields["cgroup"] == "v2"
    assert fields["cpu.max"] == "2800000 100000"
    assert fields["nr_throttled"] == "17"
    assert fields["throttled_usec"] == "456"


def test_cpu_full_is_pinned_even_when_the_kernel_emits_it(tmp_path: Path) -> None:
    """Linux gained a CPU `full` line; the reading must not follow the kernel.

    Captured here rather than inferred: /proc/pressure/cpu on a 6.14 host answers
    `full avg10=0.00 ... total=0`. A reader that passes it through makes the
    reading host-dependent -- the same commit then reports a number on one runner
    and `unavailable` on another -- so the field stays pinned and the kernel's
    extra line is ignored.
    """

    proc = tmp_path / "proc"
    pressure = proc / "pressure"
    pressure.mkdir(parents=True)
    (proc / "loadavg").write_text("0.10 0.20 0.30 1/100 1\n", encoding="utf-8")
    (proc / "meminfo").write_text(
        "MemTotal:       1024 kB\nMemAvailable:    512 kB\n", encoding="utf-8"
    )
    (pressure / "cpu").write_text(
        "some avg10=0.03 avg60=0.09 avg300=0.07 total=785679036\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n",
        encoding="utf-8",
    )
    for resource in ("memory", "io"):
        (pressure / resource).write_text(
            "some avg10=1.00 avg60=0.00 avg300=0.00 total=1\n"
            "full avg10=2.00 avg60=0.00 avg300=0.00 total=2\n",
            encoding="utf-8",
        )
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cgroup.controllers").write_text("cpu memory io\n", encoding="utf-8")
    (cgroup / "cpu.max").write_text("100000 100000\n", encoding="utf-8")
    (cgroup / "cpu.stat").write_text(
        "usage_usec 1\nnr_throttled 0\nthrottled_usec 0\n", encoding="utf-8"
    )

    result = _run(
        env={
            "CI_CONDITIONS_OS_NAME": "Linux",
            "CI_CONDITIONS_PROC_ROOT": str(proc),
            "CI_CONDITIONS_CGROUP_ROOT": str(cgroup),
            "RUNNER_TEMP": str(tmp_path),
        }
    )

    assert result.returncode == 0
    fields = _fields(result.stdout)
    assert fields["psi"] == "available"
    assert fields["psi_cpu_some_avg10"] == "0.03"
    assert fields["psi_cpu_full_avg10"] == "unavailable"
    assert fields["psi_memory_full_avg10"] == "2.00"
    assert fields["psi_io_full_avg10"] == "2.00"


def test_linux_cgroup_fallback_is_explicit(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "loadavg").write_text("1.00 2.00 3.00 1/2 3\n", encoding="utf-8")
    (proc / "meminfo").write_text(
        "MemTotal: 2000 kB\nMemAvailable: 1000 kB\n", encoding="utf-8"
    )
    missing = tmp_path / "missing-cgroup"
    env = {
        "CI_CONDITIONS_OS_NAME": "Linux",
        "CI_CONDITIONS_PROC_ROOT": str(proc),
        "CI_CONDITIONS_CGROUP_ROOT": str(missing),
        "RUNNER_TEMP": str(tmp_path),
    }
    assert _fields(_run(env=env).stdout)["cgroup"] == "unavailable"

    v1 = tmp_path / "cgroup-v1"
    (v1 / "cpu").mkdir(parents=True)
    (v1 / "cpu" / "cpu.cfs_quota_us").write_text("-1\n", encoding="utf-8")
    env["CI_CONDITIONS_CGROUP_ROOT"] = str(v1)
    assert _fields(_run(env=env).stdout)["cgroup"] == "v1"


def test_linux_missing_psi_is_explicit(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "loadavg").write_text("1.00 2.00 3.00 1/2 3\n", encoding="utf-8")
    (proc / "meminfo").write_text(
        "MemTotal: 2000 kB\nMemAvailable: 1000 kB\n", encoding="utf-8"
    )
    fields = _fields(
        _run(
            env={
                "CI_CONDITIONS_OS_NAME": "Linux",
                "CI_CONDITIONS_PROC_ROOT": str(proc),
                "CI_CONDITIONS_CGROUP_ROOT": str(tmp_path / "missing-cgroup"),
                "RUNNER_TEMP": str(tmp_path),
            }
        ).stdout
    )
    assert fields["psi"] == "unavailable"


def test_artifact_upload_failure_report_is_fail_soft_and_not_persisted(
    tmp_path: Path,
) -> None:
    summary = tmp_path / "summary.md"
    result = _run(
        phase="artifact",
        env={
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_STEP_SUMMARY": str(summary),
            "ACTIONS_RUNTIME_URL": "http://runner.example:3000/api/actions",
        },
    )

    assert result.returncode == 0
    assert "artifact upload failed/timeout: condition-contract" in summary.read_text()
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    fields = _fields(lines[0])
    assert fields["phase"] == "artifact"
    assert "artifact-upload-failed" in fields["error"]
    assert fields["runtime_origin"] == "http://runner.example:3000"
    assert not (tmp_path / "ci-conditions-condition-contract.txt").exists()


def test_unavailable_load_reader_reports_error_without_failing() -> None:
    missing = "/definitely-missing/ci-conditions-load-reader"
    if platform.system() == "Linux":
        env = {"CI_CONDITIONS_LOADAVG_PATH": missing}
    elif platform.system() == "Darwin":
        env = {"CI_CONDITIONS_SYSCTL_BIN": missing}
    else:
        raise AssertionError(f"unsupported test platform: {platform.system()}")

    result = _run(phase="end", env=env)

    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    fields = _fields(lines[0])
    assert fields["phase"] == "end"
    assert fields["loadavg"] == "unavailable"
    assert "loadavg" in fields["error"]




def test_the_missing_value_semantics_are_written_into_the_protocol_not_only_here() -> None:
    """The mover's push time is the audit's ordering key; a missing value must
    stop the ordering, not be skipped.

    ⚠️ The failure this prevents is a *silent* one: the tail is ``if: always()``,
    so when the mover did not run the file simply has no push-time record -- and
    "no record" is the easiest thing in the world to read as "that push did not
    happen" (or as "the audit ran before the upload").  Those are different
    states with different repairs, so the audit must refuse to order without the
    record.  That sentence is a property of the PROTOCOL, so it is pinned in the
    protocol text (``scripts/ci/ci-conditions.sh``), not only in a test comment.
    """

    text = SCRIPT.read_text(encoding="utf-8")
    assert "MISSING PUSH TIME" in text, (
        "scripts/ci/ci-conditions.sh must state the missing-value semantics of the "
        "mover's push-time record: the audit refuses to order without it"
    )
    assert "refuse" in text.lower(), text


@pytest.mark.parametrize(
    ("runtime_url", "expected"),
    [
        ("https://user:tok3n@forgejo.example:3000/x", "https://forgejo.example:3000"),
        ("https://tok@forgejo.example:3000", "https://forgejo.example:3000"),
        ("https://user:p?x@forgejo.example:3000/", "https://forgejo.example:3000"),
        ("https://user:p#x@forgejo.example:3000/", "https://forgejo.example:3000"),
    ],
)
def test_artifact_runtime_origin_strips_userinfo(
    tmp_path: Path, runtime_url: str, expected: str
) -> None:
    result = _run(
        phase="artifact",
        env={"RUNNER_TEMP": str(tmp_path), "ACTIONS_RUNTIME_URL": runtime_url},
    )
    fields = _fields(result.stdout.splitlines()[0])
    assert fields["runtime_origin"] == expected
    assert "tok" not in result.stdout
    assert "user:p" not in result.stdout
