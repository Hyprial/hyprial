"""Validate Windows smoke and wave evidence and write the job summary.

Decision (2026-11-26, tier 1 smoke).  Missing evidence is a failed leg because the
summary job must distinguish an interrupted job from an intentionally empty
artifact.  JUnit parsing supports both xunit2 (classname-only) and xunit1
(file attribute), lists failing nodeids verbatim with zero exemptions, and
verifies all 13 expected skip reasons in test_contract.py.
"""

from __future__ import annotations

import argparse
import json
import os
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


def validate_waves(manifest: dict[str, Any] | None, text: str) -> tuple[bool, list[str]]:
    errors: list[str] = []
    marker = _json_after("WINDOWS_WAVES_DONE ", text)
    if manifest is None and marker is None:
        return False, ["missing WINDOWS_WAVES_DONE and run-manifest.json"]
    value = manifest if manifest is not None else marker
    if value is None:
        return False, ["missing wave evidence"]
    if marker is None:
        errors.append("missing WINDOWS_WAVES_DONE")
    if value.get("status") != "passed":
        errors.append(f"wave status is {value.get('status')!r}")
    if value.get("duplicateResult"):
        errors.append(f"duplicate result: {value['duplicateResult']}")
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
        if isinstance(run, dict) and run.get("status") in HARD_STATES:
            errors.append(f"hard red state {run['status']}")
    return not errors, errors


def inspect_contract_skips(xml_paths: Sequence[Path]) -> tuple[list[tuple[str, str]], list[str]]:
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
    if len(reasons) != 13:
        errors.append(f"contract skip count is {len(reasons)}, expected 13")
    return reasons, errors


def contract_skip_errors(xml_paths: Sequence[Path]) -> list[str]:
    _, errors = inspect_contract_skips(xml_paths)
    return errors


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
        skip_reasons, skip_errors = inspect_contract_skips(xmls)
        errors.extend(skip_errors)
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", required=True)
    args = parser.parse_args(argv)
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
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as output:
            output.write("\n" + "\n".join(report_lines) + "\n")
    return 0 if overall == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
