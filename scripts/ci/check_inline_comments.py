"""Remind a pull request that adds inline comments instead of a header block.

Decision (Allen, 2026-10-17, relayed by the coordinator):

* New inline comments are discouraged because a stale comment next to code
  steers later decisions.  The decision record belongs in a **file-header
  block**; docstrings are code documentation and are kept.
* This checker is a **reminder, not a gate**.  It judges only lines added by
  the pull-request diff (``git diff --base...--head``), never the existing
  tree, so the backlog is worked down gradually.  A clean run exits 0; a run
  with findings also exits 0 unless ``--strict`` is passed.  ``--strict`` is
  the single upgrade point when Allen decides to block.
* The CI step in ``.forgejo/workflows/gate-python.yml`` is ``continue-on-error`` and
  emits ``::warning`` annotations, so a finding is visible without reddening
  the job.  That step is the single upgrade point: to block, add ``--strict``
  and drop ``continue-on-error`` there; nothing else in this file changes.

What counts as a new inline comment (rules are fixed and covered by
``tests/tooling/ci/hygiene/test_inline_comments.py``):

* hash comments in Python, Shell, YAML and TOML;
* ``//`` and ``/* */`` comments in JavaScript/TypeScript/MJS, Rust and Go;
* the file-header block is exempt: an optional shebang, a PEP 263 encoding
  line, an optional YAML ``---`` marker, then the leading contiguous run of
  comment-only lines.  The block is computed on the head file, so a new
  comment line inserted above the first code line is still a header line;
* Python comments are found with ``tokenize`` (COMMENT tokens), never a
  regular expression, so docstrings and ``#`` inside strings never count.
  Shell/YAML/TOML/JS/TS/Rust/Go use small stateful scanners that understand
  their string and block-scalar forms; when a line cannot be classified with
  confidence the file yields no finding instead of a guess;
* tool directives are exempt by a fixed allowlist: ``# noqa``, ``# type:``,
  ``# pragma``, ``# pyright:``, ``# ruff:``, ``# fmt:``, ``# isort:``,
  ``# pylint:``, ``# nosec``, ``# yapf:``, ``# mypy:``, ``# coverage:``,
  ``// eslint-disable...``, ``// @ts-ignore``, ``// prettier-ignore``,
  ``// biome-ignore``, ``// rustfmt::skip``, ``//go:build``, ``//go:generate``,
  ``//go:embed``, ``//nolint``, ``/* istanbul ignore */`` and similar.

This file follows its own rule: the explanation is this docstring; the code
carries no inline comments.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path

SCOPE_PREFIXES = (
    "src/",
    "tests/",
    "scripts/",
    "contract/",
    "sidecar/",
    "desktop/",
    "app/",
    ".forgejo/workflows/",
)

PYTHON_SUFFIXES = {".py"}
SHELL_SUFFIXES = {".sh", ".bash"}
YAML_SUFFIXES = {".yml", ".yaml"}
TOML_SUFFIXES = {".toml"}
SLASH_SUFFIXES = {
    ".js",
    ".mjs",
    ".cjs",
    ".jsx",
    ".ts",
    ".tsx",
    ".rs",
    ".go",
}

LANGUAGE_BY_SUFFIX: dict[str, str] = {}
for suffix in PYTHON_SUFFIXES:
    LANGUAGE_BY_SUFFIX[suffix] = "python"
for suffix in SHELL_SUFFIXES:
    LANGUAGE_BY_SUFFIX[suffix] = "shell"
for suffix in YAML_SUFFIXES:
    LANGUAGE_BY_SUFFIX[suffix] = "yaml"
for suffix in TOML_SUFFIXES:
    LANGUAGE_BY_SUFFIX[suffix] = "toml"
for suffix in (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"):
    LANGUAGE_BY_SUFFIX[suffix] = "javascript"
LANGUAGE_BY_SUFFIX[".rs"] = "rust"
LANGUAGE_BY_SUFFIX[".go"] = "go"

HASH_LANGUAGES = frozenset({"python", "shell", "yaml", "toml"})
SLASH_LANGUAGES = frozenset({"javascript", "rust", "go"})

HASH_DIRECTIVES = (
    "noqa",
    "type:",
    "pragma",
    "pyright:",
    "ruff:",
    "fmt:",
    "isort:",
    "pylint:",
    "nosec",
    "yapf:",
    "mypy:",
    "coverage:",
)

SLASH_DIRECTIVES = (
    "eslint-disable",
    "eslint-enable",
    "eslint-disable-line",
    "eslint-disable-next-line",
    "@ts-ignore",
    "@ts-expect-error",
    "@ts-nocheck",
    "prettier-ignore",
    "biome-ignore",
    "rustfmt::skip",
    "go:build",
    "go:generate",
    "go:embed",
    "nolint",
    "istanbul ignore",
    "c8 ignore",
    "v8 ignore",
    "webpackignore",
    "@__pure__",
)

HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
ENCODING_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-_.a-zA-Z0-9]+")
SUGGESTION = "把决策移到文件头决策块（docstring 保留）"
YAML_BLOCK_SCALAR_RE = re.compile(r"(?:^|[\s:-])[|>][+-]?\s*$")
RUST_RAW_RE = re.compile(r'b?r(#*)"')


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int
    comment: str


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _in_scope(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in SCOPE_PREFIXES)


def _language(path: str) -> str | None:
    return LANGUAGE_BY_SUFFIX.get(Path(path).suffix.lower())


def _head_text(repo: Path, head: str, path: str) -> str | None:
    completed = _git(repo, "show", f"{head}:{path}")
    if completed.returncode != 0:
        return None
    try:
        return completed.stdout
    except Exception:
        return None


def _header_end(lines: list[str], language: str) -> int:
    index = 0
    if lines and lines[0].startswith("#!"):
        index = 1
    if language == "python":
        for probe in (index, index + 1):
            if probe < len(lines) and ENCODING_RE.match(lines[probe]):
                index = probe + 1
                break
    if language == "yaml" and index < len(lines) and lines[index].strip() == "---":
        index += 1
    while index < len(lines) and not lines[index].strip():
        index += 1
    if language in HASH_LANGUAGES:
        while index < len(lines) and lines[index].lstrip().startswith("#"):
            index += 1
        return index
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped.startswith("//"):
            index += 1
            continue
        if stripped.startswith("/*"):
            while index < len(lines):
                closes = "*/" in lines[index]
                index += 1
                if closes:
                    break
            continue
        break
    return index


def _python_comments(text: str) -> dict[int, str]:
    comments: dict[int, str] = {}
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for token in tokens:
            if token.type == tokenize.COMMENT:
                comments[token.start[0]] = token.string
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return {}
    return comments


def _scan_shell(lines: list[str]) -> dict[int, str]:
    comments: dict[int, str] = {}
    quote: str | None = None
    for number, line in enumerate(lines, 1):
        position = 0
        while position < len(line):
            char = line[position]
            if quote == "'":
                if char == "'":
                    quote = None
            elif quote == '"':
                if char == "\\":
                    position += 2
                    continue
                if char == '"':
                    quote = None
            elif char == "\\":
                position += 2
                continue
            elif char in {"'", '"'}:
                quote = char
            elif char == "#" and (
                position == 0
                or line[position - 1].isspace()
                or line[position - 1] in ";|&()"
            ):
                comments[number] = line[position:]
                break
            position += 1
    return comments


def _hash_outside_quotes(line: str, require_space: bool) -> str | None:
    quote: str | None = None
    position = 0
    while position < len(line):
        char = line[position]
        if quote == "'":
            if char == "'":
                quote = None
        elif quote == '"':
            if char == "\\":
                position += 2
                continue
            if char == '"':
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == "#":
            if require_space and position > 0 and not line[position - 1].isspace():
                position += 1
                continue
            return line[position:]
        position += 1
    return None


def _yaml_block_lines(lines: list[str]) -> set[int]:
    block: set[int] = set()
    indicator_indent: int | None = None
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if indicator_indent is not None:
            if not stripped:
                block.add(number)
                continue
            indent = len(line) - len(line.lstrip())
            if indent > indicator_indent:
                block.add(number)
                continue
            indicator_indent = None
        if stripped and YAML_BLOCK_SCALAR_RE.search(line):
            indicator_indent = len(line) - len(line.lstrip())
    return block


def _scan_yaml(text: str) -> dict[int, str]:
    lines = text.splitlines()
    block = _yaml_block_lines(lines)
    comments: dict[int, str] = {}
    for number, line in enumerate(lines, 1):
        if number in block:
            continue
        found = _hash_outside_quotes(line, require_space=True)
        if found is not None:
            comments[number] = found
    return comments


def _scan_toml(text: str) -> dict[int, str]:
    comments: dict[int, str] = {}
    triple: str | None = None
    for number, line in enumerate(text.splitlines(), 1):
        position = 0
        while position < len(line):
            if triple is not None:
                if line.startswith(triple, position):
                    position += 3
                    triple = None
                else:
                    position += 1
                continue
            if line.startswith('"""', position):
                triple = '"""'
                position += 3
                continue
            if line.startswith("'''", position):
                triple = "'''"
                position += 3
                continue
            char = line[position]
            if char in {"'", '"'}:
                quote = char
                position += 1
                while position < len(line):
                    if quote == '"' and line[position] == "\\":
                        position += 2
                        continue
                    if line[position] == quote:
                        break
                    position += 1
                if position >= len(line):
                    return {}
                position += 1
                continue
            if char == "#":
                comments[number] = line[position:]
                break
            position += 1
    return comments


def _scan_slash(text: str, language: str) -> dict[int, str]:
    comments: dict[int, str] = {}
    block_depth = 0
    quote: str | None = None
    raw_hashes: int | None = None
    for number, line in enumerate(text.splitlines(), 1):
        position = 0
        while position < len(line):
            if block_depth:
                if language == "rust" and line.startswith("/*", position):
                    block_depth += 1
                    position += 2
                    continue
                if line.startswith("*/", position):
                    block_depth -= 1
                    position += 2
                    continue
                position += 1
                continue
            if raw_hashes is not None:
                closing = '"' + "#" * raw_hashes
                if line.startswith(closing, position):
                    position += len(closing)
                    raw_hashes = None
                    continue
                position += 1
                continue
            if quote is not None:
                if quote != "`" and line[position] == "\\":
                    position += 2
                    continue
                if line[position] == quote:
                    quote = None
                position += 1
                continue
            if language == "javascript" and line[position] == "`":
                quote = "`"
                position += 1
                continue
            if language == "go" and line[position] == "`":
                quote = "`"
                position += 1
                continue
            if (
                language == "rust"
                and line[position] == "r"
                and (position == 0 or not (line[position - 1].isalnum() or line[position - 1] == "_"))
            ):
                match = RUST_RAW_RE.match(line, position)
                if match is not None:
                    raw_hashes = len(match.group(1))
                    position = match.end()
                    continue
            if language in {"javascript", "rust", "go"} and line[position] in {'"', "'"}:
                char = line[position]
                if char == "'" and language in {"rust", "go"}:
                    position += 1
                    continue
                quote = char
                position += 1
                continue
            if line.startswith("//", position):
                comments[number] = line[position:]
                break
            if line.startswith("/*", position):
                comments.setdefault(number, line[position:])
                block_depth = 1
                position += 2
                continue
            position += 1
    return comments


def _comments_for(text: str, language: str) -> dict[int, str]:
    if language == "python":
        return _python_comments(text)
    if language == "shell":
        return _scan_shell(text.splitlines())
    if language == "yaml":
        return _scan_yaml(text)
    if language == "toml":
        return _scan_toml(text)
    if language in SLASH_LANGUAGES:
        return _scan_slash(text, language)
    return {}


def _directive_text(comment: str, language: str) -> str | None:
    if language in HASH_LANGUAGES:
        body = comment.lstrip("#").strip()
        lowered = body.lower()
        if any(lowered.startswith(prefix) for prefix in HASH_DIRECTIVES):
            return None
        return body
    body = comment.strip()
    if body.startswith("//"):
        body = body[2:]
    elif body.startswith("/*"):
        body = body[2:]
        if body.endswith("*/"):
            body = body[:-2]
    body = body.strip().lstrip("*").strip()
    lowered = body.lower()
    if any(lowered.startswith(prefix) for prefix in SLASH_DIRECTIVES):
        return None
    return body


def _added_lines(diff: str) -> list[tuple[str, int, str]]:
    added: list[tuple[str, int, str]] = []
    path: str | None = None
    new_line = 0
    remaining_new = 0
    remaining_old = 0
    for raw in diff.splitlines():
        if remaining_new > 0 or remaining_old > 0:
            if raw.startswith("+"):
                added.append((path or "", new_line, raw[1:]))
                new_line += 1
                remaining_new -= 1
                continue
            if raw.startswith("-"):
                remaining_old -= 1
                continue
            if raw.startswith(" "):
                new_line += 1
                remaining_new -= 1
                remaining_old -= 1
                continue
            continue
        if raw.startswith("+++ "):
            candidate = raw[4:].strip()
            path = candidate[2:] if candidate.startswith("b/") else None
            continue
        match = HUNK_RE.match(raw)
        if match is not None:
            remaining_old = int(match.group(1) or "1")
            new_line = int(match.group(2))
            remaining_new = int(match.group(3) or "1")
    return added


def _collect(repo: Path, base: str, head: str) -> tuple[list[Finding], str | None]:
    diff = _git(
        repo,
        "diff",
        "--unified=0",
        "--no-color",
        "--no-ext-diff",
        "--diff-filter=ACMR",
        f"{base}...{head}",
        "--",
        *SCOPE_PREFIXES,
    )
    if diff.returncode != 0:
        return [], diff.stderr.strip() or "git diff failed"
    by_path: dict[str, list[tuple[int, str]]] = {}
    for path, line, text in _added_lines(diff.stdout):
        if path and _in_scope(path):
            by_path.setdefault(path, []).append((line, text))
    findings: list[Finding] = []
    for path in sorted(by_path):
        language = _language(path)
        if language is None:
            continue
        text = _head_text(repo, head, path)
        if text is None:
            continue
        lines = text.splitlines()
        header = _header_end(lines, language)
        comments = _comments_for(text, language)
        for line, _added_text in sorted(by_path[path]):
            if line <= header:
                continue
            comment = comments.get(line)
            if comment is None:
                continue
            body = _directive_text(comment, language)
            if body is None:
                continue
            findings.append(Finding(path=path, line=line, comment=body))
    return findings, None


def _emit_text(findings: list[Finding], annotations: bool) -> None:
    for finding in findings:
        print(f"{finding.path}:{finding.line}: inline comment `{finding.comment}` -> {SUGGESTION}")
    print(
        f"inline-comment: {len(findings)} new inline comment line(s); "
        f"{SUGGESTION}"
    )
    if annotations:
        for finding in findings:
            print(
                f"::warning file={finding.path},line={finding.line}::"
                f"new inline comment; {SUGGESTION}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="base revision of the PR diff")
    parser.add_argument("--head", default="HEAD", help="head revision (default: HEAD)")
    parser.add_argument("--repo", default=".", help="repository root (default: .)")
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    parser.add_argument(
        "--annotations",
        action="store_true",
        help="emit ::warning file=...,line=... annotations",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 when findings exist (the single blocking upgrade point)",
    )
    args = parser.parse_args(argv)
    repo = Path(args.repo)
    findings, error = _collect(repo, args.base, args.head)
    if error is not None:
        print(f"inline-comment: cannot read the diff: {error}", file=sys.stderr)
        return 2
    if args.json:
        payload = {
            "base": args.base,
            "head": args.head,
            "strict": bool(args.strict),
            "findings": [
                {
                    "path": finding.path,
                    "line": finding.line,
                    "comment": finding.comment,
                    "suggestion": SUGGESTION,
                }
                for finding in findings
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if args.annotations:
            for finding in findings:
                print(
                    f"::warning file={finding.path},line={finding.line}::"
                    f"new inline comment; {SUGGESTION}",
                    file=sys.stderr,
                )
    else:
        _emit_text(findings, args.annotations)
    if args.strict and findings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
