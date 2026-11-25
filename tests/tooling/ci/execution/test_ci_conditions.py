"""Contract tests for the best-effort CI condition readings."""

from __future__ import annotations

import os
import platform
import pytest
import shlex
import subprocess
from pathlib import Path

import yaml

from tests.support.ci.ci_workflow_policy import TAIL_EXEMPT_JOBS, carries_reader, missing_tail_jobs, runner_counts, runner_jobs, runner_jobs_in

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "ci" / "ci-conditions.sh"
WORKFLOWS = ROOT / ".forgejo" / "workflows"

# The runner distribution of the INSTRUMENTED set -- not of the repository.
# The two sets are named separately on purpose: this value was correct when it
# was written and has never once been compared (the old contract died on a
# `steps[-3:]` unpack before reaching it), so "never executed" and "wrong"
# looked identical here. Changing it to describe the full job set (18+3) would
# turn a correct value into a wrong one while the test stayed green.
# Independent GUI replaces the former instrumented locked GUI job. Tauri
# adds one Ubuntu Rust job and one native macOS arm64 release-build job.
# 2026-10-04 CI speedup: +checks and +pytest-shard (ubuntu), hosted-agent-e2e
# moved macos-host -> ubuntu, desktop build-macos moved macos-arm64 (a label no
# runner ever carried) -> macos-host.
# 2026-10-09 PR hygiene: +pr-hygiene (ubuntu, pull_request only) carries the
# tail because it does real git-tree work and its red must be diagnosable.
# GUI retirement removed the two Ubuntu desktop-check jobs with app/src/gui.
# 2026-10-30 dev sync: six gate-python.yml jobs now carry dev's release/**,pool
# expression (`runs-on: ${{ startsWith(github.base_ref, 'release/') &&
# 'release-ci' || 'ubuntu-latest' }}`), and the two publish jobs pin
# `[release-ci]`, so the frozen distribution is measured, not re-guessed.
# 2026-11-03 ci-light move: skill-declaration's declaration runs on the
# ci-light host runner, and pr-hygiene's non-release fallback became ci-light
# (same expression shape with 'ci-light' as the fallback arm).
# 2026-11-06 desktop-release.yml retired (its app/ tooling left in d9b953805):
# -1 `[release-ci]` (publish-reviewed-legacy) and -1 macos-host (build-macos).
RELEASE_CI_ROUTE = (
    "${{ startsWith(github.base_ref, 'release/') && 'release-ci' || 'ubuntu-latest' }}"
)
PR_HYGIENE_ROUTE = (
    "${{ startsWith(github.base_ref, 'release/') && 'release-ci' || 'ci-light' }}"
)
EXPECTED_RUNNERS = {
    "ubuntu-latest": 8,
    # native-conformance.yml adds six instrumented jobs: four on ['release-ci']
    # (plan, reference-abi, native-linux-amd64, free-threaded-3-15t) and one
    # each on ['release-linux-arm64'] / ['release-macos-arm64']; P8 removed the
    # pykka job. The KP-027 candidate pipeline adds eight: four ['release-ci'],
    # one ['ubuntu-latest'], two ['release-linux-arm64'] and one
    # ['release-macos-arm64'].
    "['ubuntu-latest']": 1,
    "['release-ci']": 9,
    "['release-linux-arm64']": 3,
    "['release-macos-arm64']": 2,
    "macos-host": 3,
    "ci-light": 1,
    RELEASE_CI_ROUTE: 5,
    PR_HYGIENE_ROUTE: 1,
}


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


def _run_unreachable_sha_bootstrap(workflow: str, tmp_path: Path) -> None:
    document = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    job_name, job = next(iter(document["jobs"].items()))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text("#!/bin/sh\nexit 86\n", encoding="utf-8")
    fake_git.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_JOB": job_name,
        "GITHUB_RUN_ID": "unreachable-run",
        "GITHUB_SHA": "f" * 40,
        "CONDITIONS_SHA": "f" * 40,
        "GITHUB_TOKEN": "test-only-token",
    }
    result = subprocess.run(
        ["bash", "-c", job["steps"][0]["run"]],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )
    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    fields = _fields(lines[0])
    assert fields["phase"] == "start"
    assert fields["error"] == "script-unavailable"
    assert fields["reason"] == "git-init-failed"
    assert (tmp_path / f"ci-conditions-{job_name}.txt").read_text(
        encoding="utf-8"
    ).splitlines() == lines


def test_nightly_bootstrap_unreachable_sha_is_fail_soft(tmp_path: Path) -> None:
    _run_unreachable_sha_bootstrap("release-nightly-promote.yml", tmp_path)


def test_skill_declaration_bootstrap_unreachable_sha_is_fail_soft(tmp_path: Path) -> None:
    _run_unreachable_sha_bootstrap("gate-pr-declarations.yml", tmp_path)


# --- the instrumented set vs the repository's job set: TWO sets -------------- #
#
# This test used to hold both under one name.  `_workflow_jobs()` returned every
# job with a `runs-on` (21 at that time) while `assert len(jobs) == 12` was written on
# a day when that same call returned the twelve instrumented jobs -- so 12 came
# to mean two different cardinalities, one of them now false, and the loop died
# on an unpack error before either count was ever compared.
#
# The repair is not "12 -> 21": that keeps both sets under one name and merely
# re-freezes the same enumeration.  The two sets are derived separately and
# named:
#
#   all runner jobs   `runner_jobs()`
#   instrumented set  `runner_jobs()` - `TAIL_EXEMPT_JOBS`
#
# and the exemption table is FAIL-CLOSED: a job added tomorrow must carry the
# tail unless someone writes it into the table with a reason.  That is the
# guard against the disease this ticket diagnoses -- an enumeration written
# into a contract freezes on the day it is written while its object keeps
# growing, and "not in the list" then reads as "not required".


def test_all_tail_jobs_put_shared_reader_and_artifact_tail_in_place() -> None:
    """Every job that must carry the tail carries it, verbatim; every runner job
    is either instrumented or written into the fail-closed exemption table."""

    all_jobs = runner_jobs()
    exempt = TAIL_EXEMPT_JOBS

    for key, reason in exempt.items():
        assert reason.strip(), f"{key}: an exemption must carry its reason"
        assert key in {ref.key for ref in all_jobs}, (
            f"{key}: exempted as a runner job, but no such job exists -- a stale "
            "exemption is a job that silently stopped being checked"
        )
        assert key not in {
            ref.key for ref in all_jobs if carries_reader(ref.job)
        }, f"{key}: carries the tail and must not be exempted"

    required, complaints = missing_tail_jobs()
    assert complaints == [], "\n".join(complaints)
    assert len(required) == 33, (
        f"the instrumented set is {len(required)} jobs; expected 33 = dev's 24-job "
        "2026-11-14 set plus native-conformance.yml's plan and five suite jobs "
        "(reference-abi, native-linux-amd64, native-linux-arm64, "
        "native-darwin-arm64, free-threaded-3-15t; P8 removed the pykka job) "
        "plus mock-model-harness-e2e.yml's WIP plan job (2026-11-15) "
        "plus gate-native-shell.yml's launcher job (2026-11-16) "
        "plus gate-gui.yml's gui-acceptance job (2026-11-21). "
        "shell-macos-candidate.yml's package job (2026-11-16) and "
        "shell-macos-signed.yml's package job (2026-11-19) left with both "
        "workflows (owner 2026-11-20: packaging is not done in CI). "
        "Dev's 24 = the 16-job 2026-11-06 set plus the "
        "KP-027 candidate pipeline's eight build-only jobs (pin, three native "
        "builds, two wheel assemblies, the arm64 smoke install and the darwin/arm64 "
        "native build); those eight carry the shared tail rather than being exempted. "
        "If a job was added, give it the tail or write it into TAIL_EXEMPT_JOBS with "
        "a reason"
    )
    assert runner_counts(required) == EXPECTED_RUNNERS, (
        f"instrumented runner distribution {runner_counts(required)} != "
        f"{EXPECTED_RUNNERS}; EXPECTED_RUNNERS describes the INSTRUMENTED set only"
    )

    # The other half of the split: the full set is checked as a set, so the two
    # cardinalities can never be folded back under one name.
    assert {ref.key for ref in required} | set(exempt) == {
        ref.key for ref in all_jobs
    }, "every runner job must be instrumented or explicitly exempted"


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


# --- controls for the two-set split (⛔ "会红"不是及格线,"红在预测的位置"才是) ---


def test_a_tail_job_missing_its_tail_is_named() -> None:
    """Negative control on the REAL file: drop one tail step, get a NAMED failure.

    The complaint names the job and the missing piece, so a red here points at
    the culprit instead of at the first `steps[-3:]` unpack -- which is what
    used to happen and told the reader nothing about which jobs were involved.
    """

    document = yaml.safe_load((WORKFLOWS / "gate-python.yml").read_text(encoding="utf-8"))
    del document["jobs"]["unit"]["steps"][-1]
    refs = runner_jobs_in(
        yaml.safe_dump(document, sort_keys=False), WORKFLOWS / "gate-python.yml"
    )
    required, complaints = missing_tail_jobs(refs)
    assert len(required) > 0
    assert any(
        "gate-python.yml::unit" in complaint and "report step" in complaint
        for complaint in complaints
    ), complaints


def test_a_new_job_without_a_tail_is_required_by_default() -> None:
    """Fail-closed: a job nobody has seen before must be REQUIRED, not skipped.

    This is the property the old enumeration lacked: a new job landed silently
    outside the check because the contract was a list, and "not in the list"
    read as "not required".
    """

    document = {
        "jobs": {
            "brand-new": {"runs-on": "ubuntu-latest", "steps": [{"run": "echo hi"}]}
        }
    }
    refs = runner_jobs_in(yaml.safe_dump(document))
    required, complaints = missing_tail_jobs(refs)
    assert [ref.key for ref in required] == ["<synthetic>.yml::brand-new"]
    assert complaints != [], "a tail-less new job must be a complaint, not a skip"


def test_an_exempted_job_stops_being_required_only_by_written_reason() -> None:
    """The other direction of fail-closed: the exemption is deliberate.

    Written into the table with a reason, the same job stops being required; the
    reason is asserted separately because "deliberate" and "forgotten" look
    identical in the diff otherwise.
    """

    document = {
        "jobs": {
            "declared-out": {
                "runs-on": "ubuntu-latest",
                "steps": [{"run": "echo hi"}],
            }
        }
    }
    refs = runner_jobs_in(yaml.safe_dump(document))
    exempt_key = "<synthetic>.yml::declared-out"
    TAIL_EXEMPT_JOBS[exempt_key] = "test-only: class: tag mover, deferred"
    try:
        required, complaints = missing_tail_jobs(refs)
        assert required == []
        assert complaints == []
    finally:
        del TAIL_EXEMPT_JOBS[exempt_key]


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
