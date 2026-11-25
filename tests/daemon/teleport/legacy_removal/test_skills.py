"""Slice 7: shipped and repository skills no longer instruct the removed surface.

Decisions:

- Scope is the operational instructions an agent follows: ``.agents/skills``
  and the packaged ``src/hyprial/skills``.  ``docs/notes`` and ``docs/evidence``
  are WORM history and historical design documents describe the past; both
  are out of scope here.
- A hit is an ``agent move`` / ``agent transfer`` command or one of the nine
  legacy ``transfer.*`` daemon methods, in any text file of those trees.
"""

from __future__ import annotations

import re

import pytest

from .support import LEGACY_METHODS, ROOT

SKILL_ROOTS = (ROOT / ".agents" / "skills", ROOT / "src" / "hyprial" / "skills")

LEGACY_TEXT = re.compile(
    r"\bagent\s+(move|transfer)\b|(?<![\w.])("
    + "|".join(re.escape(name) for name in sorted(LEGACY_METHODS, key=len, reverse=True))
    + r")(?![\w])"
)


def _text_files(root):
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        try:
            yield path, path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue


@pytest.mark.parametrize("root", SKILL_ROOTS, ids=lambda root: str(root.relative_to(ROOT)))
def test_skills_do_not_instruct_the_removed_surface(root) -> None:
    assert root.is_dir()
    hits: list[str] = []
    for path, text in _text_files(root):
        for number, line in enumerate(text.splitlines(), start=1):
            match = LEGACY_TEXT.search(line)
            if match:
                hits.append(f"  {path.relative_to(ROOT)}:{number}: {match.group(0)}")
    assert hits == [], "skills still describe agent move/transfer:\n" + "\n".join(hits)
