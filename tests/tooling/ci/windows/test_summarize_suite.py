"""Linux tests for the tier 2 (full suite) summary and its fail-closed exit code.

Decision (2026-11-27, tier 2 full suite).  The suite-mode summary test moved here
unchanged from test_run_waves.py, which was close to the 1000-line limit.  The
fail-closed tests assert the return value of summarize.main, not only the printed
lines: a failing leg without expected failures, a leg whose manifest says passed
while JUnit holds a failure, corrupt JUnit XML, and a missing plan-manifest.json
must each exit 1.  The first test also asserts the "unoffset failures" and "wave
status is 'failed'" lines so that dropping either check is detected even though
the other still fails the run.  GITHUB_STEP_SUMMARY is removed so these tests never
write to a real job summary.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.ci.windows.summarize import main as summarize_main
from scripts.ci.windows.summarize import summarize_suite
from tests.tooling.ci.windows.test_run_waves import write_events, write_junit


def _write_suite_leg(
    leg_dir: Path, *, planned: list[str], status: str, run_status: str,
    cases: list[dict[str, str]], ledger_events: list[dict[str, object]],
) -> None:
    results = leg_dir / "results"
    ledger = leg_dir / "ledger" / "wave-1-run-1"
    results.mkdir(parents=True, exist_ok=True)
    ledger.mkdir(parents=True, exist_ok=True)
    manifest = {
        "tier": "2", "status": status, "offsettable": status == "failed",
        "selfCheck": {"passed": True}, "plannedFiles": planned, "reportedFiles": planned,
        "runs": [{"wave": 1, "run": 1, "status": run_status, "duration": 2.5}], "leaks": [],
    }
    (results / "run-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (results / "waves.log").write_text(f"WINDOWS_WAVES_DONE {json.dumps(manifest)}\n", encoding="utf-8")
    write_junit(results / "shard-0-wave-1-run-1.xml", cases)
    write_events(ledger / "gw0.jsonl", ledger_events)


def test_summarize_suite_green_missing_hard_red_and_expected_failures(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan-manifest.json"
    plan_path.write_text(
        json.dumps(
            {
                "version": 1, "selection": "full", "suiteRequired": True,
                "requiredLegs": ["shard-0", "shard-1"], "posixOnlyIgnored": {"files": 2, "tests": 5},
                "shards": [["tests/a.py"], ["tests/b.py"]],
                "waves": [[["tests/a.py"]], [["tests/b.py"]]],
            }
        ),
        encoding="utf-8",
    )
    shard0, shard1 = tmp_path / "shard-0", tmp_path / "shard-1"
    _write_suite_leg(
        shard0, planned=["tests/a.py"], status="passed", run_status="passed",
        cases=[{"classname": "tests.a", "name": "test_ok"}],
        ledger_events=[
            {"event": "collect", "collected": 1, "errors": []},
            {"event": "end", "nodeid": "tests/a.py::test_ok", "phase": "call", "outcome": "passed"},
        ],
    )
    _write_suite_leg(
        shard1, planned=["tests/b.py"], status="passed", run_status="passed",
        cases=[{"classname": "tests.b", "name": "test_ok"}],
        ledger_events=[
            {"event": "collect", "collected": 1, "errors": []},
            {"event": "end", "nodeid": "tests/b.py::test_ok", "phase": "call", "outcome": "passed"},
        ],
    )
    ok_green, green_lines = summarize_suite(
        plan_manifest_path=plan_path, leg_roots={"shard-0": shard0, "shard-1": shard1}
    )
    assert ok_green is True
    assert any("POSIX_ONLY_IGNORED files=2 tests=5" in line for line in green_lines)

    ok_missing, missing_lines = summarize_suite(
        plan_manifest_path=plan_path, leg_roots={"shard-0": shard0}
    )
    assert ok_missing is False
    assert any("missing required leg artifact: shard-1" in line for line in missing_lines)

    fail_case = [{"classname": "tests.a", "name": "test_acl", "outcome": "failed", "message": "unsafe-acl on temp dir"}]
    fail_ledger: list[dict[str, object]] = [
        {"event": "collect", "collected": 1, "errors": []},
        {
            "event": "end", "nodeid": "tests/a.py::test_acl", "phase": "call", "outcome": "failed",
            "excinfo": {"type": "builtins.AssertionError", "message": "unsafe-acl on temp dir"},
        },
    ]
    shard0_fail, shard0_crash = tmp_path / "shard-0-fail", tmp_path / "shard-0-crash"
    _write_suite_leg(shard0_fail, planned=["tests/a.py"], status="failed", run_status="failed", cases=fail_case, ledger_events=fail_ledger)
    _write_suite_leg(shard0_crash, planned=["tests/a.py"], status="native-worker-crash", run_status="native-worker-crash", cases=fail_case, ledger_events=fail_ledger)

    def write_expected(name: str, phase: str) -> Path:
        path = tmp_path / name
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "entries": [{
                        "nodeid": "tests/a.py::test_acl", "kp": "KP-123", "phase": phase,
                        "exception": "builtins.AssertionError", "match": "unsafe-acl",
                        "leg": "windows-x64", "since": "1",
                    }],
                }
            ),
            encoding="utf-8",
        )
        return path

    expected_ok = write_expected("expected-ok.json", "call")
    expected_bad = write_expected("expected-bad.json", "setup")

    ok_offset, offset_lines = summarize_suite(
        plan_manifest_path=plan_path, leg_roots={"shard-0": shard0_fail, "shard-1": shard1},
        expected_failures_path=expected_ok,
    )
    assert ok_offset is True, offset_lines
    assert any("matched=1 stale=0 unexpectedShape=0" in line for line in offset_lines)
    assert any("unsafe-acl=1" in line for line in offset_lines)

    ok_stale, stale_lines = summarize_suite(
        plan_manifest_path=plan_path, leg_roots={"shard-0": shard0, "shard-1": shard1},
        expected_failures_path=expected_ok,
    )
    assert ok_stale is False and any("stale-expected-failure" in line for line in stale_lines)

    ok_shape, shape_lines = summarize_suite(
        plan_manifest_path=plan_path, leg_roots={"shard-0": shard0_fail, "shard-1": shard1},
        expected_failures_path=expected_bad,
    )
    assert ok_shape is False and any("unexpected-failure-shape" in line for line in shape_lines)

    ok_crash, crash_lines = summarize_suite(
        plan_manifest_path=plan_path, leg_roots={"shard-0": shard0_crash, "shard-1": shard1},
        expected_failures_path=expected_ok,
    )
    assert ok_crash is False and any("native-worker-crash" in line for line in crash_lines)

    assert summarize_main(
        [
            "--mode", "suite", "--plan-manifest", str(plan_path),
            "--expected-failures", str(tmp_path / "nonexistent-expected.json"),
            "--input", f"{shard0}=shard-0", "--input", f"{shard1}=shard-1",
        ]
    ) == 0


FAILED_CASE = [{"classname": "tests.a", "name": "test_acl", "outcome": "failed", "message": "unsafe-acl on temp dir"}]
FAILED_LEDGER: list[dict[str, object]] = [
    {"event": "collect", "collected": 1, "errors": []},
    {
        "event": "end", "nodeid": "tests/a.py::test_acl", "phase": "call", "outcome": "failed",
        "excinfo": {"type": "builtins.AssertionError", "message": "unsafe-acl on temp dir"},
    },
]
PASSED_LEDGER: list[dict[str, object]] = [
    {"event": "collect", "collected": 1, "errors": []},
    {"event": "end", "nodeid": "tests/a.py::test_ok", "phase": "call", "outcome": "passed"},
]


def _single_leg_plan(tmp_path: Path) -> Path:
    plan_path = tmp_path / "plan-manifest.json"
    plan_path.write_text(
        json.dumps(
            {
                "version": 1, "selection": "full", "suiteRequired": True, "requiredLegs": ["shard-0"],
                "posixOnlyIgnored": {"files": 0, "tests": 0},
                "shards": [["tests/a.py"]], "waves": [[["tests/a.py"]]],
            }
        ),
        encoding="utf-8",
    )
    return plan_path


def _run_suite_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], plan_path: Path, leg: Path
) -> tuple[int, str]:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    code = summarize_main(["--mode", "suite", "--plan-manifest", str(plan_path), "--input", f"{leg}=shard-0"])
    return code, capsys.readouterr().out


def test_suite_main_fails_leg_failure_without_expected_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    leg = tmp_path / "shard-0"
    _write_suite_leg(
        leg, planned=["tests/a.py"], status="failed", run_status="failed",
        cases=FAILED_CASE, ledger_events=FAILED_LEDGER,
    )

    code, out = _run_suite_main(monkeypatch, capsys, _single_leg_plan(tmp_path), leg)

    assert code == 1
    assert "unoffset failures: 1" in out
    assert "wave status is 'failed'" in out
    assert "Overall status: **failed**" in out


def test_suite_main_fails_when_manifest_passed_but_junit_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    leg = tmp_path / "shard-0"
    _write_suite_leg(
        leg, planned=["tests/a.py"], status="passed", run_status="passed",
        cases=FAILED_CASE, ledger_events=FAILED_LEDGER,
    )

    code, out = _run_suite_main(monkeypatch, capsys, _single_leg_plan(tmp_path), leg)

    assert code == 1
    assert "unoffset failures: 1" in out
    assert "`tests/a.py::test_acl`" in out


def test_suite_main_fails_on_corrupt_junit_xml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    leg = tmp_path / "shard-0"
    _write_suite_leg(
        leg, planned=["tests/a.py"], status="passed", run_status="passed",
        cases=[{"classname": "tests.a", "name": "test_ok"}], ledger_events=PASSED_LEDGER,
    )
    (leg / "results" / "shard-0-wave-1-run-1.xml").write_text("<testsuites><testsuite", encoding="utf-8")

    code, out = _run_suite_main(monkeypatch, capsys, _single_leg_plan(tmp_path), leg)

    assert code == 1
    assert "invalid JUnit XML shard-0-wave-1-run-1.xml" in out


def test_suite_main_fails_when_plan_manifest_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    leg = tmp_path / "shard-0"
    _write_suite_leg(
        leg, planned=["tests/a.py"], status="passed", run_status="passed",
        cases=[{"classname": "tests.a", "name": "test_ok"}], ledger_events=PASSED_LEDGER,
    )

    code, out = _run_suite_main(monkeypatch, capsys, tmp_path / "absent" / "plan-manifest.json", leg)

    assert code == 1
    assert "missing plan-manifest.json" in out
