"""Ten-state classification, ledger reading, and JUnit reconciliation for Windows waves.

Decision (2026-11-26, tier 1 smoke).  Split from run_waves.py to keep every script well
under the 1000-line structural gate.  Ledger field names follow the ledger contract
between the test harness and this runner (collect.errors, crash.reason, non-empty hang-gw<N>.txt, and non-fatal
leak events recorded in manifest.leaks).  JUnit parsing supports both xunit2
(classname-only) and xunit1 (file attribute), rejects duplicate nodeids, and
synthesizes unreported::<path> errors for files still missing after reruns.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

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
EXPECTED_CONTRACT_SKIPS = frozenset(
    {
        "the AF_UNIX backend is POSIX only",
        "POSIX file modes are the contract",
    }
)
WIN_CTRL_C_EXIT_CODES = frozenset({3221225786, -1073741510})
WIN_NATIVE_CRASH_CODES = frozenset(
    {
        3221225477,
        -1073741819,
        3221226505,
        -1073740791,
        3221225725,
        -1073741571,
        3221225501,
        -1073741795,
    }
)


@dataclass(frozen=True)
class JUnitResult:
    nodeids: frozenset[str]
    files: frozenset[str]
    failures: int
    errors: int
    skipped: int
    contract_skips: int
    contract_skip_errors: tuple[str, ...]
    nodeids_by_file: dict[str, tuple[str, ...]]
    parse_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunOutcome:
    status: str
    returncode: int
    reported_files: frozenset[str]
    partial_files: frozenset[str]
    nodeids: frozenset[str]
    nodeids_by_file: dict[str, tuple[str, ...]]
    leaks: tuple[dict[str, Any], ...]
    details: tuple[str, ...]
    offsettable: bool


def is_offsettable(status: str) -> bool:
    return status == "failed"


def normalise_path(value: str) -> str:
    path = value.replace("\\", "/")
    marker = "/tests/"
    if marker in path:
        path = "tests/" + path.split(marker, 1)[1]
    return path.lstrip("./")


def _is_native_crash(code: int) -> bool:
    if code in WIN_NATIVE_CRASH_CODES:
        return True
    if code in WIN_CTRL_C_EXIT_CODES:
        return False
    return 0xC0000000 <= code <= 0xCFFFFFFF


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _resolve_case_location(
    case: ET.Element,
    planned: frozenset[str],
    root: Path | None,
) -> tuple[str | None, str]:
    classname = case.attrib.get("classname", "")
    name = case.attrib.get("name", "")
    raw_file = case.attrib.get("file")
    if raw_file:
        normalised = normalise_path(raw_file)
        if normalised in planned or (root is not None and (root / normalised).is_file()):
            module_prefix = normalised[:-3].replace("/", ".") if normalised.endswith(".py") else ""
            suffix: list[str] = []
            if module_prefix and classname.startswith(module_prefix + "."):
                suffix = classname[len(module_prefix) + 1 :].split(".")
            elif classname and classname != module_prefix and "." not in classname:
                suffix = [classname]
            nodeid = "::".join([normalised, *suffix, name]) if name else normalised
            return normalised, nodeid
    parts = [part for part in classname.split(".") if part]
    for end in range(len(parts), 0, -1):
        candidate = "/".join(parts[:end]) + ".py"
        if candidate in planned or (root is not None and (root / candidate).is_file()):
            suffix = parts[end:]
            nodeid = "::".join([candidate, *suffix, name]) if name else candidate
            return candidate, nodeid
    fallback = f"{classname}::{name}" if classname else name
    return None, fallback


def read_junit(
    paths: Iterable[Path],
    planned_files: Sequence[str],
    *,
    root: Path | None = None,
) -> JUnitResult:
    planned = frozenset(normalise_path(path) for path in planned_files)
    nodeids: set[str] = set()
    files: set[str] = set()
    by_file: dict[str, list[str]] = {}
    failures = 0
    errors = 0
    skipped = 0
    contract_skips = 0
    contract_skip_errors: list[str] = []
    parse_errors: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            xml_root = ET.parse(path).getroot()
        except ET.ParseError as error:
            parse_errors.append(f"{path.name}: {error}")
            continue
        for case in xml_root.iter("testcase"):
            resolved_file, nodeid = _resolve_case_location(case, planned, root)
            if nodeid in nodeids:
                raise ValueError(f"duplicate-result: {nodeid}")
            nodeids.add(nodeid)
            if resolved_file is not None and resolved_file in planned:
                files.add(resolved_file)
                by_file.setdefault(resolved_file, []).append(nodeid)
            if case.find("failure") is not None:
                failures += 1
            if case.find("error") is not None:
                errors += 1
            skipped_node = case.find("skipped")
            if skipped_node is not None:
                skipped += 1
                if resolved_file == CONTRACT_TEST_FILE:
                    contract_skips += 1
                    reason = skipped_node.attrib.get("message", "")
                    if reason not in EXPECTED_CONTRACT_SKIPS:
                        contract_skip_errors.append(reason)
    return JUnitResult(
        frozenset(nodeids),
        frozenset(files),
        failures,
        errors,
        skipped,
        contract_skips,
        tuple(contract_skip_errors),
        {key: tuple(values) for key, values in by_file.items()},
        tuple(parse_errors),
    )


def _ledger_events(ledger_dir: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not ledger_dir.exists():
        return events
    for path in sorted(ledger_dir.glob("*.jsonl")):
        if path.name == "interceptor-hits.jsonl":
            continue
        events.extend(read_jsonl(path))
    return events


def _junit_has_suite_error(paths: Iterable[Path]) -> bool:
    for path in paths:
        if not path.exists():
            continue
        try:
            xml_root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        for suite in xml_root.iter("testsuite"):
            if any(child.tag == "error" for child in suite):
                return True
        for case in xml_root.iter("testcase"):
            error = case.find("error")
            if error is not None and "collection failure" in error.attrib.get("message", "").lower():
                return True
    return False


def _partial_files_from_events(
    events: Sequence[dict[str, Any]],
    planned: frozenset[str],
) -> frozenset[str]:
    started: set[str] = set()
    ended: set[str] = set()
    for event in events:
        kind = event.get("event")
        nodeid = event.get("nodeid")
        if not isinstance(nodeid, str) or not nodeid:
            continue
        if kind == "start":
            started.add(nodeid)
        elif kind == "end":
            ended.add(nodeid)
    partial: set[str] = set()
    for nodeid in started - ended:
        file_part = normalise_path(nodeid.split("::", 1)[0])
        if file_part in planned:
            partial.add(file_part)
    return frozenset(partial)


def _ledger_leaks(events: Sequence[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    leaks: list[dict[str, Any]] = []
    for event in events:
        if event.get("event") == "leak":
            leaks.append(
                {
                    "worker": event.get("worker"),
                    "threads": event.get("threads", []),
                }
            )
    return tuple(leaks)


def _collection_errors(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    collected: list[dict[str, Any]] = []
    for event in events:
        if event.get("event") != "collect":
            continue
        for error in event.get("errors", []):
            if not isinstance(error, dict):
                continue
            nodeid = str(error.get("nodeid", ""))
            if nodeid in seen:
                continue
            seen.add(nodeid)
            collected.append(error)
    return collected


def classify_run(
    *,
    returncode: int,
    junit_paths: Iterable[Path],
    ledger_dir: Path,
    planned_files: Sequence[str],
    root: Path | None = None,
    forced_status: str | None = None,
) -> RunOutcome:
    junit_list = list(junit_paths)
    planned = frozenset(normalise_path(path) for path in planned_files)
    result = read_junit(junit_list, planned_files, root=root)
    events = _ledger_events(ledger_dir)
    crash_events = [event for event in events if event.get("event") == "crash"]
    hang_files = (
        [path for path in sorted(ledger_dir.glob("hang-*.txt")) if path.stat().st_size > 0]
        if ledger_dir.exists()
        else []
    )
    interceptor_hits = read_jsonl(ledger_dir / "interceptor-hits.jsonl")
    collection_errors = _collection_errors(events)
    partial_files = _partial_files_from_events(events, planned)
    details: list[str] = []
    if forced_status in HARD_STATES:
        status = forced_status
        details.append(f"forced:{forced_status}")
    elif hang_files or any(event.get("reason") == "test-timeout" for event in crash_events) or returncode == 96:
        details.extend(f"hang:{path.name}" for path in hang_files)
        status = "test-watchdog-timeout"
    elif any(event.get("reason") == "leak-timeout" for event in crash_events) or returncode == 97:
        status = "worker-crash"
    elif any(
        isinstance(event.get("exit_code"), int) and _is_native_crash(int(event["exit_code"]))
        for event in crash_events
    ) or _is_native_crash(returncode):
        status = "native-worker-crash"
    elif (
        interceptor_hits
        or any(
            event.get("exit_code") in {15, 2, *WIN_CTRL_C_EXIT_CODES}
            for event in crash_events
        )
        or returncode in {15, *WIN_CTRL_C_EXIT_CODES}
    ):
        if interceptor_hits:
            details.append(f"interceptor-hits:{len(interceptor_hits)}")
        status = "worker-self-terminated"
    elif crash_events:
        details.extend(f"crash:{event.get('reason', 'unknown')}" for event in crash_events)
        status = "worker-crash"
    elif collection_errors or _junit_has_suite_error(junit_list):
        details.extend(
            f"collection:{error.get('nodeid', '')}:{error.get('summary', '')}"
            for error in collection_errors
        )
        status = "collection-error"
    elif returncode == 2:
        status = "worker-self-terminated"
    elif returncode not in {0, 1, 5}:
        details.append(f"unexpected-exit:{returncode}")
        status = "worker-crash"
    elif result.parse_errors or partial_files or result.files != planned:
        details.extend(f"junit-parse:{error}" for error in result.parse_errors)
        missing = sorted(planned - result.files)
        if missing:
            details.append(f"unreported:{len(missing)}")
        if partial_files:
            details.append(f"partial:{len(partial_files)}")
        status = "unreported-files"
    elif returncode != 0 or result.failures or result.errors:
        status = "failed"
    else:
        status = "passed"
    if result.contract_skip_errors:
        details.extend(f"invalid-contract-skip:{reason}" for reason in result.contract_skip_errors)
        if status == "passed":
            status = "failed"
    if (
        CONTRACT_TEST_FILE in planned
        and CONTRACT_TEST_FILE in result.files
        and CONTRACT_TEST_FILE not in partial_files
        and result.contract_skips != 13
    ):
        details.append(f"contract-skips:{result.contract_skips}")
        if status == "passed":
            status = "failed"
    return RunOutcome(
        status,
        returncode,
        result.files,
        partial_files,
        result.nodeids,
        result.nodeids_by_file,
        _ledger_leaks(events),
        tuple(details),
        is_offsettable(status),
    )


def synthesize_unreported(path: Path, missing: Sequence[str]) -> None:
    xml_root = ET.Element("testsuites", tests=str(len(missing)), errors=str(len(missing)))
    suite = ET.SubElement(
        xml_root,
        "testsuite",
        name="hyprial.ci.windows_watchdog",
        tests=str(len(missing)),
        errors=str(len(missing)),
    )
    for file_name in missing:
        case = ET.SubElement(
            suite,
            "testcase",
            classname="hyprial.ci.windows_watchdog",
            name=f"unreported::{file_name}",
        )
        ET.SubElement(case, "error", message="file was not reported by pytest")
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(xml_root).write(path, encoding="utf-8", xml_declaration=True)
