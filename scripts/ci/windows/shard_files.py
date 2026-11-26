"""Deterministic test-file sharding, wave chunking, and suite planning for Windows CI.

Decision (2026-11-26, tier 1 smoke and tier 2 full suite).  Tier 1 runs one shard
and one wave while the same deterministic partitioner implements the four-shard,
120-file-per-wave layout used by tier 2 (full suite).  Suite planning runs on
ubuntu-latest without touching Linux impact sharding, supports the four fixed
selections (full, tier1-next, arm-next, targets), excludes optional posix-only.json
entries while reporting POSIX_ONLY_IGNORED counts, uses deterministic greedy LPT
sharding when pytest-durations-windows.json is present, and verifies every target
in dispatch-targets.txt against tracked git paths.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import statistics
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Sequence

VALID_SELECTIONS = ("full", "tier1-next", "arm-next", "targets")
DEFAULT_POSIX_ONLY = "scripts/ci/windows/posix-only.json"
DEFAULT_DURATIONS = "scripts/ci/windows/pytest-durations-windows.json"
DEFAULT_TARGETS = "scripts/ci/windows/dispatch-targets.txt"
DEFAULT_TIER1_NEXT = "scripts/ci/windows/tier1-next.txt"
DEFAULT_TIER1_TESTS = "scripts/ci/windows/tier1-tests.txt"
DEFAULT_TIER1_ARM_TESTS = "scripts/ci/windows/tier1-arm-tests.txt"
SKIP_DIR_NAMES = frozenset({"__pycache__", ".git"})
SKIP_DIR_PREFIX = "windows-suite-probe-"
WILDCARD_CHARS = frozenset({"*", "?", "[", "]"})
TEST_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+test_\w+", re.MULTILINE)


def _normalise_relpath(value: str) -> str:
    return value.strip().replace("\\", "/").lstrip("./")


def load_file_list(path: Path, *, root: Path | None = None) -> list[str]:
    base = root or Path.cwd()
    values: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        if value in seen:
            raise ValueError(f"duplicate test path: {value}")
        candidate = base / value
        if not candidate.is_file():
            raise FileNotFoundError(value)
        seen.add(value)
        values.append(value)
    return sorted(values)


def shard_files(files: Sequence[str], shard_count: int) -> list[list[str]]:
    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    ordered = sorted(files)
    if len(set(ordered)) != len(ordered):
        raise ValueError("duplicate test path")
    shards: list[list[str]] = [[] for _ in range(shard_count)]
    for index, path in enumerate(ordered):
        shards[index % shard_count].append(path)
    return shards


def shard_files_weighted(
    files: Sequence[str],
    shard_count: int,
    durations: Mapping[str, float],
) -> list[list[str]]:
    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    ordered = sorted(files)
    if len(set(ordered)) != len(ordered):
        raise ValueError("duplicate test path")
    if not durations:
        return shard_files(ordered, shard_count)
    positive_known = [
        float(durations[path])
        for path in ordered
        if path in durations and float(durations[path]) > 0.0
    ]
    if not positive_known:
        positive_known = [float(value) for value in durations.values() if float(value) > 0.0]
    default_weight = statistics.median(positive_known) if positive_known else 1.0
    weighted = [
        (float(durations[path]) if path in durations else default_weight, path) for path in ordered
    ]
    weighted.sort(key=lambda item: (-item[0], item[1]))
    loads = [0.0] * shard_count
    shards: list[list[str]] = [[] for _ in range(shard_count)]
    for weight, path in weighted:
        target = min(range(shard_count), key=lambda idx: (loads[idx], len(shards[idx]), idx))
        shards[target].append(path)
        loads[target] += weight
    return [sorted(shard) for shard in shards]


def wave_chunks(files: Sequence[str], wave_size: int = 120) -> list[list[str]]:
    if wave_size < 1:
        raise ValueError("wave_size must be positive")
    ordered = list(files)
    return [ordered[index : index + wave_size] for index in range(0, len(ordered), wave_size)]


def plan_shards(
    files: Sequence[str],
    shard_count: int,
    *,
    wave_size: int = 120,
) -> list[list[list[str]]]:
    return [wave_chunks(shard, wave_size=wave_size) for shard in shard_files(files, shard_count)]


def discover_test_files(root: Path) -> list[str]:
    tests_root = root / "tests"
    if not tests_root.is_dir():
        return []
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(tests_root):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in SKIP_DIR_NAMES and not name.startswith(SKIP_DIR_PREFIX)
        )
        current = Path(dirpath)
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            if not (filename.startswith("test_") or filename.endswith("_test.py")):
                continue
            rel = (current / filename).relative_to(root).as_posix()
            found.append(rel)
    return sorted(found)


def count_def_tests(path: Path) -> int:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return len(TEST_DEF_RE.findall(text))
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            count += 1
    return count


def load_posix_only_entries(path: Path) -> list[tuple[str, str]]:
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw.get("entries", []) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise ValueError(f"invalid posix-only registry format: {path}")
    entries: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "")).strip()
        target = _normalise_relpath(str(item.get("path") or item.get("entry") or ""))
        if kind not in {"dir", "file"} or not target:
            continue
        if kind == "dir":
            target = target.rstrip("/")
        entries.append((kind, target))
    return entries


def filter_posix_only(
    files: Sequence[str],
    posix_only_path: Path,
    *,
    root: Path,
) -> tuple[list[str], dict[str, int]]:
    entries = load_posix_only_entries(posix_only_path)
    if not entries:
        return list(files), {"files": 0, "tests": 0}
    dir_prefixes = tuple(target for kind, target in entries if kind == "dir")
    ignored_file_set = frozenset(target for kind, target in entries if kind == "file")
    kept: list[str] = []
    ignored: list[str] = []
    for rel in files:
        normalised = _normalise_relpath(rel)
        if normalised in ignored_file_set or any(
            normalised == prefix or normalised.startswith(prefix + "/") for prefix in dir_prefixes
        ):
            ignored.append(normalised)
        else:
            kept.append(normalised)
    ignored_tests = sum(count_def_tests(root / rel) for rel in ignored if (root / rel).is_file())
    return kept, {"files": len(ignored), "tests": ignored_tests}


def load_durations(path: Path) -> dict[str, float]:
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    mapping = raw.get("files") if isinstance(raw, dict) and isinstance(raw.get("files"), dict) else raw
    if not isinstance(mapping, dict):
        raise ValueError(f"invalid durations file format: {path}")
    durations: dict[str, float] = {}
    for key, value in mapping.items():
        if isinstance(key, str) and isinstance(value, (int, float)) and not isinstance(value, bool):
            if float(value) >= 0.0:
                durations[_normalise_relpath(key)] = float(value)
    return durations


def git_tracked_files(root: Path) -> set[str]:
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True,
        check=True,
    )
    output = completed.stdout.decode("utf-8", errors="strict")
    return {_normalise_relpath(item) for item in output.split("\0") if item}


def load_dispatch_targets(
    path: Path,
    *,
    root: Path,
    tracked_files: set[str] | None = None,
) -> list[str]:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    raw_entries: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if any(char in WILDCARD_CHARS for char in stripped):
            raise ValueError(f"wildcard not allowed in dispatch targets: {stripped}")
        normalised = _normalise_relpath(stripped)
        parts = Path(normalised).parts
        if not normalised.startswith("tests/") or ".." in parts:
            raise ValueError(f"target must be a relative path under tests/: {stripped}")
        if normalised in seen:
            raise ValueError(f"duplicate target path: {normalised}")
        seen.add(normalised)
        raw_entries.append(normalised)
    if not raw_entries:
        raise ValueError("dispatch-targets.txt is empty")
    tracked = tracked_files if tracked_files is not None else git_tracked_files(root)
    for entry in raw_entries:
        if entry not in tracked:
            raise ValueError(f"untracked target path: {entry}")
        if not (root / entry).is_file():
            raise FileNotFoundError(entry)
    return sorted(raw_entries)


def build_suite_plan(
    *,
    selection: str,
    root: Path,
    shard_count: int = 4,
    wave_size: int = 120,
    posix_only_path: Path | None = None,
    durations_path: Path | None = None,
    targets_path: Path | None = None,
    tier1_next_path: Path | None = None,
    tier1_tests_path: Path | None = None,
    tier1_arm_tests_path: Path | None = None,
    tracked_files: set[str] | None = None,
) -> dict[str, Any]:
    if selection not in VALID_SELECTIONS:
        raise ValueError(f"unsupported selection: {selection!r}")
    if wave_size < 1:
        raise ValueError("wave_size must be positive")
    posix_only_ignored = {"files": 0, "tests": 0}
    if selection == "full":
        if shard_count < 1:
            raise ValueError("shard_count must be positive")
        discovered = discover_test_files(root)
        posix_path = posix_only_path or (root / DEFAULT_POSIX_ONLY)
        files, posix_only_ignored = filter_posix_only(discovered, posix_path, root=root)
        if not files:
            raise ValueError("discovered test file list is empty")
        dur_path = durations_path or (root / DEFAULT_DURATIONS)
        durations = load_durations(dur_path)
        shards = (
            shard_files_weighted(files, shard_count, durations)
            if durations
            else shard_files(files, shard_count)
        )
        waves = [wave_chunks(shard, wave_size=wave_size) for shard in shards]
        required_legs = [f"shard-{index}" for index in range(shard_count)]
    elif selection == "targets":
        t_path = targets_path or (root / DEFAULT_TARGETS)
        files = load_dispatch_targets(t_path, root=root, tracked_files=tracked_files)
        shards = [files]
        waves = [[files]]
        required_legs = ["targets"]
    elif selection == "tier1-next":
        next_path = tier1_next_path or (root / DEFAULT_TIER1_NEXT)
        files = load_file_list(next_path, root=root)
        if not files:
            raise ValueError("tier1-next test file list is empty")
        shards = [files]
        waves = [wave_chunks(files, wave_size=wave_size)]
        required_legs = ["tier1-next"]
    else:
        t1_path = tier1_tests_path or (root / DEFAULT_TIER1_TESTS)
        arm_path = tier1_arm_tests_path or (root / DEFAULT_TIER1_ARM_TESTS)
        union = sorted(set(load_file_list(t1_path, root=root)) | set(load_file_list(arm_path, root=root)))
        if not union:
            raise ValueError("arm-next test file list is empty")
        files = union
        shards = [files]
        waves = [wave_chunks(files, wave_size=wave_size)]
        required_legs = ["arm-next"]
    return {
        "version": 1,
        "selection": selection,
        "suiteRequired": True,
        "requiredLegs": required_legs,
        "shardCount": len(shards),
        "waveSize": wave_size,
        "totalFiles": len(files),
        "posixOnlyIgnored": posix_only_ignored,
        "shards": shards,
        "waves": waves,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--selection", choices=VALID_SELECTIONS, default=None)
    parser.add_argument("--files", type=Path, default=None)
    parser.add_argument("--shards", type=int, default=None)
    parser.add_argument("--wave-size", type=int, default=120)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--posix-only", type=Path, default=None)
    parser.add_argument("--durations", type=Path, default=None)
    parser.add_argument("--targets-file", type=Path, default=None)
    parser.add_argument("--tier1-next-file", type=Path, default=None)
    parser.add_argument("--tier1-tests-file", type=Path, default=None)
    parser.add_argument("--tier1-arm-tests-file", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.plan:
        selection = args.selection or os.environ.get("WINDOWS_SUITE_SELECTION", "").strip() or "full"
        shard_count = args.shards if args.shards is not None else 4
        manifest = build_suite_plan(
            selection=selection,
            root=args.root,
            shard_count=shard_count,
            wave_size=args.wave_size,
            posix_only_path=args.posix_only,
            durations_path=args.durations,
            targets_path=args.targets_file,
            tier1_next_path=args.tier1_next_file,
            tier1_tests_path=args.tier1_tests_file,
            tier1_arm_tests_path=args.tier1_arm_tests_file,
        )
        if args.out is not None:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        else:
            print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
        ignored = manifest["posixOnlyIgnored"]
        print(f"POSIX_ONLY_IGNORED files={ignored['files']} tests={ignored['tests']}")
        return 0
    if args.files is None or args.shards is None:
        parser.error("--files and --shards are required unless --plan is passed")
    files = load_file_list(args.files, root=args.root)
    shards = shard_files(files, args.shards)
    waves = [wave_chunks(shard, wave_size=args.wave_size) for shard in shards]
    print(json.dumps({"shards": shards, "waves": waves}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
