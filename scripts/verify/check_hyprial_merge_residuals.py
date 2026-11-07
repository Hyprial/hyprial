#!/usr/bin/env python3
"""Classify every old product-token line under the merged ``src``/``tests`` tree."""

from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OLD = "h2" + "b"


@dataclass(frozen=True, slots=True)
class Allowance:
    category: str
    path: str | None
    pattern: re.Pattern[str]
    reason: str

    def matches(self, relative: str, line: str) -> bool:
        return (self.path is None or self.path == relative) and bool(
            self.pattern.search(line)
        )


def _pattern(text: str) -> re.Pattern[str]:
    return re.compile(text, re.IGNORECASE)


def _literals(*names: str) -> re.Pattern[str]:
    """Allow these exact names and nothing else.

    Not a convenience wrapper over ``_pattern``. hq-adjutant ruled 2026-09-12
    that a migration's own allowances are given **by specific name, never by
    shape**: a family pattern would re-open the very hole this guard exists to
    watch, while the specific names let through only the ones we actually
    thought about. ``re.escape`` makes that rule structural -- a family cannot
    be written through this function, so it cannot be forgotten by whoever
    edits the table next.

    Longest first, because these names nest (the legacy home dir is a prefix of
    nothing here, but the schema values share one).
    """
    ordered = sorted(names, key=len, reverse=True)
    return _pattern("|".join(re.escape(name) for name in ordered))


# The h2b -> hyprial transition is complete: no old product token is allowed
# anywhere under src/ or tests/.
ALLOWANCES: tuple[Allowance, ...] = ()


def _tracked_scope(tree: str | None) -> tuple[list[str], str]:
    command = (
        ["git", "ls-tree", "-r", "--name-only", tree, "--", "src", "tests"]
        if tree is not None
        else ["git", "ls-files", "-z", "--cached", "--others",
              "--exclude-standard", "--", "src", "tests"]
    )
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git ls-files failed")
    # GUI is a separately versioned Node component with preserved external IDs and
    # bundled binary assets. The backend rename contract does not rename those.
    listed = completed.stdout.splitlines() if tree is not None else completed.stdout.split("\0")
    paths = [line for line in listed
             if line and not line.startswith("src/gui/")]
    if tree is not None:
        resolved = subprocess.run(
            ["git", "rev-parse", tree],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if resolved.returncode != 0:
            raise RuntimeError(resolved.stderr.strip() or f"cannot resolve {tree}")
        return paths, resolved.stdout.strip()

    # The default gate scans the actual Git worktree, not only committed paths.
    # Filter tracked deletions before reporting coverage, and do not count an
    # untracked native file twice if it is also present in the index.
    paths = sorted({relative for relative in paths if (ROOT / relative).is_file()})

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    tree = head.stdout.strip() if head.returncode == 0 else "unknown"
    merge_head = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if merge_head.returncode == 0:
        tree = f"merge-worktree(head={tree},merge_head={merge_head.stdout.strip()})"
    return paths, tree


def _read_text(relative: str, tree: str | None) -> str:
    if tree is None:
        return (ROOT / relative).read_text(encoding="utf-8")
    completed = subprocess.run(
        ["git", "show", f"{tree}:{relative}"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise OSError(completed.stderr.decode("utf-8", errors="replace").strip())
    return completed.stdout.decode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tree", help="scan this content-addressed Git tree")
    arguments = parser.parse_args(argv)
    paths, label = _tracked_scope(arguments.tree)
    decoded = 0
    parse_errors: list[str] = []
    classified: dict[str, int] = {category: 0 for category in "abcd"}
    entries = 0
    unclassified: list[str] = []
    records: list[str] = []
    for relative in paths:
        path = ROOT / relative
        if arguments.tree is None and not path.is_file():
            continue
        try:
            lines = _read_text(relative, arguments.tree).splitlines()
        except (OSError, UnicodeError) as error:
            parse_errors.append(f"{relative}: {error}")
            continue
        decoded += 1
        for number, line in enumerate(lines, 1):
            if OLD not in line.lower():
                continue
            entries += 1
            allowance = next(
                (item for item in ALLOWANCES if item.matches(relative, line)),
                None,
            )
            if allowance is None:
                unclassified.append(f"{relative}:{number}:{line.strip()}")
                continue
            classified[allowance.category] += 1
            records.append(
                f"{relative}:{number} class={allowance.category} "
                f"reason={allowance.reason}"
            )

    for record in records:
        print(record)
    for error in parse_errors:
        print(f"PARSE_ERROR {error}")
    for record in unclassified:
        print(f"UNCLASSIFIED {record}")
    print(
        f"tree={label} scanned_files={len(paths)} decoded_files={decoded} "
        f"parse_errors={len(parse_errors)} entries={entries} "
        f"class_a={classified['a']} class_b={classified['b']} "
        f"class_c={classified['c']} class_d={classified['d']} "
        f"unclassified={len(unclassified)}"
    )
    return 1 if parse_errors or unclassified or decoded == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
