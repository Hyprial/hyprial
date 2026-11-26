"""Linux tests for Windows wave accounting, rerun reconciliation, and summaries.

Decision (2026-11-26, tier 1 smoke).  Exercises the ten-state ledger classifier
(the ledger contract between the test harness and the runner), xunit2 classname resolution, duplicate-result
rejection, unreported-file error synthesis, rerun --deselect flow, and
fail-closed summarize.py validation using synthetic ledgers and JUnit XMLs on
Linux.
"""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from scripts.ci.windows import run_waves as run_waves_module
from scripts.ci.windows import wave_state as wave_state_module
from scripts.ci.windows.run_waves import (
    CONTRACT_TEST_FILE,
    HARD_STATES,
    build_pytest_command,
    classify_run,
    is_offsettable,
    read_junit,
    run_waves,
    synthesize_unreported,
    validate_durations,
)
from scripts.ci.windows.summarize import (
    inspect_contract_skips,
    junit_failure_nodeids,
    main as summarize_main,
    summarize_leg,
    summarize_suite,
    validate_smoke,
    validate_waves,
)


def write_events(path: Path, events: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


def write_complete_ledger(
    ledger: Path,
    nodeids: list[str],
    extra: list[dict[str, object]] | None = None,
) -> None:
    events: list[dict[str, object]] = [
        {"event": "collect", "collected": len(nodeids), "errors": []},
        *(
            {"event": "end", "nodeid": nodeid, "outcome": "passed"}
            for nodeid in nodeids
        ),
        *(extra or []),
    ]
    write_events(ledger / "gw0.jsonl", events)


def write_junit(
    path: Path,
    cases: list[dict[str, str]],
    *,
    suite_error: str | None = None,
) -> None:
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", name="pytest")
    if suite_error is not None:
        ET.SubElement(suite, "error", message=suite_error)
    for item in cases:
        attrs = {
            "classname": item["classname"],
            "name": item["name"],
        }
        if "file" in item:
            attrs["file"] = item["file"]
        case = ET.SubElement(suite, "testcase", attrib=attrs)
        outcome = item.get("outcome", "passed")
        if outcome == "failed":
            ET.SubElement(case, "failure", message=item.get("message", "failure"))
        elif outcome == "error":
            ET.SubElement(case, "error", message=item.get("message", "error"))
        elif outcome == "skipped":
            ET.SubElement(case, "skipped", message=item.get("message", "skipped"))
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def test_watchdog_requires_non_empty_hang_file(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    junit = tmp_path / "result.xml"
    write_junit(junit, [{"classname": "tests.a", "name": "test_ok"}])
    write_complete_ledger(ledger, ["tests/a.py::test_ok"])
    (ledger / "hang-gw0.txt").write_text("", encoding="utf-8")

    outcome = classify_run(
        returncode=0,
        junit_paths=[junit],
        ledger_dir=ledger,
        planned_files=["tests/a.py"],
    )

    assert outcome.status == "passed"
    assert outcome.reported_files == frozenset({"tests/a.py"})

    (ledger / "hang-gw0.txt").write_text("Timeout (0:05:00)!\n", encoding="utf-8")
    outcome = classify_run(
        returncode=1,
        junit_paths=[junit],
        ledger_dir=ledger,
        planned_files=["tests/a.py"],
    )
    assert outcome.status == "test-watchdog-timeout"
    assert outcome.offsettable is False


def test_leak_event_is_non_fatal_until_leak_timeout_crash(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    junit = tmp_path / "result.xml"
    write_junit(junit, [{"classname": "tests.a", "name": "test_ok"}])
    write_complete_ledger(
        ledger,
        ["tests/a.py::test_ok"],
        extra=[{"event": "leak", "worker": "gw0", "threads": ["thread-a"]}],
    )

    clean = classify_run(
        returncode=0,
        junit_paths=[junit],
        ledger_dir=ledger,
        planned_files=["tests/a.py"],
    )
    assert clean.status == "passed"
    assert clean.leaks == ({"worker": "gw0", "threads": ["thread-a"]},)

    write_complete_ledger(
        ledger,
        ["tests/a.py::test_ok"],
        extra=[
            {"event": "leak", "worker": "gw0", "threads": ["thread-a"]},
            {"event": "crash", "worker": "gw0", "reason": "leak-timeout", "exit_code": 97},
        ],
    )
    crashed = classify_run(
        returncode=97,
        junit_paths=[junit],
        ledger_dir=ledger,
        planned_files=["tests/a.py"],
    )
    assert crashed.status == "worker-crash"
    assert crashed.offsettable is False


def test_native_crash_interceptor_collection_and_partial_states(tmp_path: Path) -> None:
    junit = tmp_path / "result.xml"
    write_junit(junit, [{"classname": "tests.a", "name": "test_ok"}])

    native_ledger = tmp_path / "native"
    native_ledger.mkdir()
    native = classify_run(
        returncode=3221225477,
        junit_paths=[junit],
        ledger_dir=native_ledger,
        planned_files=["tests/a.py"],
    )
    assert native.status == "native-worker-crash"

    hit_ledger = tmp_path / "hits"
    hit_ledger.mkdir()
    write_events(
        hit_ledger / "interceptor-hits.jsonl",
        [{"pid": 123, "sig": 0, "nodeid": "tests/a.py::test_ok"}],
    )
    terminated = classify_run(
        returncode=0,
        junit_paths=[junit],
        ledger_dir=hit_ledger,
        planned_files=["tests/a.py"],
    )
    assert terminated.status == "worker-self-terminated"

    collect_ledger = tmp_path / "collect"
    collect_ledger.mkdir()
    write_events(
        collect_ledger / "gw0.jsonl",
        [{"event": "collect", "errors": [{"nodeid": "tests/a.py", "summary": "bad import"}]}],
    )
    collected = classify_run(
        returncode=1,
        junit_paths=[junit],
        ledger_dir=collect_ledger,
        planned_files=["tests/a.py"],
    )
    assert collected.status == "collection-error"

    partial_ledger = tmp_path / "partial"
    partial_ledger.mkdir()
    write_events(
        partial_ledger / "gw0.jsonl",
        [
            {"event": "start", "nodeid": "tests/a.py::test_ok"},
            {"event": "end", "nodeid": "tests/a.py::test_ok", "outcome": "passed"},
            {"event": "start", "nodeid": "tests/a.py::test_hung"},
        ],
    )
    partial = classify_run(
        returncode=0,
        junit_paths=[junit],
        ledger_dir=partial_ledger,
        planned_files=["tests/a.py"],
    )
    assert partial.status == "unreported-files"
    assert partial.partial_files == frozenset({"tests/a.py"})


def test_offsettable_only_allows_failed_and_duration_order() -> None:
    assert is_offsettable("failed") is True
    assert is_offsettable("passed") is False
    for state in HARD_STATES:
        assert is_offsettable(state) is False

    validate_durations(300, 600, 20, 25, 30, 60)
    with pytest.raises(ValueError, match="duration order"):
        validate_durations(600, 300, 20, 25, 30, 60)
    with pytest.raises(ValueError, match="duration order"):
        validate_durations(300, 600, 20, 25, 50, 60)


def test_xunit2_duplicate_detection_and_unreported_synthesis(tmp_path: Path) -> None:
    first = tmp_path / "run1.xml"
    second = tmp_path / "run2.xml"
    write_junit(first, [{"classname": "tests.pkg.test_mod.TestGroup", "name": "test_one"}])
    write_junit(second, [{"classname": "tests.pkg.test_mod.TestGroup", "name": "test_one"}])

    parsed = read_junit([first], ["tests/pkg/test_mod.py"])
    assert parsed.files == frozenset({"tests/pkg/test_mod.py"})
    assert parsed.nodeids == frozenset({"tests/pkg/test_mod.py::TestGroup::test_one"})

    with pytest.raises(ValueError, match="duplicate-result"):
        read_junit([first, second], ["tests/pkg/test_mod.py"])

    unreported = tmp_path / "unreported.xml"
    synthesize_unreported(unreported, ["tests/missing.py"])
    cases = list(ET.parse(unreported).getroot().iter("testcase"))
    assert len(cases) == 1
    assert cases[0].attrib["classname"] == "hyprial.ci.windows_watchdog"
    assert cases[0].attrib["name"] == "unreported::tests/missing.py"
    assert "file" not in cases[0].attrib
    assert cases[0].find("error") is not None


def test_build_pytest_command_keeps_argsfile_paths_and_deselect_on_argv(tmp_path: Path) -> None:
    command = build_pytest_command(
        workers=2,
        junit=tmp_path / "out.xml",
        basetemp=tmp_path / "bt",
        argsfile=tmp_path / "wave.args",
        deselect=["tests/a.py::test_done"],
    )
    assert "-o" in command and "junit_family=xunit2" in command
    assert "--deselect=tests/a.py::test_done" in command
    assert command[-1] == f"@{tmp_path / 'wave.args'}"


def test_run_waves_reruns_with_deselect_and_synthesizes_remaining_unreported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = tmp_path / "tier.txt"
    listing.write_text("tests/a.py\ntests/b.py\n", encoding="utf-8")
    seen_calls: list[tuple[list[str], list[str]]] = []

    def fake_run_one(
        *,
        files: list[str],
        deselect: list[str],
        args: argparse.Namespace,
        run_number: int,
        wave_number: int,
        wave_deadline: float,
        job_deadline: float,
        root: Path,
    ) -> tuple[int, Path, Path, str | None, list[dict[str, Any]]]:
        seen_calls.append((list(files), list(deselect)))
        junit = args.results / f"shard-1-wave-{wave_number}-run-{run_number}.xml"
        ledger = args.ledger_root / f"wave-{wave_number}-run-{run_number}"
        ledger.mkdir(parents=True, exist_ok=True)
        if run_number == 1:
            write_junit(junit, [{"classname": "tests.a", "name": "test_part1"}])
            write_events(
                ledger / "gw0.jsonl",
                [
                    {"event": "start", "nodeid": "tests/a.py::test_part1"},
                    {"event": "end", "nodeid": "tests/a.py::test_part1", "outcome": "passed"},
                    {"event": "start", "nodeid": "tests/a.py::test_part2"},
                ],
            )
            return 1, junit, ledger, None, []
        if run_number == 2:
            write_junit(junit, [{"classname": "tests.a", "name": "test_part2"}])
            return 0, junit, ledger, None, []
        write_junit(junit, [])
        return 0, junit, ledger, None, []

    monkeypatch.setattr(run_waves_module, "_run_one", fake_run_one)
    monkeypatch.setattr(run_waves_module, "_self_check", lambda: {"passed": True})
    args = argparse.Namespace(
        tier="1",
        files=listing,
        workers=2,
        watchdog_seconds=300,
        idle_seconds=600,
        wave_minutes=20,
        deadline_minutes=25,
        step_minutes=30,
        job_minutes=60,
        basetemp_root=tmp_path / "bt",
        results=tmp_path / "results",
        ledger_root=tmp_path / "ledger",
        report=tmp_path / "results" / "run-manifest.json",
    )

    manifest = run_waves(args, tmp_path)

    assert manifest["status"] == "unreported-files"
    assert manifest["duplicateResult"] is None
    assert manifest["plannedFiles"] == ["tests/a.py", "tests/b.py"]
    assert manifest["reportedFiles"] == ["tests/a.py"]
    assert seen_calls[0] == (["tests/a.py", "tests/b.py"], [])
    assert seen_calls[1] == (["tests/a.py", "tests/b.py"], ["tests/a.py::test_part1"])
    assert seen_calls[2] == (["tests/b.py"], [])
    assert (args.results / "unreported.xml").is_file()
    assert (args.results / "run-manifest.json").is_file()
    assert "WINDOWS_WAVES_DONE " in (args.results / "waves.log").read_text(encoding="utf-8")


def test_summary_rejects_missing_evidence_bad_states_and_invalid_contract_skips(
    tmp_path: Path,
) -> None:
    valid, _, errors = validate_smoke("SMOKE_STEP 1 passed\n", "x64")
    assert not valid and "missing SMOKE_DONE" in errors

    valid, _, errors = validate_smoke(
        'SMOKE_DONE {"leg":"x64","steps":{"1":"passed","2":"passed"}}\n',
        "x64",
    )
    assert not valid and "missing smoke step 3" in errors

    valid_arm, _, arm_errors = validate_smoke(
        'SMOKE_DONE {"leg":"arm","steps":{"1":"passed","2":"passed","6":"passed"},"exitCode":0}\n',
        "arm",
    )
    assert valid_arm and arm_errors == []

    valid_w, wave_errors = validate_waves(
        {
            "status": "failed",
            "selfCheck": {"passed": True},
            "plannedFiles": ["a"],
            "reportedFiles": [],
            "runs": [],
        },
        'WINDOWS_WAVES_DONE {"status": "failed"}\n',
    )
    assert not valid_w
    assert "wave status is 'failed'" in wave_errors
    assert "plannedFiles != reportedFiles" in wave_errors

    good_xml = tmp_path / "good.xml"
    skip_cases = [
        {
            "classname": "tests.kernel.ipc.local_ipc.test_contract",
            "name": f"test_skip_{idx}",
            "outcome": "skipped",
            "message": (
                "the AF_UNIX backend is POSIX only"
                if idx < 10
                else "POSIX file modes are the contract"
            ),
        }
        for idx in range(13)
    ]
    skip_cases.append(
        {
            "classname": "tests.tooling.architecture.platform.test_windows_portability_gate",
            "name": "test_rule_ids_agree_across_gate_ci_and_pr_template",
            "outcome": "failed",
            "message": "missing forgejo workflow",
        }
    )
    write_junit(good_xml, skip_cases)

    reasons, skip_errors = inspect_contract_skips([good_xml])
    assert len(reasons) == 13 and skip_errors == []
    assert junit_failure_nodeids([good_xml]) == [
        "tests.tooling.architecture.platform.test_windows_portability_gate::test_rule_ids_agree_across_gate_ci_and_pr_template"
    ]

    bad_xml = tmp_path / "bad.xml"
    write_junit(
        bad_xml,
        [
            {
                "classname": "tests.kernel.ipc.local_ipc.test_contract",
                "name": "test_unexpected_skip",
                "outcome": "skipped",
                "message": "unexpected reason",
            }
        ],
    )
    _, bad_skip_errors = inspect_contract_skips([bad_xml])
    assert any("invalid contract skip reason" in error for error in bad_skip_errors)
    assert any("expected 13" in error for error in bad_skip_errors)

    leg_ok, leg_errors, _ = summarize_leg(tmp_path / "missing-dir", "w1", waves=True)
    assert not leg_ok
    assert any("missing WINDOWS_WAVES_DONE" in error for error in leg_errors)
    assert CONTRACT_TEST_FILE == "tests/kernel/ipc/local_ipc/test_contract.py"


def test_run_waves_catches_duplicate_result_inside_rerun_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = tmp_path / "tier.txt"
    listing.write_text("tests/a.py\n", encoding="utf-8")
    calls = 0

    def fake_run_one(
        *,
        files: list[str],
        deselect: list[str],
        args: argparse.Namespace,
        run_number: int,
        wave_number: int,
        wave_deadline: float,
        job_deadline: float,
        root: Path,
    ) -> tuple[int, Path, Path, str | None, list[dict[str, Any]]]:
        nonlocal calls
        calls += 1
        junit = args.results / f"shard-1-wave-{wave_number}-run-{run_number}.xml"
        ledger = args.ledger_root / f"wave-{wave_number}-run-{run_number}"
        ledger.mkdir(parents=True, exist_ok=True)
        write_junit(
            junit,
            [
                {"classname": "tests.a", "name": "test_dup"},
                {"classname": "tests.a", "name": "test_dup"},
            ],
        )
        write_complete_ledger(ledger, ["tests/a.py::test_dup"])
        return 0, junit, ledger, None, []

    monkeypatch.setattr(run_waves_module, "_run_one", fake_run_one)
    monkeypatch.setattr(run_waves_module, "_self_check", lambda: {"passed": True})
    args = argparse.Namespace(
        tier="1",
        files=listing,
        workers=2,
        watchdog_seconds=300,
        idle_seconds=600,
        wave_minutes=20,
        deadline_minutes=25,
        step_minutes=30,
        job_minutes=60,
        basetemp_root=tmp_path / "bt",
        results=tmp_path / "results",
        ledger_root=tmp_path / "ledger",
        report=tmp_path / "results" / "run-manifest.json",
    )

    manifest = run_waves(args, tmp_path)

    assert calls == 1
    assert manifest["status"] == "unreported-files"
    assert manifest["offsettable"] is False
    assert isinstance(manifest["duplicateResult"], str)
    assert manifest["duplicateResult"].startswith("duplicate-result:")
    assert manifest["plannedFiles"] == ["tests/a.py"]
    assert len(manifest["runs"]) == 1
    assert manifest["runs"][0]["status"] == "unreported-files"
    assert any(
        detail.startswith("duplicate-result:")
        for detail in manifest["runs"][0]["details"]
    )


def test_leak_timeout_with_a_hang_file_is_a_worker_crash(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    junit = tmp_path / "result.xml"
    write_junit(junit, [{"classname": "tests.a", "name": "test_ok"}])
    write_complete_ledger(
        ledger,
        ["tests/a.py::test_ok"],
        extra=[
            {"event": "crash", "worker": "gw0", "reason": "leak-timeout", "exit_code": 97},
        ],
    )
    (ledger / "hang-gw0.txt").write_text(
        "hyprial watchdog: leak-timeout worker=gw0 exit_code=97 {}\nThread 0x1:\n",
        encoding="utf-8",
    )

    outcome = classify_run(
        returncode=97,
        junit_paths=[junit],
        ledger_dir=ledger,
        planned_files=["tests/a.py"],
    )

    assert outcome.status == "worker-crash"
    assert outcome.offsettable is False
    assert "crash:leak-timeout" in outcome.details


def test_leak_timeout_fallback_exit_is_a_worker_crash(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    junit = tmp_path / "result.xml"
    write_junit(junit, [{"classname": "tests.a", "name": "test_ok"}])
    write_complete_ledger(ledger, ["tests/a.py::test_ok"])
    (ledger / "hang-gw0.txt").write_text(
        "hyprial watchdog: leak-timeout worker=gw0 exit_code=97 {}\nTimeout (0:02:30)!\n",
        encoding="utf-8",
    )

    outcome = classify_run(
        returncode=1,
        junit_paths=[junit],
        ledger_dir=ledger,
        planned_files=["tests/a.py"],
    )

    assert outcome.status == "worker-crash"
    assert outcome.offsettable is False
    assert "leak-timeout-fallback" in outcome.details


def test_unexpected_skip_outside_contract_file_marks_run_failed(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    junit = tmp_path / "result.xml"
    write_junit(
        junit,
        [
            {"classname": "tests.a", "name": "test_ok"},
            {
                "classname": "tests.b",
                "name": "test_skipped",
                "outcome": "skipped",
                "message": "skipped outside contract",
            },
        ],
    )
    write_complete_ledger(ledger, ["tests/a.py::test_ok", "tests/b.py::test_skipped"])

    outcome = classify_run(
        returncode=0,
        junit_paths=[junit],
        ledger_dir=ledger,
        planned_files=["tests/a.py", "tests/b.py"],
    )

    assert outcome.status == "failed"
    assert outcome.offsettable is True
    assert any(
        detail.startswith("unexpected-skip:tests/b.py::test_skipped:")
        for detail in outcome.details
    )

    leg_dir = tmp_path / "leg"
    leg_dir.mkdir()
    manifest = {
        "status": "failed",
        "selfCheck": {"passed": True},
        "plannedFiles": ["tests/a.py", "tests/b.py"],
        "reportedFiles": ["tests/a.py", "tests/b.py"],
        "runs": [{"run": 1, "status": "failed", "duration": 1.0}],
        "leaks": [],
    }
    (leg_dir / "run-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (leg_dir / "waves.log").write_text(
        f"WINDOWS_WAVES_DONE {json.dumps(manifest)}\n",
        encoding="utf-8",
    )
    (leg_dir / "result.xml").write_text(junit.read_text(encoding="utf-8"), encoding="utf-8")
    _, _, lines = summarize_leg(leg_dir, "w1", waves=True)
    assert any("test_skipped" in line and "skipped outside contract" in line for line in lines)


def test_missing_ledger_directory_is_unreported_files(tmp_path: Path) -> None:
    junit = tmp_path / "result.xml"
    write_junit(junit, [{"classname": "tests.a", "name": "test_ok"}])

    outcome = classify_run(
        returncode=0,
        junit_paths=[junit],
        ledger_dir=tmp_path / "missing-ledger",
        planned_files=["tests/a.py"],
    )

    assert outcome.status == "unreported-files"
    assert outcome.offsettable is False
    assert "ledger-missing" in outcome.details


def test_ledger_missing_end_event_for_junit_case_is_unreported_files(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    junit = tmp_path / "result.xml"
    write_junit(
        junit,
        [
            {"classname": "tests.a", "name": "test_one"},
            {"classname": "tests.a", "name": "test_two"},
        ],
    )
    write_events(
        ledger / "gw0.jsonl",
        [
            {"event": "collect", "collected": 2, "errors": []},
            {"event": "end", "nodeid": "tests/a.py::test_one", "outcome": "passed"},
        ],
    )

    outcome = classify_run(
        returncode=0,
        junit_paths=[junit],
        ledger_dir=ledger,
        planned_files=["tests/a.py"],
    )

    assert outcome.status == "unreported-files"
    assert outcome.offsettable is False
    assert "ledger-uncovered:1" in outcome.details


def test_summary_validates_contract_skips_only_when_contract_file_is_planned(
    tmp_path: Path,
) -> None:
    clean_leg = tmp_path / "w1-clean"
    clean_leg.mkdir()
    clean_manifest = {
        "status": "passed",
        "selfCheck": {"passed": True},
        "plannedFiles": ["tests/a.py"],
        "reportedFiles": ["tests/a.py"],
        "runs": [{"run": 1, "status": "passed", "duration": 1.0}],
        "leaks": [],
    }
    (clean_leg / "run-manifest.json").write_text(json.dumps(clean_manifest), encoding="utf-8")
    (clean_leg / "waves.log").write_text(
        f"WINDOWS_WAVES_DONE {json.dumps(clean_manifest)}\n",
        encoding="utf-8",
    )
    write_junit(
        clean_leg / "shard-1-wave-1-run-1.xml",
        [{"classname": "tests.a", "name": "test_ok"}],
    )

    ok, errors, _ = summarize_leg(clean_leg, "w1", waves=True)
    assert ok is True, errors
    assert errors == []

    unplanned_leg = tmp_path / "w1-unplanned"
    unplanned_leg.mkdir()
    (unplanned_leg / "run-manifest.json").write_text(
        json.dumps(clean_manifest), encoding="utf-8"
    )
    (unplanned_leg / "waves.log").write_text(
        f"WINDOWS_WAVES_DONE {json.dumps(clean_manifest)}\n",
        encoding="utf-8",
    )
    write_junit(
        unplanned_leg / "shard-1-wave-1-run-1.xml",
        [
            {"classname": "tests.a", "name": "test_ok"},
            {
                "classname": "tests.kernel.ipc.local_ipc.test_contract",
                "name": "test_skip_0",
                "outcome": "skipped",
                "message": "the AF_UNIX backend is POSIX only",
            },
        ],
    )

    unplanned_ok, unplanned_errors, _ = summarize_leg(unplanned_leg, "w1", waves=True)
    assert unplanned_ok is False
    assert "contract skips found for an unplanned file" in unplanned_errors


def test_ledger_events_excludes_acl_probe_and_interceptor_hits(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    junit = tmp_path / "result.xml"
    write_junit(junit, [{"classname": "tests.a", "name": "test_ok"}])
    write_complete_ledger(ledger, ["tests/a.py::test_ok"])
    write_events(
        ledger / "acl-probe.jsonl",
        [
            {"event": "os.mkdir", "path": "C:/tmp/a", "nodeid": "tests/a.py::test_ok"},
            {"event": "start", "nodeid": "tests/a.py::never_ended"},
        ],
    )
    write_events(ledger / "interceptor-hits.jsonl", [])

    events = wave_state_module._ledger_events(ledger)
    assert all(event.get("event") in {"collect", "end"} for event in events)
    outcome = classify_run(
        returncode=0, junit_paths=[junit], ledger_dir=ledger, planned_files=["tests/a.py"]
    )
    assert outcome.status == "passed"


def test_run_waves_tier2_multi_wave_and_allows_conditional_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_path = tmp_path / "plan-manifest.json"
    plan_path.write_text(
        json.dumps(
            {
                "version": 1,
                "selection": "full",
                "suiteRequired": True,
                "requiredLegs": ["shard-0", "shard-1"],
                "shards": [["tests/z.py"], ["tests/a.py", "tests/b.py", "tests/c.py"]],
                "waves": [[["tests/z.py"]], [["tests/a.py", "tests/b.py"], ["tests/c.py"]]],
            }
        ),
        encoding="utf-8",
    )
    seen_waves: list[tuple[int, list[str]]] = []

    def fake_run_one(
        *, files: list[str], deselect: list[str], args: argparse.Namespace,
        run_number: int, wave_number: int, wave_deadline: float, job_deadline: float, root: Path,
    ) -> tuple[int, Path, Path, str | None, list[dict[str, Any]]]:
        seen_waves.append((wave_number, list(files)))
        junit = args.results / f"shard-1-wave-{wave_number}-run-{run_number}.xml"
        ledger = args.ledger_root / f"wave-{wave_number}-run-{run_number}"
        ledger.mkdir(parents=True, exist_ok=True)
        if wave_number == 1:
            write_junit(
                junit,
                [
                    {"classname": "tests.a", "name": "test_ok"},
                    {
                        "classname": "tests.b",
                        "name": "test_cond_skip",
                        "outcome": "skipped",
                        "message": "platform conditional skip",
                    },
                ],
            )
            write_complete_ledger(ledger, ["tests/a.py::test_ok", "tests/b.py::test_cond_skip"])
        else:
            write_junit(junit, [{"classname": "tests.c", "name": "test_ok"}])
            write_complete_ledger(ledger, ["tests/c.py::test_ok"])
        return 0, junit, ledger, None, []

    monkeypatch.setattr(run_waves_module, "_run_one", fake_run_one)
    monkeypatch.setattr(run_waves_module, "_self_check", lambda: {"passed": True})
    args = argparse.Namespace(
        tier="2", files=None, plan_manifest=plan_path, shard=1, acl_probe=True, workers=4,
        watchdog_seconds=600, idle_seconds=900, wave_minutes=40, deadline_minutes=140,
        step_minutes=150, job_minutes=170, basetemp_root=tmp_path / "bt",
        results=tmp_path / "results", ledger_root=tmp_path / "ledger",
        report=tmp_path / "results" / "run-manifest.json",
    )

    manifest = run_waves(args, tmp_path)

    assert manifest["status"] == "passed"
    assert (manifest["tier"], manifest["shard"], manifest["waveCount"]) == ("2", 1, 2)
    assert manifest["aclProbe"] is True
    assert manifest["plannedFiles"] == ["tests/a.py", "tests/b.py", "tests/c.py"]
    assert manifest["reportedFiles"] == ["tests/a.py", "tests/b.py", "tests/c.py"]
    assert seen_waves == [(1, ["tests/a.py", "tests/b.py"]), (2, ["tests/c.py"])]
    assert [entry["wave"] for entry in manifest["runs"]] == [1, 2]


def test_run_one_propagates_acl_probe_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_envs: list[dict[str, str]] = []

    class FakeChild:
        def __init__(self, *, command: list[str], output: Path, env: dict[str, str], cwd: Path) -> None:
            captured_envs.append(dict(env))

    monkeypatch.setattr(run_waves_module, "_ManagedChild", FakeChild)
    monkeypatch.setattr(run_waves_module, "_wait_with_watchdogs", lambda child, **kw: (0, None))
    monkeypatch.setattr(run_waves_module, "_query_windows_processes", lambda needles: [])
    monkeypatch.delenv("HYPRIAL_TEST_ACL_PROBE", raising=False)

    base_args = argparse.Namespace(
        tier="2", files=None, plan_manifest=tmp_path / "plan.json", shard=0, acl_probe=True,
        workers=2, watchdog_seconds=300, idle_seconds=600, wave_minutes=20, deadline_minutes=25,
        step_minutes=30, job_minutes=45, basetemp_root=tmp_path / "bt",
        results=tmp_path / "results", ledger_root=tmp_path / "ledger",
        report=tmp_path / "results" / "run-manifest.json",
    )
    run_waves_module._run_one(
        files=["tests/a.py"], deselect=[], args=base_args, run_number=1, wave_number=1,
        wave_deadline=1000.0, job_deadline=2000.0, root=tmp_path,
    )
    base_args.acl_probe = False
    run_waves_module._run_one(
        files=["tests/a.py"], deselect=[], args=base_args, run_number=2, wave_number=1,
        wave_deadline=1000.0, job_deadline=2000.0, root=tmp_path,
    )
    assert captured_envs[0].get("HYPRIAL_TEST_ACL_PROBE") == "1"
    assert "HYPRIAL_TEST_ACL_PROBE" not in captured_envs[1]


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
