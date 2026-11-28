#!/usr/bin/env python3
"""Replay a fixed pytest-selector snapshot and commit cohort read-only.

The runner extracts one immutable tree, reads fixed commit cohorts from the
repository history, and sends every commit's changed paths through the final
path planner. It does not modify the checkout or read duration data.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

try:
    from scripts.ci import pytest_impact
    from scripts.git_environment import hardened_git_environment
except ModuleNotFoundError:
    ROOT = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(ROOT))
    from scripts.ci import pytest_impact
    from scripts.git_environment import hardened_git_environment

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_COHORT = Path(__file__).with_name("pytest-selector-cohort.json")
SCHEMA = "hyprial.ci.pytest-selector-replay/v1"


def git(*args: str) -> bytes:
    environment = hardened_git_environment(os.environ, include_home=True, propagate_proxy=True)
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        env=environment,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    ).stdout


def load_cohort(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise ValueError("cohort schema is invalid")
    snapshot = document.get("snapshot")
    cohort = document.get("cohort")
    if not isinstance(snapshot, str) or len(snapshot) != 40:
        raise ValueError("cohort snapshot is invalid")
    if not isinstance(cohort, list) or not cohort:
        raise ValueError("cohort is empty")
    for case in cohort:
        if not isinstance(case, dict) or not isinstance(case.get("name"), str) or not case["name"]:
            raise ValueError("cohort case name is invalid")
        args = case.get("gitArgs")
        expected = case.get("expectedCount")
        if not isinstance(args, list) or not args or not all(isinstance(item, str) and item for item in args):
            raise ValueError("cohort git arguments are invalid")
        if not isinstance(expected, int) or expected < 1:
            raise ValueError("cohort expected count is invalid")
    return document


def commit_cohort(case: dict[str, Any]) -> list[tuple[str, tuple[str, ...]]]:
    rows: list[tuple[str, tuple[str, ...]]] = []
    for line in git(*case["gitArgs"]).decode("utf-8").splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        commit = fields[0]
        if any(len(parent) != 40 for parent in fields[1:]):
            raise ValueError(f"invalid parent in cohort {case['name']}")
        rows.append((commit, tuple(fields[1:])))
    if len(rows) != case["expectedCount"]:
        raise ValueError(f"cohort {case['name']} has {len(rows)} commits, expected {case['expectedCount']}")
    return rows


def changed_paths(commit: str, merge: bool) -> list[str]:
    args = ["diff-tree", "--no-commit-id", "--name-only", "--no-renames", "-r"]
    if merge:
        args.append("--first-parent")
    args.extend(["--root", "-z", commit, "--"])
    raw = git(*args)
    paths = [path for path in raw.decode("utf-8").split("\0") if path]
    if any("\n" in path or "\r" in path or "\\" in path for path in paths):
        raise ValueError(f"invalid path in commit {commit}")
    return paths


def extract_snapshot(snapshot: str, destination: Path) -> None:
    archive = subprocess.Popen(
        ["git", "archive", "--format=tar", snapshot],
        cwd=ROOT,
        env=hardened_git_environment(os.environ, include_home=True, propagate_proxy=True),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert archive.stdout is not None
    with tarfile.open(fileobj=archive.stdout, mode="r|") as stream:
        stream.extractall(destination, filter="data")
    _, stderr = archive.communicate(timeout=120)
    if archive.returncode != 0:
        raise subprocess.CalledProcessError(archive.returncode, archive.args, stderr=stderr)


def index_snapshot(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)


def replay(root: Path, cohort: dict[str, Any]) -> dict[str, Any]:
    test_total = len(pytest_impact.tracked_test_files(root))
    reports: list[dict[str, Any]] = []
    for case in cohort["cohort"]:
        rows = commit_cohort(case)
        merge = "--merges" in case["gitArgs"]
        commits: list[dict[str, Any]] = []
        counts = {"full": 0, "selected": 0, "docs-only": 0}
        selected_total = 0
        for commit, parents in rows:
            paths = changed_paths(commit, merge)
            decision = pytest_impact.plan_paths(root, paths)
            mode = str(decision["mode"])
            kind = str(decision["kind"])
            counts[mode] = counts.get(mode, 0) + 1
            tests = decision.get("tests")
            reasons = decision.get("reasons")
            if not isinstance(tests, list) or not isinstance(reasons, list):
                raise ValueError(f"selector returned an invalid plan for {commit}")
            selected_count = len(tests) if mode == "selected" else len(pytest_impact.tracked_test_files(root))
            selected_total += selected_count
            commits.append(
                {
                    "commit": commit,
                    "parents": list(parents),
                    "mode": mode,
                    "kind": kind,
                    "changedCount": len(paths),
                    "selectedCount": selected_count,
                    "reasons": reasons,
                }
            )
        test_total = len(pytest_impact.tracked_test_files(root))
        reports.append(
            {
                "name": case["name"],
                "commitCount": len(commits),
                "counts": counts,
                "averageSelectedFraction": selected_total / len(commits) / test_total,
                "averageSkippedFraction": 1 - selected_total / len(commits) / test_total,
                "commits": commits,
            }
        )
    return {"snapshot": cohort["snapshot"], "testCount": test_total, "cohorts": reports}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", type=Path, default=DEFAULT_COHORT)
    args = parser.parse_args(argv)
    try:
        cohort = load_cohort(args.cohort)
        with tempfile.TemporaryDirectory(prefix="pytest-selector-replay-") as directory:
            snapshot_root = Path(directory)
            extract_snapshot(cohort["snapshot"], snapshot_root)
            index_snapshot(snapshot_root)
            report = replay(snapshot_root, cohort)
    except (OSError, ValueError, TypeError, json.JSONDecodeError, subprocess.SubprocessError, tarfile.TarError) as error:
        print(f"selector replay failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
