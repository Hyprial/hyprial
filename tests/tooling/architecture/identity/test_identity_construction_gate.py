"""Identity construction stays behind the canonical kernel helpers."""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SOURCE_ROOT = REPO_ROOT / "src" / "hyprial"

# Compatibility-only construction from pre-validation stored/migration data.
# Each call preserves the former f"user:{value}" output; new identity writers
# must use canonical_user_uri instead.
LEGACY_USER_URI_ALLOWLIST = {
    "daemon/impl/application/actors/routines.py:299": "stored agent owner in routine escalation",
    "daemon/impl/application/actors/routines.py:313": "stored agent owner in routine record",
    "daemon/impl/application/actors/routines.py:322": "stored agent owner in coordinator request",
}


def _source_lines() -> list[tuple[str, int, str]]:
    rows: list[tuple[str, int, str]] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        rows.extend(
            (relative, number, line)
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            )
        )
    return rows


def test_no_hand_built_f_string_user_uri() -> None:
    findings = [
        f"{path}:{number}: {line.strip()}"
        for path, number, line in _source_lines()
        if 'f"user:' in line or "f'user:" in line
    ]
    assert not findings, "use canonical_user_uri():\n" + "\n".join(findings)


def test_legacy_user_uri_calls_are_exactly_the_reviewed_compatibility_sites() -> None:
    observed: set[str] = set()
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        observed.update(
            f"{relative}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "legacy_user_uri"
        )
    assert observed == set(LEGACY_USER_URI_ALLOWLIST), (
        "legacy_user_uri() is only for reviewed stored/migration compatibility; "
        f"unexpected allowlist delta: {sorted(observed ^ set(LEGACY_USER_URI_ALLOWLIST))}"
    )
