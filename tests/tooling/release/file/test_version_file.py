"""The version lives in ./VERSION, and nothing reads it out of pyproject.toml.

Allen 2026-09-18: 「VERSION 文件单独放,pyproject.toml 引用这个文件」.

Why it is worth a file of its own, measured rather than asserted: at the v0.4.6
tag and at every commit since -- 357 of them when this was written --
``pyproject.toml`` said ``version = "0.4.6"``. So "0.4.6" named 358 different
trees and identified none of them, and two installations reporting the same
version differed by a release's worth of code. Three commands reproduce it::

    git show v0.4.6:pyproject.toml | grep '^version'
    git show dev:pyproject.toml    | grep '^version'
    git rev-list --count v0.4.6..dev

A version is an assertion about what a tree contains, and that one asserted
something nobody checked. On its own line in its own file, "did the version
change?" becomes a diff a machine can read.

The load-bearing test here is not that VERSION exists -- it is
``test_nothing_reads_the_version_out_of_pyproject``. Moving the number is the
easy half; the half that breaks things is that four callers were parsing
``["project"]["version"]``, and with the version declared dynamic that key is
simply gone. Two of them read it with ``tomllib`` (KeyError) and one with
``sed`` (empty string). The empty string is the interesting one: it flowed into
a release gate's comparison, which happens to fail closed, so that gate blocks
releases rather than passing them -- loud, but still broken.
"""

from __future__ import annotations

import ast
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
VERSION_FILE = ROOT / "VERSION"
PYPROJECT = ROOT / "pyproject.toml"

# PEP 440 enough for this purpose: a release number, optionally with a local
# segment, which is what scripts/build/build-wheel.sh stamps onto built artifacts.
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+([.\-+][0-9A-Za-z.\-+]+)?$")


def _is_literal_assignment(body: str) -> bool:
    """A file-name inventory is data, not a reader of project metadata."""
    try:
        statements = ast.parse(body).body
        if len(statements) != 1 or not isinstance(statements[0], ast.Assign):
            return False
        ast.literal_eval(statements[0].value)
        return True
    except (SyntaxError, ValueError, TypeError):
        return False


@pytest.mark.parametrize(("body", "harmless"), [
    ("ROOT_FILES = {'VERSION', 'pyproject.toml'}", True),
    ('VERSION = metadata["project"]["version"]', False),
    ('version = Path("pyproject.toml").read_text()', False),
])
def test_literal_inventory_exception_does_not_hide_metadata_reads(body: str, harmless: bool) -> None:
    assert _is_literal_assignment(body) is harmless


def test_the_version_file_holds_exactly_one_version() -> None:
    """One line, nothing else -- so a diff touching it is unambiguous."""

    raw = VERSION_FILE.read_text(encoding="utf-8")
    lines = [line for line in raw.splitlines() if line.strip()]

    assert len(lines) == 1, f"VERSION must hold one line, got {lines!r}"
    assert VERSION_RE.match(lines[0]), f"not a version: {lines[0]!r}"
    assert raw.endswith("\n"), "VERSION must end with a newline"


def test_pyproject_defers_to_the_version_file() -> None:
    """The reference Allen asked for, asserted at the level the build reads."""

    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))

    assert "version" in data["project"].get("dynamic", []), (
        "the version must be dynamic, or pyproject is a second place to change"
    )
    assert "version" not in data["project"], (
        "a static version here would silently win over VERSION"
    )
    assert data["tool"]["hatch"]["version"]["path"] == "VERSION"


def test_the_configured_pattern_actually_matches_the_file() -> None:
    """⭐ The two halves are configured separately and can drift apart.

    A pattern that stops matching is a build error rather than a silent
    fallback, but it is a build error at release time -- which is the worst
    moment to discover it. Apply the configured pattern to the configured file
    here instead, and assert it yields the version the file states.
    """

    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    pattern = data["tool"]["hatch"]["version"]["pattern"]
    stated = VERSION_FILE.read_text(encoding="utf-8").strip()

    match = re.search(pattern, VERSION_FILE.read_text(encoding="utf-8"))

    assert match is not None, f"pattern {pattern!r} does not match VERSION"
    assert match.group("version") == stated, (
        f"pattern yields {match.group('version')!r}, file states {stated!r}"
    )


def test_nothing_reads_the_version_out_of_pyproject() -> None:
    """⛔ The control on the half of this change that breaks things.

    Four callers parsed ``["project"]["version"]`` before this: build-wheel.sh,
    gate-gui.yml, release-tag.yml's version gate, and the docs describing the wheel
    stamp. Declaring the version dynamic removes that key, so each of them
    reads nothing -- and the ``sed`` one yields an empty string rather than an
    error, which is the shape that gets past a careless reader.

    Tracked ("does anyone still read the old place?") rather than by listing
    the four, because the failure being guarded against is a FIFTH caller
    appearing later, and a list of four cannot see that one.
    """

    # Two shapes, because the four callers used two: a tomllib subscript, and
    # a sed/grep over pyproject's text.
    hits: list[str] = []
    # Order-independent on purpose: `sed -n 's/^version.../p' pyproject.toml`
    # puts the two tokens the other way round, and a pattern that assumed one
    # order let exactly that shape through when it was mutation-checked.
    for pattern in (
        r'\[.project.\]\[.version.\]',
        r'pyproject[^:]*version|version[^:]*pyproject',
    ):
        hits += subprocess.run(
            ["git", "grep", "-n", "-E", pattern,
             "--", "*.py", "*.sh", "*.yml", "*.yaml", "*.toml"],
            cwd=ROOT, capture_output=True, text=True,
        ).stdout.splitlines()

    def _is_prose(line: str) -> bool:
        """Prose mentioning the move is not a read of it.

        Deliberately NOT "skip the tests directory": the fifth caller this
        guard actually caught was in tests/, so excluding tests would have
        hidden the one hit that mattered. Filter on shape instead -- a read
        subscripts, calls, redirects or assigns, and a sentence does none of
        those.
        """

        body = line.split(":", 2)[-1].lstrip()
        if body.startswith("#"):
            return True
        return _is_literal_assignment(body) or not any(ch in body for ch in "[(<=$|")

    # This file always matches itself: a guard has to name
    # what it forbids, so its own docstring and patterns quote both tokens.
    # Excluded by exact path, NOT by directory -- the fifth caller this guard
    # caught was tests/test_cli_version.py, so skipping tests/ would have
    # hidden the only hit that mattered.
    #
    # This exclusion was added because the guard shipped red: every run that
    # validated it happened while this file was still untracked, and `git
    # grep` does not search untracked files. The instrument could not see
    # itself until the moment it was committed, which is also the moment it
    # started being wrong.
    own_path = Path(__file__).resolve().relative_to(ROOT).as_posix()

    offenders = [
        line for line in sorted(set(hits))
        # Seed repos built by the E2E scenarios are their own projects: they
        # write a static version into a throwaway pyproject on purpose, and
        # are not readers of THIS repository's version.
        if not line.startswith("contract/e2e/")
        and not line.startswith(f"{own_path}:")
        # Prose about the move is not a read of it -- see _is_prose for why
        # this filters on shape rather than on which directory the line is in.
        and not _is_prose(line)
    ]

    assert not offenders, (
        "something still reads the version out of pyproject.toml, where it no "
        "longer exists:\n  " + "\n  ".join(offenders)
    )


def test_the_search_above_can_actually_find_things() -> None:
    """⭐ Positive control for the previous test's instrument.

    A negative from a grep whose pattern or pathspec is wrong is indentical to
    a negative from a clean tree. This runs the same machinery against a token
    that must be present, so an empty result there means the sweep is broken
    rather than the tree being clean.
    """

    hits = subprocess.run(
        ["git", "grep", "-l", "-E", r"hatch", "--", "*.toml"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.splitlines()

    assert "pyproject.toml" in hits, (
        f"the sweep cannot see pyproject.toml; the previous test proves "
        f"nothing. saw: {hits!r}"
    )
