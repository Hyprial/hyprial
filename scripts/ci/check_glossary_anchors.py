"""Read-only, standard-library GLOSSARY.md anchor gate (Python 3.12+).

Decisions (cell model phase B, S9a):

- GLOSSARY.md names a code anchor for each term.  The anchors of the cell /
  thread / conversation model drift as the model moves code (``links`` became
  ``threads``, the squire retires), so every anchor in the CHECKED_SECTIONS
  tables must point at a path, or a ``path::Symbol``, that exists in the tree.
- Only the sections named in CHECKED_SECTIONS are gated.  The other tables still
  carry prose anchors (``design.md §2 命名词表``); widening the gate is a one-line
  change to CHECKED_SECTIONS once their anchors are paths.
- Inside a checked section an anchor cell is strict: one or more anchors joined
  by ``、``, each optionally wrapped in backticks and followed by one parenthesised
  annotation (``（待迁移）``), or exactly ``同上`` for the previous row's anchors.
  Anything else (prose, an empty cell, two paths joined by a space, a path that
  leaves the repository) is ``unparseable-anchor``, so the table cannot silently
  stop being checked.
- A path is relative to the repository root and must exist; a trailing ``/``
  demands a directory.  ``path::Name`` or ``path::Class.member`` must be a Python
  file whose AST defines (or imports) ``Name`` at module level and, for each
  further segment, a member of that class (def, assignment or nested class).
- A checked section that is missing, has no ``锚点`` column, or yields no anchor
  at all is a failure: the gate never passes by checking nothing.
- Output follows check_module_boundaries.py: a JSON report on stdout, one line
  per violation on stderr, exit 1 on FAIL.  The CI gate is
  tests/tooling/documentation/test_glossary_anchors.py, and GLOSSARY.md is an
  exact test input in scripts/ci/docs_only.py, so a glossary-only change runs it.
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path, PurePosixPath
import re
import sys

CHECKED_SECTIONS = ("组织与会话模型",)
ANCHOR_COLUMN = "锚点"
SAME_AS_ABOVE = "同上"
SEPARATOR = "、"
ANNOTATION = re.compile(r"\s*[（(][^（）()]*[）)]$")
PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")
SYMBOL = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
TABLE_RULE = re.compile(r"^\|\s*:?-{3,}")


def cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_anchor(piece: str) -> tuple[str, str | None] | None:
    """(path, symbol) for one anchor, or None when it is not an anchor."""

    text = ANNOTATION.sub("", piece.strip()).strip()
    if len(text) > 1 and text.startswith("`") and text.endswith("`"):
        text = text[1:-1].strip()
    path, _, symbol = text.partition("::")
    if not PATH.fullmatch(path) or (symbol and not SYMBOL.fullmatch(symbol)) or (_ and not symbol):
        return None
    if ".." in PurePosixPath(path).parts:
        return None
    return path, symbol or None


def members(body: list[ast.stmt]) -> dict[str, ast.AST]:
    found: dict[str, ast.AST] = {}
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found[node.name] = node
        elif isinstance(node, ast.Assign):
            found.update((target.id, node) for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found[node.target.id] = node
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            found.update(((alias.asname or alias.name).split(".")[0], node) for alias in node.names)
    return found


def symbol_exists(tree: ast.Module, symbol: str) -> bool:
    body: list[ast.stmt] | None = tree.body
    for name in symbol.split("."):
        if body is None:
            return False
        node = members(body).get(name)
        if node is None:
            return False
        body = node.body if isinstance(node, ast.ClassDef) else None
    return True


class Audit:
    def __init__(self, root: Path, glossary: Path):
        self.root = root
        self.glossary = glossary
        self.checked: list[dict] = []
        self.violations: list[dict] = []
        self.trees: dict[Path, ast.Module | None] = {}

    def error(self, kind: str, line: int | None = None, term: str | None = None, anchor: str | None = None, **details):
        self.violations.append({"kind": kind, "line": line, "term": term, "anchor": anchor, **details})

    def tree(self, path: Path) -> ast.Module | None:
        if path not in self.trees:
            try:
                self.trees[path] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError, ValueError):
                self.trees[path] = None
        return self.trees[path]

    def resolve(self, line: int, term: str, path: str, symbol: str | None):
        anchor = f"{path}::{symbol}" if symbol else path
        self.checked.append({"line": line, "term": term, "anchor": anchor})
        target = self.root / path
        if not (target.is_dir() if path.endswith("/") else target.exists()):
            self.error("missing-anchor-path", line, term, anchor)
            return
        if symbol is None:
            return
        if target.suffix != ".py" or not target.is_file():
            self.error("symbol-on-non-python", line, term, anchor)
            return
        tree = self.tree(target)
        if tree is None:
            self.error("unparseable-python", line, term, anchor)
        elif not symbol_exists(tree, symbol):
            self.error("missing-anchor-symbol", line, term, anchor)

    def section(self, title: str, lines: list[tuple[int, str]]):
        header = next(((index, cells(text)) for index, (_, text) in enumerate(lines) if text.startswith("|")), None)
        if header is None or ANCHOR_COLUMN not in header[1]:
            self.error("missing-anchor-column", lines[0][0] if lines else None, section=title)
            return
        start, columns = header
        column = columns.index(ANCHOR_COLUMN)
        previous: list[tuple[str, str | None]] | None = None
        for number, text in lines[start + 1:]:
            if not text.startswith("|") or TABLE_RULE.match(text):
                continue
            row = cells(text)
            term = row[0] if row else ""
            if len(row) != len(columns):
                self.error("malformed-row", number, term, None, section=title)
                previous = None
                continue
            cell = row[column]
            if cell == SAME_AS_ABOVE:
                if previous is None:
                    self.error("dangling-same-as-above", number, term, cell, section=title)
                    continue
                anchors = previous
            else:
                parsed = [parse_anchor(piece) for piece in cell.split(SEPARATOR)] if cell else [None]
                if any(item is None for item in parsed):
                    self.error("unparseable-anchor", number, term, cell, section=title)
                    previous = None
                    continue
                anchors = parsed
            for path, symbol in anchors:
                self.resolve(number, term, path, symbol)
            previous = anchors

    def run(self) -> dict:
        try:
            text = self.glossary.read_text(encoding="utf-8")
        except OSError as error:
            self.error("missing-glossary", detail=str(error))
            return self.report()
        sections: dict[str, list[tuple[int, str]]] = {}
        current: list[tuple[int, str]] | None = None
        for number, line in enumerate(text.splitlines(), start=1):
            if line.startswith("## "):
                title = line[3:].strip()
                key = next((name for name in CHECKED_SECTIONS if title.startswith(name)), None)
                current = sections.setdefault(key, []) if key else None
                continue
            if current is not None:
                current.append((number, line.strip()))
        for name in CHECKED_SECTIONS:
            if name not in sections:
                self.error("missing-section", section=name)
                continue
            before = len(self.checked)
            self.section(name, sections[name])
            if len(self.checked) == before and not any(row.get("section") == name for row in self.violations):
                self.error("no-anchors-checked", section=name)
        return self.report()

    def report(self) -> dict:
        return {
            "status": "FAIL" if self.violations else "PASS",
            "glossary": str(self.glossary),
            "sections": list(CHECKED_SECTIONS),
            "checked": self.checked,
            "violations": self.violations,
        }


def scan(root: Path, glossary: Path | None = None) -> dict:
    root = Path(root).resolve()
    return Audit(root, Path(glossary) if glossary else root / "GLOSSARY.md").run()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--glossary", type=Path, default=None)
    arguments = parser.parse_args()
    report = scan(arguments.root, arguments.glossary)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    for row in report["violations"]:
        where = f"GLOSSARY.md:{row['line']}" if row.get("line") else "GLOSSARY.md"
        print(f"{row['kind']}: {row.get('term') or row.get('section') or ''} -> {row.get('anchor') or ''} ({where})", file=sys.stderr)
    return 1 if report["violations"] else 0


if __name__ == "__main__":
    sys.exit(main())
