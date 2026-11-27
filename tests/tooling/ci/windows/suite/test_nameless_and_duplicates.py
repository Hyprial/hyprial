"""Linux tests for nameless JUnit records and duplicate results in Windows waves.

Decision (2026-11-27, tier 2 full suite).  A testcase element can arrive with
neither a classname nor a name, holding nothing but a time attribute.  Such a
record names no test, so it is not a result: it is counted per run as
nameless-testcases:N and never takes part in duplicate detection.  It cannot
turn a run green.  A run that holds it keeps the state its exit code and its
other evidence gave it, and a run that would otherwise pass is red instead.

Decision (2026-11-27, tier 2 full suite).  A repeated nodeid inside one run is
still rejected by read_junit and is still a hard red that nothing offsets, but
it no longer stops the waves after it.  The manifest keeps two kinds of
duplicate apart: duplicateRuns lists every run that raised inside the wave loop
(wave, attempt, nodeid) and duplicateUnion holds what the final read of all
XMLs together raised.  duplicateResult is the first of the former, else the
latter, so a union duplicate can no longer hide a run-time one.

When the final read raises, the status is set to a hard red at that point.  No
test here can fail on that line alone: a wave is rerun only after an attempt
that left a file unfinished, and such an attempt is always a hard red, so on
every input the runner can reach today the status is already a hard red when
the final read raises.  The test for that branch therefore asserts what can be
observed (hard red, not offsettable, the union duplicate recorded and no
run-time one claimed), and on the code before this change it fails on the
new manifest fields.

Decision (2026-11-27, tier 2 full suite).  summarize.py applies the same rule to
a nameless testcase: it is neither counted as a test nor taken for a duplicate.
It is also the second reader of the manifest, so it rejects a duplicate in each
of the three fields on its own.  Two bodies here pass on the code before this
change and are guards, not regressions: a manifest with all three fields empty
is accepted, and a duplicate recorded in duplicateResult alone is rejected, as
it always was.  The XML in this file is written by hand with invented paths
only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.ci.windows import run_waves as run_waves_module
from scripts.ci.windows.run_waves import HARD_STATES, run_waves
from scripts.ci.windows.summarize import _collect_suite_cases, validate_waves
from scripts.ci.windows.wave_state import classify_run, read_junit
from tests.tooling.ci.windows.suite.test_run_waves import write_events

NAMED_PASS = '<testcase classname="tests.a" name="test_ok" time="0.010" />'
NAMELESS = '<testcase time="0.001" />'


def write_xml(path: Path, *cases: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuites><testsuite name="pytest">{"".join(cases)}</testsuite></testsuites>',
        encoding="utf-8",
    )
    return path


def write_ledger(ledger: Path, ends: tuple[str, ...] = ()) -> Path:
    ledger.mkdir(parents=True, exist_ok=True)
    events: list[dict[str, Any]] = [{"event": "collect", "collected": len(ends), "errors": []}]
    for nodeid in ends:
        events.append({"event": "start", "nodeid": nodeid})
        events.append({"event": "end", "nodeid": nodeid, "outcome": "passed"})
    write_events(ledger / "gw0.jsonl", events)
    return ledger


def wave_args(tmp_path: Path, *, tier: str = "2", plan_manifest: Path | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        tier=tier, files=None, plan_manifest=plan_manifest, shard=0, acl_probe=False, workers=4,
        watchdog_seconds=600, idle_seconds=900, wave_minutes=40, deadline_minutes=140,
        step_minutes=150, job_minutes=170, basetemp_root=tmp_path / "bt",
        results=tmp_path / "results", ledger_root=tmp_path / "ledger",
        report=tmp_path / "results" / "run-manifest.json",
    )


def plan(tmp_path: Path, waves: list[list[str]]) -> Path:
    path = tmp_path / "plan-manifest.json"
    path.write_text(
        json.dumps(
            {
                "version": 1, "selection": "full", "suiteRequired": True, "requiredLegs": ["shard-0"],
                "shards": [[item for wave in waves for item in wave]], "waves": [waves],
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("returncode", "interceptor_hit", "status", "details"),
    [
        (0, False, "unreported-files", ("nameless-testcases:3",)),
        (3, False, "worker-crash", ("unexpected-exit:3", "nameless-testcases:3")),
        (3, True, "worker-self-terminated", ("interceptor-hits:1", "nameless-testcases:3")),
    ],
    ids=["exit-0-would-have-passed", "exit-3-unknown-exit-code", "exit-3-interceptor-hit"],
)
def test_nameless_records_are_counted_and_never_pass(
    tmp_path: Path, returncode: int, interceptor_hit: bool, status: str, details: tuple[str, ...]
) -> None:
    junit = write_xml(tmp_path / "r.xml", NAMED_PASS, NAMELESS, NAMELESS, NAMELESS)
    ledger = write_ledger(tmp_path / "ledger", ("tests/a.py::test_ok",))
    if interceptor_hit:
        write_events(
            ledger / "interceptor-hits.jsonl",
            [{"pid": 4242, "sig": 0, "nodeid": "tests/a.py::test_ok"}],
        )

    outcome = classify_run(
        returncode=returncode,
        junit_paths=[junit],
        ledger_dir=ledger,
        planned_files=["tests/a.py"],
        root=tmp_path,
        strict_skips=False,
    )

    assert outcome.status == status
    assert outcome.status in HARD_STATES
    assert outcome.offsettable is False
    assert outcome.details == details
    assert outcome.nameless_testcases == 3


def test_one_nameless_record_is_not_in_the_nodeid_set(tmp_path: Path) -> None:
    junit = write_xml(tmp_path / "r.xml", NAMED_PASS, NAMELESS)
    ledger = write_ledger(tmp_path / "ledger", ("tests/a.py::test_ok",))

    result = read_junit([junit], ["tests/a.py"], root=tmp_path)
    outcome = classify_run(
        returncode=0,
        junit_paths=[junit],
        ledger_dir=ledger,
        planned_files=["tests/a.py"],
        root=tmp_path,
        strict_skips=False,
    )

    assert result.nodeids == frozenset({"tests/a.py::test_ok"})
    assert result.nameless_testcases == 1
    assert outcome.status == "unreported-files"
    assert outcome.offsettable is False
    assert outcome.details == ("nameless-testcases:1",)


def test_nameless_records_do_not_stop_the_later_waves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan_path = plan(tmp_path, [["tests/a.py"], ["tests/b.py"], ["tests/c.py"]])
    started: list[tuple[int, int, list[str]]] = []

    def fake_run_one(
        *, files: list[str], deselect: list[str], args: argparse.Namespace, run_number: int,
        wave_number: int, wave_deadline: float, job_deadline: float, root: Path,
    ) -> tuple[int, Path, Path, str | None, list[dict[str, Any]]]:
        started.append((wave_number, run_number, list(files)))
        junit = args.results / f"shard-0-wave-{wave_number}-run-{run_number}.xml"
        ledger = write_ledger(
            args.ledger_root / f"wave-{wave_number}-run-{run_number}",
            (f"tests/{'abc'[wave_number - 1]}.py::test_ok",),
        )
        if wave_number == 1:
            write_xml(junit, NAMED_PASS, NAMELESS, NAMELESS, NAMELESS)
            write_events(
                ledger / "interceptor-hits.jsonl",
                [{"pid": 4242, "sig": 0, "nodeid": "tests/a.py::test_ok"}],
            )
            return 3, junit, ledger, None, []
        write_xml(junit, f'<testcase classname="tests.{"abc"[wave_number - 1]}" name="test_ok" time="0.010" />')
        return 0, junit, ledger, None, []

    monkeypatch.setattr(run_waves_module, "_run_one", fake_run_one)
    monkeypatch.setattr(run_waves_module, "_self_check", lambda: {"passed": True})
    args = wave_args(tmp_path, plan_manifest=plan_path)

    manifest = run_waves(args, tmp_path)

    assert [(wave, run) for wave, run, _files in started] == [(1, 1), (2, 1), (3, 1)]
    assert manifest["status"] == "worker-self-terminated"
    assert manifest["offsettable"] is False
    assert manifest["runs"][0]["status"] == "worker-self-terminated"
    assert manifest["runs"][0]["details"] == ["interceptor-hits:1", "nameless-testcases:3"]
    assert manifest["reportedFiles"] == ["tests/a.py", "tests/b.py", "tests/c.py"]
    assert manifest["duplicateRuns"] == []
    assert manifest["duplicateUnion"] is None
    assert manifest["duplicateResult"] is None


DUP_CASE = '<testcase classname="tests.a" name="test_dup" time="0.010" />'


def test_a_real_duplicate_keeps_the_later_waves_running_and_the_leg_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_path = plan(tmp_path, [["tests/a.py"], ["tests/b.py"], ["tests/c.py"]])
    started: list[tuple[int, int, list[str]]] = []

    def fake_run_one(
        *, files: list[str], deselect: list[str], args: argparse.Namespace, run_number: int,
        wave_number: int, wave_deadline: float, job_deadline: float, root: Path,
    ) -> tuple[int, Path, Path, str | None, list[dict[str, Any]]]:
        started.append((wave_number, run_number, list(files)))
        junit = args.results / f"shard-0-wave-{wave_number}-run-{run_number}.xml"
        ledger = write_ledger(
            args.ledger_root / f"wave-{wave_number}-run-{run_number}",
            (f"tests/{'abc'[wave_number - 1]}.py::test_dup" if wave_number == 1 else f"tests/{'abc'[wave_number - 1]}.py::test_ok",),
        )
        if wave_number == 1:
            write_xml(junit, DUP_CASE, DUP_CASE)
            return 0, junit, ledger, None, []
        write_xml(junit, f'<testcase classname="tests.{"abc"[wave_number - 1]}" name="test_ok" time="0.010" />')
        return 0, junit, ledger, None, []

    monkeypatch.setattr(run_waves_module, "_run_one", fake_run_one)
    monkeypatch.setattr(run_waves_module, "_self_check", lambda: {"passed": True})
    args = wave_args(tmp_path, plan_manifest=plan_path)

    manifest = run_waves(args, tmp_path)

    assert [(wave, run) for wave, run, _files in started] == [(1, 1), (1, 2), (1, 3), (2, 1), (3, 1)]
    assert manifest["status"] in HARD_STATES
    assert manifest["offsettable"] is False
    assert all(run["status"] == "unreported-files" for run in manifest["runs"] if run["wave"] == 1)
    assert [run["status"] for run in manifest["runs"] if run["wave"] in (2, 3)] == ["passed", "passed"]
    assert manifest["duplicateRuns"] == [
        {"wave": 1, "run": 1, "result": "duplicate-result: tests/a.py::test_dup"},
        {"wave": 1, "run": 2, "result": "duplicate-result: tests/a.py::test_dup"},
        {"wave": 1, "run": 3, "result": "duplicate-result: tests/a.py::test_dup"},
    ]
    assert manifest["duplicateResult"] == "duplicate-result: tests/a.py::test_dup"
    assert manifest["duplicateUnion"] == "duplicate-result: tests/a.py::test_dup"
    assert manifest["reportedFiles"] == ["tests/b.py", "tests/c.py"]


COLLECTION_ERROR = (
    '<testcase classname="" name="tests.a" time="0.00">'
    '<error message="collection failure">ImportError: boom</error></testcase>'
)
RUN_TIME_DUP_CASE = '<testcase classname="tests.b" name="test_dup" time="0.010" />'


def test_union_duplicate_does_not_hide_a_run_time_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_path = plan(tmp_path, [["tests/a.py"], ["tests/b.py"]])
    started: list[tuple[int, int]] = []

    def fake_run_one(
        *, files: list[str], deselect: list[str], args: argparse.Namespace, run_number: int,
        wave_number: int, wave_deadline: float, job_deadline: float, root: Path,
    ) -> tuple[int, Path, Path, str | None, list[dict[str, Any]]]:
        started.append((wave_number, run_number))
        junit = args.results / f"shard-0-wave-{wave_number}-run-{run_number}.xml"
        ledger = args.ledger_root / f"wave-{wave_number}-run-{run_number}"
        if wave_number == 1:
            write_xml(junit, COLLECTION_ERROR)
            ledger.mkdir(parents=True, exist_ok=True)
            write_events(
                ledger / "gw0.jsonl",
                [{"event": "collect", "collected": 0, "errors": [{"nodeid": "tests/a.py", "summary": "bad import"}]}],
            )
            return 1, junit, ledger, None, []
        write_xml(junit, RUN_TIME_DUP_CASE, RUN_TIME_DUP_CASE)
        write_ledger(ledger)
        return 0, junit, ledger, None, []

    monkeypatch.setattr(run_waves_module, "_run_one", fake_run_one)
    monkeypatch.setattr(run_waves_module, "_self_check", lambda: {"passed": True})
    args = wave_args(tmp_path, plan_manifest=plan_path)

    manifest = run_waves(args, tmp_path)

    assert started == [(1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (2, 3)]
    assert manifest["status"] in HARD_STATES
    assert manifest["duplicateUnion"] == "duplicate-result: tests.a"
    assert manifest["duplicateRuns"] == [
        {"wave": 2, "run": 1, "result": "duplicate-result: tests/b.py::test_dup"},
        {"wave": 2, "run": 2, "result": "duplicate-result: tests/b.py::test_dup"},
        {"wave": 2, "run": 3, "result": "duplicate-result: tests/b.py::test_dup"},
    ]
    assert manifest["duplicateResult"] == "duplicate-result: tests/b.py::test_dup"


def test_union_read_failure_leaves_the_leg_hard_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_path = plan(tmp_path, [["tests/a.py", "tests/b.py"]])
    started: list[tuple[int, int]] = []

    def fake_run_one(
        *, files: list[str], deselect: list[str], args: argparse.Namespace, run_number: int,
        wave_number: int, wave_deadline: float, job_deadline: float, root: Path,
    ) -> tuple[int, Path, Path, str | None, list[dict[str, Any]]]:
        started.append((wave_number, run_number))
        junit = args.results / f"shard-0-wave-{wave_number}-run-{run_number}.xml"
        ledger = write_ledger(args.ledger_root / f"wave-{wave_number}-run-{run_number}")
        write_xml(junit, COLLECTION_ERROR + '<testcase classname="tests.b" name="test_ok" time="0.010" />')
        return 1, junit, ledger, None, []

    monkeypatch.setattr(run_waves_module, "_run_one", fake_run_one)
    monkeypatch.setattr(run_waves_module, "_self_check", lambda: {"passed": True})
    args = wave_args(tmp_path, tier="1", plan_manifest=plan_path)

    manifest = run_waves(args, tmp_path)

    assert started == [(1, 1), (1, 2), (1, 3)]
    assert manifest["status"] == "collection-error"
    assert manifest["status"] in HARD_STATES
    assert manifest["offsettable"] is False
    assert manifest["duplicateRuns"] == []
    assert manifest["duplicateUnion"] == "duplicate-result: tests.a"
    assert manifest["duplicateResult"] == "duplicate-result: tests.a"
    assert manifest["reportedFiles"] == ["tests/b.py"]


@pytest.mark.parametrize("nameless", [1, 3])
def test_summary_neither_counts_a_nameless_record_nor_takes_it_for_a_duplicate(
    tmp_path: Path, nameless: int
) -> None:
    junit = write_xml(tmp_path / "r.xml", NAMED_PASS, *([NAMELESS] * nameless))

    totals, failures, problems = _collect_suite_cases([junit], ["tests/a.py"], {})

    assert totals == {"tests": 1, "passed": 1, "failures": 0, "errors": 0, "skipped": 0}
    assert failures == []
    assert problems == []


@pytest.mark.parametrize(
    "extra",
    [
        {"duplicateResult": "duplicate-result: tests/a.py::test_ok"},
        {"duplicateRuns": [{"wave": 1, "run": 1, "result": "duplicate-result: tests/a.py::test_ok"}]},
        {"duplicateUnion": "duplicate-result: tests/a.py"},
    ],
    ids=["result-only", "run-time-only", "union-only"],
)
def test_summary_rejects_a_duplicate_in_whichever_field_records_it(tmp_path: Path, extra: dict[str, Any]) -> None:
    manifest = {
        "status": "passed",
        "selfCheck": {"passed": True},
        "plannedFiles": ["tests/a.py"],
        "reportedFiles": ["tests/a.py"],
        "runs": [{"wave": 1, "run": 1, "status": "passed", "details": []}],
        **extra,
    }

    valid, errors = validate_waves(manifest, "WINDOWS_WAVES_DONE {}\n")

    assert valid is False
    assert any(error.startswith("duplicate result:") for error in errors)


def test_summary_rejects_a_manifest_with_a_run_time_and_a_union_duplicate(tmp_path: Path) -> None:
    manifest = {
        "status": "passed",
        "selfCheck": {"passed": True},
        "plannedFiles": ["tests/a.py", "tests/b.py"],
        "reportedFiles": ["tests/b.py"],
        "runs": [{"wave": 2, "run": 1, "status": "passed", "details": []}],
        "duplicateResult": "duplicate-result: tests/b.py::test_dup",
        "duplicateRuns": [
            {"wave": 2, "run": 1, "result": "duplicate-result: tests/b.py::test_dup"},
            {"wave": 2, "run": 2, "result": "duplicate-result: tests/b.py::test_dup"},
            {"wave": 2, "run": 3, "result": "duplicate-result: tests/b.py::test_dup"},
        ],
        "duplicateUnion": "duplicate-result: tests.a",
    }

    valid, errors = validate_waves(manifest, "WINDOWS_WAVES_DONE {}\n")

    assert valid is False
    duplicates = [error for error in errors if error.startswith("duplicate result:")]
    assert len(duplicates) == 3
    assert duplicates[0] == "duplicate result: duplicate-result: tests/b.py::test_dup"
    assert duplicates[2] == "duplicate result: duplicate-result: tests.a"


def test_summary_accepts_a_clean_manifest_with_empty_duplicate_fields(tmp_path: Path) -> None:
    manifest = {
        "status": "passed",
        "selfCheck": {"passed": True},
        "plannedFiles": ["tests/a.py"],
        "reportedFiles": ["tests/a.py"],
        "runs": [{"wave": 1, "run": 1, "status": "passed", "details": []}],
        "duplicateRuns": [],
        "duplicateUnion": None,
        "duplicateResult": None,
    }

    valid, errors = validate_waves(manifest, "WINDOWS_WAVES_DONE {}\n")

    assert valid is True, errors
