"""Linux tests for nameless JUnit records and duplicate results in Windows waves.

Decision (2026-11-27, tier 2 full suite).  A testcase element can arrive with
neither a classname nor a name, holding nothing but a time attribute.  Such a
record names no test, so it is not a result: it is counted per run as
nameless-testcases:N and never takes part in duplicate detection.  It cannot
turn a run green.  A run that holds it keeps a hard state it already has, and
a run that would otherwise pass or be failed is red instead.  The failed branch
has its own test through the summary: failed is the one state a list of
expected failures can offset, so a run with a real failure and a nameless
record would otherwise end green there without a line naming the record.

Decision (2026-11-27, tier 2 full suite).  A repeated nodeid inside one run is
still rejected by read_junit and is still a hard red that nothing offsets, but
it no longer stops the waves after it.  The manifest keeps two kinds of
duplicate apart: duplicateRuns lists every run that raised inside the wave loop
(wave, attempt, nodeid) and duplicateUnion holds what the final read of all
XMLs together raised.  duplicateResult is the first of the former, else the
latter, so a union duplicate can no longer hide a run-time one.

The final read can raise while the status is passed: one attempt is classified
on its own XML, so a record that resolves to no planned file raises nothing as
long as each XML holds it once, and the final read sees it once per attempt.
Two passing waves that each hold such a record are that case.  The runner then
sets the manifest status to unreported-files; the summary is red on that input
either way, because it rejects the duplicate in duplicateUnion on its own.
That test fails without the assignment and passes with it.  Three tests reach
the branch from an attempt that is already a hard red, where the assignment
changes nothing: test_a_real_duplicate_keeps_the_later_waves_running_and_the_leg_red,
test_union_duplicate_does_not_hide_a_run_time_duplicate and
test_union_read_failure_leaves_the_leg_hard_red.  Each asserts the hard red and
the union duplicate.  The first two also assert three run-time duplicates, and
the first asserts that the leg is not offsettable; only the last asserts both
that the leg is not offsettable and that no run-time duplicate is claimed.

The final read catches a duplicate-result error only.  Any other ValueError is
raised out of run_waves.  XML that declares a multi-byte encoding such as gbk,
shift_jis or big5 makes read_junit raise one (measured by direct call on
CPython 3.12.13; that the runner's Python does the same is an inference), but
every attempt's XML is first read on its own in the wave loop and raises there,
so it never reaches the final read.  That test replaces the read to reach the
branch.

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
from scripts.ci.windows.summarize import _collect_suite_cases, summarize_suite, validate_waves
from scripts.ci.windows.wave_state import classify_run, read_junit
from tests.tooling.ci.windows.suite.test_run_waves import write_events

NAMED_PASS = '<testcase classname="tests.a" name="test_ok" time="0.010" />'
REAL_FAILURE = (
    '<testcase classname="tests.a" name="test_bad" time="0.010">'
    '<failure message="assert 1 == 2">call trace</failure></testcase>'
)
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
    ("returncode", "interceptor_hit", "real_failure", "nameless", "status", "details"),
    [
        (0, False, False, 3, "unreported-files", ("nameless-testcases:3",)),
        (3, False, False, 3, "worker-crash", ("unexpected-exit:3", "nameless-testcases:3")),
        (3, True, False, 3, "worker-self-terminated", ("interceptor-hits:1", "nameless-testcases:3")),
        (1, False, True, 1, "unreported-files", ("nameless-testcases:1",)),
        (1, False, True, 2, "unreported-files", ("nameless-testcases:2",)),
    ],
    ids=[
        "exit-0-would-have-passed",
        "exit-3-unknown-exit-code",
        "exit-3-interceptor-hit",
        "exit-1-one-real-failure-one-nameless",
        "exit-1-one-real-failure-two-nameless",
    ],
)
def test_nameless_records_are_counted_and_never_pass(
    tmp_path: Path,
    returncode: int,
    interceptor_hit: bool,
    real_failure: bool,
    nameless: int,
    status: str,
    details: tuple[str, ...],
) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    cases = [NAMED_PASS]
    events: list[dict[str, Any]] = [
        {"event": "collect", "collected": 2 if real_failure else 1, "errors": []},
        {"event": "start", "nodeid": "tests/a.py::test_ok"},
        {"event": "end", "nodeid": "tests/a.py::test_ok", "outcome": "passed"},
    ]
    if real_failure:
        cases.append(REAL_FAILURE)
        events += [
            {"event": "start", "nodeid": "tests/a.py::test_bad"},
            {"event": "end", "nodeid": "tests/a.py::test_bad", "outcome": "failed"},
        ]
    write_events(ledger / "gw0.jsonl", events)
    junit = write_xml(tmp_path / "r.xml", *cases, *([NAMELESS] * nameless))
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
    assert outcome.nameless_testcases == nameless


@pytest.mark.parametrize("nameless", [1, 2])
def test_a_nameless_record_cannot_be_offset_by_expected_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nameless: int
) -> None:
    plan_path = plan(tmp_path, [["tests/a.py"]])

    def fake_run_one(
        *, files: list[str], deselect: list[str], args: argparse.Namespace, run_number: int,
        wave_number: int, wave_deadline: float, job_deadline: float, root: Path,
    ) -> tuple[int, Path, Path, str | None, list[dict[str, Any]]]:
        junit = args.results / f"shard-0-wave-{wave_number}-run-{run_number}.xml"
        ledger = args.ledger_root / f"wave-{wave_number}-run-{run_number}"
        ledger.mkdir(parents=True)
        write_events(
            ledger / "gw0.jsonl",
            [
                {"event": "collect", "collected": 2, "errors": []},
                {"event": "start", "nodeid": "tests/a.py::test_ok"},
                {"event": "end", "nodeid": "tests/a.py::test_ok", "outcome": "passed"},
                {"event": "start", "nodeid": "tests/a.py::test_bad"},
                {"event": "end", "nodeid": "tests/a.py::test_bad", "outcome": "failed"},
            ],
        )
        write_xml(junit, NAMED_PASS, REAL_FAILURE, *([NAMELESS] * nameless))
        return 1, junit, ledger, None, []

    monkeypatch.setattr(run_waves_module, "_run_one", fake_run_one)
    monkeypatch.setattr(run_waves_module, "_self_check", lambda: {"passed": True})
    expected = tmp_path / "expected-failures.json"
    expected.write_text(
        json.dumps({"entries": [{"nodeid": "tests/a.py::test_bad", "kp": "KP-000"}]}), encoding="utf-8"
    )

    manifest = run_waves(wave_args(tmp_path, plan_manifest=plan_path), tmp_path)
    ok, lines = summarize_suite(
        plan_manifest_path=plan_path,
        leg_roots={"shard-0": tmp_path / "results"},
        expected_failures_path=expected,
    )

    assert manifest["runs"][0]["status"] == "unreported-files"
    assert manifest["offsettable"] is False
    assert any("matched=1 stale=0 unexpectedShape=0" in line for line in lines)
    assert ok is False
    assert "Overall status: **failed**" in "\n".join(lines)


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


def test_union_read_failure_on_passing_waves_sets_the_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_path = plan(tmp_path, [["tests/a.py"], ["tests/b.py"]])
    outside = '<testcase classname="plugin.check" name="session" time="0.001" />'

    def fake_run_one(
        *, files: list[str], deselect: list[str], args: argparse.Namespace, run_number: int,
        wave_number: int, wave_deadline: float, job_deadline: float, root: Path,
    ) -> tuple[int, Path, Path, str | None, list[dict[str, Any]]]:
        junit = args.results / f"shard-0-wave-{wave_number}-run-{run_number}.xml"
        ledger = write_ledger(
            args.ledger_root / f"wave-{wave_number}-run-{run_number}",
            (f"tests/{'ab'[wave_number - 1]}.py::test_ok",),
        )
        own = f'<testcase classname="tests.{"ab"[wave_number - 1]}" name="test_ok" time="0.010" />'
        write_xml(junit, outside, own)
        return 0, junit, ledger, None, []

    monkeypatch.setattr(run_waves_module, "_run_one", fake_run_one)
    monkeypatch.setattr(run_waves_module, "_self_check", lambda: {"passed": True})

    manifest = run_waves(wave_args(tmp_path, plan_manifest=plan_path), tmp_path)

    assert [(run["wave"], run["status"]) for run in manifest["runs"]] == [(1, "passed"), (2, "passed")]
    assert manifest["status"] == "unreported-files"
    assert manifest["status"] in HARD_STATES
    assert manifest["offsettable"] is False
    assert manifest["duplicateRuns"] == []
    assert manifest["duplicateUnion"] == "duplicate-result: plugin.check::session"
    assert manifest["duplicateResult"] == "duplicate-result: plugin.check::session"
    assert manifest["reportedFiles"] == ["tests/a.py", "tests/b.py"]
    assert not (tmp_path / "results" / "unreported.xml").exists()


def test_union_read_error_that_is_no_duplicate_is_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_path = plan(tmp_path, [["tests/a.py"]])

    def fake_run_one(
        *, files: list[str], deselect: list[str], args: argparse.Namespace, run_number: int,
        wave_number: int, wave_deadline: float, job_deadline: float, root: Path,
    ) -> tuple[int, Path, Path, str | None, list[dict[str, Any]]]:
        junit = args.results / f"shard-0-wave-{wave_number}-run-{run_number}.xml"
        ledger = write_ledger(args.ledger_root / f"wave-{wave_number}-run-{run_number}", ("tests/a.py::test_ok",))
        write_xml(junit, NAMED_PASS)
        return 0, junit, ledger, None, []

    def raising_read_junit(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("not-a-duplicate: boom")

    monkeypatch.setattr(run_waves_module, "_run_one", fake_run_one)
    monkeypatch.setattr(run_waves_module, "_self_check", lambda: {"passed": True})
    monkeypatch.setattr(run_waves_module, "read_junit", raising_read_junit)

    with pytest.raises(ValueError, match="^not-a-duplicate: boom$"):
        run_waves(wave_args(tmp_path, plan_manifest=plan_path), tmp_path)


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
