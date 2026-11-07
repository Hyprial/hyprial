"""Guard: no personal git identity literal may return to src/ (brief agent-git-identity).

The default author was once hard-coded as one person (introduced caf80aa5a5,
2026-09-23).  This guard fails the suite if the removed name/email pair or
the removed built-in profile constant reappears under the product source root.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[4] / "src"
FORBIDDEN_LITERALS = (
    "Allen Woods",
    "allenwoods@users.noreply.code.hyprial.com",
)
FORBIDDEN_SYMBOLS = (
    "DEFAULT_AGENT_TOOL_PROFILE",
    "agent-home-p2-allen-v1",
)


def _source_files() -> list[Path]:
    return [path for path in SRC_ROOT.rglob("*") if path.is_file()]


@pytest.mark.parametrize("literal", FORBIDDEN_LITERALS)
def test_src_contains_no_personal_git_identity_literal(literal: str) -> None:
    offenders = [
        path
        for path in _source_files()
        if literal.lower() in path.read_text(encoding="utf-8", errors="replace").lower()
    ]
    assert offenders == [], (
        f"personal git identity literal {literal!r} reappeared in src/: "
        + ", ".join(str(path) for path in offenders)
    )


@pytest.mark.parametrize("symbol", FORBIDDEN_SYMBOLS)
def test_src_contains_no_built_in_person_profile_symbol(symbol: str) -> None:
    offenders = [
        path
        for path in _source_files()
        if symbol in path.read_text(encoding="utf-8", errors="replace")
    ]
    assert offenders == [], (
        f"built-in person profile symbol {symbol!r} reappeared in src/: "
        + ", ".join(str(path) for path in offenders)
    )
