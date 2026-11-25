"""Version edits for disposable clones; separate from installed CLI resolution."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

PROJECT_VERSION = re.compile(r'(?m)^(version\s*=\s*")([^"]+)("\s*)$')

def write_project_version(
    project: Path, version_value: str, *, expected: str | None = None
) -> str:
    """Set a disposable clone's package version and return its old value.

    Two layouts are live at once: trees before d9382ea6 (2026-09-18) carry a
    literal ``version = "..."`` in pyproject.toml, later ones declare the
    version dynamic and keep it on the first line of the file named by
    ``[tool.hatch.version] path``.  E2E-010 installs one of each (an older
    promoted tree, then the candidate), so both must work; anything else is
    an error rather than a guess.
    """

    before = project.read_text(encoding="utf-8")
    match = PROJECT_VERSION.search(before)
    if match is not None:
        old = match.group(2)
        _expect_version(old, expected)
        project.write_text(
            before[: match.start(2)] + version_value + before[match.end(2) :],
            encoding="utf-8",
        )
        return old
    version_file = _dynamic_version_file(project, before)
    lines = version_file.read_text(encoding="utf-8").splitlines(keepends=True)
    old = lines[0].strip() if lines else ""
    if not old:
        raise AssertionError(f"project version is missing from {version_file}")
    _expect_version(old, expected)
    version_file.write_text(
        "".join([version_value + "\n", *lines[1:]]), encoding="utf-8"
    )
    return old

def _expect_version(old: str, expected: str | None) -> None:
    if expected is not None and old != expected:
        raise AssertionError(
            f"expected disposable package version {expected!r}, got {old!r}"
        )

def _dynamic_version_file(project: Path, text: str) -> Path:
    """The file hatchling reads the version from, or an AssertionError."""

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise AssertionError(f"{project} is not valid TOML: {error}") from error
    dynamic = data.get("project", {}).get("dynamic", [])
    path = data.get("tool", {}).get("hatch", {}).get("version", {}).get("path")
    if "version" not in dynamic or not isinstance(path, str) or not path:
        raise AssertionError(f"project version is missing from {project}")
    return project.parent / path
