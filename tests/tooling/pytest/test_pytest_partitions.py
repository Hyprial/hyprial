"""Whole-file partitions are diagnostic ordering, never a coverage filter."""
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture
def impact():
    path = Path(__file__).resolve().parents[3] / 'scripts/ci/pytest_impact.py'
    spec = importlib.util.spec_from_file_location("partition_impact", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_partition_collection_is_complete_disjoint_and_preserves_parameters(impact, tmp_path, monkeypatch):
    paths = ["test_fast.py", "test_ci_example.py", "test_state_db_example.py",
             "actor_runtime/test_example.py", "test_mcp_example.py",
             "test_lark_example.py", "test_cli_example.py", "new/test_unknown.py"]
    for path in paths:
        target = tmp_path / "tests" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("import pytest\n@pytest.mark.parametrize('i', [1,2])\ndef test_example(i): pass\n")
    monkeypatch.setattr(impact, "FAST_FILES", ("tests/test_fast.py",))

    def collect(args):
        result = subprocess.run([sys.executable, "-m", "pytest", *args, "--collect-only", "-q", "--import-mode=importlib"],
                                cwd=tmp_path, capture_output=True, text=True, check=True)
        return {line for line in result.stdout.splitlines() if "::" in line}

    complete = collect(["tests/"])
    seen = set()
    groups = impact.partitions(tmp_path, ["tests/"])
    assert [name for name, _ in groups] == ["fast", "ci", "storage", "runtime", "harness", "transport", "cli", "remaining"]
    for name, args in groups:
        current = collect(args)
        assert not current & seen, name
        seen |= current
    assert len(complete) == 16
    assert seen == complete
    assert impact.partitions(tmp_path, ["tests/test_mcp_example.py"]) == [("harness", ["tests/test_mcp_example.py"])]


def test_ambiguous_group_fails_instead_of_running_twice(impact, tmp_path, monkeypatch):
    monkeypatch.setattr(impact, "PARTITION_PREFIXES", (("a", ("tests/test_x",)), ("b", ("tests/test_x",))))
    with pytest.raises(ValueError, match="ambiguous"):
        impact.partition_name("tests/test_x.py")


def test_group_failure_stops_without_retry_and_has_distinct_evidence(impact, tmp_path, monkeypatch):
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(impact, "partitions", lambda *args: [("ci", ["tests/test_ci_x.py"]), ("storage", ["tests/test_state_x.py"]), ("remaining", ["tests/"])])
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1 if len(calls) == 2 else 0)
    monkeypatch.setattr(impact.subprocess, "run", run)
    assert impact.execute(tmp_path, ["tests/"], ["--basetemp", "/tmp/p", "--junitxml", "/tmp/result.xml"], True, grouped=True) == 1
    assert len(calls) == 2
    for args, name in zip(calls, ["ci", "storage"], strict=True):
        assert f"/tmp/p-{name}" in args
        assert f"/tmp/result.xml.{name}.xml" in args
