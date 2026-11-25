#!/usr/bin/env python3
"""Retain the version-specific changelog gate for legacy release-target PRs.

Dev-first preparation targets dev and is not a promotion. Formal release-tag
validation checks the selected dev candidate's VERSION/CHANGELOG independently.

Scope (Allen 2026-10-29): a pull request whose base branch is ``release/vX.Y.Z`` must

1. contain a ``## [X.Y.Z]`` (or ``## X.Y.Z``) section in ``CHANGELOG.md`` at the head commit, and
2. actually change that section in this pull request (adding an unrelated section, or a
   section that already existed untouched, is not a changelog entry).

Everything else is out of scope and exits 0 with a reason:

* base branch is not ``release/**`` (this is the normal dev/main pull request);
* the pull request is a sync (``dev`` -> ``release/**``), which only fast-forwards work that
  was already changelogged when it entered dev (Allen 2026-11-03).

The check reads Git, never the working tree, so it is deterministic in CI and in a repro.
Exit codes: 0 pass or not applicable, 1 gate failure, 2 usage/input error.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

CHANGELOG = "CHANGELOG.md"
RELEASE_PREFIX = "release/"

# ``## [0.5.5.1] - 2026-10-28`` and ``## 0.5.5.1`` are both accepted; ATX only, matching
# the file's own convention (see scripts/ci/check-changelog.mjs).
_VERSION_PATTERN = r"\d+\.\d+\.\d+(?:\.\d+)*(?:\.[A-Za-z]+\d*)?"
_SECTION = re.compile(r"^##[ \t]+\[?(?P<version>" + _VERSION_PATTERN + r")\]?(?:[ \t].*)?$", re.MULTILINE)


def version_of_branch(branch: str) -> str | None:
    """release/v0.5.5.3 -> 0.5.5.3 ; anything else -> None."""
    if not branch.startswith(RELEASE_PREFIX):
        return None
    candidate = branch[len(RELEASE_PREFIX):].strip()
    if candidate.startswith("v"):
        candidate = candidate[1:]
    return candidate if re.fullmatch(_VERSION_PATTERN, candidate) else None


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout


def section_spans(text: str) -> list[tuple[str, int, int]]:
    """(version, start_line, end_line) for every ATX version section, 1-based, end exclusive.

    A section ends at the NEXT level-2 heading of any kind, not at the next version heading:
    ``## [Unreleased]`` closes the section above it, and a change inside Unreleased must not
    count as a change inside the version section.
    """
    headings = [match.start() for match in re.finditer(r"^##[ \t]", text, re.MULTILINE)]
    headings.append(len(text))
    spans: list[tuple[str, int, int]] = []
    for index in range(len(headings) - 1):
        start_offset, end_offset = headings[index], headings[index + 1]
        heading_line = text[start_offset:].splitlines()[0]
        match = _SECTION.match(heading_line)
        if match is None:
            continue
        # 1-based, same coordinate system as unified-diff hunk line numbers.
        start = text.count("\n", 0, start_offset) + 1
        end = text.count("\n", 0, end_offset) + 1  # start line of the next heading == exclusive end
        spans.append((match.group("version"), start, end))
    return spans


class Hunk(NamedTuple):
    """One ``@@ -a,b +c,d @@`` hunk: where it lands at head, and what it does."""

    head_start: int
    head_count: int
    added: tuple[int, ...]
    removed: int

    def touches(self, start: int, end: int) -> bool:
        """Whether the hunk lands inside the 1-based, end-exclusive span [start, end)."""
        if self.head_count == 0:
            # A pure deletion carries no head lines; it is a point at head_start.
            return start <= self.head_start < end
        return any(start <= number < end for number in range(self.head_start, self.head_start + self.head_count))


def parse_hunks(diff_text: str) -> list[Hunk]:
    """Hunks with their head-side additions and their removal count.

    A ``+c,0`` hunk has NO head lines. Treating it as one line (the old bug) invented a changed
    line that nobody wrote, so a deletion-only pull request passed the gate.
    """
    hunks: list[Hunk] = []
    for block in re.split(r"^(?=@@ )", diff_text, flags=re.MULTILINE):
        header = re.match(r"@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", block)
        if header is None:
            continue
        removed = int(header.group(1)) if header.group(1) is not None else 1
        head_start = int(header.group(2))
        head_count = int(header.group(3)) if header.group(3) is not None else 1
        added: list[int] = []
        line_number = head_start
        for line in block.splitlines()[1:]:
            if line.startswith("+"):
                added.append(line_number)
                line_number += 1
            elif line.startswith("-"):
                continue
            else:
                line_number += 1
        hunks.append(Hunk(head_start, head_count, tuple(added), removed))
    return hunks


def changed_line_numbers(diff_text: str) -> set[int]:
    """Head-side line numbers a unified diff actually ADDS (deletions add nothing)."""
    touched: set[int] = set()
    for hunk in parse_hunks(diff_text):
        touched.update(hunk.added)
    return touched


def evaluate(root: Path, *, base_ref: str, base_sha: str, head_sha: str, head_ref: str = "",
             allow_unversioned_release: bool = False) -> tuple[bool, str]:
    """Return (ok, message). Pure decision function so tests can drive it directly."""
    version = version_of_branch(base_ref)
    if version is None:
        if base_ref.startswith(RELEASE_PREFIX):
            # A release branch whose name carries no version cannot be gated: the check would have
            # to guess which section to read, and guessing is how a gate silently stops gating.
            # Fail closed; the escaping hatch is explicit and visible in the command line.
            if allow_unversioned_release:
                return True, (
                    f"base branch {base_ref!r} starts with {RELEASE_PREFIX} but carries no version; "
                    "accepted because --allow-unversioned-release was passed"
                )
            return False, (
                f"base branch {base_ref!r} starts with {RELEASE_PREFIX} but carries no vX.Y.Z version; "
                "rename the branch to release/v<version>, or pass --allow-unversioned-release for a "
                "release branch that deliberately has no single version"
            )
        return True, f"base branch {base_ref!r} is not release/<version>: not applicable"

    try:
        head_text = git(root, "show", f"{head_sha}:{CHANGELOG}")
    except RuntimeError:
        return False, f"{CHANGELOG} is missing at {head_sha}"

    if head_ref == "dev":
        return True, f"dev -> {base_ref} sync pull request: no changelog entry required"

    spans = section_spans(head_text)
    matching = [span for span in spans if span[0] == version]
    if not matching:
        found = ", ".join(span[0] for span in spans[:6]) or "(none)"
        return False, (
            f"a pull request into {base_ref} must add a '## [{version}]' section to {CHANGELOG}; "
            f"head sections are: {found}"
        )

    _, start, end = matching[0]
    try:
        diff = git(root, "diff", "-U0", f"{base_sha}...{head_sha}", "--", CHANGELOG)
    except RuntimeError as error:  # pragma: no cover - git failures are reported, never guessed
        return False, f"cannot read the {CHANGELOG} diff: {error}"
    touched = changed_line_numbers(diff)
    head_lines = head_text.splitlines()
    section_lines = range(start, end)
    # An entry is a line that actually says something: not blank, not a heading, not a comment.
    entry_lines = {
        number for number in section_lines
        if (text := head_lines[number - 1].strip())
        and not text.startswith("#")
        and not text.startswith("<!--")
    }
    if not entry_lines & touched:
        hunks = [hunk for hunk in parse_hunks(diff) if hunk.touches(start, end)]
        if hunks:
            return False, (
                f"the '## [{version}]' section in {CHANGELOG} has no added or rewritten entry line "
                "(only its heading, scaffolding, or deletions changed)"
            )
        return False, (
            f"the '## [{version}]' section in {CHANGELOG} is unchanged by this pull request; "
            f"describe the release's changes there (section covers lines {start}-{end - 1})"
        )
    return True, f"'## [{version}]' section present and changed (lines {start}-{end - 1})"


def pull_request_context(root: Path, event: dict) -> tuple[str, str, str, str]:
    pull = event.get("pull_request") or {}
    base_ref = (pull.get("base") or {}).get("ref") or ""
    head_ref = (pull.get("head") or {}).get("ref") or ""
    base_sha = (pull.get("base") or {}).get("sha") or ""
    head_sha = (pull.get("head") or {}).get("sha") or ""
    if not base_sha:
        # Merge-base rather than the current base tip: the check must judge the change the PR
        # introduces, not whatever landed on the branch afterwards.
        base_sha = git(root, "merge-base", f"origin/{base_ref}" if base_ref else "HEAD", head_sha or "HEAD").strip()
    if not (base_ref and head_sha):
        raise ValueError("event payload has no pull_request base ref/head sha")
    return base_ref, base_sha, head_sha, head_ref


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--event-path", type=Path)
    parser.add_argument("--base-ref")
    parser.add_argument("--base-sha")
    parser.add_argument("--head-sha")
    parser.add_argument("--head-ref")
    parser.add_argument("--allow-unversioned-release", action="store_true",
                        help="accept a release/<name> base branch that carries no vX.Y.Z version")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    try:
        if args.base_ref and args.head_sha:
            base_ref, head_sha = args.base_ref, args.head_sha
            head_ref = args.head_ref or ""
            base_sha = args.base_sha or git(root, "merge-base", f"origin/{base_ref}", head_sha).strip()
        else:
            event_path = args.event_path or _event_path_from_env()
            if event_path is None:
                parser.error("pass --event-path or set GITHUB_EVENT_PATH (or --base-ref/--head-sha)")
            event = json.loads(Path(event_path).read_text())
            base_ref, base_sha, head_sha, head_ref = pull_request_context(root, event)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"check_release_changelog: INPUT_ERROR: {error}", file=sys.stderr)
        return 2

    ok, message = evaluate(root, base_ref=base_ref, base_sha=base_sha, head_sha=head_sha,
                           head_ref=locals().get("head_ref", ""),
                           allow_unversioned_release=args.allow_unversioned_release)
    stream = sys.stdout if ok else sys.stderr
    print(f"check_release_changelog: {'OK' if ok else 'FAIL'}: {message}", file=stream)
    return 0 if ok else 1


def _event_path_from_env() -> Path | None:
    import os

    value = os.environ.get("GITHUB_EVENT_PATH")
    return Path(value) if value else None


if __name__ == "__main__":
    raise SystemExit(main())
