"""Linux tests for attributing JUnit testcases that carry no classname to planned files.

Decision (2026-11-27, tier 2 full suite).  pytest 8 with junit_family=xunit2 writes a
module skipped at collection time as one testcase with classname="" and the dotted
module path as name, holding <skipped message="collection skipped">; a module that
fails to collect is the same shape holding one or more <error message="collection
failure"> children.  A call failure followed by a teardown exception is two
testcases with the same name in one file, one failure-only and one error-only.
The XML here is written by hand in exactly those shapes so a single testcase can
carry several children.  Only the skipped shape that satisfies all four conditions
counts as reported, and only the failure-then-error pair inside one file merges;
every other shape stays red.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.ci.windows import run_waves as run_waves_module
from scripts.ci.windows.run_waves import RunOutcome, classify_run, read_junit, run_waves
from scripts.ci.windows.summarize import _collect_suite_cases, summarize_suite
from tests.tooling.ci.windows.test_run_waves import write_events

SKIPPED_MODULE = "tests/pkg/test_optional.py"
REAL_MODULE = "tests/pkg/test_real.py"
PLANNED = [REAL_MODULE, SKIPPED_MODULE]
REAL_CASE = '<testcase classname="tests.pkg.test_real" name="test_ok" time="0.01" />'
SKIP_BODY = '<skipped type="pytest.skip" message="collection skipped">optional dependency missing</skipped>'
COLLECTION_ERROR = '<error message="collection failure">ImportError: boom</error>'


def write_xml(path: Path, *cases: str) -> Path:
    body = "".join(cases)
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuites><testsuite name="pytest">{body}</testsuite></testsuites>',
        encoding="utf-8",
    )
    return path


def module_case(name: str, *children: str, classname: str = "") -> str:
    return f'<testcase classname="{classname}" name="{name}" time="0.00">{"".join(children)}</testcase>'


def write_ledger(ledger: Path, *names: str) -> Path:
    ledger.mkdir(parents=True, exist_ok=True)
    nodeids = [f"{REAL_MODULE}::{name}" for name in ("test_ok", *names)]
    write_events(
        ledger / "gw0.jsonl",
        [
            {"event": "collect", "collected": len(nodeids), "errors": []},
            *({"event": "start", "nodeid": nodeid} for nodeid in nodeids),
            *({"event": "end", "nodeid": nodeid, "outcome": "passed"} for nodeid in nodeids),
        ],
    )
    return ledger


def classify(tmp_path: Path, junit: Path, *, strict_skips: bool = False) -> RunOutcome:
    return classify_run(
        returncode=0,
        junit_paths=[junit],
        ledger_dir=write_ledger(tmp_path / "ledger"),
        planned_files=PLANNED,
        root=tmp_path,
        strict_skips=strict_skips,
    )


def test_module_level_importorskip_counts_as_reported(tmp_path: Path) -> None:
    junit = write_xml(tmp_path / "r.xml", REAL_CASE, module_case("tests.pkg.test_optional", SKIP_BODY))

    outcome = classify(tmp_path, junit)

    assert outcome.status == "passed", outcome.details
    assert outcome.reported_files == frozenset(PLANNED)
    assert outcome.collection_skipped_files == frozenset({SKIPPED_MODULE})
    assert SKIPPED_MODULE not in outcome.nodeids_by_file

    strict = classify(tmp_path, junit, strict_skips=True)
    assert strict.status == "failed"
    assert f"unexpected-skip:{SKIPPED_MODULE}:collection skipped" in strict.details


@pytest.mark.parametrize("error_count", [1, 4])
def test_collection_error_module_stays_red(tmp_path: Path, error_count: int) -> None:
    junit = write_xml(
        tmp_path / "r.xml",
        REAL_CASE,
        module_case("tests.pkg.test_optional", *([COLLECTION_ERROR] * error_count)),
    )

    outcome = classify(tmp_path, junit)

    assert outcome.status == "collection-error"
    assert outcome.offsettable is False
    assert outcome.collection_skipped_files == frozenset()
    assert SKIPPED_MODULE not in outcome.reported_files


@pytest.mark.parametrize(
    "case",
    [
        module_case("tests.pkg.test_optional", SKIP_BODY, '<error message="teardown">x</error>'),
        module_case(
            "tests.pkg.test_optional",
            SKIP_BODY,
            '<error message="a">x</error>',
            '<error message="b">y</error>',
        ),
        module_case("tests.pkg.test_optional", SKIP_BODY, '<failure message="f">x</failure>'),
        module_case("tests.pkg.test_optional"),
        module_case("tests.pkg.test_elsewhere", SKIP_BODY),
        module_case("tests.pkg.test_optional.py", SKIP_BODY),
        module_case("tests.pkg.test_optional", SKIP_BODY, classname="tests.pkg"),
    ],
    ids=[
        "skipped-plus-error",
        "skipped-plus-two-errors",
        "skipped-plus-failure",
        "no-skipped-child",
        "name-not-planned",
        "name-with-py-suffix",
        "non-empty-classname",
    ],
)
def test_module_case_failing_any_condition_is_not_reported(tmp_path: Path, case: str) -> None:
    junit = write_xml(tmp_path / "r.xml", REAL_CASE, case)

    result = read_junit([junit], PLANNED, root=tmp_path)
    outcome = classify(tmp_path, junit)

    assert result.collection_skipped_files == frozenset()
    assert SKIPPED_MODULE not in result.files
    assert outcome.status in {"unreported-files", "collection-error"}
    assert outcome.offsettable is False


def test_skipped_module_and_dropped_files_reach_manifest_and_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_path = tmp_path / "plan-manifest.json"
    plan = {
        "version": 1, "selection": "full", "suiteRequired": True, "requiredLegs": ["shard-0"],
        "posixOnlyIgnored": {"files": 0, "tests": 0},
        "noTestDefinitionsIgnored": {"files": 1, "paths": ["tests/pkg/test_helpers_only.py"]},
        "shards": [PLANNED], "waves": [[PLANNED]],
    }
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    def fake_run_one(
        *, files: list[str], deselect: list[str], args: argparse.Namespace,
        run_number: int, wave_number: int, wave_deadline: float, job_deadline: float, root: Path,
    ) -> tuple[int, Path, Path, str | None, list[dict[str, Any]]]:
        args.results.mkdir(parents=True, exist_ok=True)
        junit = write_xml(
            args.results / f"shard-0-wave-{wave_number}-run-{run_number}.xml",
            REAL_CASE,
            module_case("tests.pkg.test_optional", SKIP_BODY),
        )
        ledger = write_ledger(args.ledger_root / f"wave-{wave_number}-run-{run_number}")
        return 0, junit, ledger, None, []

    monkeypatch.setattr(run_waves_module, "_run_one", fake_run_one)
    monkeypatch.setattr(run_waves_module, "_self_check", lambda: {"passed": True})
    leg = tmp_path / "shard-0"
    args = argparse.Namespace(
        tier="2", files=None, plan_manifest=plan_path, shard=0, acl_probe=False, workers=4,
        watchdog_seconds=600, idle_seconds=900, wave_minutes=40, deadline_minutes=140,
        step_minutes=150, job_minutes=170, basetemp_root=tmp_path / "bt",
        results=leg / "results", ledger_root=leg / "ledger",
        report=leg / "results" / "run-manifest.json",
    )

    manifest = run_waves(args, tmp_path)

    assert manifest["status"] == "passed"
    assert manifest["reportedFiles"] == sorted(PLANNED)
    assert manifest["collectionSkippedFiles"] == {"files": 1, "paths": [SKIPPED_MODULE]}

    ok, lines = summarize_suite(plan_manifest_path=plan_path, leg_roots={"shard-0": leg})

    assert ok is True, lines
    assert "NO_TEST_DEFINITIONS_IGNORED files=1" in lines
    assert "  - `tests/pkg/test_helpers_only.py`" in lines
    assert "COLLECTION_SKIPPED_FILES files=1" in lines
    assert f"  - `{SKIPPED_MODULE}`" in lines


CALL_FAILURE = '<failure message="assert 1 == 2">call trace</failure>'
TEARDOWN_ERROR = '<error message="failed on teardown with &quot;RuntimeError: cleanup&quot;">teardown trace</error>'


def flaky(*children: str) -> str:
    return module_case("test_flaky", *children, classname="tests.pkg.test_real")


def test_call_failure_then_teardown_error_merges_into_one_failed_case(tmp_path: Path) -> None:
    junit = write_xml(tmp_path / "r.xml", REAL_CASE, flaky(CALL_FAILURE), flaky(TEARDOWN_ERROR))

    result = read_junit([junit], [REAL_MODULE], root=tmp_path)
    outcome = classify_run(
        returncode=1,
        junit_paths=[junit],
        ledger_dir=write_ledger(tmp_path / "ledger", "test_flaky"),
        planned_files=[REAL_MODULE],
        root=tmp_path,
        strict_skips=False,
    )
    totals, failures, problems = _collect_suite_cases([junit], [REAL_MODULE], {})

    assert (result.failures, result.errors) == (1, 0)
    assert result.nodeids_by_file[REAL_MODULE] == (f"{REAL_MODULE}::test_ok", f"{REAL_MODULE}::test_flaky")
    assert outcome.status == "failed"
    assert outcome.offsettable is True
    assert problems == []
    assert (totals["tests"], totals["failures"], totals["errors"]) == (2, 1, 0)
    assert [item["nodeid"] for item in failures] == [f"{REAL_MODULE}::test_flaky"]
    assert failures[0]["message"] == 'assert 1 == 2\nfailed on teardown with "RuntimeError: cleanup"'
    assert failures[0]["text"] == "call trace\nteardown trace"


@pytest.mark.parametrize(
    "xml_files",
    [
        [[flaky(CALL_FAILURE), flaky(CALL_FAILURE)]],
        [[flaky(TEARDOWN_ERROR), flaky(TEARDOWN_ERROR)]],
        [[flaky(), flaky()]],
        [[flaky(CALL_FAILURE), flaky(TEARDOWN_ERROR), flaky(TEARDOWN_ERROR)]],
        [[flaky(CALL_FAILURE, TEARDOWN_ERROR), flaky(TEARDOWN_ERROR)]],
        [[flaky(CALL_FAILURE)], [flaky(TEARDOWN_ERROR)]],
    ],
    ids=[
        "two-failures",
        "two-errors",
        "two-passes",
        "third-occurrence",
        "failure-and-error-in-one-case",
        "pair-across-xml-files",
    ],
)
def test_other_repeated_names_stay_duplicate_result(tmp_path: Path, xml_files: list[list[str]]) -> None:
    paths = [write_xml(tmp_path / f"r{index}.xml", *cases) for index, cases in enumerate(xml_files)]

    with pytest.raises(ValueError, match=r"^duplicate-result: tests/pkg/test_real\.py::test_flaky$"):
        read_junit(paths, [REAL_MODULE], root=tmp_path)
    _, _, problems = _collect_suite_cases(paths, [REAL_MODULE], {})
    assert f"duplicate-result: {REAL_MODULE}::test_flaky" in problems
