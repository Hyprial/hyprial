"""Tests for deterministic Windows test-file sharding and wave chunking.

Decision (2026-11-26, tier 1 smoke).  Locks both the tier 1 (smoke)
single-shard/single-wave layout and the tier 2 (full suite) four-shard/120-file-per-wave
layout so moving to tier 2 only changes arguments.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.ci.windows.shard_files import (
    load_file_list,
    main,
    plan_shards,
    shard_files,
    wave_chunks,
)


def test_load_file_list_ignores_comments_and_sorts(tmp_path: Path) -> None:
    first = tmp_path / "a.py"
    second = tmp_path / "b.py"
    first.write_text("", encoding="utf-8")
    second.write_text("", encoding="utf-8")
    listing = tmp_path / "files.txt"
    listing.write_text("# note\nb.py\n\na.py\n", encoding="utf-8")

    assert load_file_list(listing, root=tmp_path) == ["a.py", "b.py"]


def test_shard_files_is_stable_and_balanced() -> None:
    files = ["tests/c.py", "tests/a.py", "tests/b.py", "tests/d.py", "tests/e.py"]

    assert shard_files(files, 1) == [["tests/a.py", "tests/b.py", "tests/c.py", "tests/d.py", "tests/e.py"]]
    assert shard_files(files, 2) == [
        ["tests/a.py", "tests/c.py", "tests/e.py"],
        ["tests/b.py", "tests/d.py"],
    ]
    with pytest.raises(ValueError, match="positive"):
        shard_files(files, 0)
    with pytest.raises(ValueError, match="duplicate"):
        shard_files(["tests/a.py", "tests/a.py"], 2)


def test_wave_chunks_and_four_shard_120_file_plan() -> None:
    files = [f"tests/t_{index:04d}.py" for index in range(500)]

    assert [len(chunk) for chunk in wave_chunks(files[:250], wave_size=120)] == [120, 120, 10]
    with pytest.raises(ValueError, match="positive"):
        wave_chunks(files, wave_size=0)

    planned = plan_shards(files, 4, wave_size=120)
    assert len(planned) == 4
    assert all([len(wave) for wave in shard] == [120, 5] for shard in planned)
    flattened = [path for shard in planned for wave in shard for path in wave]
    assert sorted(flattened) == files
    assert plan_shards(list(reversed(files)), 4, wave_size=120) == planned


def test_load_file_list_rejects_duplicate_and_missing_paths(tmp_path: Path) -> None:
    path = tmp_path / "a.py"
    path.write_text("", encoding="utf-8")
    duplicate = tmp_path / "duplicate.txt"
    duplicate.write_text("a.py\na.py\n", encoding="utf-8")
    missing = tmp_path / "missing.txt"
    missing.write_text("missing.py\n", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate"):
        load_file_list(duplicate, root=tmp_path)
    with pytest.raises(FileNotFoundError, match="missing.py"):
        load_file_list(missing, root=tmp_path)


def test_cli_emits_shards_and_waves_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    names = [f"t{index}.py" for index in range(5)]
    for name in names:
        (tmp_path / name).write_text("", encoding="utf-8")
    listing = tmp_path / "list.txt"
    listing.write_text("\n".join(reversed(names)) + "\n", encoding="utf-8")

    code = main(["--files", str(listing), "--shards", "2", "--wave-size", "2", "--root", str(tmp_path)])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["shards"] == [["t0.py", "t2.py", "t4.py"], ["t1.py", "t3.py"]]
    assert payload["waves"] == [[["t0.py", "t2.py"], ["t4.py"]], [["t1.py", "t3.py"]]]
