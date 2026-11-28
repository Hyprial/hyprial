"""Validate Windows smoke and suite wave evidence and write the job summary.

Decision (2026-11-26, tier 1 smoke and tier 2 full suite).  Missing evidence is a
failed leg because the summary job must distinguish an interrupted job from an
intentionally empty artifact.  JUnit parsing supports both xunit2 (classname-only)
and xunit1 (file attribute), lists failing nodeids verbatim in tier 1 with zero
exemptions, and verifies all 13 expected skip reasons in test_contract.py.  In
tier 2 (--mode suite), plan-manifest.json defines the required legs for the active
selection, hard-red wave states can never be offset, optional expected-failures.json
reconciliation checks exact phase, ledger excinfo.type, and message regex (failing on
stale-expected-failure or unexpected-failure-shape), and coarse failure buckets plus
POSIX_ONLY_IGNORED counts are printed for nightly trend tracking.

Decision (2026-11-27, tier 2 full suite).  Files dropped from the plan because they
define no test (plan key noTestDefinitionsIgnored) and files reported only through
a collection-time module skip (run-manifest key collectionSkippedFiles) are printed
with their count and every path, so neither is silent.

Decision (2026-11-27, tier 2 full suite).  Inside one XML file, a repeated testcase
name made of exactly one failure-only case and one error-only case (a call failure
followed by a teardown exception) is merged into one failed case whose message and
text join both parts; every other repeat stays duplicate-result, matching
wave_state.py.

Decision (2026-11-27, tier 2 full suite).  A testcase with neither a classname
nor a name is handled as in wave_state.py: it is neither counted as a test nor
taken for a duplicate.  Counted, it raised the pass count by one per record, so
a leg could show more passed tests than it has; resolved, the second one and
every later one came out as duplicate-result <unknown>, which names no test.
The wave manifest is rejected on each of duplicateResult, duplicateRuns and
duplicateUnion on its own, so a duplicate is red whichever field records it.

Decision (2026-11-27, tier 1 smoke and tier 2 full suite).  A wave run whose
details hold nameless-testcases:N adds one leg error naming the wave, the run
and that detail.  Without it, a leg turned red by such a run showed only
unreported-files with zero failures and zero errors, and the cause was in the
manifest alone.  The leg errors are used rather than the leg line because both
tiers print them under the leg with the same "error:" prefix, while the two
leg lines have different formats.  wave_state.py puts every such run in a hard
state, so a leg that shows the error is red already; the error names the cause.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Sequence

HARD_STATES = frozenset(
    {
        "collection-error",
        "test-watchdog-timeout",
        "worker-self-terminated",
        "native-worker-crash",
        "worker-crash",
        "controller-hang",
        "unreported-files",
        "job-deadline",
    }
)
CONTRACT_TEST_FILE = "tests/kernel/ipc/local_ipc/test_contract.py"
CONTRACT_CLASS_PREFIX = "tests.kernel.ipc.local_ipc.test_contract"
EXPECTED_SKIP_MESSAGES = frozenset(
    {"the AF_UNIX backend is POSIX only", "POSIX file modes are the contract"}
)
DEFAULT_STDIN_STOP_SCOPE = "pipe-eof-only; ConPTY/terminal-host not covered (KP-136 unfixed)"
SUITE_LEG_RE = re.compile(r"^(?:shard-\d+|targets|tier1-next|arm-next)$")
WINERROR_RE = re.compile(r"\bWinError\b")
IGNORED_LEDGER_FILES = frozenset({"interceptor-hits.jsonl", "acl-probe.jsonl"})


def _json_after(prefix: str, text: str) -> dict[str, Any] | None:
    for line in reversed(text.splitlines()):
        if not line.startswith(prefix):
            continue
        try:
            value = json.loads(line[len(prefix) :].strip())
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None
    return None


def _normalise_path(value: str) -> str:
    path = value.replace("\\", "/")
    marker = "/tests/"
    if marker in path:
        path = "tests/" + path.split(marker, 1)[1]
    return path.lstrip("./")


def _normalise_nodeid(nodeid: str) -> str:
    if "::" not in nodeid:
        return _normalise_path(nodeid)
    file_part, rest = nodeid.split("::", 1)
    return f"{_normalise_path(file_part)}::{rest}"


def _is_contract_case(case: ET.Element) -> bool:
    file_attr = case.attrib.get("file", "").replace("\\", "/")
    if CONTRACT_TEST_FILE in file_attr:
        return True
    classname = case.attrib.get("classname", "")
    return classname == CONTRACT_CLASS_PREFIX or classname.startswith(CONTRACT_CLASS_PREFIX + ".")


def _case_nodeid(case: ET.Element) -> str:
    file_attr = case.attrib.get("file", "").replace("\\", "/")
    classname = case.attrib.get("classname", "")
    name = case.attrib.get("name", "")
    if file_attr:
        return f"{file_attr}::{name}" if name else file_attr
    if classname and name:
        return f"{classname}::{name}"
    return name or classname or "<unknown>"


def _resolve_suite_nodeid(case: ET.Element, planned: frozenset[str]) -> str:
    classname = case.attrib.get("classname", "")
    name = case.attrib.get("name", "")
    raw_file = case.attrib.get("file")
    if raw_file:
        normalised = _normalise_path(raw_file)
        module_prefix = normalised[:-3].replace("/", ".") if normalised.endswith(".py") else ""
        suffix: list[str] = []
        if module_prefix and classname.startswith(module_prefix + "."):
            suffix = classname[len(module_prefix) + 1 :].split(".")
        elif classname and classname != module_prefix and "." not in classname:
            suffix = [classname]
        return "::".join([normalised, *suffix, name]) if name else normalised
    parts = [part for part in classname.split(".") if part]
    for end in range(len(parts), 0, -1):
        candidate = "/".join(parts[:end]) + ".py"
        if candidate in planned:
            suffix = parts[end:]
            return "::".join([candidate, *suffix, name]) if name else candidate
    return _case_nodeid(case)


def validate_smoke(text: str, leg: str) -> tuple[bool, dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    marker = _json_after("SMOKE_DONE ", text)
    if marker is None:
        return False, None, ["missing SMOKE_DONE"]
    required = {"1", "2", "6"} if leg == "arm" else {"1", "2", "3", "4", "5", "6"}
    steps = marker.get("steps")
    if not isinstance(steps, dict):
        errors.append("SMOKE_DONE steps is not an object")
    else:
        missing = sorted(required - set(steps))
        errors.extend(f"missing smoke step {step}" for step in missing)
        errors.extend(
            f"smoke step {step} is {steps[step]!r}"
            for step in sorted(required & set(steps))
            if not (
                steps[step] == "passed"
                or isinstance(steps[step], dict)
                and steps[step].get("status") == "passed"
            )
        )
    if marker.get("leg") not in {None, leg}:
        errors.append(f"SMOKE_DONE leg is {marker.get('leg')!r}")
    exit_code = marker.get("exitCode")
    if exit_code is not None and exit_code != 0:
        errors.append(f"SMOKE_DONE exitCode is {exit_code!r}")
    return not errors, marker, errors


def validate_waves(
    manifest: dict[str, Any] | None,
    text: str,
    *,
    allow_failed_status: bool = False,
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    marker = _json_after("WINDOWS_WAVES_DONE ", text)
    if manifest is None and marker is None:
        return False, ["missing WINDOWS_WAVES_DONE and run-manifest.json"]
    value = manifest if manifest is not None else marker
    if value is None:
        return False, ["missing wave evidence"]
    if marker is None:
        errors.append("missing WINDOWS_WAVES_DONE")
    status = value.get("status")
    allowed_statuses = {"passed", "failed"} if allow_failed_status else {"passed"}
    if status not in allowed_statuses:
        errors.append(f"wave status is {status!r}")
    for duplicate in (
        value.get("duplicateResult"),
        value.get("duplicateRuns"),
        value.get("duplicateUnion"),
    ):
        if duplicate:
            errors.append(f"duplicate result: {duplicate}")
    self_check = value.get("selfCheck")
    if not isinstance(self_check, dict) or self_check.get("passed") is not True:
        errors.append("wave selfCheck did not pass")
    planned = value.get("plannedFiles")
    reported = value.get("reportedFiles")
    if not isinstance(planned, list) or not planned or planned != reported:
        errors.append("plannedFiles != reportedFiles")
    runs = value.get("runs", [])
    if not isinstance(runs, list):
        errors.append("wave runs is not a list")
        runs = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        if run.get("status") in HARD_STATES:
            errors.append(f"hard red state {run['status']}")
        details = run.get("details")
        for detail in details if isinstance(details, list) else []:
            if isinstance(detail, str) and detail.startswith("nameless-testcases:"):
                errors.append(
                    f"wave {run.get('wave')} run {run.get('run')}: {detail}"
                    " (testcase elements with neither classname nor name)"
                )
    return not errors, errors


def inspect_contract_skips(
    xml_paths: Sequence[Path],
    planned_files: Sequence[str] | None = None,
) -> tuple[list[tuple[str, str]], list[str]]:
    reasons: list[tuple[str, str]] = []
    errors: list[str] = []
    for path in xml_paths:
        if not path.exists():
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as error:
            errors.append(f"invalid JUnit XML {path.name}: {error}")
            continue
        for case in root.iter("testcase"):
            if not _is_contract_case(case):
                continue
            skipped = case.find("skipped")
            if skipped is None:
                continue
            nodeid = _case_nodeid(case)
            reason = skipped.attrib.get("message", "")
            reasons.append((nodeid, reason))
            if reason not in EXPECTED_SKIP_MESSAGES:
                errors.append(f"invalid contract skip reason for {nodeid}: {reason!r}")
    if planned_files is None:
        if len(reasons) != 13:
            errors.append(f"contract skip count is {len(reasons)}, expected 13")
    else:
        planned = {path.replace("\\", "/").lstrip("./") for path in planned_files}
        if CONTRACT_TEST_FILE in planned:
            if len(reasons) != 13:
                errors.append(f"contract skip count is {len(reasons)}, expected 13")
        elif reasons:
            errors.append("contract skips found for an unplanned file")
    return reasons, errors


def contract_skip_errors(
    xml_paths: Sequence[Path],
    planned_files: Sequence[str] | None = None,
) -> list[str]:
    _, errors = inspect_contract_skips(xml_paths, planned_files=planned_files)
    return errors


def inspect_other_skips(xml_paths: Sequence[Path]) -> list[tuple[str, str]]:
    skips: list[tuple[str, str]] = []
    for path in xml_paths:
        if not path.exists():
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        for case in root.iter("testcase"):
            if _is_contract_case(case):
                continue
            skipped = case.find("skipped")
            if skipped is None:
                continue
            skips.append((_case_nodeid(case), skipped.attrib.get("message", "")))
    return skips


def junit_counts(xml_paths: Sequence[Path]) -> dict[str, int]:
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for path in xml_paths:
        if not path.exists():
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            totals["errors"] += 1
            continue
        for case in root.iter("testcase"):
            totals["tests"] += 1
            if case.find("failure") is not None:
                totals["failures"] += 1
            if case.find("error") is not None:
                totals["errors"] += 1
            if case.find("skipped") is not None:
                totals["skipped"] += 1
    return totals


def junit_failure_nodeids(xml_paths: Sequence[Path]) -> list[str]:
    failed: list[str] = []
    for path in xml_paths:
        if not path.exists():
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            failed.append(f"invalid-junit::{path.name}")
            continue
        for case in root.iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                failed.append(_case_nodeid(case))
    return failed


def categorize_failure(exception_type: str, message: str, text: str) -> str:
    combined = f"{exception_type}\n{message}\n{text}"
    if "TAILNET_STORAGE_UNSAFE" in combined:
        return "TAILNET_STORAGE_UNSAFE"
    if "unsafe-acl" in combined:
        return "unsafe-acl"
    if "TAILNET_CONTEXT_UNAVAILABLE" in combined:
        return "TAILNET_CONTEXT_UNAVAILABLE"
    if WINERROR_RE.search(combined):
        return "WinError"
    if "ModuleNotFoundError" in combined:
        return "ModuleNotFoundError"
    if "AssertionError" in combined:
        return "AssertionError"
    if exception_type:
        return exception_type.rsplit(".", 1)[-1]
    return "other"


def _read_ledger_ends(root: Path) -> dict[str, dict[str, Any]]:
    ends: dict[str, dict[str, Any]] = {}
    if not root.exists():
        return ends
    for path in sorted(root.rglob("*.jsonl")):
        if path.name in IGNORED_LEDGER_FILES:
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("event") != "end":
                continue
            nodeid = event.get("nodeid")
            if isinstance(nodeid, str) and nodeid:
                ends[_normalise_nodeid(nodeid)] = event
    return ends


def _single_outcome_kind(case: ET.Element) -> str | None:
    failures = len(case.findall("failure"))
    errors = len(case.findall("error"))
    if case.find("skipped") is not None:
        return None
    if (failures, errors) == (1, 0):
        return "failure"
    if (failures, errors) == (0, 1):
        return "error"
    return None


def _merge_pair_detail(entry: dict[str, Any], detail_node: ET.Element | None) -> None:
    if detail_node is None:
        return
    extra_message = detail_node.attrib.get("message", "")
    extra_text = detail_node.text or ""
    entry["message"] = "\n".join(part for part in (entry["message"], extra_message) if part)
    entry["text"] = "\n".join(part for part in (entry["text"], extra_text) if part)
    entry["category"] = categorize_failure(entry["exception"], entry["message"], entry["text"])


def _collect_suite_cases(
    xml_paths: Sequence[Path],
    planned_files: Sequence[str],
    ledger_ends: dict[str, dict[str, Any]],
) -> tuple[dict[str, int], list[dict[str, Any]], list[str]]:
    planned = frozenset(_normalise_path(path) for path in planned_files)
    totals = {"tests": 0, "passed": 0, "failures": 0, "errors": 0, "skipped": 0}
    failures: list[dict[str, Any]] = []
    parse_or_dup_errors: list[str] = []
    seen_nodeids: set[str] = set()
    for path in xml_paths:
        if not path.exists():
            continue
        try:
            xml_root = ET.parse(path).getroot()
        except ET.ParseError as error:
            totals["errors"] += 1
            parse_or_dup_errors.append(f"invalid JUnit XML {path.name}: {error}")
            continue
        pair_candidates: dict[str, tuple[str, int]] = {}
        for case in xml_root.iter("testcase"):
            if not case.attrib.get("classname", "") and not case.attrib.get("name", ""):
                continue
            nodeid = _resolve_suite_nodeid(case, planned)
            kind = _single_outcome_kind(case)
            if nodeid in seen_nodeids:
                previous = pair_candidates.pop(nodeid, None)
                if previous is not None and kind is not None and {previous[0], kind} == {"failure", "error"}:
                    if previous[0] == "error":
                        totals["errors"] -= 1
                        totals["failures"] += 1
                    _merge_pair_detail(failures[previous[1]], case.find(kind))
                    continue
                parse_or_dup_errors.append(f"duplicate-result: {nodeid}")
            elif kind is not None:
                pair_candidates[nodeid] = (kind, len(failures))
            totals["tests"] += 1
            seen_nodeids.add(nodeid)
            fail_node = case.find("failure")
            err_node = case.find("error")
            skip_node = case.find("skipped")
            if fail_node is not None:
                totals["failures"] += 1
            if err_node is not None:
                totals["errors"] += 1
            if skip_node is not None:
                totals["skipped"] += 1
            if fail_node is None and err_node is None and skip_node is None:
                totals["passed"] += 1
            detail_node = fail_node if fail_node is not None else err_node
            if detail_node is not None:
                end_event = ledger_ends.get(nodeid, {})
                raw_excinfo = end_event.get("excinfo")
                excinfo = raw_excinfo if isinstance(raw_excinfo, dict) else {}
                exc_type = str(excinfo.get("type", ""))
                exc_msg = str(excinfo.get("message", ""))
                junit_msg = detail_node.attrib.get("message", "")
                junit_text = detail_node.text or ""
                message = exc_msg or junit_msg
                failures.append(
                    {
                        "nodeid": nodeid,
                        "phase": str(end_event.get("phase", "")),
                        "exception": exc_type,
                        "message": message,
                        "text": junit_text,
                        "category": categorize_failure(exc_type, message, junit_text),
                    }
                )
    return totals, failures, parse_or_dup_errors


def _paths_from(value: object) -> list[str]:
    raw = value.get("paths") if isinstance(value, dict) else None
    if not isinstance(raw, list):
        return []
    return sorted({str(item) for item in raw})


def _required_legs_from_plan(plan: dict[str, Any]) -> list[str]:
    explicit = plan.get("requiredLegs")
    if isinstance(explicit, list) and explicit:
        return [str(item) for item in explicit]
    selection = str(plan.get("selection", "full"))
    if selection == "full":
        shard_count = int(plan.get("shardCount") or len(plan.get("shards", [])) or 4)
        return [f"shard-{index}" for index in range(shard_count)]
    return [selection]


def _entry_applies_to_leg(entry_leg: str | None, leg: str) -> bool:
    if not entry_leg:
        return True
    if entry_leg == leg:
        return True
    if entry_leg == "windows-x64":
        return leg != "arm-next"
    if entry_leg == "windows-arm":
        return leg == "arm-next"
    return False


def _load_expected_failures(path: Path | None) -> tuple[list[dict[str, Any]], list[str]]:
    if path is None or not path.exists():
        return [], []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return [], [f"invalid expected-failures.json: {error}"]
    if not isinstance(raw, dict):
        return [], ["expected-failures.json must be a JSON object"]
    entries = raw.get("entries", [])
    if not isinstance(entries, list):
        return [], ["expected-failures.json entries must be a list"]
    return [entry for entry in entries if isinstance(entry, dict)], []


def _reconcile_expected_failures(
    entries: Sequence[dict[str, Any]],
    leg_records: dict[str, dict[str, Any]],
    *,
    selection: str,
) -> dict[str, Any]:
    matched: set[tuple[str, str]] = set()
    stale: list[str] = []
    shape_mismatches: list[str] = []
    if not entries:
        return {
            "active": False,
            "matched": matched,
            "stale": stale,
            "shapeMismatches": shape_mismatches,
        }
    for entry in entries:
        nodeid = _normalise_nodeid(str(entry.get("nodeid", "")))
        if not nodeid:
            continue
        file_part = nodeid.split("::", 1)[0]
        entry_leg = str(entry.get("leg", "")).strip() or None
        expected_phase = str(entry.get("phase", "")).strip()
        expected_exception = str(entry.get("exception", "")).strip()
        pattern = str(entry.get("match") if "match" in entry else entry.get("message_regex", ""))
        kp = str(entry.get("kp", ""))
        candidate_legs = [
            leg
            for leg, record in leg_records.items()
            if _entry_applies_to_leg(entry_leg, leg)
            and file_part in record["plannedSet"]
        ]
        if not candidate_legs:
            if selection == "full":
                stale.append(f"stale-expected-failure: {nodeid} ({kp}) not in planned files")
            continue
        for leg in candidate_legs:
            record = leg_records[leg]
            failure_map: dict[str, dict[str, Any]] = record["failureByNodeid"]
            actual = failure_map.get(nodeid)
            if actual is None:
                stale.append(f"stale-expected-failure: {leg} {nodeid} ({kp})")
                continue
            phase_ok = not expected_phase or actual["phase"] == expected_phase
            exc_ok = not expected_exception or actual["exception"] == expected_exception
            search_target = actual["message"] or actual["text"]
            regex_ok = True
            if pattern:
                try:
                    regex_ok = re.search(pattern, search_target) is not None
                except re.error as error:
                    regex_ok = False
                    shape_mismatches.append(
                        f"unexpected-failure-shape: {leg} {nodeid} invalid regex {pattern!r}: {error}"
                    )
                    continue
            if phase_ok and exc_ok and regex_ok:
                matched.add((leg, nodeid))
            else:
                shape_mismatches.append(
                    f"unexpected-failure-shape: {leg} {nodeid} expected "
                    f"phase={expected_phase!r} exception={expected_exception!r} pattern={pattern!r}; "
                    f"got phase={actual['phase']!r} exception={actual['exception']!r}"
                )
    return {
        "active": True,
        "matched": matched,
        "stale": stale,
        "shapeMismatches": shape_mismatches,
    }


def summarize_suite(
    *,
    plan_manifest_path: Path,
    leg_roots: dict[str, Path],
    expected_failures_path: Path | None = None,
) -> tuple[bool, list[str]]:
    report_lines: list[str] = []
    if not plan_manifest_path.is_file():
        report_lines.append("## Windows suite summary")
        report_lines.append(f"- error: missing plan-manifest.json at `{plan_manifest_path}`")
        report_lines.append("\nOverall status: **failed**")
        for line in report_lines:
            print(line)
        return False, report_lines
    try:
        plan = json.loads(plan_manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        report_lines.append("## Windows suite summary")
        report_lines.append(f"- error: invalid plan-manifest.json: {error}")
        report_lines.append("\nOverall status: **failed**")
        for line in report_lines:
            print(line)
        return False, report_lines
    if not isinstance(plan, dict):
        report_lines.append("## Windows suite summary")
        report_lines.append("- error: plan-manifest.json is not an object")
        report_lines.append("\nOverall status: **failed**")
        for line in report_lines:
            print(line)
        return False, report_lines
    selection = str(plan.get("selection", "unknown"))
    required_legs = _required_legs_from_plan(plan)
    raw_ignored = plan.get("posixOnlyIgnored")
    ignored = raw_ignored if isinstance(raw_ignored, dict) else {}
    ignored_files = int(ignored.get("files", 0))
    ignored_tests = int(ignored.get("tests", 0))
    report_lines.append(f"## Windows suite summary (selection={selection})")
    report_lines.append(f"POSIX_ONLY_IGNORED files={ignored_files} tests={ignored_tests}")
    dropped_paths = _paths_from(plan.get("noTestDefinitionsIgnored"))
    report_lines.append(f"NO_TEST_DEFINITIONS_IGNORED files={len(dropped_paths)}")
    report_lines.extend(f"  - `{path}`" for path in dropped_paths)
    collection_skipped: set[str] = set()
    expected_entries, expected_load_errors = _load_expected_failures(expected_failures_path)
    leg_records: dict[str, dict[str, Any]] = {}
    category_counts: dict[str, int] = {}
    all_leaks: list[dict[str, Any]] = []
    for leg in required_legs:
        root = leg_roots.get(leg)
        errors: list[str] = []
        if root is None or not root.exists():
            errors.append(f"missing required leg artifact: {leg}")
            leg_records[leg] = {
                "errors": errors,
                "hardReds": [],
                "states": [],
                "counts": {"tests": 0, "passed": 0, "failures": 0, "errors": 0, "skipped": 0},
                "duration": 0.0,
                "leaks": [],
                "failures": [],
                "failureByNodeid": {},
                "plannedSet": frozenset(),
                "manifestStatus": "missing",
            }
            continue
        text_paths = sorted(root.rglob("*.log")) + sorted(root.rglob("*.txt"))
        text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in text_paths)
        manifests = sorted(root.rglob("run-manifest.json"))
        manifest: dict[str, Any] | None = None
        if manifests:
            try:
                loaded = json.loads(manifests[0].read_text(encoding="utf-8"))
                manifest = loaded if isinstance(loaded, dict) else None
            except json.JSONDecodeError as error:
                errors.append(f"invalid run-manifest.json: {error}")
        valid, wave_errors = validate_waves(
            manifest,
            text,
            allow_failed_status=bool(expected_entries),
        )
        if not valid:
            errors.extend(wave_errors)
        raw_planned = manifest.get("plannedFiles", []) if isinstance(manifest, dict) else []
        planned_files = [str(item) for item in raw_planned] if isinstance(raw_planned, list) else []
        xmls = sorted(root.rglob("*.xml"))
        ledger_ends = _read_ledger_ends(root)
        counts, failures, xml_errors = _collect_suite_cases(xmls, planned_files, ledger_ends)
        errors.extend(xml_errors)
        runs = manifest.get("runs", []) if isinstance(manifest, dict) else []
        states = [str(run.get("status")) for run in runs if isinstance(run, dict) and run.get("status")]
        hard_reds = [state for state in states if state in HARD_STATES]
        manifest_status = str(manifest.get("status", "missing")) if isinstance(manifest, dict) else "missing"
        if manifest_status in HARD_STATES and manifest_status not in hard_reds:
            hard_reds.append(manifest_status)
        leaks = manifest.get("leaks", []) if isinstance(manifest, dict) else []
        if isinstance(leaks, list):
            for leak in leaks:
                if isinstance(leak, dict):
                    all_leaks.append({"leg": leg, **leak})
        if isinstance(manifest, dict):
            collection_skipped.update(_paths_from(manifest.get("collectionSkippedFiles")))
        duration = round(
            sum(
                float(run.get("duration", 0.0))
                for run in runs
                if isinstance(run, dict)
            ),
            3,
        )
        for failure in failures:
            bucket = failure["category"]
            category_counts[bucket] = category_counts.get(bucket, 0) + 1
        leg_records[leg] = {
            "errors": errors,
            "hardReds": hard_reds,
            "states": states,
            "counts": counts,
            "duration": duration,
            "leaks": leaks if isinstance(leaks, list) else [],
            "failures": failures,
            "failureByNodeid": {item["nodeid"]: item for item in failures},
            "plannedSet": frozenset(_normalise_path(path) for path in planned_files),
            "manifestStatus": manifest_status,
        }
    reconciliation = _reconcile_expected_failures(
        expected_entries,
        leg_records,
        selection=selection,
    )
    matched: set[tuple[str, str]] = reconciliation["matched"]
    overall_ok = not expected_load_errors and not reconciliation["stale"] and not reconciliation["shapeMismatches"]
    for error in expected_load_errors:
        report_lines.append(f"- error: {error}")
    for leg in required_legs:
        record = leg_records[leg]
        unoffset = [
            item["nodeid"]
            for item in record["failures"]
            if (leg, item["nodeid"]) not in matched
        ]
        leg_errors = list(record["errors"])
        if record["hardReds"]:
            overall_ok = False
        if unoffset:
            leg_errors.append(f"unoffset failures: {len(unoffset)}")
        elif record["manifestStatus"] == "failed" and not record["failures"]:
            leg_errors.append("wave status is 'failed' with no JUnit failures to offset")
        leg_ok = not leg_errors and not record["hardReds"]
        if not leg_ok:
            overall_ok = False
        label = "passed" if leg_ok else "failed"
        report_lines.append(
            f"- **{leg}**: `{label}` duration={record['duration']}s "
            f"counts={record['counts']} states={record['states']} "
            f"hardRed={record['hardReds']} leaks={len(record['leaks'])}"
        )
        for error in leg_errors:
            report_lines.append(f"  - error: {error}")
        if unoffset:
            report_lines.append(f"  - unoffset failed nodeids ({len(unoffset)}):")
            report_lines.extend(f"    - `{nodeid}`" for nodeid in unoffset[:20])
    if reconciliation["active"]:
        report_lines.append(
            f"- **expected-failures**: matched={len(matched)} "
            f"stale={len(reconciliation['stale'])} "
            f"unexpectedShape={len(reconciliation['shapeMismatches'])}"
        )
        report_lines.extend(f"  - {item}" for item in reconciliation["stale"])
        report_lines.extend(f"  - {item}" for item in reconciliation["shapeMismatches"])
    if category_counts:
        ordered = ", ".join(
            f"{name}={count}" for name, count in sorted(category_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        report_lines.append(f"- **failure-categories**: {ordered}")
    if all_leaks:
        report_lines.append(f"- **leaks**: total={len(all_leaks)}")
    report_lines.append(f"COLLECTION_SKIPPED_FILES files={len(collection_skipped)}")
    report_lines.extend(f"  - `{path}`" for path in sorted(collection_skipped))
    overall ="passed" if overall_ok else "failed"
    report_lines.append(f"\nOverall status: **{overall}**")
    for line in report_lines:
        print(line)
    return overall_ok, report_lines


def summarize_leg(root: Path, leg: str, *, waves: bool = False) -> tuple[bool, list[str], list[str]]:
    errors: list[str] = []
    lines: list[str] = []
    text_paths = (
        sorted(root.rglob("*.log")) + sorted(root.rglob("*.txt"))
        if root.exists()
        else []
    )
    text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in text_paths)
    if waves:
        manifests = sorted(root.rglob("run-manifest.json")) if root.exists() else []
        manifest: dict[str, Any] | None = None
        if manifests:
            try:
                loaded = json.loads(manifests[0].read_text(encoding="utf-8"))
                manifest = loaded if isinstance(loaded, dict) else None
            except json.JSONDecodeError as error:
                errors.append(f"invalid run-manifest.json: {error}")
        valid, wave_errors = validate_waves(manifest, text)
        if not valid:
            errors.extend(wave_errors)
        xmls = sorted(root.rglob("*.xml")) if root.exists() else []
        raw_planned = manifest.get("plannedFiles") if isinstance(manifest, dict) else None
        planned_files = (
            [str(item) for item in raw_planned]
            if isinstance(raw_planned, list)
            else None
        )
        skip_reasons, skip_errors = inspect_contract_skips(xmls, planned_files=planned_files)
        errors.extend(skip_errors)
        other_skips = inspect_other_skips(xmls)
        counts = junit_counts(xmls)
        failed_nodeids = junit_failure_nodeids(xmls)
        leaks = manifest.get("leaks", []) if isinstance(manifest, dict) else []
        hard_reds = [
            run.get("status")
            for run in (manifest.get("runs", []) if isinstance(manifest, dict) else [])
            if isinstance(run, dict) and run.get("status") in HARD_STATES
        ]
        duration = round(
            sum(
                float(run.get("duration", 0.0))
                for run in (manifest.get("runs", []) if isinstance(manifest, dict) else [])
                if isinstance(run, dict)
            ),
            3,
        )
        status_label = "passed" if not errors else "failed"
        lines.append(
            f"- **{leg}**: `{status_label}` duration={duration}s counts={counts} hardRed={hard_reds} leaks={leaks}"
        )
        if failed_nodeids:
            lines.append("  - failed nodeids:")
            lines.extend(f"    - `{nodeid}`" for nodeid in failed_nodeids)
        if skip_reasons:
            lines.append(f"  - `test_contract.py` skips ({len(skip_reasons)}):")
            lines.extend(f"    - `{nodeid}`: {reason}" for nodeid, reason in skip_reasons)
        if other_skips:
            lines.append(f"  - unexpected skips ({len(other_skips)}):")
            lines.extend(f"    - `{nodeid}`: {reason}" for nodeid, reason in other_skips)
        lines.extend(f"  - error: {error}" for error in errors)
        for line in lines:
            print(line)
        return not errors, errors, lines
    valid, marker, smoke_errors = validate_smoke(text, leg)
    if not valid:
        errors.extend(smoke_errors)
    duration = marker.get("duration") if isinstance(marker, dict) else None
    scope = (
        marker.get("stdinStopScope", DEFAULT_STDIN_STOP_SCOPE)
        if isinstance(marker, dict)
        else DEFAULT_STDIN_STOP_SCOPE
    )
    status_label = "passed" if not errors else "failed"
    lines.append(f"- **{leg}**: `{status_label}` duration={duration}s")
    if leg == "x64":
        lines.append(f"  - stdin-stop scope: {scope}")
    lines.extend(f"  - error: {error}" for error in errors)
    for line in lines:
        print(line)
    return not errors, errors, lines


def _write_step_summary(report_lines: Sequence[str]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as output:
            output.write("\n" + "\n".join(report_lines) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("smoke", "suite"), default="smoke")
    parser.add_argument("--plan-manifest", type=Path, default=None)
    parser.add_argument("--expected-failures", type=Path, default=None)
    parser.add_argument("--input", action="append", required=True)
    args = parser.parse_args(argv)
    if args.mode == "suite":
        if args.plan_manifest is None:
            parser.error("--plan-manifest is required when --mode suite")
        leg_roots: dict[str, Path] = {}
        for item in args.input:
            raw_root, separator, leg = item.partition("=")
            if not separator or not SUITE_LEG_RE.fullmatch(leg):
                parser.error("--input must be PATH=shard-<N>, PATH=targets, PATH=tier1-next, or PATH=arm-next")
            leg_roots[leg] = Path(raw_root)
        overall_ok, report_lines = summarize_suite(
            plan_manifest_path=args.plan_manifest,
            leg_roots=leg_roots,
            expected_failures_path=args.expected_failures,
        )
        _write_step_summary(report_lines)
        return 0 if overall_ok else 1
    results: list[bool] = []
    report_lines = ["## Windows smoke summary"]
    for item in args.input:
        raw_root, separator, leg = item.partition("=")
        if not separator or leg not in {"x64", "arm", "w1"}:
            parser.error("--input must be PATH=x64, PATH=arm, or PATH=w1")
        valid, _, leg_lines = summarize_leg(Path(raw_root), leg, waves=leg == "w1")
        results.append(valid)
        report_lines.extend(leg_lines)
    overall = "passed" if results and all(results) else "failed"
    report_lines.append(f"\nOverall status: **{overall}**")
    _write_step_summary(report_lines)
    return 0 if overall == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
