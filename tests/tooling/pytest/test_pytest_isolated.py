"""The isolated lane runs the memory-heavy modules alone; shards never run them.

Decision record:
- ``tests/integration/teleport/`` starts two real daemons plus their workers,
  about 0.6 GB on top of a shard whose two pytest workers already approach the
  runner's 4 GB limit. On 2026-11-14 every shard that held those modules lost
  a worker and read 19-101 GB from disk, while shards without them read under
  1 GB and lost none. The modules therefore run in their own job.
- Together the shards and the isolated lane must still collect every test
  exactly once, for a full run and for a PR selection alike.
"""
import importlib.util
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
HEAVY = "tests/integration/teleport"


@pytest.fixture(scope="module")
def impact():
    spec = importlib.util.spec_from_file_location(
        "pytest_impact_isolated", ROOT / "scripts/ci/pytest_impact.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(root, path, content="def test_case(): pass\n"):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


def collect(root, paths):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *paths, "--collect-only", "-q", "-p", "no:randomly"],
        cwd=root, text=True, capture_output=True, check=True,
    )
    return {line for line in result.stdout.splitlines() if "::" in line}


@pytest.fixture
def suite(tmp_path):
    for name in ("a", "b", "c", "d"):
        write(tmp_path, f"tests/{name}/test_{name}.py")
    write(tmp_path, f"{HEAVY}/test_two_daemons.py")
    write(tmp_path, f"{HEAVY}/test_routines.py")
    write(tmp_path, "tests/integration/other/test_single_daemon.py")
    return tmp_path


def run_main(impact, monkeypatch, argv, decision):
    calls = []
    monkeypatch.setattr(impact, "plan", lambda root, event, name: decision)
    monkeypatch.setattr(
        impact.subprocess, "run",
        lambda args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(sys, "argv", ["pytest_impact.py", *argv])
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return impact.main(), calls


@pytest.mark.parametrize("count", [1, 2, 4])
def test_shards_and_the_isolated_lane_cover_a_full_run_exactly_once(impact, suite, count):
    bins = impact.shard_plan(suite, ["tests/"], count, {})
    lane = impact.isolated_paths(suite, ["tests/"])
    parts = [collect(suite, paths) for paths in [*bins, lane] if paths]
    complete = collect(suite, ["tests/"])
    assert set().union(*parts) == complete
    assert sum(len(part) for part in parts) == len(complete)
    shard_ids = set().union(*parts[:-1])
    assert not any(node.startswith(HEAVY) for node in shard_ids)
    assert all(node.startswith(HEAVY) for node in parts[-1])


def test_a_pr_selection_is_split_between_shards_and_the_isolated_lane(impact, suite):
    selected = ["tests/a/test_a.py", f"{HEAVY}/test_routines.py"]
    bins = impact.shard_plan(suite, selected, 4, {})
    assert sorted(path for paths in bins for path in paths) == ["tests/a/test_a.py"]
    assert impact.isolated_paths(suite, selected) == [f"{HEAVY}/test_routines.py"]


def test_a_tree_without_the_heavy_modules_has_an_empty_lane(impact, tmp_path):
    write(tmp_path, "tests/a/test_a.py")
    assert impact.isolated_paths(tmp_path, ["tests/"]) == []
    assert impact.shard_plan(tmp_path, ["tests/"], 2, {})[-1] == ["tests/", "--ignore=tests/a/test_a.py"]


def test_the_isolated_lane_runs_one_wave_with_its_own_temp_and_junit(impact, monkeypatch, capsys):
    decision = {"mode": "full", "tests": ["tests/"]}
    status, calls = run_main(
        impact, monkeypatch,
        ["--isolated", "--", "-q", "--basetemp=/tmp/bt", "--junitxml=/tmp/unit.xml"], decision,
    )
    assert status == 0 and len(calls) == 1
    argv = calls[0]
    assert argv[3:argv.index("-q")] == [HEAVY]
    assert "--basetemp=/tmp/bt-isolated" in argv and "--junitxml=/tmp/unit.xml.isolated.xml" in argv
    assert "PYTEST_ISOLATED " in capsys.readouterr().out


def test_an_empty_isolated_lane_never_invokes_pytest(impact, monkeypatch, capsys):
    decision = {"mode": "selected", "tests": ["tests/kernel/addressing/test_uri_leaf.py"]}
    status, calls = run_main(impact, monkeypatch, ["--isolated", "--", "-q"], decision)
    assert status == 0 and calls == []
    assert "PYTEST_ISOLATED_EMPTY" in capsys.readouterr().out


@pytest.mark.parametrize("extra", [["--shard", "1/4"], ["--grouped"], ["--staged"]])
def test_the_isolated_lane_cannot_be_combined_with_other_waves(impact, monkeypatch, extra):
    with pytest.raises(SystemExit):
        run_main(impact, monkeypatch, ["--isolated", *extra], {"mode": "full", "tests": ["tests/"]})


def test_repository_shards_hold_no_isolated_module(impact):
    weights = impact.load_weights(impact.DURATIONS)
    bins = impact.shard_plan(ROOT, ["tests/"], 4, weights)
    explicit = [path for paths in bins[:-1] for path in paths]
    assert not any(path.startswith(HEAVY + "/") for path in explicit)
    assert f"--ignore={HEAVY}" in bins[-1]
    assert impact.isolated_paths(ROOT, ["tests/"]) == [HEAVY]
    assert any((ROOT / HEAVY).glob("test_*.py"))
