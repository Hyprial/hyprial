#!/usr/bin/env python3
"""Conservatively classify pull-request paths for the docs-only CI fast path.

GLOSSARY.md is an exact test input (cell model phase B, S9a): the anchor gate
tests/tooling/documentation/test_glossary_anchors.py opens it by path, so a
glossary-only change must run that gate instead of the docs-only fast path.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import PurePosixPath
from typing import NamedTuple


# These Markdown files are inputs to tests that open them by exact repository
# path. They are fixtures/contracts, not docs-only changes:
# - README.md: repository rename, installer, and public-project contracts.
# - CONTRIBUTING.md: public-project documentation contract.
# - docs/guides/operator-guide.md and docs/guides/cutover-runbook.md: repository rename and
#   operator-documentation contracts.
# - docs/design/hyprial-login-design.md: login design/tag contract.
# - docs/domains/squire/SKILL-template.md: packaged-template and Skill-Impact contracts.
# - docs/notes/contract-align-report.md: repository rename/alignment contract.
# - docs/reference/cli-command-inventory.json: generated command-tree input
#   compared by the CLI inventory gate; a JSON-only change or deletion must run
#   that gate instead of taking the Markdown docs-only fast path.
EXACT_TEST_INPUTS = frozenset(
    {
        "CONTRIBUTING.md",
        "GLOSSARY.md",
        "README.md",
        "docs/reference/cli-command-inventory.json",
        "docs/guides/cutover-runbook.md",
        "docs/design/hyprial-login-design.md",
        "docs/guides/operator-guide.md",
        "docs/domains/squire/SKILL-template.md",
        "docs/notes/contract-align-report.md",
    }
)

# Markdown under these trees can be executable, generated, scanned, packaged,
# or otherwise test-owned. Keep this list broader than the positive allowlist.
EXCLUDED_PREFIXES = (
    ".agents/",
    ".github/",
    "contract/",
    "gui/",
    "scripts/",
    "sidecar/",
    "src/",
    "tests/",
)


class Classification(NamedTuple):
    docs_only: bool
    reason: str


def _valid_repository_path(path: str) -> bool:
    if not path or path.startswith("/") or "\\" in path:
        return False
    parsed = PurePosixPath(path)
    return parsed.as_posix() == path and ".." not in parsed.parts


def _path_reason(path: str) -> str | None:
    if not _valid_repository_path(path):
        return "invalid repository-relative path"
    if path in EXACT_TEST_INPUTS:
        return "exact test input"
    if path.startswith(EXCLUDED_PREFIXES):
        return "excluded test, runtime, build, or workflow tree"
    if path.startswith("docs/") or path.endswith(".md"):
        return None
    return "outside the documentation allowlist"


def classify_paths(paths: Iterable[str]) -> Classification:
    """Return a fail-closed classification for a complete changed-path list."""

    changed = tuple(paths)
    if not changed:
        return Classification(False, "empty change list")
    for path in changed:
        reason = _path_reason(path)
        if reason is not None:
            return Classification(False, f"{path}: {reason}")
    return Classification(True, "every changed path is documentation-only")


def is_docs_only(paths: Iterable[str]) -> bool:
    """Pure boolean interface shared by unit tests and callers."""

    return classify_paths(paths).docs_only


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return completed.stdout


def _pull_request_paths(event: object, event_name: str) -> Classification:
    if event_name != "pull_request":
        return Classification(False, "event is not pull_request")
    if not isinstance(event, dict):
        return Classification(False, "invalid event payload")
    pull_request = event.get("pull_request")
    if not isinstance(pull_request, dict):
        return Classification(False, "missing pull_request payload")
    try:
        base = pull_request["base"]["sha"]
        head = pull_request["head"]["sha"]
    except (KeyError, TypeError):
        return Classification(False, "missing comparison commit")
    if not all(
        isinstance(sha, str)
        and len(sha) == 40
        and sha != "0" * 40
        and all(character in "0123456789abcdef" for character in sha)
        for sha in (base, head)
    ):
        return Classification(False, "invalid comparison commit")
    try:
        merge_base = _git("merge-base", base, head).strip()
        raw_paths = _git(
            "diff", "--no-renames", "--name-only", "-z", merge_base, head, "--"
        )
    except (OSError, subprocess.SubprocessError):
        return Classification(False, "comparison unavailable")
    return classify_paths(path for path in raw_paths.split("\0") if path)


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
    if argv:
        _emit(classify_paths(argv))
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
    _emit(_pull_request_paths(event, event_name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(os.sys.argv[1:]))
