"""Deterministic test-file sharding and wave chunking for Windows CI.

Decision (2026-11-26, tier 1 smoke).  Tier 1 runs one shard and one wave while the
same deterministic partitioner already implements the four-shard, 120-file-per-
wave layout used by tier 2 (full suite), so moving to tier 2 only changes arguments.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", type=Path, required=True)
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--wave-size", type=int, default=120)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    files = load_file_list(args.files, root=args.root)
    shards = shard_files(files, args.shards)
    waves = [wave_chunks(shard, wave_size=args.wave_size) for shard in shards]
    print(json.dumps({"shards": shards, "waves": waves}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
