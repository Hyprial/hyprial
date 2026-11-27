"""The ``posix_only`` marker check: which shapes are judged, and what is refused.

The shapes are parsed from source strings or written to a fake directory
under ``tmp_path``, so no real test file is touched.

Decision (2026-11-27): the marker may only be written on a single test case.
On ``pytestmark``, on a class, or in a ``conftest.py`` one call takes every
test of a module, a class or a directory out of a Windows run, which is what
a registered file or directory does, and a registration needs approval.  The
gate keeps no second list of allowed files.

Decision (same date): the gate names the two places where the marker may
stand and refuses every other one, instead of naming the places it must not
stand: a list of forbidden places misses every statement it does not name.
The two places are the call that directly decorates a ``def`` or ``async
def``, and ``marks=`` of ``pytest.param`` outside a ``pytestmark`` statement,
a class decorator and a fixture.  A class decorator is refused even when only
one parameter carries the marker, so that the rule stays one sentence long.
``param`` imported from pytest by name is refused: the allowed place is
spelled ``pytest.param``, as the marker is spelled ``pytest.mark.posix_only``.
In a ``conftest.py`` neither place is allowed.  ``pytest.yield_fixture`` is
pytest's own deprecated name that calls ``pytest.fixture`` unchanged, so it
counts as a fixture.

Decision (same date): a ``parametrize(...)`` call that holds the marker in a
``pytest.param`` must itself decorate a ``def`` or ``async def``.  Any other
``parametrize`` reaches more than one test function: a module or a class it
is applied to, or every test a module's or class's ``pytest_generate_tests``
hands to ``metafunc.parametrize``, which needs no conftest and no plugin.
One rule covers the hook, a ``parametrize`` applied to a class by a call or
through a name, and a ``parametrize`` bound to ``pytestmark`` where no simple
statement names it.  A ``pytest.param`` built in an allowed place and then
moved by value is not followed.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from tests.tooling.ci.windows.registries.registry_gate import (
    GATE,
    LOCKED_REASONS,
    MODULE_REASONS,
    REASON_PATTERNS,
    ROOT,
    SCRIPT_NEEDS,
    TESTS,
    conftest_problems,
    marker_calls,
    marker_problems,
    script_imports,
    string_mappings,
    suite_marker_problems,
)

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
ONE_REASON = "posix_only takes exactly one positional reason"
REFUSED_KILLPG = ["reason 'killpg' is refused by name", "reason 'killpg' is not one of the locked reasons", "reason 'killpg' is not in allowed_needs"]


def test_every_posix_only_marker_in_the_suite_names_a_locked_reason() -> None:
    assert suite_marker_problems() == []


def test_the_gate_file_is_not_judged_by_the_marker_check_it_runs() -> None:
    own = [(path.name, *problem) for path in sorted(GATE.rglob("*.py")) for problem in marker_problems(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))]
    assert own == [*[("test_plugin.py", line, OFF_PLACE) for line in (62, 64, 124, 125, 126, 145)], ("test_plugin.py", 145, ONE_REASON)]


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
    assert marker_problems(ast.parse("import pytest\n@other.mark.posix_only('pty')\ndef test_a():\n    pass\n")) == [(2, "posix_only is called on other.mark, not on pytest.mark")]
    assert marker_problems(ast.parse("import pytest\n@pytest.mark.posix_only('pty')\ndef test_a():\n    pass\n")) == []


def test_a_posix_only_marker_on_pytestmark_is_reported(tmp_path: Path) -> None:
    shapes = {
        "import pytest\npytestmark = pytest.mark.posix_only('pty')\n": 2,
        "import pytest\npytestmark = [pytest.mark.skip(reason='slow'), pytest.mark.posix_only('pty')]\n": 2,
        "import pytest\npytestmark = (pytest.mark.posix_only('pty'),)\n": 2,
        "import pytest\npytestmark = [pytest.mark.skip(reason='slow')]\npytestmark += [pytest.mark.posix_only('pty')]\n": 3,
        "import pytest\npytestmark = pytest.mark.skip(reason='slow')\npytestmark |= pytest.mark.posix_only('pty')\n": 3,
        "import pytest\npytestmark: list = [pytest.mark.posix_only('pty')]\n": 2,
        "import pytest\npytestmark = pytest.mark.posix_only\n": 2,
        "import pytest\npytestmark = MARKS = pytest.mark.posix_only('pty')\n": 2,
    }
    for source, line in shapes.items():
        assert marker_problems(ast.parse(source)) == [(line, ON_PYTESTMARK)], source
    both = "import pytest\npytestmark = pytest.mark.posix_only('killpg')\n"
    assert [problem for _, problem in marker_problems(ast.parse(both))] == [ON_PYTESTMARK, *REFUSED_KILLPG]
    for source in (
        "import pytest\npytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='windows')\n",
        "import pytest\npytestmark = [pytest.mark.skip(reason='slow'), pytest.mark.anyio]\n",
        "import pytest\npytestmark: list\n",
    ):
        assert marker_problems(ast.parse(source)) == [], source
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_a.py").write_text("import pytest\npytestmark = pytest.mark.posix_only('pty')\ndef test_a():\n    pass\n", encoding="utf-8")
    (tests / "test_b.py").write_text("import pytest\n@pytest.mark.posix_only('pty')\ndef test_b():\n    pass\n", encoding="utf-8")
    assert suite_marker_problems(tests, root=tmp_path) == [("tests/test_a.py", 2, ON_PYTESTMARK)]


def test_a_posix_only_marker_on_a_class_is_reported() -> None:
    assert marker_problems(ast.parse("import pytest\n@pytest.mark.posix_only('pty')\nclass TestGroup:\n    def test_a(self):\n        pass\n")) == [(2, ON_CLASS)]
    assert marker_problems(ast.parse("import pytest\nclass TestGroup:\n    pytestmark = pytest.mark.posix_only('pty')\n    def test_a(self):\n        pass\n")) == [(3, IN_CLASS)]
    assert marker_problems(ast.parse("import pytest\nclass TestGroup:\n    pytestmark = [pytest.mark.posix_only('pty')]\n    def test_a(self):\n        pass\n")) == [(3, IN_CLASS)]
    assert marker_problems(ast.parse("import pytest\nclass Outer:\n    class Inner:\n        pytestmark = pytest.mark.posix_only('pty')\n")) == [(4, IN_CLASS)]
    both = "import pytest\n@pytest.mark.posix_only('pty')\nclass TestGroup:\n    pytestmark = pytest.mark.posix_only('pty')\n"
    assert marker_problems(ast.parse(both)) == [(2, ON_CLASS), (4, IN_CLASS)]
    for source in (
        "import pytest\n@pytest.mark.skip(reason='slow')\nclass TestGroup:\n    def test_a(self):\n        pass\n",
        "import pytest\nclass TestGroup:\n    posix_only = True\n",
        "import pytest\nclass TestGroup:\n    @pytest.mark.posix_only('pty')\n    def test_a(self):\n        pass\n",
    ):
        assert marker_problems(ast.parse(source)) == [], source


OUTSIDE_THE_TWO_PLACES = {
    "pytestmark inside if": ("if True:\n    pytestmark = pytest.mark.posix_only('pty')\n", [(3, ON_PYTESTMARK)]),
    "pytestmark inside try": ("try:\n    pytestmark = pytest.mark.posix_only('pty')\nfinally:\n    pass\n", [(3, ON_PYTESTMARK)]),
    "pytestmark inside with": ("import contextlib\nwith contextlib.nullcontext():\n    pytestmark = pytest.mark.posix_only('pty')\n", [(4, ON_PYTESTMARK)]),
    "pytestmark inside for": ("for _ in (0,):\n    pytestmark = pytest.mark.posix_only('pty')\n", [(3, ON_PYTESTMARK)]),
    "name, then pytestmark": ("_M = pytest.mark.posix_only('pty')\npytestmark = _M\n", [(2, OFF_PLACE)]),
    "name, then class decorator": ("_M = pytest.mark.posix_only('pty')\n@_M\nclass TestGroup:\n    pass\n", [(2, OFF_PLACE)]),
    "walrus": ("(pytestmark := pytest.mark.posix_only('pty'))\n", [(2, ON_PYTESTMARK)]),
    "tuple target": ("pytestmark, _x = pytest.mark.posix_only('pty'), 1\n", [(2, ON_PYTESTMARK)]),
    "globals()": ("globals()['pytestmark'] = pytest.mark.posix_only('pty')\n", [(2, ON_PYTESTMARK)]),
    "empty pytestmark, then append": ("pytestmark = []\npytestmark.append(pytest.mark.posix_only('pty'))\n", [(3, ON_PYTESTMARK)]),
    "empty pytestmark, then extend": ("pytestmark = []\npytestmark.extend([pytest.mark.posix_only('pty')])\n", [(3, ON_PYTESTMARK)]),
    "name inside a list": ("POSIX = pytest.mark.posix_only('pty')\npytestmark = [POSIX]\n", [(2, OFF_PLACE)]),
    "list unpacked into pytestmark": ("BASE = [pytest.mark.posix_only('pty')]\npytestmark = [*BASE]\n", [(2, OFF_PLACE)]),
    "class body pytestmark inside if": ("class TestGroup:\n    if True:\n        pytestmark = pytest.mark.posix_only('pty')\n", [(4, IN_CLASS)]),
    "class body tuple target": ("class TestGroup:\n    pytestmark, other = pytest.mark.posix_only('pty'), 1\n", [(3, IN_CLASS)]),
    "class pytestmark set after the class": ("class TestGroup:\n    pass\nTestGroup.pytestmark = pytest.mark.posix_only('pty')\n", [(4, ON_PYTESTMARK)]),
    "marker applied to a class by call": ("class TestGroup:\n    pass\nTestGroup = pytest.mark.posix_only('pty')(TestGroup)\n", [(4, OFF_PLACE)]),
    "add_marker in a plugin module": ("def pytest_collection_modifyitems(items):\n    for item in items:\n        item.add_marker(pytest.mark.posix_only('pty'))\n", [(4, OFF_PLACE)]),
    "marker bound to a name": ("MARK = pytest.mark.posix_only('pty')\n", [(2, OFF_PLACE)]),
    "pytestmark as a local of a function": ("def test_a():\n    pytestmark = pytest.mark.posix_only('pty')\n", [(3, OFF_PLACE)]),
    "pytest.param in pytestmark": ("pytestmark = pytest.mark.parametrize('_', [pytest.param(0, marks=pytest.mark.posix_only('pty'))])\n", [(2, ON_PYTESTMARK)]),
    "pytest.param in a class decorator, every one": ("@pytest.mark.parametrize('x', [pytest.param(2, marks=pytest.mark.posix_only('pty'))])\nclass TestGroup:\n    def test_a(self, x):\n        pass\n", [(2, ON_CLASS)]),
    "pytest.param in a class decorator, one of two":("@pytest.mark.parametrize('x', [1, pytest.param(2, marks=pytest.mark.posix_only('pty'))])\nclass TestGroup:\n    def test_a(self, x):\n        pass\n", [(2, ON_CLASS)]),
    "pytest.param in pytest.fixture": ("@pytest.fixture(autouse=True, params=[pytest.param(0, marks=pytest.mark.posix_only('pty'))])\ndef posix(request):\n    return request.param\n", [(2, IN_FIXTURE)]),
    "pytest.param in pytest.yield_fixture": ("@pytest.yield_fixture(autouse=True, params=[pytest.param(0, marks=pytest.mark.posix_only('pty'))])\ndef posix(request):\n    yield request.param\n", [(2, IN_FIXTURE)]),
    "pytest.param in fixture imported by name": ("from pytest import fixture\n@fixture(params=[pytest.param(0, marks=pytest.mark.posix_only('pty'))])\ndef posix(request):\n    return request.param\n", [(3, IN_FIXTURE)]),
    "param imported by name": ("from pytest import param\n@pytest.mark.parametrize('x', [param(2, marks=pytest.mark.posix_only('pty'))])\ndef test_a(x):\n    pass\n", [(3, OFF_PLACE)]),
    "decorator without a call": ("@pytest.mark.posix_only\ndef test_a():\n    pass\n", [(2, OFF_PLACE)]),
    "an attribute of another object": ("def test_a(args):\n    assert args.posix_only is False\n", [(3, OFF_PLACE)]),
    "pytest.param in a module's pytest_generate_tests": (
        "def pytest_generate_tests(metafunc):\n    if 'x' in metafunc.fixturenames:\n        metafunc.parametrize('x', [pytest.param(0, marks=pytest.mark.posix_only('pty'))])\n",
        [(4, IN_PARAMETRIZE)],
    ),
    "one of two pytest.param in pytest_generate_tests": (
        "def pytest_generate_tests(metafunc):\n    metafunc.parametrize('x', [1, pytest.param(2, marks=pytest.mark.posix_only('pty'))])\n",
        [(3, IN_PARAMETRIZE)],
    ),
    "pytest.param in a class's pytest_generate_tests": (
        "class TestGroup:\n    def pytest_generate_tests(self, metafunc):\n        metafunc.parametrize('x', [pytest.param(0, marks=pytest.mark.posix_only('pty'))])\n    def test_a(self, x):\n        pass\n",
        [(4, IN_PARAMETRIZE)],
    ),
    "parametrize applied to a class by call": (
        "class TestGroup:\n    def test_a(self, x):\n        pass\nTestGroup = pytest.mark.parametrize('x', [pytest.param(0, marks=pytest.mark.posix_only('pty'))])(TestGroup)\n",
        [(5, IN_PARAMETRIZE)],
    ),
    "parametrize bound to a name, then class decorator": (
        "CASES = pytest.mark.parametrize('x', [pytest.param(0, marks=pytest.mark.posix_only('pty'))])\n@CASES\nclass TestGroup:\n    def test_a(self, x):\n        pass\n",
        [(2, IN_PARAMETRIZE)],
    ),
    "parametrize on pytestmark in an if header": ("if (pytestmark := pytest.mark.parametrize('_', [pytest.param(0, marks=pytest.mark.posix_only('pty'))])):\n    pass\n", [(2, IN_PARAMETRIZE)]),
    "parametrize on a global pytestmark set in a function": (
        "def _set():\n    global pytestmark\n    pytestmark = pytest.mark.parametrize('_', [pytest.param(0, marks=pytest.mark.posix_only('pty'))])\n",
        [(4, IN_PARAMETRIZE)],
    ),
}


@pytest.mark.parametrize(("source", "expected"), list(OUTSIDE_THE_TWO_PLACES.values()), ids=list(OUTSIDE_THE_TWO_PLACES))
def test_posix_only_outside_the_two_allowed_places_is_refused(source: str, expected: list[tuple[int, str]]) -> None:
    assert marker_problems(ast.parse("import pytest\n" + source)) == expected


IN_ONE_OF_THE_TWO_PLACES = {
    "function decorator": "@pytest.mark.posix_only('pty')\ndef test_a():\n    pass\n",
    "async function decorator": "@pytest.mark.posix_only('pty')\nasync def test_a():\n    pass\n",
    "method decorator": "class TestGroup:\n    @pytest.mark.posix_only('pty')\n    def test_a(self):\n        pass\n",
    "two markers stacked": "@pytest.mark.posix_only('SIGHUP')\n@pytest.mark.posix_only('SIGUSR1')\ndef test_a():\n    pass\n",
    "function that is not a test": "@pytest.mark.posix_only('pty')\ndef helper():\n    pass\n",
    "pytest.param in a function's parametrize": "@pytest.mark.parametrize('x', [1, pytest.param(2, marks=pytest.mark.posix_only('pty'))])\ndef test_a(x):\n    pass\n",
    "marks= as a list": "@pytest.mark.parametrize('x', [pytest.param(2, marks=[pytest.mark.posix_only('pty'), pytest.mark.slow])])\ndef test_a(x):\n    pass\n",
    "marks= as a tuple": "@pytest.mark.parametrize('x', [pytest.param(2, marks=(pytest.mark.slow, pytest.mark.posix_only('pty')))])\ndef test_a(x):\n    pass\n",
    "pytest.param returned by a helper": "def wrap(path):\n    return pytest.param(path, marks=pytest.mark.posix_only('pty'))\n",
    "pytest.param in a module-level list": "CASES = [pytest.param(1, marks=pytest.mark.posix_only('pty'))]\n",
    "pytest.param in a comprehension in a function's parametrize": "@pytest.mark.parametrize('x', [pytest.param(n, marks=pytest.mark.posix_only('pty')) for n in range(3)])\ndef test_a(x):\n    pass\n",
}


@pytest.mark.parametrize("source", list(IN_ONE_OF_THE_TWO_PLACES.values()), ids=list(IN_ONE_OF_THE_TWO_PLACES))
def test_posix_only_in_one_of_the_two_allowed_places_is_accepted(source: str) -> None:
    assert marker_problems(ast.parse("import pytest\n" + source)) == []


def test_a_marker_defined_in_one_file_is_refused_where_it_is_defined(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    (tests / "support").mkdir(parents=True)
    (tests / "pkg").mkdir()
    (tests / "support/marks.py").write_text("import pytest\nPOSIX = pytest.mark.posix_only('pty')\n", encoding="utf-8")
    (tests / "pkg/test_module.py").write_text("from tests.support.marks import POSIX\npytestmark = POSIX\ndef test_a():\n    pass\n", encoding="utf-8")
    (tests / "pkg/test_class.py").write_text("from tests.support.marks import POSIX\n@POSIX\nclass TestGroup:\n    def test_a(self):\n        pass\n", encoding="utf-8")
    (tests / "pkg/conftest.py").write_text("from tests.support.marks import POSIX\ndef pytest_collection_modifyitems(items):\n    for item in items:\n        item.add_marker(POSIX)\n", encoding="utf-8")
    (tests / "support/hooks.py").write_text("import pytest\ndef pytest_collection_modifyitems(items):\n    for item in items:\n        item.add_marker(pytest.mark.posix_only('pty'))\n", encoding="utf-8")
    assert suite_marker_problems(tests, root=tmp_path) == [("tests/support/hooks.py", 4, OFF_PLACE), ("tests/support/marks.py", 2, OFF_PLACE)]


def test_posix_only_is_refused_in_a_conftest_py(tmp_path: Path) -> None:
    shapes = {
        "import pytest\ndef pytest_collection_modifyitems(items):\n    for item in items:\n        item.add_marker(pytest.mark.posix_only('pty'))\n": 4,
        "import pytest\ndef pytest_collection_modifyitems(items):\n    for item in items:\n        if item.get_closest_marker('posix_only'):\n            pass\n": 4,
        "import pytest\ndef pytest_collection_modifyitems(items):\n    for item in items:\n        if list(item.iter_markers('posix_only')):\n            pass\n": 4,
        "import pytest\nMARK = pytest.mark.posix_only\n": 2,
        "import pytest\n@pytest.mark.posix_only('pty')\ndef pytest_configure(config):\n    pass\n": 2,
        "import pytest\npytestmark = pytest.mark.posix_only('pty')\n": 2,
        "import pytest\nCASES = [pytest.param(0, marks=pytest.mark.posix_only('pty'))]\n": 2,
    }
    for source, line in shapes.items():
        assert conftest_problems(ast.parse(source)) == [(line, IN_CONFTEST)], source
    assert conftest_problems(ast.parse("import pytest\nMARKS = [pytest.mark.posix_only('pty'), pytest.mark.posix_only('fcntl')]\n")) == [(2, IN_CONFTEST)]
    for source in (
        "import pytest\npytestmark = pytest.mark.skipif(os.name == 'nt', reason='windows')\n",
        "import pytest\ndef pytest_collection_modifyitems(items):\n    for item in items:\n        item.add_marker(pytest.mark.skip(reason='posix_only: pty'))\n",
        '"""No test below this directory needs the posix_only marker."""\nimport pytest\n',
    ):
        assert conftest_problems(ast.parse(source)) == [], source
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_a.py").write_text("import pytest\n@pytest.mark.posix_only('pty')\ndef test_a():\n    pass\n", encoding="utf-8")
    conftest = tests / "conftest.py"
    conftest.write_text("import pytest\npytestmark = pytest.mark.skipif(os.name == 'nt', reason='windows')\n", encoding="utf-8")
    assert suite_marker_problems(tests, root=tmp_path) == []
    conftest.write_text("import pytest\ndef pytest_collection_modifyitems(items):\n    for item in items:\n        item.add_marker(pytest.mark.posix_only('pty'))\n", encoding="utf-8")
    assert suite_marker_problems(tests, root=tmp_path) == [("tests/conftest.py", 4, IN_CONFTEST)]
    conftest.write_text("import pytest\npytestmark = pytest.mark.posix_only('pty')\n", encoding="utf-8")
    assert suite_marker_problems(tests, root=tmp_path) == [("tests/conftest.py", 2, IN_CONFTEST)]
    conftest.write_text("import pytest\npytestmark = pytest.mark.posix_only('killpg')\n", encoding="utf-8")
    assert suite_marker_problems(tests, root=tmp_path) == [
        ("tests/conftest.py", 2, "reason 'killpg' is refused by name"),
        ("tests/conftest.py", 2, "reason 'killpg' is not one of the locked reasons"),
        ("tests/conftest.py", 2, "reason 'killpg' is not in allowed_needs"),
        ("tests/conftest.py", 2, IN_CONFTEST),
    ]
    assert marker_problems(ast.parse("import pytest\npytestmark = pytest.mark.posix_only('pty')\n"), conftest=True) == []


def test_a_full_width_letter_in_the_name_does_not_hide_a_marker(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_a.py").write_text("import pytest\n@pytest.mark.posix_ｏnly('killpg')\ndef test_a():\n    pass\n", encoding="utf-8")
    (tests / "test_b.py").write_text("import pytest\npytestmark = pytest.mark.ｐosix_only('pty')\ndef test_b():\n    pass\n", encoding="utf-8")
    assert suite_marker_problems(tests, root=tmp_path) == [*[("tests/test_a.py", 2, problem) for problem in REFUSED_KILLPG], ("tests/test_b.py", 2, ON_PYTESTMARK)]


def test_a_space_before_the_parenthesis_does_not_hide_a_marker(tmp_path: Path) -> None:
    refused = ["reason 'killpg' is refused by name", "reason 'killpg' is not one of the locked reasons", "reason 'killpg' is not in allowed_needs"]
    spaced = "import pytest\n@pytest.mark.posix_only ('killpg')\ndef test_a():\n    pass\n"
    plain = "import pytest\n@pytest.mark.posix_only('killpg')\ndef test_a():\n    pass\n"
    assert marker_problems(ast.parse(spaced)) == marker_problems(ast.parse(plain)) == [(2, problem) for problem in refused]
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_a.py").write_text(spaced, encoding="utf-8")
    assert suite_marker_problems(tests, root=tmp_path) == [("tests/test_a.py", 2, problem) for problem in refused]
    (tests / "test_a.py").write_text("import pytest\n@pytest.mark.posix_only('pty')\ndef test_a():\n    pass\n", encoding="utf-8")
    assert suite_marker_problems(tests, root=tmp_path) == []


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
