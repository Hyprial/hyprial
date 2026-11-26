"""Static gates for the Windows test registries under ``scripts/ci/windows``.

Decision (2026-11-26): one gate file for every data file that decides what a
Windows run does not execute.  ``posix-only.json`` names whole directories and
whole files that are not collected on Windows, and the ``posix_only`` marker
skips single test cases there.  Both take tests out of a Windows run, so both
are checked here against the tree they describe: a registered path must
exist, its stated reason must be visible in the source lines it cites, and
every marker under ``tests/`` outside this file must name a reason from the
locked list.

Decision (same date): the four initial registry entries are listed here by
path, kind and reason.  A directory-level or file-level entry changes how
many tests a Windows run never sees, so it is a decision to make deliberately:
the gate turns red on an unlisted entry, on a changed reason, and on an entry
removed without updating this file alike.

Decision (same date): the list of allowed reasons is closed and lives in the
data file, and ``killpg`` is not on it under any circumstances.  Every check
that accepts a reason goes through ``reason_problems``, which refuses
``killpg`` by name even when a registry lists it as allowed, so no caller can
reach it through the data file.

Decision (same date): the plugin is exercised by calling its two hooks with
``tests.support.windows_suite.host.is_windows`` replaced, never by starting a
pytest run and never by changing ``os.name``: ``pathlib`` dispatches on
``os.name`` and would refuse to build a path.  Bad registries are written to
``tmp_path`` and loaded through ``load_posix_only(path)``, which is why that
function takes its path as an argument.

Decision (same date): pytest does not ask ``pytest_ignore_collect`` about a
path that was given on the command line, and the Windows runner hands pytest
a list of test files.  A registered path that appears in a Windows test list
would therefore be collected and fail there, so the gate refuses any
registered path that a test list under ``scripts/ci/windows`` names, at the
file itself or anywhere below a registered directory.

Decision (same date): this file is not scanned for ``posix_only`` markers.
It calls the marker to build test input for the plugin, including shapes
that are wrong on purpose, so the suite-wide marker check and the marker
count skip it and judge only the test files that carry a marker.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.support.windows_suite import collection, host

ROOT = Path(__file__).resolve().parents[4]
REGISTRIES = ROOT / "scripts/ci/windows"
POSIX_ONLY = REGISTRIES / "posix-only.json"
EXPECTED_FAILURES = REGISTRIES / "expected-failures.json"
KNOWN_PROBLEMS = ROOT / "docs/reference/known-problems.md"
TESTS = ROOT / "tests"
TEST_LIST = re.compile(r"tier\d+[\w-]*\.(?:txt|tsv)$")
KNOWN_ID = r"KP-\d+[a-z]?"
TEST_FUNCTION = r"test\w*"
TEST_CLASS = r"Test\w*"
MARKER_DECLARATION = "posix_only(reason): test exercises POSIX-only OS semantics and is skipped on Windows"

MODULE_REASONS = ("fcntl", "pty", "termios", "resource", "pwd", "grp")
REFUSED_REASONS = ("killpg",)
LOCKED_REASONS = (
    "fcntl",
    "pty",
    "termios",
    "resource",
    "pwd",
    "grp",
    "/proc",
    "SIGHUP",
    "SIGUSR1",
    "SIGUSR2",
    "SIGTERM-exit-143",
    "macos-bundle",
    "AF_UNIX-path",
)
REASON_PATTERNS = {
    **{name: rf"^\s*(?:import {name}\b|from {name}\s+import\b)" for name in MODULE_REASONS},
    "/proc": r"/proc/|/dev/fd",
    "SIGHUP": r"signal\.SIGHUP\b",
    "SIGUSR1": r"signal\.SIGUSR1\b",
    "SIGUSR2": r"signal\.SIGUSR2\b",
    "SIGTERM-exit-143": r"signal\.SIGTERM\b|\b143\b",
    "macos-bundle": r"signal\.SIGHUP\b|Contents/MacOS",
    "AF_UNIX-path": r'AF_UNIX|dir="/tmp"',
}
KINDS = ("dir", "file")
SMOKE = "tests/tooling/release/shell/daemon/test_smoke_"
APPROVED_ENTRIES = [
    ("tests/tooling/release/shell/signing", "dir", ["pwd"]),
    (f"{SMOKE}cleanup.py", "file", ["macos-bundle"]),
    (f"{SMOKE}daemon.py", "file", ["macos-bundle"]),
    (f"{SMOKE}interrupt.py", "file", ["macos-bundle"]),
]
SCRIPT_NEEDS = {
    "with-machine-lock.py": "fcntl",
    "run-lark-app-onboarding.py": "pty",
    "run-real-harness-loopback.py": "pty",
}
CITED = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:py|json|toml|md|yml|yaml)):(\d+)"
)
BARE_LINE = re.compile(r"(?<![\w:])(?<!\d):(\d+)\b")


def document(path: Path) -> Any:
    """The JSON document at ``path``."""

    return json.loads(path.read_text(encoding="utf-8"))


def reason_problems(reason: object, allowed: object) -> list[str]:
    """Why ``reason`` is not a usable ``posix_only`` reason under ``allowed``."""

    if not isinstance(reason, str) or not reason.strip():
        return [f"reason {reason!r} is not a non-empty string"]
    problems = []
    if reason in REFUSED_REASONS:
        problems.append(f"reason {reason!r} is refused by name")
    if reason not in LOCKED_REASONS:
        problems.append(f"reason {reason!r} is not one of the locked reasons")
    if not isinstance(allowed, (list, tuple)) or reason not in allowed:
        problems.append(f"reason {reason!r} is not in allowed_needs")
    return problems


def cited_lines(evidence: str) -> list[tuple[str, int]]:
    """Every ``path:line`` in ``evidence``; a bare ``:line`` is returned as ``("", line)``."""

    cited = [(match.group(1), int(match.group(2))) for match in CITED.finditer(evidence)]
    spans = [match.span() for match in CITED.finditer(evidence)]
    for match in BARE_LINE.finditer(evidence):
        if not any(start <= match.start() and match.end() <= end for start, end in spans):
            cited.append(("", int(match.group(1))))
    return cited


def evidence_problems(needs: list[str], evidence: str, root: Path) -> list[str]:
    """Why ``evidence`` does not show, on lines it cites by full path, each need of its entry."""

    cited = cited_lines(evidence)
    if not cited or all(path == "" for path, _ in cited):
        return ["evidence cites no path:line"]
    problems = [
        f"evidence cites a line without naming its file: :{number}"
        for path, number in cited
        if path == ""
    ]
    lines: dict[tuple[str, int], str] = {}
    for cited_path, number in cited:
        if cited_path == "":
            continue
        if number < 1:
            problems.append(f"evidence cites a line number below 1: {cited_path}:{number}")
            continue
        try:
            lines[(cited_path, number)] = (root / cited_path).read_text(encoding="utf-8").splitlines()[number - 1]
        except (OSError, IndexError):
            problems.append(f"evidence cites a line that does not exist: {cited_path}:{number}")
    for need in needs:
        pattern = REASON_PATTERNS.get(need) if isinstance(need, str) else None
        if pattern is not None and not any(re.search(pattern, line) for line in lines.values()):
            problems.append(f"no cited line shows the stated need {need!r}")
    return problems


def entry_problems(
    path: object,
    kind: object,
    needs: object,
    evidence: object,
    allowed: object,
    root: Path,
) -> list[str]:
    """Why one registry entry is not a valid directory or file registration under ``root``."""

    problems: list[str] = []
    if kind not in KINDS:
        problems.append(f"kind {kind!r} is not one of {KINDS}")
    checked_needs = needs if isinstance(needs, list) else []
    if not checked_needs or not all(isinstance(need, str) for need in checked_needs):
        problems.append(f"needs {needs!r} is not a non-empty list of strings")
    for need in checked_needs:
        problems.extend(reason_problems(need, allowed))
    stated = evidence if isinstance(evidence, str) and evidence.strip() else ""
    if not stated:
        problems.append(f"evidence {evidence!r} is not a non-empty string")
    if not isinstance(path, str) or not path.strip():
        problems.append(f"path {path!r} is not a non-empty string")
        return problems
    if path.startswith("/") or ".." in Path(path).parts or not path.startswith("tests/"):
        problems.append(f"path {path!r} is not a repository-relative path under tests/")
        return problems
    target = root / path
    if kind == "dir" and not target.is_dir():
        problems.append(f"path {path!r} is not a directory")
    if kind == "file" and not target.is_file():
        problems.append(f"path {path!r} is not a file")
    if checked_needs and stated:
        problems.extend(evidence_problems(checked_needs, stated, root))
    return problems


def registry_problems(value: object, *, root: Path = ROOT) -> list[str]:
    """Every problem of a ``posix-only.json`` document, checked against the tree at ``root``."""

    if not isinstance(value, dict):
        return ["registry is not an object"]
    problems = []
    if value.get("version") != 1:
        problems.append(f"version {value.get('version')!r} is not 1")
    allowed = value.get("allowed_needs")
    if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
        problems.append("allowed_needs is not a list of strings")
        allowed = []
    if len(set(allowed)) != len(allowed):
        problems.append("allowed_needs has duplicates")
    if tuple(allowed) != LOCKED_REASONS:
        problems.append(f"allowed_needs {allowed!r} is not the locked list of 13 reasons")
    for refused in REFUSED_REASONS:
        if refused in allowed:
            problems.append(f"allowed_needs contains the refused reason {refused!r}")
    entries = value.get("entries")
    if not isinstance(entries, list):
        return [*problems, "entries is not a list"]
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"entries[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{where} is not an object")
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path.strip():
            problems.append(f"{where} path {path!r} is not a non-empty string")
            continue
        normalised = path.rstrip("/")
        found = entry_problems(
            normalised,
            entry.get("kind"),
            entry.get("needs"),
            entry.get("evidence"),
            allowed,
            root,
        )
        if normalised in seen:
            found.append(f"path {normalised!r} is already registered")
        seen.add(normalised)
        problems.extend(f"{where}: {problem}" for problem in found)
    return problems


def registered(value: object) -> list[tuple[str, str]]:
    """``[(path, kind), ...]`` of a registry document; malformed entries are skipped."""

    entries = value.get("entries") if isinstance(value, dict) else None
    if not isinstance(entries, list):
        return []
    return [
        (str(entry.get("path")).rstrip("/"), kind)
        for entry in entries
        if isinstance(entry, dict) and (kind := entry.get("kind")) in KINDS and isinstance(entry.get("path"), str)
    ]


def covers(registered_path: str, kind: str, candidate: str) -> bool:
    """Whether the registration ``registered_path`` of ``kind`` covers ``candidate``."""

    if kind == "dir":
        return candidate == registered_path or candidate.startswith(registered_path + "/")
    return candidate == registered_path


def list_problems(value: object, directory: Path, *, root: Path = ROOT) -> list[str]:
    """Why a Windows test list under ``directory`` names a path the registry keeps out."""

    problems = []
    lists = sorted(path for path in directory.glob("*") if path.is_file() and TEST_LIST.match(path.name))
    for path in lists:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            name = line.split("\t")[0].strip()
            if not name or name.startswith("#"):
                continue
            for registered_path, kind in registered(value):
                if covers(registered_path, kind, name):
                    problems.append(f"{path.relative_to(root).as_posix()}:{number} names the registered {kind} {registered_path!r}")
    return problems


def string_mappings(tree: ast.Module) -> dict[str, dict[str, str]]:
    """Module-level ``NAME = {literal: literal, ...}`` assignments, as plain dictionaries."""

    mappings: dict[str, dict[str, str]] = {}
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        pairs: dict[str, str] = {}
        for key, value in zip(node.value.keys, node.value.values):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                break
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                break
            pairs[key.value] = value.value
        else:
            mappings[node.targets[0].id] = pairs
    return mappings


def mapping_lookups(tree: ast.AST) -> dict[str, str]:
    """``{local name: mapping name}`` for every ``name = MAPPING.get(...)`` assignment."""

    lookups: dict[str, str] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
            continue
        call = node.value
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "get"):
            continue
        if isinstance(call.func.value, ast.Name):
            lookups[node.targets[0].id] = call.func.value.id
    return lookups


def marker_problems(tree: ast.Module) -> list[tuple[int, str]]:
    """``(line, problem)`` for every ``posix_only(...)`` call whose reason is not a known valid one."""

    mappings = string_mappings(tree)
    lookups = mapping_lookups(tree)
    problems: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not (isinstance(function, ast.Attribute) and function.attr == "posix_only"):
            continue
        owner = function.value
        if not (
            isinstance(owner, ast.Attribute)
            and owner.attr == "mark"
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "pytest"
        ):
            problems.append((node.lineno, f"posix_only is called on {ast.unparse(owner)}, not on pytest.mark"))
            continue
        if node.keywords or len(node.args) != 1:
            problems.append((node.lineno, "posix_only takes exactly one positional reason"))
            continue
        argument = node.args[0]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            problems.extend((node.lineno, problem) for problem in reason_problems(argument.value, LOCKED_REASONS))
        elif isinstance(argument, ast.Name) and argument.id in lookups:
            mapping = mappings.get(lookups[argument.id])
            if mapping is None:
                problems.append(
                    (
                        node.lineno,
                        f"reason {argument.id!r} is read from {lookups[argument.id]!r}, which is not a module-level dictionary of string literals",
                    )
                )
            else:
                for value in dict.fromkeys(mapping.values()):
                    problems.extend((node.lineno, problem) for problem in reason_problems(value, LOCKED_REASONS))
        else:
            problems.append((node.lineno, f"reason is {ast.unparse(argument)}, which is not a string literal or a mapping lookup"))
    return problems


GATE = Path(__file__).resolve()


def suite_marker_problems(tests: Path = TESTS) -> list[tuple[str, int, str]]:
    """``(file, line, problem)`` for every ``posix_only`` marker under ``tests`` that a test file carries."""

    found: list[tuple[str, int, str]] = []
    for path in sorted(tests.rglob("*.py")):
        if path == GATE:
            continue
        source = path.read_text(encoding="utf-8")
        if "posix_only(" not in source:
            continue
        name = path.relative_to(ROOT).as_posix()
        found.extend((name, line, problem) for line, problem in marker_problems(ast.parse(source, filename=str(path))))
    return found


def known_problem_ids(text: str) -> set[str]:
    """Every ``KP-nnn`` that ``docs/reference/known-problems.md`` lists as a section."""

    return set(re.findall(rf"^## ({KNOWN_ID})(?![^\s])", text, re.MULTILINE))


def nodeid_problems(nodeid: str, root: Path) -> list[str]:
    """Why ``file::Class::test_name[param]`` does not name a test of the tree at ``root``.

    Each ``::`` segment must be defined one level deeper than the one before, and
    the last one must be a ``test...`` function; the text after ``[`` is a
    parameter id that pytest builds at run time and is not checked.
    """

    file_name, separator, tail = nodeid.partition("::")
    if not separator or not tail:
        return [f"nodeid {nodeid!r} is not a file path followed by :: and a test name"]
    target = root / file_name
    if not target.is_file():
        return [f"nodeid file {file_name!r} does not exist"]
    body = ast.parse(target.read_text(encoding="utf-8"), filename=str(target)).body
    parts = tail.split("::")
    problems = []
    for position, part in enumerate(parts):
        last = position == len(parts) - 1
        name = part.split("[", 1)[0]
        expected = TEST_FUNCTION if last else TEST_CLASS
        if re.fullmatch(expected, name) is None:
            problems.append(f"nodeid names {name!r}, which is not a {'test function' if last else 'test class'}")
            break
        if not last and "[" in part:
            problems.append(f"nodeid {nodeid!r} parametrizes a class, not a test")
            break
        kinds: tuple[type[ast.stmt], ...] = (ast.FunctionDef, ast.AsyncFunctionDef) if last else (ast.ClassDef,)
        found = [node for node in body if isinstance(node, kinds) and node.name == name]
        if not found:
            where = "the file" if position == 0 else f"class {parts[position - 1]!r}"
            problems.append(f"nodeid names {name!r}, which is not defined in {where}")
            break
        if not last:
            body = found[0].body
    return problems


def expected_failure_problems(value: object, known: str, root: Path) -> list[str]:
    """Every problem of an ``expected-failures.json`` document against ``known`` and the tree at ``root``."""

    if not isinstance(value, dict):
        return ["registry is not an object"]
    problems = []
    if value.get("version") != 1:
        problems.append(f"version {value.get('version')!r} is not 1")
    entries = value.get("entries")
    if not isinstance(entries, list):
        return [*problems, "entries is not a list"]
    listed = known_problem_ids(known)
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"entries[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{where} is not an object")
            continue
        kp = entry.get("kp")
        if not isinstance(kp, str) or not re.fullmatch(KNOWN_ID, kp):
            problems.append(f"{where}: kp {kp!r} is not a known-problem id")
        elif kp not in listed:
            problems.append(f"{where}: kp {kp!r} is not a section of known-problems.md")
        nodeid = entry.get("nodeid")
        if not isinstance(nodeid, str):
            problems.append(f"{where}: nodeid {nodeid!r} is not a string")
            continue
        problems.extend(f"{where}: {problem}" for problem in nodeid_problems(nodeid, root))
        if nodeid in seen:
            problems.append(f"{where}: nodeid {nodeid!r} is already expected")
        seen.add(nodeid)
    return problems


def approved_entries(value: object) -> list[tuple[object, object, object]]:
    """``[(path, kind, needs), ...]`` of a registry document."""

    entries = value.get("entries") if isinstance(value, dict) else None
    if not isinstance(entries, list):
        return []
    return [(entry.get("path"), entry.get("kind"), entry.get("needs")) for entry in entries if isinstance(entry, dict)]


def script_imports(path: Path) -> set[str]:
    """Top-level modules a script imports, by ``import x`` or ``from x import``."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = {alias.name.split(".")[0] for node in tree.body if isinstance(node, ast.Import) for alias in node.names}
    names |= {(node.module or "").split(".")[0] for node in tree.body if isinstance(node, ast.ImportFrom) and node.level == 0}
    return names


def windows_test_lists(directory: Path = REGISTRIES) -> list[Path]:
    """Windows test lists under ``directory``: one repository-relative test path per line."""

    return sorted(path for path in directory.glob("*") if path.is_file() and TEST_LIST.match(path.name))


def listed_test_paths(path: Path) -> list[str]:
    """The test paths a Windows test list names, without comments and without extra columns."""

    return [
        line.split("\t")[0].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_the_allowed_reasons_are_the_locked_thirteen_and_never_killpg() -> None:
    value = document(POSIX_ONLY)
    assert value["version"] == 1
    assert tuple(value["allowed_needs"]) == LOCKED_REASONS
    assert len(LOCKED_REASONS) == 13
    assert len(set(LOCKED_REASONS)) == 13
    assert set(REFUSED_REASONS).isdisjoint(LOCKED_REASONS)
    assert set(REFUSED_REASONS).isdisjoint(value["allowed_needs"])
    assert set(REASON_PATTERNS) == set(LOCKED_REASONS)


def test_the_registry_names_exactly_the_four_approved_entries() -> None:
    value = document(POSIX_ONLY)
    assert approved_entries(value) == APPROVED_ENTRIES
    assert len(value["entries"]) == 4


def test_no_registered_path_is_a_test_list_path_or_under_a_registered_directory() -> None:
    value = document(POSIX_ONLY)
    assert windows_test_lists(), "no Windows test list found under scripts/ci/windows"
    assert list_problems(value, REGISTRIES) == []


def test_the_machine_lock_test_file_is_marked_not_registered() -> None:
    value = document(POSIX_ONLY)
    registered_paths = [path for path, _ in registered(value)]
    assert "tests/integration/e2e/harness/isolation/test_e2e_machine_lock.py" not in registered_paths
    assert not any("test_e2e_machine_lock" in path for path in registered_paths)


def test_the_registry_is_well_formed_and_its_evidence_holds() -> None:
    assert registry_problems(document(POSIX_ONLY)) == []


def test_a_registered_path_exists_with_the_kind_it_declares() -> None:
    for entry in document(POSIX_ONLY)["entries"]:
        target = ROOT / entry["path"]
        expected = "dir" if target.is_dir() else "file" if target.is_file() else "nothing"
        assert expected == entry["kind"], (entry["path"], entry["kind"], expected)


def test_killpg_is_refused_even_when_a_registry_lists_it_as_allowed() -> None:
    value = document(POSIX_ONLY)
    assert "killpg" not in value["allowed_needs"]
    listed = {**value, "allowed_needs": [*value["allowed_needs"], "killpg"]}
    unlocked = f"allowed_needs {[*LOCKED_REASONS, 'killpg']!r} is not the locked list of 13 reasons"
    assert set(registry_problems(listed)) == {unlocked, "allowed_needs contains the refused reason 'killpg'"}
    assert registry_problems({**listed, "entries": [{**value["entries"][0], "needs": ["killpg"]}]}) == [
        unlocked,
        "allowed_needs contains the refused reason 'killpg'",
        "entries[0]: reason 'killpg' is refused by name",
        "entries[0]: reason 'killpg' is not one of the locked reasons",
    ]


def test_a_reason_is_accepted_only_when_locked_and_allowed() -> None:
    assert reason_problems("fcntl", LOCKED_REASONS) == []
    assert reason_problems("AF_UNIX-path", LOCKED_REASONS) == []
    assert reason_problems("fcntl", ["pty"]) == ["reason 'fcntl' is not in allowed_needs"]
    assert reason_problems("pty", "pty") == ["reason 'pty' is not in allowed_needs"]
    assert reason_problems("pty", None) == ["reason 'pty' is not in allowed_needs"]
    assert reason_problems("killpg", [*LOCKED_REASONS, "killpg"]) == ["reason 'killpg' is refused by name", "reason 'killpg' is not one of the locked reasons"]
    assert reason_problems("killpg", LOCKED_REASONS) == ["reason 'killpg' is refused by name", "reason 'killpg' is not one of the locked reasons", "reason 'killpg' is not in allowed_needs"]
    for empty in ("", "   ", None, 7):
        assert reason_problems(empty, LOCKED_REASONS) == [f"reason {empty!r} is not a non-empty string"], empty


def test_a_registry_with_a_wrong_version_or_shape_is_refused() -> None:
    value = document(POSIX_ONLY)
    assert registry_problems(value) == []
    assert "version 2 is not 1" in registry_problems({**value, "version": 2})
    assert "version None is not 1" in registry_problems({k: v for k, v in value.items() if k != "version"})
    assert registry_problems([]) == ["registry is not an object"]
    assert registry_problems(None) == ["registry is not an object"]
    assert registry_problems({**value, "entries": {}}) == ["entries is not a list"]
    assert registry_problems({**value, "entries": "x"}) == ["entries is not a list"]


def test_an_allowed_list_that_is_not_the_locked_one_is_refused() -> None:
    value = document(POSIX_ONLY)
    refused = registry_problems({**value, "allowed_needs": [*value["allowed_needs"], "killpg"]})
    assert refused == [f"allowed_needs {[*LOCKED_REASONS, 'killpg']!r} is not the locked list of 13 reasons", "allowed_needs contains the refused reason 'killpg'"]
    for allowed in ([need for need in value["allowed_needs"] if need != "grp"], [*value["allowed_needs"][1:], value["allowed_needs"][0]]):
        assert [problem.endswith("is not the locked list of 13 reasons") for problem in registry_problems({**value, "allowed_needs": allowed})] == [True]
    assert registry_problems({**value, "allowed_needs": [*value["allowed_needs"], "fcntl"]}) == ["allowed_needs has duplicates", f"allowed_needs {[*LOCKED_REASONS, 'fcntl']!r} is not the locked list of 13 reasons"]
    assert "allowed_needs is not a list of strings" in registry_problems({**value, "allowed_needs": "fcntl"})
    assert "allowed_needs is not a list of strings" in registry_problems({**value, "allowed_needs": ["fcntl", 7]})
    assert "allowed_needs is not a list of strings" in registry_problems({**value, "allowed_needs": None})


def test_an_entry_with_a_bad_kind_need_or_evidence_is_refused() -> None:
    value = document(POSIX_ONLY)
    first = value["entries"][0]

    def problems_of(entry: object) -> list[str]:
        return registry_problems({**value, "entries": [entry]})

    assert any("kind 'module' is not one of" in problem for problem in problems_of({**first, "kind": "module"}))
    assert any("kind None is not one of" in problem for problem in problems_of({k: v for k, v in first.items() if k != "kind"}))
    assert any("needs [] is not a non-empty list" in problem for problem in problems_of({**first, "needs": []}))
    assert any("needs 'pwd' is not a non-empty list" in problem for problem in problems_of({**first, "needs": "pwd"}))
    assert any("needs None is not a non-empty list" in problem for problem in problems_of({k: v for k, v in first.items() if k != "needs"}))
    assert any("needs ['pty', 7] is not a non-empty list" in problem for problem in problems_of({**first, "needs": ["pty", 7]}))
    assert any("reason 'windows-only' is not one of the locked reasons" in problem for problem in problems_of({**first, "needs": ["windows-only"]}))
    without_termios = [need for need in value["allowed_needs"] if need != "termios"]
    assert "entries[0]: reason 'termios' is not in allowed_needs" in registry_problems(
        {**value, "allowed_needs": [*without_termios, "killpg"], "entries": [{**first, "needs": ["termios"]}]}
    )
    assert any("evidence '' is not a non-empty string" in problem for problem in problems_of({**first, "evidence": ""}))
    assert any("evidence '  ' is not a non-empty string" in problem for problem in problems_of({**first, "evidence": "  "}))
    assert any("evidence None is not a non-empty string" in problem for problem in problems_of({k: v for k, v in first.items() if k != "evidence"}))
    assert any("evidence 7 is not a non-empty string" in problem for problem in problems_of({**first, "evidence": 7}))


def test_an_entry_whose_need_its_evidence_does_not_show_is_refused() -> None:
    value = document(POSIX_ONLY)
    smoke = value["entries"][1]
    conftest = "tests/tooling/release/shell/signing/conftest.py"
    interrupt = "tests/tooling/release/shell/daemon/test_smoke_interrupt.py"
    bundle = "packaging/clients/smoke_bundle.py"
    hidden = "entries[0]: no cited line shows the stated need 'macos-bundle'"

    def problems_of(evidence: object) -> list[str]:
        return registry_problems({**value, "entries": [{**smoke, "evidence": evidence}]})

    for evidence in (f"{conftest}:14 imports pwd", f"{interrupt}:54 calls load", f"{bundle}:124 maps signal names"):
        assert problems_of(evidence) == [hidden], evidence
    for evidence in ("the macOS bundle tooling is only built on macOS", "it is loaded at :25"):
        assert problems_of(evidence) == ["entries[0]: evidence cites no path:line"], evidence
    assert problems_of(f"{conftest}:14 and :27 read pwd") == ["entries[0]: evidence cites a line without naming its file: :27", hidden]
    for cited in (f"{conftest}:99999 imports pwd", "tests/tooling/release/shell/absent.py:14 imports pwd"):
        assert problems_of(cited) == [f"entries[0]: evidence cites a line that does not exist: {cited.split(' ', 1)[0]}", hidden], cited
    for evidence in (f"{conftest}:14 imports pwd; {interrupt}:50 uses SIGHUP", f"{interrupt}:50 uses SIGHUP", f"{bundle}:123 reads SIGHUP"):
        assert problems_of(evidence) == [], evidence
    assert registry_problems({**value, "entries": [{**smoke, "needs": ["pwd"]}]}) == ["entries[0]: no cited line shows the stated need 'pwd'"]


def test_an_entry_with_a_missing_duplicate_or_misplaced_path_is_refused() -> None:
    value = document(POSIX_ONLY)
    smoke = value["entries"][1]

    def last_problem(entries: list) -> str:
        return registry_problems({**value, "entries": entries})[-1]

    assert "path 'tests/tooling/release/shell/daemon/test_absent.py' is not a file" in last_problem([{**smoke, "path": "tests/tooling/release/shell/daemon/test_absent.py"}])
    assert "is not a directory" in last_problem([{**smoke, "kind": "dir"}])
    assert "is not a file" in last_problem([{**value["entries"][0], "kind": "file", "evidence": "tests/tooling/release/shell/signing/conftest.py:14 imports pwd"}])
    assert "path 'src/hyprial/kernel/__init__.py' is not a repository-relative path under tests/" in last_problem([{**smoke, "path": "src/hyprial/kernel/__init__.py"}])
    assert "path '/tests/x' is not a repository-relative path under tests/" in last_problem([{**smoke, "path": "/tests/x"}])
    assert "path 'tests/../pyproject.toml' is not a repository-relative path under tests/" in last_problem([{**smoke, "path": "tests/../pyproject.toml"}])
    assert "path None is not a non-empty string" in last_problem([{k: v for k, v in smoke.items() if k != "path"}])
    assert "path '' is not a non-empty string" in last_problem([{**smoke, "path": ""}])
    assert "is already registered" in last_problem([*value["entries"], value["entries"][1]])
    assert "is already registered" in last_problem([*value["entries"], {**value["entries"][1], "path": value["entries"][1]["path"] + "/"}])
    assert "entries[4] is not an object" in last_problem([*value["entries"], "x"])
    assert registry_problems({**value, "entries": []}) == []


def test_every_posix_only_marker_in_the_suite_names_a_locked_reason() -> None:
    assert suite_marker_problems() == []


def test_the_gate_file_is_not_judged_by_the_marker_check_it_runs() -> None:
    own = marker_problems(ast.parse(GATE.read_text(encoding="utf-8"), filename=str(GATE)))
    assert [problem for _, problem in own] == ["posix_only takes exactly one positional reason"]


def test_the_suite_uses_the_marker_widely_enough_to_make_the_gate_meaningful() -> None:
    files = [path for path in sorted(TESTS.rglob("*.py")) if path != GATE and marker_calls(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))]
    calls = sum(len(marker_calls(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))) for path in files)
    assert (calls, len(files)) == (36, 14), (calls, len(files))


def marker_calls(tree: ast.Module) -> list[ast.Call]:
    """Every ``....posix_only(...)`` call in a module, whatever object it is called on."""

    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "posix_only"
    ]


def marker_source(reason: str) -> str:
    return f"import pytest\n@pytest.mark.posix_only({reason})\ndef test_a():\n    pass\n"


def test_a_marker_reason_must_be_a_locked_reason() -> None:
    for reason in LOCKED_REASONS:
        assert marker_problems(ast.parse(marker_source(f"'{reason}'"))) == []
    refused = ["reason 'killpg' is refused by name", "reason 'killpg' is not one of the locked reasons", "reason 'killpg' is not in allowed_needs"]
    assert marker_problems(ast.parse(marker_source("'killpg'"))) == [(2, problem) for problem in refused]
    unknown = ["reason 'windows-only' is not one of the locked reasons", "reason 'windows-only' is not in allowed_needs"]
    assert marker_problems(ast.parse(marker_source("'windows-only'"))) == [(2, problem) for problem in unknown]
    for spelling in ("FCNTL", "sigusr1", "pty ", "/PROC", "Pty"):
        problems = marker_problems(ast.parse(marker_source(f"'{spelling}'")))
        assert [problem for _, problem in problems] == [
            f"reason {spelling!r} is not one of the locked reasons",
            f"reason {spelling!r} is not in allowed_needs",
        ], spelling


def test_a_marker_with_no_usable_reason_is_reported() -> None:
    cases = {
        "": "posix_only takes exactly one positional reason",
        "'pty', 'fcntl'": "posix_only takes exactly one positional reason",
        "reason='pty'": "posix_only takes exactly one positional reason",
        "''": "reason '' is not a non-empty string",
        "7": "reason is 7, which is not a string literal or a mapping lookup",
        "OTHER": "reason is OTHER, which is not a string literal or a mapping lookup",
        "CONST.PTY": "reason is CONST.PTY, which is not a string literal or a mapping lookup",
        "'pty' + OTHER": "reason is 'pty' + OTHER, which is not a string literal or a mapping lookup",
        "None": "reason is None, which is not a string literal or a mapping lookup",
        "b'pty'": "reason is b'pty', which is not a string literal or a mapping lookup",
    }
    for call, problem in cases.items():
        assert marker_problems(ast.parse(marker_source(call))) == [(2, problem)], call


def test_a_marker_that_is_not_on_pytest_mark_is_reported() -> None:
    source = "import pytest\nMARK = other.mark.posix_only('pty')\npytestmark = pytest.mark.posix_only('pty')\n"
    assert marker_problems(ast.parse(source)) == [(2, "posix_only is called on other.mark, not on pytest.mark")]
    assert marker_problems(ast.parse("import pytest\npytestmark = pytest.mark.posix_only('pty')\n")) == []


MAPPING_SOURCE = """import pytest
NEEDS = {'a.py': 'fcntl', 'b.py': 'pty'}
def parameter(path):
    need = NEEDS.get(path.name)
    return pytest.param(path, marks=pytest.mark.posix_only(need))
"""


def test_a_marker_reason_read_from_a_mapping_checks_every_value_of_the_mapping() -> None:
    assert marker_problems(ast.parse(MAPPING_SOURCE)) == []
    assert marker_problems(ast.parse(MAPPING_SOURCE.replace("'b.py': 'pty'", "'b.py': 'killpg'"))) == [
        (5, "reason 'killpg' is refused by name"),
        (5, "reason 'killpg' is not one of the locked reasons"),
        (5, "reason 'killpg' is not in allowed_needs"),
    ]
    assert marker_problems(ast.parse(MAPPING_SOURCE.replace("'b.py': 'pty'", "'b.py': 'anything'"))) == [
        (5, "reason 'anything' is not one of the locked reasons"),
        (5, "reason 'anything' is not in allowed_needs"),
    ]
    not_literal = "reason 'need' is read from 'NEEDS', which is not a module-level dictionary of string literals"
    assert marker_problems(ast.parse(MAPPING_SOURCE.replace("'b.py': 'pty'", "'b.py': str(1)"))) == [(5, not_literal)]
    assert marker_problems(ast.parse(MAPPING_SOURCE.replace("'b.py': 'pty'", "'b.py': 3"))) == [(5, not_literal)]
    assert marker_problems(ast.parse(MAPPING_SOURCE.replace("NEEDS.get", "MISSING.get"))) == [(5, "reason 'need' is read from 'MISSING', which is not a module-level dictionary of string literals")]


def test_the_load_by_path_mapping_names_real_scripts_with_real_module_imports() -> None:
    path = ROOT / "tests/integration/e2e/harness/isolation/test_e2e_scenarios_load_by_path.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    mappings = string_mappings(tree)
    assert mappings["POSIX_ONLY_SCRIPTS"] == SCRIPT_NEEDS
    assert marker_problems(tree) == []
    scenarios = ROOT / "contract/e2e"
    assert all((scenarios / name).is_file() for name in SCRIPT_NEEDS), sorted(SCRIPT_NEEDS)
    for name, module in SCRIPT_NEEDS.items():
        assert module in script_imports(scenarios / name), (name, module)


def test_no_other_scenario_script_imports_a_posix_only_module_at_module_level() -> None:
    found = {
        path.name: script_imports(path) & set(MODULE_REASONS)
        for path in sorted((ROOT / "contract/e2e").glob("*.py"))
        if script_imports(path) & set(MODULE_REASONS)
    }
    assert found == {name: {module} for name, module in SCRIPT_NEEDS.items()}


def test_a_registered_need_pattern_matches_the_module_it_names() -> None:
    assert len(set(REASON_PATTERNS.values())) == len(REASON_PATTERNS)
    for module in MODULE_REASONS:
        pattern = REASON_PATTERNS[module]
        assert [bool(re.search(pattern, line)) for line in (f"import {module}", f"from {module} import something", f"import {module}_extra", f"import other, {module}x", f"x = '{module}.flock'")] == [True, True, False, False, False], module
    assert [bool(re.search(REASON_PATTERNS["SIGHUP"], line)) for line in ("signal.SIGHUP", "SIGHUP", "signal.SIGHUP_extra")] == [True, False, False]
    for need, line in (("SIGUSR1", "signal.SIGUSR1"), ("SIGUSR2", "signal.SIGUSR2"), ("SIGTERM-exit-143", "assert process.returncode == 143"), ("SIGTERM-exit-143", "os.kill(pid, signal.SIGTERM)"), ("/proc", "open('/proc/self/fd')"), ("/proc", "os.listdir('/dev/fd')"), ("AF_UNIX-path", "socket.AF_UNIX"), ("AF_UNIX-path", 'mkdtemp(dir="/tmp")'), ("macos-bundle", "Contents/MacOS/hyprial-desktop")):
        assert re.search(REASON_PATTERNS[need], line), (need, line)


class Item:
    """Smallest stand-in for a collected test: its own markers, as pytest's ``Node`` keeps them."""

    def __init__(self, *marks: pytest.MarkDecorator) -> None:
        self.own_markers: list[pytest.Mark] = [mark.mark for mark in marks]

    def iter_markers(self, name: str) -> list[pytest.Mark]:
        return [mark for mark in self.own_markers if mark.name == name]

    def add_marker(self, marker: pytest.MarkDecorator) -> None:
        self.own_markers.append(marker.mark)


class Manager:
    """``pluginmanager`` with the two calls ``configure_posix_only`` makes."""

    def __init__(self) -> None:
        self.plugins: dict[str, object] = {}

    def get_plugin(self, name: str) -> object:
        return self.plugins.get(name)

    def register(self, plugin: object, name: str) -> None:
        self.plugins[name] = plugin


def skip_reasons(items: list[Item]) -> list[object]:
    return [next(iter(item.iter_markers("skip")), None) for item in items]


def test_off_windows_the_plugin_ignores_nothing_and_skips_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "is_windows", lambda: False)
    plugin = collection.PosixOnlyPlugin({"dir": ("tests/a",), "file": ("tests/b/test_c.py",)})
    ignored = [
        plugin.pytest_ignore_collect(path, None)  # type: ignore[arg-type]
        for path in (ROOT / "tests/a", ROOT / "tests/a/deep/test_x.py", ROOT / "tests/b/test_c.py", ROOT / "tests/d")
    ]
    assert ignored == [None, None, None, None]
    items = [Item(pytest.mark.posix_only("pty")), Item()]
    plugin.pytest_collection_modifyitems(None, items)  # type: ignore[arg-type]
    assert [item.own_markers for item in items] == [[pytest.mark.posix_only("pty").mark], []]
    assert skip_reasons(items) == [None, None]


def test_on_windows_the_plugin_ignores_a_registered_directory_and_everything_below_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "is_windows", lambda: True)
    plugin = collection.PosixOnlyPlugin({"dir": ("tests/a",), "file": ("tests/b/test_c.py",)})
    answers = {
        ROOT / "tests/a": True,
        ROOT / "tests/a/deep/test_x.py": True,
        ROOT / "tests/a/deep": True,
        ROOT / "tests/ab/test_x.py": None,
        ROOT / "tests": None,
        ROOT / "other/a/test_x.py": None,
    }
    assert {path: plugin.pytest_ignore_collect(path, None) for path in answers} == answers  # type: ignore[arg-type]


def test_on_windows_the_plugin_ignores_a_registered_file_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "is_windows", lambda: True)
    plugin = collection.PosixOnlyPlugin({"dir": (), "file": ("tests/b/test_c.py",)})
    answers = {
        ROOT / "tests/b/test_c.py": True,
        ROOT / "tests/b/test_cd.py": None,
        ROOT / "tests/b/test_c.py/inner.py": None,
        ROOT / "tests/b": None,
    }
    assert {path: plugin.pytest_ignore_collect(path, None) for path in answers} == answers  # type: ignore[arg-type]


def test_on_windows_a_relative_collection_path_and_a_trailing_slash_are_matched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "is_windows", lambda: True)
    monkeypatch.chdir(ROOT)
    plugin = collection.PosixOnlyPlugin({"dir": ("tests/a/",), "file": ("tests/b/test_c.py",)})
    assert plugin.pytest_ignore_collect(Path("tests/a/deep/test_x.py"), None) is True  # type: ignore[arg-type]
    assert plugin.pytest_ignore_collect(Path("tests/b/test_c.py"), None) is True  # type: ignore[arg-type]
    assert plugin.pytest_ignore_collect(Path("tests/b/test_d.py"), None) is None  # type: ignore[arg-type]


def test_on_windows_a_path_outside_the_repository_is_left_alone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(host, "is_windows", lambda: True)
    plugin = collection.PosixOnlyPlugin({"dir": ("tests",), "file": ()})
    assert plugin.pytest_ignore_collect(tmp_path / "tests" / "test_x.py", None) is None  # type: ignore[arg-type]
    assert plugin.pytest_ignore_collect(ROOT / "tests" / "test_x.py", None) is True  # type: ignore[arg-type]


def test_the_plugin_reads_the_real_registry_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "is_windows", lambda: True)
    plugin = collection.PosixOnlyPlugin()
    assert plugin.directories == ("tests/tooling/release/shell/signing",)
    assert plugin.files == tuple(path for path, kind, _ in APPROVED_ENTRIES if kind == "file")
    answers = {ROOT / path: plugin.pytest_ignore_collect(ROOT / path, None) for path, _, _ in APPROVED_ENTRIES}  # type: ignore[arg-type]
    assert set(answers.values()) == {True}
    assert plugin.pytest_ignore_collect(ROOT / "tests/tooling/release/shell/daemon/test_verify_daemon.py", None) is None  # type: ignore[arg-type]


def test_on_windows_the_plugin_skips_every_marked_item_with_its_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "is_windows", lambda: True)
    plugin = collection.PosixOnlyPlugin()
    items = [
        Item(pytest.mark.posix_only("pty")),
        Item(pytest.mark.posix_only("SIGUSR1"), pytest.mark.posix_only("SIGHUP")),
        Item(pytest.mark.posix_only("fcntl"), pytest.mark.skip(reason="already skipped")),
        Item(pytest.mark.skip(reason="plain skip")),
        Item(),
    ]
    plugin.pytest_collection_modifyitems(None, items)  # type: ignore[arg-type]
    added = [item.own_markers[len(item.own_markers) - 1] for item in items[:3]]
    assert [(mark.name, mark.args, dict(mark.kwargs)) for mark in added] == [
        ("skip", (), {"reason": "posix_only: pty"}),
        ("skip", (), {"reason": "posix_only: SIGUSR1, SIGHUP"}),
        ("skip", (), {"reason": "posix_only: fcntl"}),
    ]
    assert [len(item.own_markers) for item in items] == [2, 3, 3, 1, 0]
    assert dict(items[2].own_markers[1].kwargs) == {"reason": "already skipped"}
    assert skip_reasons(items) == [pytest.mark.skip(reason="posix_only: pty").mark, pytest.mark.skip(reason="posix_only: SIGUSR1, SIGHUP").mark, pytest.mark.skip(reason="already skipped").mark, pytest.mark.skip(reason="plain skip").mark, None]


def test_a_posix_only_marker_without_a_reason_skips_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "is_windows", lambda: True)
    plugin = collection.PosixOnlyPlugin()
    items = [Item(pytest.mark.posix_only), Item(pytest.mark.posix_only(reason="pty"))]
    plugin.pytest_collection_modifyitems(None, items)  # type: ignore[arg-type]
    assert [len(item.own_markers) for item in items] == [1, 1]


def test_the_plugin_is_registered_once_on_every_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    for windows in (False, True):
        monkeypatch.setattr(host, "is_windows", lambda: windows)
        manager = Manager()
        for _ in range(2):
            collection.configure_posix_only(SimpleNamespace(pluginmanager=manager))  # type: ignore[arg-type]
        assert list(manager.plugins) == [collection.PLUGIN_NAME]
        assert manager.plugins[collection.PLUGIN_NAME].__class__ is collection.PosixOnlyPlugin
    assert collection.PLUGIN_NAME == "hyprial-windows-posix-only"


def test_expected_failures_are_absent_or_valid() -> None:
    if not EXPECTED_FAILURES.exists():
        assert collection.load_posix_only(EXPECTED_FAILURES) == {"dir": (), "file": ()}
        return
    problems = expected_failure_problems(document(EXPECTED_FAILURES), KNOWN_PROBLEMS.read_text(encoding="utf-8"), ROOT)
    assert problems == [], problems


def test_an_expected_failures_document_is_checked_against_known_problems_and_the_tree(tmp_path: Path) -> None:
    known = "## KP-001 first\n## KP-120a split\n"
    one = {"kp": "KP-001", "nodeid": "tests/a/test_b.py::test_one"}
    good = {"version": 1, "entries": [one]}
    tests = tmp_path / "tests/a"
    tests.mkdir(parents=True)
    (tests / "test_b.py").write_text("def helper():\n    pass\ndef test_one():\n    pass\nclass TestGroup:\n    def test_nested(self):\n        pass\n", encoding="utf-8")

    def problems_of(*entries: object, **top: object) -> list[str]:
        return expected_failure_problems({**good, **top, "entries": list(entries)}, known, tmp_path)

    assert problems_of(one, {"kp": "KP-120a", "nodeid": "tests/a/test_b.py::TestGroup::test_nested[x]"}) == [] and problems_of() == []
    assert expected_failure_problems([], known, tmp_path) == ["registry is not an object"]
    assert problems_of(version=2) == ["version 2 is not 1"] and expected_failure_problems({**good, "entries": {}}, known, tmp_path) == ["entries is not a list"]
    assert problems_of("x") == ["entries[0] is not an object"]
    for kp, why in (("KP-003", "is not a section of known-problems.md"), ("KP-120ab", "is not a known-problem id"), (None, "is not a known-problem id"), (7, "is not a known-problem id")):
        assert problems_of({**one, "kp": kp}) == [f"entries[0]: kp {kp!r} {why}"]
    for nodeid, problem in (("tests/a/test_b.py", "nodeid 'tests/a/test_b.py' is not a file path followed by :: and a test name"), ("tests/a/test_absent.py::test_one", "nodeid file 'tests/a/test_absent.py' does not exist"), ("tests/a/test_b.py::test_missing", "nodeid names 'test_missing', which is not defined in the file"), ("tests/a/test_b.py::helper", "nodeid names 'helper', which is not a test function"), ("tests/a/test_b.py::TestGroup", "nodeid names 'TestGroup', which is not a test function"), ("tests/a/test_b.py::TestGroup::Nope", "nodeid names 'Nope', which is not a test function"), ("tests/a/test_b.py::TestGroup::test_one", "nodeid names 'test_one', which is not defined in class 'TestGroup'"), ("tests/a/test_b.py::test_one::more", "nodeid names 'test_one', which is not a test class"), ("tests/a/test_b.py::TestGroup[p]::test_nested", "nodeid 'tests/a/test_b.py::TestGroup[p]::test_nested' parametrizes a class, not a test"), (5, "nodeid 5 is not a string")):
        assert problems_of({**one, "nodeid": nodeid}) == [f"entries[0]: {problem}"], nodeid
    assert problems_of(one, {**one, "kp": "KP-120a"}) == ["entries[1]: nodeid 'tests/a/test_b.py::test_one' is already expected"]


def test_every_known_problem_id_the_gate_accepts_is_a_section_of_the_document() -> None:
    ids = known_problem_ids(KNOWN_PROBLEMS.read_text(encoding="utf-8"))
    assert len(ids) >= 136 and {"KP-001", "KP-120a", "KP-120b"} <= ids and all(re.fullmatch(KNOWN_ID, kp) for kp in ids)
    assert known_problem_ids("## KP-120a split") == {"KP-120a"} and known_problem_ids("## KP-120") == {"KP-120"}
    assert known_problem_ids("## KP-001 x\n### KP-002 y\n## KP-003a-bad\n## KP-004! more\n## KP-005-z\n## KP-006ab x\n##KP-007\n") == {"KP-001"}


def test_the_marker_is_declared_to_pytest() -> None:
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]["markers"]
    assert [marker for marker in declared if marker.startswith("posix_only")] == [MARKER_DECLARATION]


def test_the_plugin_and_the_gate_read_the_same_registry_and_an_unreadable_one_is_empty(tmp_path: Path) -> None:
    value = document(POSIX_ONLY)
    assert collection.load_posix_only(POSIX_ONLY) == {"dir": tuple(path for path, kind in registered(value) if kind == "dir"), "file": tuple(path for path, kind in registered(value) if kind == "file")}
    path = tmp_path / "registry.json"
    for text in ("{not json", "[]", '{"version": 1}', "null", '{"entries": [{"path": 1, "kind": "file"}, {"path": "tests/x", "kind": "module"}, "x"]}', '{"entries": 5}'):
        path.write_text(text, encoding="utf-8")
        assert collection.load_posix_only(path) == {"dir": (), "file": ()}, text
    assert collection.load_posix_only(tmp_path / "absent.json") == {"dir": (), "file": ()}
    path.write_text('{"entries": [{"path": "tests/a/", "kind": "dir"}, {"path": "tests/b/", "kind": "file"}]}', encoding="utf-8")
    assert collection.load_posix_only(path) == {"dir": ("tests/a",), "file": ("tests/b",)}


def test_a_registered_path_may_not_appear_in_a_windows_test_list(tmp_path: Path) -> None:
    value = document(POSIX_ONLY)
    lists = tmp_path / "scripts/ci/windows"
    lists.mkdir(parents=True)
    assert windows_test_lists(lists) == [] and list_problems(value, lists, root=tmp_path) == []
    (lists / "tier9-tests.txt").write_text("# comment\n\ntests/kernel/test_a.py\n tests/tooling/release/shell/signing/bundle/test_x.py \ntests/tooling/release/shell/daemon/test_smoke_daemon.py\ntests/tooling/release/shell/signing\n", encoding="utf-8")
    (lists / "tier9-rejected.tsv").write_text("path\treason\ntests/tooling/release/shell/daemon/test_smoke_cleanup.py\twhy\t12\n", encoding="utf-8")
    (lists / "notes.txt").write_text("tests/tooling/release/shell/signing\n", encoding="utf-8")
    assert [path.name for path in windows_test_lists(lists)] == ["tier9-rejected.tsv", "tier9-tests.txt"] and listed_test_paths(lists / "tier9-rejected.tsv") == ["path", "tests/tooling/release/shell/daemon/test_smoke_cleanup.py"]
    assert list_problems(value, lists, root=tmp_path) == [
        "scripts/ci/windows/tier9-rejected.tsv:2 names the registered file 'tests/tooling/release/shell/daemon/test_smoke_cleanup.py'",
        "scripts/ci/windows/tier9-tests.txt:4 names the registered dir 'tests/tooling/release/shell/signing'",
        "scripts/ci/windows/tier9-tests.txt:5 names the registered file 'tests/tooling/release/shell/daemon/test_smoke_daemon.py'",
        "scripts/ci/windows/tier9-tests.txt:6 names the registered dir 'tests/tooling/release/shell/signing'",
    ]
    assert covers("tests/a", "dir", "tests/a/x.py") and not covers("tests/a", "dir", "tests/ax/x.py") and covers("tests/a/b.py", "file", "tests/a/b.py") and not covers("tests/a/b.py", "file", "tests/a/c.py")
    (lists / "tier9-tests.txt").write_text("tests/kernel/test_a.py\n", encoding="utf-8")
    (lists / "tier9-rejected.tsv").write_text("path\treason\n", encoding="utf-8")
    assert list_problems(value, lists, root=tmp_path) == [] and list_problems({"entries": []}, lists, root=tmp_path) == []
