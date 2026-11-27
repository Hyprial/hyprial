"""The ``posix_only`` marker check: which shapes are judged, and what is refused.

The shapes are parsed from source strings, so no real test file is touched.
"""

from __future__ import annotations

import ast
import re


from tests.tooling.ci.windows.registries.registry_gate import (
    GATE,
    LOCKED_REASONS,
    MODULE_REASONS,
    REASON_PATTERNS,
    ROOT,
    SCRIPT_NEEDS,
    TESTS,
    marker_calls,
    marker_problems,
    script_imports,
    string_mappings,
    suite_marker_problems,
)

def test_every_posix_only_marker_in_the_suite_names_a_locked_reason() -> None:
    assert suite_marker_problems() == []


def test_the_gate_file_is_not_judged_by_the_marker_check_it_runs() -> None:
    own = [(line, problem) for path in sorted(GATE.glob("*.py")) for line, problem in marker_problems(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))]
    assert [problem for _, problem in own] == ["posix_only takes exactly one positional reason"]


def test_the_suite_uses_the_marker_widely_enough_to_make_the_gate_meaningful() -> None:
    files = [path for path in sorted(TESTS.rglob("*.py")) if not path.is_relative_to(GATE) and marker_calls(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))]
    calls = sum(len(marker_calls(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))) for path in files)
    assert (calls, len(files)) == (36, 14), (calls, len(files))


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
