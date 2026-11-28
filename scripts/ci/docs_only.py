#!/usr/bin/env python3
"""Classify pull-request changes for the conservative documentation fast path.

The classifier owns the shared two-parent pull-request diff protocol used by
``pytest_impact.py``.  It reads only the pull request head SHA and base ref from
the event payload, compares the merge commit with its first parent, and fails
closed whenever the checked-out history or the runtime reference corpus cannot
be verified.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tokenize
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath
from typing import NamedTuple

try:
    from scripts.git_environment import hardened_git_environment
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.git_environment import hardened_git_environment

MAX_CHANGED_FILES = 5000
_SHA40 = re.compile(r"[0-9a-f]{40}")


class Classification(NamedTuple):
    docs_only: bool
    reason: str


class PullRequestDiff(NamedTuple):
    paths: tuple[str, ...]
    base_ref: str
    head_sha: str
    merge_sha: str
    parent_sha: str


def valid_repository_path(path: str) -> bool:
    if not isinstance(path, str) or not path or path.startswith("/") or any(character in path for character in "\\\0\r\n"):
        return False
    parsed = PurePosixPath(path)
    return parsed.as_posix() == path and not any(part in {"", ".", ".."} for part in parsed.parts)


def _git(root: Path, *args: str) -> str:
    environment = hardened_git_environment(os.environ, include_home=True, propagate_proxy=True)
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        env=environment,
        check=True,
        capture_output=True,
        timeout=60,
    )
    return completed.stdout.decode("utf-8")


def pull_request_diff(root: Path, event: object, event_name: str) -> PullRequestDiff:
    """Return the complete PR net diff from the checked-out merge commit."""

    if event_name != "pull_request" or not isinstance(event, dict):
        raise ValueError("event is not a pull request")
    pull_request = event.get("pull_request")
    if not isinstance(pull_request, dict):
        raise ValueError("missing pull request")
    base = pull_request.get("base")
    head = pull_request.get("head")
    if not isinstance(base, dict) or not isinstance(head, dict):
        raise ValueError("missing pull request refs")
    base_ref = base.get("ref")
    head_sha = head.get("sha")
    if not isinstance(base_ref, str) or not base_ref:
        raise ValueError("missing base ref")
    if not isinstance(head_sha, str) or not _SHA40.fullmatch(head_sha) or head_sha == "0" * 40:
        raise ValueError("invalid pull request head")
    parents = _git(root, "rev-list", "--parents", "-n", "1", "HEAD").split()
    if len(parents) != 3 or parents[2] != head_sha or any(not _SHA40.fullmatch(sha) for sha in parents):
        raise ValueError("checked out commit is not the expected two-parent merge")
    merge_sha, parent_sha = parents[0], parents[1]
    raw = _git(root, "diff", "--no-renames", "--name-status", "-z", parent_sha, "HEAD", "--")
    fields = raw.split("\0")
    if not fields or fields[-1] != "":
        raise ValueError("malformed diff")
    records = fields[:-1]
    if len(records) % 2:
        raise ValueError("malformed diff records")
    paths = tuple(records[index] for index in range(1, len(records), 2))
    if not paths or len(paths) > MAX_CHANGED_FILES or any(not valid_repository_path(path) for path in paths):
        raise ValueError("invalid changed paths")
    return PullRequestDiff(paths, base_ref, head_sha, merge_sha, parent_sha)


def _path_is_regular(root: Path, path: str) -> bool:
    target = root / path
    if target.is_symlink():
        return False
    current = target.parent
    while current != root:
        if current.is_symlink():
            return False
        current = current.parent
    if not target.exists():
        return True
    return target.is_file()


def _is_allowed_doc(root: Path, path: str) -> bool:
    return (
        valid_repository_path(path)
        and path.startswith("docs/")
        and path.endswith(".md")
        and _path_is_regular(root, path)
    )


def _iter_regular_files(root: Path, directory: str, suffixes: frozenset[str] | None = None) -> Iterable[Path]:
    base = root / directory
    try:
        entries = base.rglob("*")
        for path in entries:
            relative = path.relative_to(root).as_posix()
            if "__pycache__" in PurePosixPath(relative).parts or path.suffix == ".pyc":
                continue
            if path.is_symlink() or not path.is_file():
                continue
            if suffixes is not None and path.suffix not in suffixes:
                continue
            yield path
    except OSError:
        raise


def regular_files(root: Path, directory: str) -> Iterable[Path]:
    return _iter_regular_files(root, directory)


def _source_without_comments(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    tokens = tokenize.generate_tokens(iter(text.splitlines(keepends=True)).__next__)
    pieces: list[str] = []
    for token in tokens:
        if token.type != tokenize.COMMENT:
            pieces.append(token.string)
    return " ".join(pieces)


def _corpus(root: Path) -> str:
    pieces: list[str] = []
    for path in _iter_regular_files(root, "tests"):
        pieces.append(path.read_text(encoding="utf-8"))
    for directory in ("scripts", "contract"):
        for path in _iter_regular_files(root, directory, frozenset({".py", ".json", ".sh", ".mjs", ".yaml", ".yml"})):
            if directory == "scripts" and "archived" in path.relative_to(root).parts:
                continue
            pieces.append(path.read_text(encoding="utf-8"))
    for path in _iter_regular_files(root, "src", frozenset({".py"})):
        pieces.append(_source_without_comments(path))
    pieces.extend(path.read_text(encoding="utf-8") for path in _iter_regular_files(root, ".github"))
    for path in (root / "pyproject.toml", root / "GLOSSARY.md"):
        pieces.append(path.read_text(encoding="utf-8"))
    return "\n".join(pieces)


def classify_paths(paths: Iterable[str], *, root: Path | None = None) -> Classification:
    """Return a fail-closed classification for a complete changed-path list."""

    changed = tuple(paths)
    if not changed:
        return Classification(False, "empty change list")
    if any(not _is_allowed_doc(root, path) if root is not None else not (valid_repository_path(path) and path.startswith("docs/") and path.endswith(".md")) for path in changed):
        return Classification(False, "a changed path is outside docs/**/*.md")
    if root is None:
        return Classification(True, "every changed path is an allowed documentation path")
    try:
        corpus = _corpus(root)
        for path in changed:
            if path in corpus or PurePosixPath(path).name in corpus:
                return Classification(False, f"{path}: runtime reference")
    except (OSError, tokenize.TokenError, IndentationError, UnicodeError):
        return Classification(False, "runtime reference scan unavailable")
    return Classification(True, "every changed documentation path is unreferenced")


def is_docs_only(paths: Iterable[str]) -> bool:
    """Pure path-only interface retained for callers that have no checkout."""

    return classify_paths(paths).docs_only


def classify_pull_request(root: Path, event: object, event_name: str) -> Classification:
    """Classify the complete PR diff using the merge-tree runtime corpus."""

    try:
        comparison = pull_request_diff(root, event, event_name)
        return classify_paths(comparison.paths, root=root)
    except (OSError, ValueError, subprocess.SubprocessError):
        return Classification(False, "pull request comparison unavailable")


def _emit(result: Classification) -> None:
    value = str(result.docs_only).lower()
    reason = result.reason.replace("\n", " ").replace("\r", " ")
    print(f"docs_only={value}")
    print(f"reason={reason}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"docs_only={value}\nreason={reason}\n")


def main(argv: Sequence[str] | None = None) -> int:
    values = list(argv or [])
    if values:
        _emit(classify_paths(values, root=Path(__file__).resolve().parents[2]))
        return 0
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    if not event_path:
        _emit(Classification(False, "missing event path"))
        return 0
    try:
        with open(event_path, encoding="utf-8") as stream:
            event = json.load(stream)
    except (OSError, json.JSONDecodeError):
        _emit(Classification(False, "invalid event payload"))
        return 0
    _emit(classify_pull_request(Path(__file__).resolve().parents[2], event, event_name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
