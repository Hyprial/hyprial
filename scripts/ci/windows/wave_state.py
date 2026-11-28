"""Ten-state classification, ledger reading, and JUnit reconciliation for Windows waves.

Decision (2026-11-26, tier 1 smoke and tier 2 full suite).  Split from run_waves.py
to keep every script well under the 1000-line structural gate.  Ledger field names
follow the ledger contract between the test harness and this runner (collect.errors,
crash.reason, non-empty hang-gw<N>.txt without a "hyprial watchdog: leak-timeout"
header, and non-fatal leak events recorded in manifest.leaks).  Non-ledger JSONL
side files (interceptor-hits.jsonl and acl-probe.jsonl) are excluded from ledger
event streams.  JUnit parsing supports both xunit2 (classname-only) and xunit1
(file attribute), rejects duplicate nodeids, and synthesizes unreported::<path>
errors for files still missing after reruns.  Tier 1 enforces strict contract and
non-contract skip rules; tier 2 keeps full ten-state classification without failing
on legitimate conditional skips.

Decision (2026-11-27, tier 2 full suite).  A module skipped at collection time (for
example a module-level pytest.importorskip) produces one testcase with an empty
classname and the dotted module path as name, so it cannot be attributed through
classname.  Such a testcase marks its planned file as reported only when all four
hold: classname is empty, name equals the dotted module path of exactly one planned
file, it has a skipped child, and it has no failure child and no error child (any
number of error children rejects it).  The file is reported through
collection_skipped_files, never through nodeids_by_file, so the ledger end-event
check and rerun deselection are unaffected.  Tier 1 still lists it as an
unexpected skip.  A collection error keeps its "collection failure" error child and
stays a hard red.

Decision (2026-11-27, tier 2 full suite).  pytest 8.4 with xdist 3.8 writes a test
whose call fails and whose teardown then raises as two testcases with the same name
in the same XML file: the first with one failure child, the second with one error
child.  Inside one XML file such a pair (exactly one failure-only case and exactly
one error-only case, neither skipped) is merged into a single failed case.  Any
other repeated name, any third occurrence, and any repeat across XML files is still
rejected as duplicate-result.  summarize.py applies the same rule and keeps both
messages.

Decision (2026-11-27, tier 2 full suite).  pytest 8.4.2 exits 5 when it collected no
test.  That is the exit code of a run whose only planned files are skipped at
collection time, which the four conditions above already report, so exit code 5 is
passed only when the planned set is not empty and every planned file is in
collection_skipped_files.  Measured on pytest 8.4.2 without xdist: a collection-time
skip next to a passing test exits 0, next to a failing test exits 1, and next to a
file that collects nothing exits 5.  The last shape never reaches this condition:
the file that collects nothing is missing from the reported files, so the
unreported-files branch has already ended the classification.  The exemption covers
the exit code only: a testcase holding a failure or an error child still fails the
run whatever the exit code is, including a testcase that resolves to no planned file
and therefore counts in neither the reported files nor collection_skipped_files.

Decision (2026-11-27, tier 2 full suite).  A testcase element can carry neither a
classname nor a name and hold nothing but a time attribute.  It names no test, so
it is not a result.  Resolving it used to give one empty nodeid, so the second
such record in a run raised duplicate-result with an empty nodeid and the wave
loop stopped every later wave.  A nameless testcase is now counted per run as
nameless-testcases:N before any resolution: it enters neither the nodeid set nor
duplicate detection, and it counts as neither a pass nor a failure.  It never
turns a run green.  A run that is already in a hard state keeps that state and
gains the detail only.  A run that would otherwise pass or be failed is
unreported-files instead.  The failed branch matters as much as the passed one:
failed is the only state a list of expected failures can offset, so a run with
a real failure and a nameless record would otherwise turn green in the summary
without one line naming the nameless record.
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
    other_skips: tuple[tuple[str, str], ...] = ()
    collection_skipped_files: frozenset[str] = frozenset()
    nameless_testcases: int = 0


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
    collection_skipped_files: frozenset[str] = frozenset()
    nameless_testcases: int = 0


def is_offsettable(status: str) -> bool:
    return status == "failed"


def normalise_path(value: str) -> str:
    path = value.replace("\\", "/")
    marker = "/tests/"
    if marker in path:
        path = "tests/" + path.split(marker, 1)[1]
    return path.lstrip("./")


def _normalise_nodeid(nodeid: str) -> str:
    if "::" not in nodeid:
        return normalise_path(nodeid)
    file_part, rest = nodeid.split("::", 1)
    return f"{normalise_path(file_part)}::{rest}"


def _is_leak_timeout_hang(path: Path) -> bool:
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        first_line = stream.readline()
    return "hyprial watchdog: leak-timeout" in first_line


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


def _planned_module_names(planned: frozenset[str]) -> dict[str, str]:
    by_name: dict[str, str] = {}
    collisions: set[str] = set()
    for path in planned:
        if not path.endswith(".py"):
            continue
        dotted = path[:-3].replace("/", ".")
        if dotted in by_name:
            collisions.add(dotted)
        by_name[dotted] = path
    return {dotted: path for dotted, path in by_name.items() if dotted not in collisions}


def _collection_skipped_file(case: ET.Element, module_names: dict[str, str]) -> str | None:
    if case.attrib.get("classname", ""):
        return None
    if case.find("skipped") is None:
        return None
    if case.find("failure") is not None or case.find("error") is not None:
        return None
    return module_names.get(case.attrib.get("name", ""))


def _is_nameless_case(case: ET.Element) -> bool:
    return not case.attrib.get("classname", "") and not case.attrib.get("name", "")


def single_outcome_kind(case: ET.Element) -> str | None:
    failures = len(case.findall("failure"))
    errors = len(case.findall("error"))
    if case.find("skipped") is not None:
        return None
    if (failures, errors) == (1, 0):
        return "failure"
    if (failures, errors) == (0, 1):
        return "error"
    return None


def read_junit(
    paths: Iterable[Path],
    planned_files: Sequence[str],
    *,
    root: Path | None = None,
) -> JUnitResult:
    planned = frozenset(normalise_path(path) for path in planned_files)
    module_names = _planned_module_names(planned)
    nodeids: set[str] = set()
    files: set[str] = set()
    collection_skipped: set[str] = set()
    by_file: dict[str, list[str]] = {}
    failures = 0
    errors = 0
    skipped = 0
    contract_skips = 0
    contract_skip_errors: list[str] = []
    other_skips: list[tuple[str, str]] = []
    parse_errors: list[str] = []
    nameless = 0
    for path in paths:
        if not path.exists():
            continue
        try:
            xml_root = ET.parse(path).getroot()
        except ET.ParseError as error:
            parse_errors.append(f"{path.name}: {error}")
            continue
        pair_candidates: dict[str, str] = {}
        for case in xml_root.iter("testcase"):
            if _is_nameless_case(case):
                nameless += 1
                continue
            skipped_module = _collection_skipped_file(case, module_names)
            if skipped_module is not None:
                if skipped_module in nodeids:
                    raise ValueError(f"duplicate-result: {skipped_module}")
                nodeids.add(skipped_module)
                files.add(skipped_module)
                collection_skipped.add(skipped_module)
                skipped += 1
                skipped_node = case.find("skipped")
                reason = "" if skipped_node is None else skipped_node.attrib.get("message", "")
                other_skips.append((skipped_module, reason))
                continue
            resolved_file, nodeid = _resolve_case_location(case, planned, root)
            kind = single_outcome_kind(case)
            if nodeid in nodeids:
                previous = pair_candidates.pop(nodeid, None)
                if previous is None or kind is None or {previous, kind} != {"failure", "error"}:
                    raise ValueError(f"duplicate-result: {nodeid}")
                if previous == "error":
                    errors -= 1
                    failures += 1
                continue
            nodeids.add(nodeid)
            if kind is not None:
                pair_candidates[nodeid] = kind
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
                reason = skipped_node.attrib.get("message", "")
                if resolved_file == CONTRACT_TEST_FILE:
                    contract_skips += 1
                    if reason not in EXPECTED_CONTRACT_SKIPS:
                        contract_skip_errors.append(reason)
                elif resolved_file is not None and resolved_file in planned:
                    other_skips.append((nodeid, reason))
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
        tuple(other_skips),
        frozenset(collection_skipped),
        nameless,
    )


def _ledger_events(ledger_dir: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not ledger_dir.exists():
        return events
    for path in sorted(ledger_dir.glob("*.jsonl")):
        if path.name in {"interceptor-hits.jsonl", "acl-probe.jsonl"}:
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
    strict_skips: bool = True,
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
    elif any(event.get("reason") == "leak-timeout" for event in crash_events) or returncode == 97:
        details.append("crash:leak-timeout")
        status = "worker-crash"
    elif any(_is_leak_timeout_hang(path) for path in hang_files):
        details.append("leak-timeout-fallback")
        status = "worker-crash"
    elif hang_files or any(event.get("reason") == "test-timeout" for event in crash_events) or returncode == 96:
        details.extend(f"hang:{path.name}" for path in hang_files)
        status = "test-watchdog-timeout"
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
    elif not ledger_dir.exists() or not any(event.get("event") == "collect" for event in events):
        details.append("ledger-missing")
        status = "unreported-files"
    else:
        reported_nodeids = {
            _normalise_nodeid(event["nodeid"])
            for event in events
            if event.get("event") == "end"
            and isinstance(event.get("nodeid"), str)
            and event["nodeid"]
        }
        planned_nodeids = {
            _normalise_nodeid(nodeid)
            for values in result.nodeids_by_file.values()
            for nodeid in values
        }
        uncovered = planned_nodeids - reported_nodeids
        if uncovered:
            details.append(f"ledger-uncovered:{len(uncovered)}")
            status = "unreported-files"
        elif (
            returncode != 0
            and not (returncode == 5 and planned and planned <= result.collection_skipped_files)
        ) or result.failures or result.errors:
            status = "failed"
        else:
            status = "passed"
    if result.nameless_testcases:
        details.append(f"nameless-testcases:{result.nameless_testcases}")
        if status not in HARD_STATES:
            status = "unreported-files"
    if strict_skips:
        if result.contract_skip_errors:
            details.extend(
                f"invalid-contract-skip:{reason}" for reason in result.contract_skip_errors
            )
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
        if result.other_skips:
            details.extend(
                f"unexpected-skip:{nodeid}:{reason}" for nodeid, reason in result.other_skips
            )
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
        result.collection_skipped_files,
        result.nameless_testcases,
    )


def merge_wave_status(current: str, incoming: str) -> str:
    if incoming in HARD_STATES:
        return incoming
    if incoming == "failed" and current not in HARD_STATES:
        return "failed"
    return current


def load_planned_waves(
    files_path: Path | None,
    plan_manifest: Path | None,
    shard: int | None,
) -> list[list[str]]:
    if files_path is not None and plan_manifest is not None:
        raise ValueError("pass either --files or --plan-manifest, not both")
    if plan_manifest is not None:
        raw = json.loads(plan_manifest.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("plan manifest must be a JSON object")
        raw_waves = raw.get("waves")
        if not isinstance(raw_waves, list) or not raw_waves:
            raise ValueError("plan manifest has no waves")
        index = 0 if shard is None else int(shard)
        if index < 0 or index >= len(raw_waves):
            raise ValueError(f"shard index {index} out of range for {len(raw_waves)} shards")
        shard_waves = raw_waves[index]
        if not isinstance(shard_waves, list):
            raise ValueError(f"shard {index} waves must be a list")
        waves: list[list[str]] = []
        seen: set[str] = set()
        for wave in shard_waves:
            if not isinstance(wave, list):
                raise ValueError(f"shard {index} contains non-list wave")
            items: list[str] = []
            for entry in wave:
                normalised = normalise_path(str(entry).strip())
                if not normalised:
                    continue
                if normalised in seen:
                    raise ValueError(f"duplicate test path across waves: {normalised}")
                seen.add(normalised)
                items.append(normalised)
            if items:
                waves.append(items)
        if not waves:
            raise ValueError(f"planned test file list is empty for shard {index}")
        return waves
    if files_path is None:
        raise ValueError("either --files or --plan-manifest is required")
    planned = [
        normalise_path(line.strip())
        for line in files_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    planned = sorted(dict.fromkeys(planned))
    if not planned:
        raise ValueError("planned test file list is empty")
    return [planned]


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
