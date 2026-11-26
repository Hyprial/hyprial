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
    build_suite_plan,
    load_file_list,
    main,
    plan_shards,
    shard_files,
    shard_files_weighted,
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


def test_plan_full_discovers_filters_posix_only_and_uses_durations(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    files_to_create = {
        "tests/sub/test_alpha.py": "def test_a1():\n    pass\n\ndef test_a2():\n    pass\n",
        "tests/sub/test_beta.py": "def test_b1():\n    pass\n",
        "tests/sub/gamma_test.py": "async def test_g1():\n    pass\n",
        "tests/sub/helper.py": "def helper():\n    pass\n",
        "tests/windows-suite-probe-123/test_probe.py": "def test_probe():\n    pass\n",
        "tests/__pycache__/test_cached.py": "def test_cached():\n    pass\n",
        "tests/posix_dir/test_posix_a.py": "def test_p1():\n    pass\n\ndef test_p2():\n    pass\n",
        "tests/test_posix_file.py": "def test_f1():\n    pass\n\ndef test_f2():\n    pass\n\ndef test_f3():\n    pass\n",
    }
    for rel, content in files_to_create.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    plain = build_suite_plan(selection="full", root=tmp_path, shard_count=2, wave_size=2)
    assert plain["selection"] == "full"
    assert plain["suiteRequired"] is True
    assert plain["requiredLegs"] == ["shard-0", "shard-1"]
    assert plain["posixOnlyIgnored"] == {"files": 0, "tests": 0}
    plain_files = sorted(path for shard in plain["shards"] for path in shard)
    assert plain_files == [
        "tests/posix_dir/test_posix_a.py",
        "tests/sub/gamma_test.py",
        "tests/sub/test_alpha.py",
        "tests/sub/test_beta.py",
        "tests/test_posix_file.py",
    ]

    posix_only = tmp_path / "scripts/ci/windows/posix-only.json"
    posix_only.parent.mkdir(parents=True, exist_ok=True)
    posix_only.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {"kind": "dir", "path": "tests/posix_dir/"},
                    {"kind": "file", "path": "tests/test_posix_file.py"},
                ],
            }
        ),
        encoding="utf-8",
    )
    durations = tmp_path / "scripts/ci/windows/pytest-durations-windows.json"
    durations.write_text(
        json.dumps(
            {
                "files": {
                    "tests/sub/test_alpha.py": 100.0,
                    "tests/sub/test_beta.py": 10.0,
                }
            }
        ),
        encoding="utf-8",
    )
    out_manifest = tmp_path / "plan-manifest.json"
    code = main(
        [
            "--plan",
            "--selection",
            "full",
            "--shards",
            "2",
            "--wave-size",
            "2",
            "--out",
            str(out_manifest),
            "--root",
            str(tmp_path),
        ]
    )
    assert code == 0
    stdout = capsys.readouterr().out
    assert "POSIX_ONLY_IGNORED files=2 tests=5" in stdout
    written = json.loads(out_manifest.read_text(encoding="utf-8"))
    assert written["posixOnlyIgnored"] == {"files": 2, "tests": 5}
    assert written["totalFiles"] == 3
    assert written["shards"] == [
        ["tests/sub/test_alpha.py"],
        ["tests/sub/gamma_test.py", "tests/sub/test_beta.py"],
    ]
    assert shard_files_weighted(
        ["tests/a.py", "tests/b.py", "tests/c.py"],
        2,
        {"tests/a.py": 50.0, "tests/b.py": 20.0, "tests/c.py": 20.0},
    ) == [["tests/a.py"], ["tests/b.py", "tests/c.py"]]


def test_plan_targets_validates_tracked_files_and_rejects_empty(tmp_path: Path) -> None:
    test_file = tmp_path / "tests/test_sample.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_ok():\n    pass\n", encoding="utf-8")
    targets = tmp_path / "scripts/ci/windows/dispatch-targets.txt"
    targets.parent.mkdir(parents=True, exist_ok=True)

    targets.write_text("# empty comment only\n\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        build_suite_plan(
            selection="targets",
            root=tmp_path,
            targets_path=targets,
            tracked_files={"tests/test_sample.py"},
        )

    targets.write_text("tests/test_*.py\n", encoding="utf-8")
    with pytest.raises(ValueError, match="wildcard"):
        build_suite_plan(
            selection="targets",
            root=tmp_path,
            targets_path=targets,
            tracked_files={"tests/test_sample.py"},
        )

    targets.write_text("tests/test_sample.py\n", encoding="utf-8")
    with pytest.raises(ValueError, match="untracked"):
        build_suite_plan(
            selection="targets",
            root=tmp_path,
            targets_path=targets,
            tracked_files=set(),
        )

    plan = build_suite_plan(
        selection="targets",
        root=tmp_path,
        targets_path=targets,
        tracked_files={"tests/test_sample.py"},
    )
    assert plan["selection"] == "targets"
    assert plan["requiredLegs"] == ["targets"]
    assert plan["shards"] == [["tests/test_sample.py"]]
    assert plan["waves"] == [[["tests/test_sample.py"]]]


def test_plan_tier1_next_and_arm_next(tmp_path: Path) -> None:
    for rel in ("tests/test_a.py", "tests/test_b.py", "tests/test_c.py"):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def test_x():\n    pass\n", encoding="utf-8")
    tier1_next = tmp_path / "tier1-next.txt"
    tier1_next.write_text("tests/test_b.py\ntests/test_a.py\n", encoding="utf-8")
    tier1_tests = tmp_path / "tier1-tests.txt"
    tier1_tests.write_text("tests/test_a.py\n", encoding="utf-8")
    tier1_arm = tmp_path / "tier1-arm-tests.txt"
    tier1_arm.write_text("# arm extras\ntests/test_c.py\ntests/test_a.py\n", encoding="utf-8")

    next_plan = build_suite_plan(
        selection="tier1-next",
        root=tmp_path,
        wave_size=120,
        tier1_next_path=tier1_next,
    )
    assert next_plan["requiredLegs"] == ["tier1-next"]
    assert next_plan["shards"] == [["tests/test_a.py", "tests/test_b.py"]]

    arm_plan = build_suite_plan(
        selection="arm-next",
        root=tmp_path,
        wave_size=120,
        tier1_tests_path=tier1_tests,
        tier1_arm_tests_path=tier1_arm,
    )
    assert arm_plan["requiredLegs"] == ["arm-next"]
    assert arm_plan["shards"] == [["tests/test_a.py", "tests/test_c.py"]]


def test_dispatch_targets_file_is_empty_in_mergeable_tree() -> None:
    repo_root = Path(__file__).parents[4]
    targets_file = repo_root / "scripts/ci/windows/dispatch-targets.txt"
    assert targets_file.is_file()
    entries = [
        line.strip()
        for line in targets_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert entries == []
