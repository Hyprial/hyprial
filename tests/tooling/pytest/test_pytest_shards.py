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


def test_nested_fd_probe_runs_before_other_tests_in_its_shard(impact, suite):
    probe = "tests/tooling/isolation/test_fd_hygiene_collection.py"
    write(suite, probe)
    selected = ["tests/a/test_a.py", probe, "tests/b/test_b.py"]
    assert impact.shard_plan(suite, selected, 1, {}) == [
        [probe, "tests/a/test_a.py", "tests/b/test_b.py"]
    ]


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


def test_only_the_probe_wave_is_bounded_and_the_rest_keeps_the_declared_workers(impact, monkeypatch, capsys):
    probe = impact.NESTED_XDIST_PROBE
    shard = [probe, "tests/other/test_other.py", "tests/a/test_a.py"]
    bins = [[], [], shard, []]
    monkeypatch.setattr(impact, "shard_plan", lambda *args: bins)
    decision = {"mode": "selected", "tests": shard}
    status, calls = run_main(
        impact, monkeypatch,
        ["--shard", "3/4", "--", "-q", "-n", "2", "--basetemp=/tmp/bt", "--junitxml=/tmp/unit.xml"],
        decision,
    )
    assert status == 0
    assert len(calls) == 2
    probe_argv, rest_argv = calls
    assert probe_argv[3] == probe
    assert rest_argv[3:5] == shard[1:]
    assert probe_argv[probe_argv.index("-n") + 1] == "1"
    assert rest_argv[rest_argv.index("-n") + 1] == "2"
    assert "--basetemp=/tmp/bt-fd-probe" in probe_argv
    assert "--junitxml=/tmp/unit.xml.fd-probe.xml" in probe_argv
    assert "--basetemp=/tmp/bt-shard-3of4" in rest_argv
    assert "--junitxml=/tmp/unit.xml.shard-3of4.xml" in rest_argv
    stages = [json.loads(line.split(" ", 1)[1]) for line in capsys.readouterr().out.splitlines()
              if line.startswith("PYTEST_STAGE ")]
    assert [stage["name"] for stage in stages] == ["fd-probe", "shard-3of4"]


def test_probe_wave_and_rest_partition_the_shard_exactly(impact):
    shard = [impact.NESTED_XDIST_PROBE, "tests/a/test_a.py", "tests/b/test_b.py"]
    probe, rest = impact.probe_wave(shard)
    assert probe == [impact.NESTED_XDIST_PROBE]
    assert rest == ["tests/a/test_a.py", "tests/b/test_b.py"]
    assert probe + rest == shard
    assert not set(probe) & set(rest)
    assert impact.probe_wave(["tests/a/test_a.py", "tests/b/test_b.py"]) == (
        [], ["tests/a/test_a.py", "tests/b/test_b.py"])
    assert impact.probe_wave([impact.NESTED_XDIST_PROBE]) == ([impact.NESTED_XDIST_PROBE], [])
    assert impact.probe_wave([]) == ([], [])


@pytest.mark.parametrize("probe_status,rest_status", [(1, 0), (0, 1), (1, 1), (0, 0)])
def test_a_failed_probe_wave_never_skips_the_rest_of_its_shard(impact, monkeypatch, probe_status, rest_status):
    probe = impact.NESTED_XDIST_PROBE
    shard = [probe, "tests/other/test_other.py"]
    bins = [[], [], shard, []]
    monkeypatch.setattr(impact, "shard_plan", lambda *args: bins)
    statuses = iter([probe_status, rest_status])
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=next(statuses))

    monkeypatch.setattr(impact.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["pytest_impact.py", "--shard", "3/4", "--", "-q", "-n", "2"])
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert impact.main() == (probe_status or rest_status)
    assert len(calls) == 2


def test_a_probe_only_shard_stays_a_single_bounded_wave(impact, monkeypatch):
    probe = impact.NESTED_XDIST_PROBE
    bins = [[probe], [], [], []]
    monkeypatch.setattr(impact, "shard_plan", lambda *args: bins)
    decision = {"mode": "selected", "tests": [probe]}
    status, calls = run_main(
        impact, monkeypatch,
        ["--shard", "1/4", "--", "-q", "-n", "2", "--junitxml=/tmp/unit.xml"], decision,
    )
    assert status == 0
    assert len(calls) == 1
    assert calls[0][3] == probe
    assert calls[0][calls[0].index("-n") + 1] == "1"
    assert "--junitxml=/tmp/unit.xml.shard-1of4.xml" in calls[0]


def test_other_shards_keep_the_declared_worker_count(impact, monkeypatch):
    bins = [["tests/a/test_a.py"], [], [], []]
    monkeypatch.setattr(impact, "shard_plan", lambda *args: bins)
    decision = {"mode": "selected", "tests": ["tests/a/test_a.py"]}
    status, calls = run_main(
        impact, monkeypatch, ["--shard", "1/4", "--", "-q", "-n", "2"], decision
    )
    assert status == 0
    assert calls[0][calls[0].index("-n") + 1] == "2"


def test_shard_cannot_be_combined_with_grouped_waves(impact, monkeypatch):
    with pytest.raises(SystemExit):
        run_main(impact, monkeypatch, ["--shard", "1/4", "--grouped"], {"mode": "full", "tests": ["tests/"]})


def test_repository_shards_cover_every_test_file(impact):
    weights = impact.load_weights(impact.DURATIONS)
    bins = impact.shard_plan(ROOT, ["tests/"], 4, weights)
    explicit = [path for paths in bins[:-1] for path in paths]
    assert len(explicit) == len(set(explicit))
    assert all((ROOT / path).is_file() for path in explicit)
    assert sum(paths[:1] == [impact.NESTED_XDIST_PROBE] for paths in bins[:-1]) == 1
    assert bins[-1] == [
        "tests/",
        *(f"--ignore={path}" for path in sorted(explicit)),
        *(f"--ignore={base}" for base in impact.isolated_paths(ROOT, ["tests/"])),
    ]


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
