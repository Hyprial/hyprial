"""Shards must be deterministic, mutually exclusive and complete over real collection."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def impact():
    spec = importlib.util.spec_from_file_location("pytest_impact_shards", ROOT / "scripts/ci/pytest_impact.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(root, path, content="def test_case(): pass\n"):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


def collect(root, paths):
    result = subprocess.run([sys.executable, "-m", "pytest", *paths, "--collect-only", "-q", "-p", "no:randomly"],
                            cwd=root, text=True, capture_output=True, check=True)
    return {line for line in result.stdout.splitlines() if "::" in line}


@pytest.fixture
def suite(tmp_path):
    for name in ("a", "b", "c", "d", "e", "f"):
        write(tmp_path, f"tests/{name}/test_{name}.py",
              "import pytest\n@pytest.mark.parametrize('i', [1, 2])\ndef test_case(i): pass\n")
    # Pytest's second default pattern and a file no durations entry knows.
    write(tmp_path, "tests/legacy/thing_test.py")
    write(tmp_path, "tests/new_domain/test_brand_new.py")
    return tmp_path


@pytest.mark.parametrize("count", [1, 2, 4, 7])
def test_full_suite_shards_cover_collection_exactly_once(impact, suite, count):
    weights = {"tests/a/test_a.py": 30.0, "tests/b/test_b.py": 1.0}
    bins = impact.shard_plan(suite, ["tests/"], count, weights)
    assert len(bins) == count
    collected = [collect(suite, paths) for paths in bins if paths]
    complete = collect(suite, ["tests/"])
    assert set().union(*collected) == complete
    assert sum(len(part) for part in collected) == len(complete)  # no node id twice


def test_files_the_planner_cannot_see_still_run_once_in_the_catch_all(impact, suite):
    # A collection rule the planner does not model (a project-level extra
    # python_files pattern) must not drop tests: only explicit files are
    # ignored by the catch-all shard, so the unknown module runs there.
    write(suite, "pytest.ini", "[pytest]\npython_files = test_*.py *_test.py check_*.py\n")
    write(suite, "tests/odd/check_unmodelled.py", "def test_hidden(): pass\n")
    bins = impact.shard_plan(suite, ["tests/"], 4, {})
    assert "tests/odd/check_unmodelled.py" not in {path for paths in bins for path in paths}
    holders = [index for index, paths in enumerate(bins)
               if any("check_unmodelled" in node for node in collect(suite, paths))]
    assert holders == [3]
    explicit = {path for paths in bins[:-1] for path in paths}
    assert set(bins[-1][1:]) == {f"--ignore={path}" for path in explicit}


def test_plan_is_deterministic_and_balances_by_weight(impact, suite):
    weights = {"tests/a/test_a.py": 100.0, "tests/b/test_b.py": 90.0, "tests/c/test_c.py": 10.0}
    first = impact.shard_plan(suite, ["tests/"], 3, weights)
    assert first == impact.shard_plan(suite, ["tests/"], 3, dict(reversed(list(weights.items()))))
    assert impact.plan_digest(first) == impact.plan_digest(impact.shard_plan(suite, ["tests/"], 3, weights))
    # The two heavy files never share a shard.
    holder = {path: index for index, paths in enumerate(first) for path in paths}
    assert holder["tests/a/test_a.py"] != holder["tests/b/test_b.py"]


def test_pr_selection_is_packed_exhaustively_and_may_leave_shards_empty(impact, suite):
    selected = ["tests/a/test_a.py", "tests/b/test_b.py"]
    bins = impact.shard_plan(suite, selected, 4, {})
    assert sorted(path for paths in bins for path in paths) == selected
    assert sum(1 for paths in bins if not paths) == 2
    assert all("tests/" not in paths for paths in bins)  # never a catch-all for a subset


@pytest.mark.parametrize("value", ["0/4", "5/4", "1/0", "1/17", "a/4", "2", "1/4/2"])
def test_invalid_shard_specs_are_rejected(impact, value):
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        impact.parse_shard(value)


def run_main(impact, monkeypatch, argv, decision):
    calls = []
    monkeypatch.setattr(impact, "plan", lambda root, event, name: decision)
    monkeypatch.setattr(impact.subprocess, "run",
                        lambda args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(sys, "argv", ["pytest_impact.py", *argv])
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return impact.main(), calls


def test_an_empty_shard_never_invokes_pytest(impact, monkeypatch, capsys):
    decision = {"mode": "selected", "tests": ["tests/kernel/addressing/test_uri_leaf.py"]}
    status, calls = run_main(impact, monkeypatch, ["--shard", "3/4", "--", "-q"], decision)
    assert status == 0 and calls == []
    assert "PYTEST_SHARD_EMPTY" in capsys.readouterr().out


def test_a_shard_runs_one_wave_with_its_own_temp_and_junit(impact, monkeypatch, capsys):
    decision = {"mode": "full", "tests": ["tests/"]}
    status, calls = run_main(impact, monkeypatch,
                             ["--shard", "4/4", "--", "-q", "--basetemp=/tmp/bt", "--junitxml=/tmp/unit.xml"], decision)
    assert status == 0 and len(calls) == 1
    argv = calls[0]
    assert argv[3] == "tests/" and all(arg.startswith("--ignore=") for arg in argv[4:argv.index("-q")])
    assert "--basetemp=/tmp/bt-shard-4of4" in argv and "--junitxml=/tmp/unit.xml.shard-4of4.xml" in argv
    line = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("PYTEST_SHARD "))
    assert json.loads(line.split(" ", 1)[1])["catchAll"] is True


def test_shard_cannot_be_combined_with_grouped_waves(impact, monkeypatch):
    with pytest.raises(SystemExit):
        run_main(impact, monkeypatch, ["--shard", "1/4", "--grouped"], {"mode": "full", "tests": ["tests/"]})


def test_repository_shards_cover_every_test_file(impact):
    weights = impact.load_weights(impact.DURATIONS)
    bins = impact.shard_plan(ROOT, ["tests/"], 4, weights)
    explicit = [path for paths in bins[:-1] for path in paths]
    assert len(explicit) == len(set(explicit))
    assert all((ROOT / path).is_file() for path in explicit)
    # Everything not explicit belongs to the catch-all, which ignores exactly the explicit set.
    assert bins[-1] == ["tests/", *(f"--ignore={path}" for path in sorted(explicit))]


def test_durations_resolve_xunit2_classnames_to_real_modules(impact, suite, tmp_path):
    # xunit2 has no file attribute; test classes append their name to classname.
    junit = tmp_path / "unit.xml"
    junit.write_text(
        "<testsuites><testsuite>"
        '<testcase classname="tests.a.test_a" name="test_case[1]" time="1.5"/>'
        '<testcase classname="tests.a.test_a" name="test_case[2]" time="2.0"/>'
        '<testcase classname="tests.b.test_b.SomeTests" name="test_x" time="4.0"/>'
        '<testcase classname="tests.gone.test_gone" name="test_y" time="9.0"/>'
        "</testsuite></testsuites>"
    )
    assert impact.durations_from_junit(suite, [junit]) == {
        "tests/a/test_a.py": 3.5,
        "tests/b/test_b.py": 4.0,
    }


def test_written_durations_round_trip_through_the_loader(impact, suite, tmp_path, monkeypatch):
    junit = tmp_path / "unit.xml"
    junit.write_text('<testsuite><testcase classname="tests.c.test_c" name="t" time="7.25"/></testsuite>')
    target = tmp_path / "durations.json"
    monkeypatch.setattr(impact, "ROOT", suite)
    assert impact.write_durations(target, [junit], "fixture run") == 0
    data = json.loads(target.read_text())
    assert data["source"] == "fixture run" and data["version"] == 1
    assert impact.load_weights(target) == {"tests/c/test_c.py": 7.2}


def test_the_committed_durations_carry_their_source_and_only_test_paths(impact):
    # Stale entries are harmless (weights only balance; unknown files get the
    # median and the catch-all still runs everything), so a deleted test does
    # not force a refresh -- but every entry must be a tests/ module path.
    data = json.loads(impact.DURATIONS.read_text())
    assert data["source"] and "never selects or skips" in data["use"]
    assert all(impact.safe_path(name, "tests/") and name.endswith(".py") for name in data["files"])
    assert all(seconds >= 0 for seconds in data["files"].values())
