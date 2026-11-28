"""Checks for the Windows test registries under ``scripts/ci/windows``.

This module holds every check; the ``test_*.py`` files beside it are the gate that
runs them.

Decision (2026-11-26): one gate for every data file that decides what a Windows
run does not execute.  ``posix-only.json`` names whole directories and whole
files that are not collected on Windows, and the ``posix_only`` marker skips
single test cases there.  Both take tests out of a Windows run, so both are
checked against the tree they describe: a registered path must exist, its
stated reason must be visible in the source lines it cites, and every marker
under ``tests/`` outside this directory must name a reason from the locked
list.

Decision (same date): the four initial registry entries are listed here by
path, kind and reason.  A directory-level or file-level entry changes how many
tests a Windows run never sees, so it is a decision to make deliberately: the
gate turns red on an unlisted entry, on a changed reason, and on an entry
removed without updating this module alike.

Decision (same date): the list of allowed reasons is closed and lives in the
data file, and ``killpg`` is not on it under any circumstances.  Every check
that accepts a reason goes through ``reason_problems``, which refuses
``killpg`` by name even when a registry lists it as allowed, so no caller can
reach it through the data file.

Decision (same date): bad registries are written to ``tmp_path`` and loaded
through ``load_posix_only(path)``, which is why that function takes its path
as an argument.

Decision (same date): pytest does not ask ``pytest_ignore_collect`` about a
path that was given on the command line, and the Windows runner hands pytest a
list of test files.  A registered path that appears in a Windows test list
would therefore be collected and fail there, so the gate refuses any
registered path that a test list under ``scripts/ci/windows`` names, at the
file itself or anywhere below a registered directory.

Decision (2026-11-27): the files of this directory are not scanned for
``posix_only`` markers.  They call the marker to build test input for the
plugin, including shapes that are wrong on purpose, so the suite-wide marker
check and the marker count skip the whole directory and judge only the test
files that carry a marker.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[5]
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


OFF_PLACE = (
    "posix_only is allowed only as the call that decorates a def or async def, or in marks= of pytest.param, spelled pytest.param; "
    "written anywhere else it can reach more than one test case.  The rule goes by the attribute name alone, on any object"
)
IN_PARAMETRIZE = (
    "posix_only in a parametrize that does not decorate a def or async def reaches every test it is applied to: "
    "those of a module or a class, or every test that pytest_generate_tests parametrises; put the parametrize on the single test function instead"
)
ON_PYTESTMARK = "posix_only on pytestmark skips every test of a module or a class; mark the single test cases instead, or register the file in posix-only.json, which needs approval"
IN_CLASS = "posix_only on pytestmark in a class body skips every test in the class; mark the single test cases instead"
ON_CLASS = "posix_only in a class decorator reaches every test in the class, inside pytest.param as well; mark the single test methods instead"
IN_FIXTURE = "posix_only in pytest.fixture reaches every test that uses the fixture, and every test of the module when it is autouse; mark the single test cases instead"
IN_CONFTEST = "posix_only in conftest.py can skip every test of a directory; mark the single test cases in their own files instead, or register the directory in posix-only.json, which needs approval"
COMPOUND = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.TryStar,
    ast.Match,
)


def ancestors(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> list[tuple[ast.AST, ast.AST]]:
    """``[(child, parent), ...]`` from ``node`` up to the module, ``node`` itself as the first child."""

    chain = []
    while node in parents:
        chain.append((node, parents[node]))
        node = parents[node]
    return chain


def last_name(node: ast.expr) -> str | None:
    """``fixture`` for both ``pytest.fixture`` and ``fixture``; ``None`` for an expression that is not a name."""

    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def mentions_pytestmark(statement: ast.stmt) -> bool:
    """Whether ``statement`` names ``pytestmark``: as a name, as an attribute, or as a string."""

    return any(
        (isinstance(node, ast.Name) and node.id == "pytestmark")
        or (isinstance(node, ast.Attribute) and node.attr == "pytestmark")
        or (isinstance(node, ast.Constant) and node.value == "pytestmark")
        for node in ast.walk(statement)
    )


def in_param_marks(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    """Whether ``call`` is the value of ``marks=`` of ``pytest.param(...)``, alone or as an element of a list or tuple."""

    holder = parents.get(call)
    if isinstance(holder, (ast.List, ast.Tuple)):
        holder = parents.get(holder)
    if not (isinstance(holder, ast.keyword) and holder.arg == "marks"):
        return False
    param = parents.get(holder)
    return (
        isinstance(param, ast.Call)
        and isinstance(param.func, ast.Attribute)
        and param.func.attr == "param"
        and isinstance(param.func.value, ast.Name)
        and param.func.value.id == "pytest"
    )


def decorates_def(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Whether ``node`` is one of the decorators of a ``def`` or ``async def``."""

    holder = parents.get(node)
    return isinstance(holder, (ast.FunctionDef, ast.AsyncFunctionDef)) and node in holder.decorator_list


def placement_problem(attribute: ast.Attribute, parents: dict[ast.AST, ast.AST]) -> str | None:
    """Why the attribute ``posix_only`` stands where it must not; ``None`` when it stands in one of the two allowed places."""

    call = parents.get(attribute)
    called = isinstance(call, ast.Call) and call.func is attribute
    if called and decorates_def(call, parents):
        return None
    chain = ancestors(attribute, parents)
    if any(isinstance(parent, ast.ClassDef) and child in parent.decorator_list for child, parent in chain):
        return ON_CLASS
    if any(isinstance(parent, ast.Call) and last_name(parent.func) in ("fixture", "yield_fixture") for _, parent in chain):
        return IN_FIXTURE
    statement = next(parent for _, parent in chain if isinstance(parent, ast.stmt))
    if not isinstance(statement, COMPOUND) and mentions_pytestmark(statement):
        owner = next(parent for _, parent in ancestors(statement, parents) if isinstance(parent, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)))
        if isinstance(owner, ast.ClassDef):
            return IN_CLASS
        if isinstance(owner, ast.Module):
            return ON_PYTESTMARK
    if not (called and in_param_marks(call, parents)):
        return OFF_PLACE
    if any(isinstance(parent, ast.Call) and last_name(parent.func) == "parametrize" and not decorates_def(parent, parents) for _, parent in chain):
        return IN_PARAMETRIZE
    return None


def placement_problems(tree: ast.Module) -> list[tuple[int, str]]:
    """``(line, problem)`` for every attribute ``posix_only`` outside the two places where it marks single test cases.

    The attribute may stand only as the call that directly decorates a
    ``def`` or ``async def``, methods included, or in ``marks=`` of
    ``pytest.param(...)``, alone or as an element of a list or tuple, provided
    that ``pytest.param`` is not inside a ``pytestmark`` statement of a module
    or class body, a class decorator, a ``fixture(...)`` or
    ``yield_fixture(...)`` call, or a ``parametrize(...)`` call that does not itself decorate a ``def`` or
    ``async def``.  Each of those reaches every test of a module, a class,
    every user of a fixture, or every test a ``pytest_generate_tests`` hook
    parametrises.  Everywhere else the attribute is refused, whatever it is
    assigned to and whatever statement wraps it, so a marker bound to a name,
    put into a list, or handed to ``add_marker`` is refused where it is
    written, also when another file uses it.
    """

    attributes = [node for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr == "posix_only"]
    if not attributes:
        return []
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    return sorted({(node.lineno, problem) for node in attributes if (problem := placement_problem(node, parents)) is not None})


def conftest_problems(tree: ast.Module) -> list[tuple[int, str]]:
    """``(line, problem)`` for every reference to ``posix_only`` in a ``conftest.py``.

    A ``conftest.py`` holds no test case, so the marker written there belongs
    to no single test case, and one line in it can take every test of a
    directory out of a run.  Every reference is refused: the attribute, and a
    string that is exactly the marker's name.  The two places that
    ``placement_problems`` allows elsewhere are refused here as well.
    """

    return sorted(
        {
            (node.lineno, IN_CONFTEST)
            for node in ast.walk(tree)
            if (isinstance(node, ast.Attribute) and node.attr == "posix_only") or (isinstance(node, ast.Constant) and node.value == "posix_only")
        }
    )


def marker_problems(tree: ast.Module, *, conftest: bool = False) -> list[tuple[int, str]]:
    """``(line, problem)`` for every ``posix_only`` marker that is misplaced or whose reason is not a known valid one.

    The placement is judged by ``placement_problems``.  ``conftest`` leaves it
    out: a ``conftest.py`` is refused for every reference to the marker by
    ``conftest_problems``, which says so in its own words.
    """

    if not any(isinstance(node, ast.Attribute) and node.attr == "posix_only" for node in ast.walk(tree)):
        return []
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
    return sorted([*([] if conftest else placement_problems(tree)), *problems], key=lambda problem: problem[0])


GATE = Path(__file__).resolve().parent


def suite_marker_problems(tests: Path = TESTS, *, root: Path = ROOT) -> list[tuple[str, int, str]]:
    """``(file, line, problem)`` for every ``posix_only`` marker under ``tests`` that is misplaced or has no valid reason.

    Every file is parsed, ``conftest.py`` included: no text search decides
    which files to read, so no spelling that Python reads as ``posix_only``
    can keep a file out of the scan.  The gate's own directory is left out:
    its files write the marker into source strings and call it to build test
    input.
    """

    found: list[tuple[str, int, str]] = []
    for path in sorted(tests.rglob("*.py")):
        if path.is_relative_to(GATE):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        name = path.relative_to(root).as_posix()
        if path.name == "conftest.py":
            found.extend((name, line, problem) for line, problem in marker_problems(tree, conftest=True))
            found.extend((name, line, problem) for line, problem in conftest_problems(tree))
        else:
            found.extend((name, line, problem) for line, problem in marker_problems(tree))
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


def marker_calls(tree: ast.Module) -> list[ast.Call]:
    """Every ``....posix_only(...)`` call in a module, whatever object it is called on."""

    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "posix_only"
    ]
