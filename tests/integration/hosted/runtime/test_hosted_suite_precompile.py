"""run-hosted-suite.py warms both build trees before the first scenario (AT04).

The isolated daemon starts with PYTHONDONTWRITEBYTECODE=1, while mock-model-harness-e2e
builds both venvs with ``uv sync`` (no bytecode) and imports hyprial through
PYTHONPATH=<tree>/src, so every daemon/CLI start re-read and compiled hyprial and
its dependencies from source inside its readiness window (task 37735: the SIGUSR2
stack sat in ``get_code`` reading source). The suite now compiles each tree with
its own interpreter, imports once in a fresh interpreter with the daemon's env, and
records ``precompile.json`` with a verdict that is evidence, never a gate.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

import pytest


SUITE = Path(__file__).resolve().parents[4] / 'contract/e2e/run-hosted-suite.py'
INVITE = Path(__file__).resolve().parents[4] / 'contract/e2e/run-host-invite.py'
BOOTSTRAP_MODULES = ("shell.impl.cli.runtime.entry", "daemon.impl.application", "daemon.impl.owner_migration", "identity.impl.agents.registry")
# The fake venv's site-packages, named after the interpreter that runs the tests.
SITE = f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_suite() -> Any:
    return _load(SUITE, "hosted_suite_precompile")


def _fake_tree(root: Path) -> Path:
    """A minimal build tree: BOOTSTRAP's four modules under src/hyprial, one dependency in .venv."""
    src = root / "src" / "hyprial"
    for rel in ("__init__.py", "cli.py", "identity/__init__.py", "identity/impl/__init__.py",
                "identity/impl/agents/__init__.py", "identity/impl/agents/registry.py",
                "daemon/__init__.py", "daemon/impl/__init__.py", "daemon/impl/application.py", "daemon/impl/owner_migration.py",
                "shell/__init__.py", "shell/impl/__init__.py", "shell/impl/cli/runtime/__init__.py", "shell/impl/cli/runtime/entry.py"):
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("import sample_dep\nVALUE = sample_dep.VALUE\n" if rel == "shell/impl/cli/runtime/entry.py" else "VALUE = 1\n")
    # The venv's site directory is named after the interpreter it links to, not a
    # fixed 3.12: the fake interpreter below is whatever runs the tests.
    site = root / ".venv" / SITE
    site.mkdir(parents=True)
    (site / "sample_dep.py").write_text("VALUE = 1\n")
    (root / ".venv" / "bin").mkdir(parents=True)
    # A real venv interpreter, so the fake site-packages is on sys.path like a uv venv.
    (root / ".venv" / "pyvenv.cfg").write_text(
        f"home = {Path(sys.executable).resolve().parent}\ninclude-system-site-packages = false\n"
        f"version = {sys.version.split()[0]}\n")
    os.symlink(Path(sys.executable).resolve(), root / ".venv" / "bin" / "python")
    return root


def _pyc(source: Path) -> Path:
    return Path(importlib.util.cache_from_source(str(source)))


# ---------- targets and census ----------

def test_targets_cover_src_and_site_packages(tmp_path: Path) -> None:
    suite = _load_suite()
    tree = _fake_tree(tmp_path / "tree")
    targets = suite.precompile_targets(tree)
    assert tree / "src" in targets
    assert tree / ".venv" / SITE in targets


def test_targets_skip_missing_directories(tmp_path: Path) -> None:
    suite = _load_suite()
    (tmp_path / "bare" / "src").mkdir(parents=True)
    assert suite.precompile_targets(tmp_path / "bare") == [tmp_path / "bare" / "src"]


# ---------- the probe observes real source compiles, not cache presence ----------

def test_probe_without_bytecode_reports_source_compiles(tmp_path: Path) -> None:
    """Missing cache: the fresh-interpreter probe compiles from source and writes nothing."""
    suite = _load_suite()
    tree = _fake_tree(tmp_path / "tree")
    probe = suite.import_probe(tree)
    assert probe["status"] == "ok"
    assert probe["sourceCompiles"] >= len(BOOTSTRAP_MODULES) + 1  # plus sample_dep
    assert probe["cachePresent"] == 0 and probe["cacheMissing"] == probe["modulesInTree"]
    assert all(entry["compiledFromSource"] for entry in probe["bootstrap"].values())
    assert not list((tree / "src").rglob("*.pyc")), "the probe itself must not write bytecode"


def test_warm_tree_makes_the_daemon_path_adopt_bytecode(tmp_path: Path) -> None:
    suite = _load_suite()
    tree = _fake_tree(tmp_path / "tree")
    report = suite.warm_tree(tree)

    assert report["compile"]["status"] == "ok" and report["compile"]["survivors"] == []
    probe = report["import"]
    assert probe["status"] == "ok"
    assert probe["sourceCompiles"] == 0
    assert probe["bytecodeAdopted"] == probe["modulesInTree"] >= len(BOOTSTRAP_MODULES) + 1
    assert probe["dontWriteBytecode"] is True and probe["pycachePrefix"] is None and probe["optimize"] == 0
    assert probe["cacheTag"] == sys.implementation.cache_tag
    for name in BOOTSTRAP_MODULES:
        entry = probe["bootstrap"][name]
        assert entry["underSrc"] and not entry["compiledFromSource"]
        assert entry["origin"].startswith(str((tree / "src").resolve()))
    assert _pyc(tree / ".venv" / SITE / "sample_dep.py").exists()
    assert report["before"]["src"]["pyc"] == 0
    assert report["after"]["src"]["pyc"] >= report["after"]["src"]["py"] > 0
    assert suite.precompile_verdict({"new": report, "old": report}) == {"effective": True, "problems": []}


def test_stale_bytecode_is_detected_as_source_compile(tmp_path: Path) -> None:
    suite = _load_suite()
    tree = _fake_tree(tmp_path / "tree")
    report = suite.warm_tree(tree)
    target = tree / "src/hyprial/daemon/impl/application.py"
    target.write_text("VALUE = 2  # edited after compileall\n")
    os.utime(target, (time.time() + 10, time.time() + 10))
    probe = suite.import_probe(tree)
    assert _pyc(target).exists(), "the cache is still present..."
    assert probe["bootstrap"]["daemon.impl.application"]["compiledFromSource"], "...but stale, so source compiles"
    assert probe["sourceCompiles"] >= 1
    report["import"] = probe
    assert not suite.precompile_verdict({"new": report, "old": report})["effective"]


def test_wrong_magic_bytecode_is_detected_as_source_compile(tmp_path: Path) -> None:
    suite = _load_suite()
    tree = _fake_tree(tmp_path / "tree")
    suite.warm_tree(tree)
    cache = _pyc(tree / "src/hyprial/identity/impl/agents/registry.py")
    data = bytearray(cache.read_bytes())
    data[0:4] = b"\x00\x00\x0d\x0a"
    cache.write_bytes(bytes(data))
    probe = suite.import_probe(tree)
    assert probe["cachePresent"] == probe["modulesInTree"]
    assert probe["bootstrap"]["identity.impl.agents.registry"]["compiledFromSource"]
    assert probe["sourceCompiles"] == 1


def test_runtime_compile_of_a_module_file_is_not_a_source_fallback(tmp_path: Path) -> None:
    """anyio's lazy importer compile()s its own source to an AST at import time; that
    is not the loader falling back to source and must not count as one."""
    suite = _load_suite()
    tree = _fake_tree(tmp_path / "tree")
    (tree / "src/hyprial/identity/impl/agents/registry.py").write_text(
        "import ast\nast.parse(open(__file__).read(), __file__)\n"
        "compile(open(__file__).read(), __file__, 'exec', ast.PyCF_ONLY_AST)\nVALUE = 1\n")
    report = suite.warm_tree(tree)
    probe = report["import"]
    assert probe["sourceCompiles"] == 0
    assert probe["runtimeCompiledPaths"] == [str((tree / "src/hyprial/identity/impl/agents/registry.py").resolve())]
    assert suite.precompile_verdict({"new": report, "old": report})["effective"]


# ---------- environments ----------

def test_probe_env_is_the_isolated_daemons_env(tmp_path: Path) -> None:
    """Same keys and isolation as run-host-invite's Scenario + use_build, in a scratch root."""
    suite = _load_suite()
    invite = _load(INVITE, "hosted_suite_invite_ref")
    tree = _fake_tree(tmp_path / "tree")
    scenario_root = tmp_path / "scenario"
    scenario_root.mkdir()
    reference = invite.Scenario(scenario_root, tmp_path / "prod.sock").env
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = suite.daemon_env(tree, scratch)
    assert set(env) == set(reference)
    assert env["PYTHONPATH"] == str(tree / "src") and env["AT01_SOURCE"] == str(tree)
    assert env["PYTHONDONTWRITEBYTECODE"] == "1" and env["HYPRIAL_NETWORK_ISOLATED"] == "1"
    for key in ("HOME", "HYPRIAL_HOME", "HARNESS_STATE_DIR", "TMPDIR", "XDG_CONFIG_HOME"):
        assert Path(env[key]).resolve().is_relative_to(scratch.resolve())
    assert {k: v for k, v in env.items() if k not in {"PYTHONPATH", "AT01_SOURCE"} and not v.startswith(str(scratch))} \
        == {k: v for k, v in reference.items() if k not in {"PYTHONPATH", "AT01_SOURCE"}
            and not v.startswith(str(scenario_root))}


def test_inherited_cache_prefix_and_optimize_do_not_leak(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite = _load_suite()
    tree = _fake_tree(tmp_path / "tree")
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("PYTHONOPTIMIZE", "2")
    report = suite.warm_tree(tree)
    assert not (tmp_path / "elsewhere").exists()
    assert _pyc(tree / "src/hyprial/cli.py").exists()
    assert report["import"]["sourceCompiles"] == 0 and report["import"]["optimize"] == 0


# ---------- budgets, failures and reaping ----------

TERM_IGNORING_TREE = r"""
import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)'])
time.sleep(600)
"""


def test_run_bounded_timeout_kills_the_whole_group(tmp_path: Path) -> None:
    suite = _load_suite()
    started = time.monotonic()
    ran = suite.run_bounded([sys.executable, "-c", TERM_IGNORING_TREE], {"PATH": "/usr/bin:/bin"}, tmp_path, 1)
    assert ran["status"] == "timeout" and ran["survivors"] == []
    assert time.monotonic() - started < 1 + suite.REAP_TIMEOUT + 2


def test_run_bounded_spawn_error_and_nonzero(tmp_path: Path) -> None:
    suite = _load_suite()
    assert suite.run_bounded([str(tmp_path / "missing-python")], {}, tmp_path, 5)["status"] == "spawn-error"
    ran = suite.run_bounded([sys.executable, "-c", "raise SystemExit(3)"], {"PATH": "/usr/bin:/bin"}, tmp_path, 5)
    assert ran["status"] == "nonzero" and ran["returncode"] == 3


def test_run_bounded_unobservable_group_is_reap_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite = _load_suite()
    monkeypatch.setattr(suite, "group_members", lambda pgid, timeout: None)
    ran = suite.run_bounded([sys.executable, "-c", "pass"], {"PATH": "/usr/bin:/bin"}, tmp_path, 5)
    assert ran["status"] == "reap-failed"


def test_compile_timeout_is_recorded_and_not_effective(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite = _load_suite()
    tree = _fake_tree(tmp_path / "tree")
    real = suite.run_bounded

    def slow_compile(command: list[str], env: dict[str, str], cwd: Path, timeout: float) -> dict[str, Any]:
        if "compileall" in command:
            return real([sys.executable, "-c", TERM_IGNORING_TREE], env, cwd, 1)
        return real(command, env, cwd, timeout)

    monkeypatch.setattr(suite, "run_bounded", slow_compile)
    report = suite.warm_tree(tree)
    assert report["compile"]["status"] == "timeout" and report["compile"]["survivors"] == []
    verdict = suite.precompile_verdict({"new": report})
    assert not verdict["effective"] and "new: compile timeout" in verdict["problems"]


def test_missing_tree_is_a_distinguishable_failure(tmp_path: Path) -> None:
    suite = _load_suite()
    report = suite.warm_tree(tmp_path / "no-such-tree")
    assert report["compile"]["status"] == "skipped-no-targets"
    assert report["import"]["status"] == "spawn-error"
    assert not suite.precompile_verdict({"old": report})["effective"]


# ---------- evidence and suite wiring ----------

def test_warm_trees_writes_evidence_with_verdict(tmp_path: Path) -> None:
    suite = _load_suite()
    trees = {"new": _fake_tree(tmp_path / "new"), "old": _fake_tree(tmp_path / "old")}
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    lines: list[str] = []
    suite.warm_trees(trees, evidence, lines.append)

    record = json.loads((evidence / "precompile.json").read_text())
    assert set(record) == {"new", "old", "verdict"}
    assert record["verdict"] == {"effective": True, "problems": []}
    for name in ("new", "old"):
        assert record[name]["tree"] == str(trees[name])
        assert record[name]["import"]["sourceCompiles"] == 0
    assert lines[-1] == "PRECOMPILE effective=True problems=0"


def _run_main(suite: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events: list[Any]) -> Path:
    def fake_scenario(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if command[0] == "git":  # the readiness summary records each tree's HEAD
            return subprocess.CompletedProcess(command, 0, "0" * 40 + "\n", "")
        events.append(("scenario", Path(command[1]).name))
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(suite.subprocess, "run", fake_scenario)
    old = tmp_path / "old"
    old.mkdir(exist_ok=True)
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(sys, "argv", ["run-hosted-suite.py", "--old-tree", str(old),
                                      "--production-socket", str(tmp_path / "prod.sock"),
                                      "--evidence", str(evidence)])
    suite.main()
    return evidence


def test_main_warms_both_trees_before_first_scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite = _load_suite()
    events: list[Any] = []

    def fake_warm(trees: dict[str, Path], evidence: Path, echo: Any) -> dict[str, Any]:
        events.append(("warm", {name: str(path) for name, path in trees.items()}))
        (evidence / "precompile.json").write_text("{}\n")
        return {}

    monkeypatch.setattr(suite, "warm_trees", fake_warm)
    evidence = _run_main(suite, tmp_path, monkeypatch, events)
    assert events[0] == ("warm", {"new": str(suite.ROOT), "old": str(tmp_path / "old")})
    assert events[1] == ("scenario", "run-host-invite.py")
    manifest = json.loads((evidence / "manifest.json").read_text())
    assert "precompile.json" in manifest


def test_main_continues_when_warm_up_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The warm-up is best effort: even if it raises, it leaves a note and the scenarios still run."""
    suite = _load_suite()
    events: list[Any] = []

    def broken_census(_: Path) -> dict[str, Any]:
        raise OSError("disk gone")

    monkeypatch.setattr(suite, "pyc_census", broken_census)
    evidence = _run_main(suite, tmp_path, monkeypatch, events)
    assert events == [("scenario", "run-host-invite.py")]
    record = json.loads((evidence / "precompile.json").read_text())
    assert "disk gone" in record["new"]["note"] and "disk gone" in record["old"]["note"]
    assert record["verdict"]["effective"] is False


# ---------- review round 3 (R1-R4) ----------

class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _good_probe() -> dict[str, Any]:
    return {"status": "ok", "sourceCompiles": 0, "modulesInTree": 5, "bytecodeAdopted": 5,
            "dontWriteBytecode": True, "pycachePrefix": None, "optimize": 0, "preloadedInTree": [],
            "bootstrap": {name: {"origin": f"/t/src/hyprial/{name}.py", "underSrc": True,
                                 "compiledFromSource": False, "preloaded": False} for name in BOOTSTRAP_MODULES}}


def _good_record() -> dict[str, Any]:
    return {name: {"compile": {"status": "ok"}, "import": _good_probe()} for name in ("new", "old")}


def test_modules_imported_before_the_audit_hook_are_not_counted_as_bytecode(tmp_path: Path) -> None:
    """R1 PTH_PRELOAD: a .pth that imports BOOTSTRAP at interpreter start runs before the
    probe's hook; those loads are unobserved, never "bytecode adopted"."""
    suite = _load_suite()
    tree = _fake_tree(tmp_path / "tree")
    site = tree / ".venv" / SITE
    (site / "preload.pth").write_text(
        "import importlib; [importlib.import_module('hyprial.' + n) for n in "
        "('shell.impl.cli.runtime.entry', 'daemon.impl.application', 'daemon.impl.owner_migration', 'identity.impl.agents.registry')]\n")
    probe = suite.import_probe(tree)
    assert probe["status"] == "ok"
    assert probe["cachePresent"] == 0
    assert probe["bytecodeAdopted"] == 0
    assert len(probe["preloadedInTree"]) >= len(BOOTSTRAP_MODULES)
    assert all(entry["preloaded"] for entry in probe["bootstrap"].values())
    verdict = suite.precompile_verdict({"new": {"compile": {"status": "ok"}, "import": probe},
                                        "old": _good_record()["old"]})
    assert not verdict["effective"]


@pytest.mark.parametrize("mutate", [
    lambda r: r.clear(),                                                    # EMPTY_RECORD
    lambda r: r.pop("old"),                                                 # MISSING_OLD
    lambda r: r["new"]["import"].__setitem__("bootstrap", {}),              # EMPTY_BOOTSTRAP
    lambda r: r["new"]["import"].__setitem__("sourceCompiles", None),       # null is not zero
    lambda r: r["new"]["import"].pop("modulesInTree"),                      # KeyError before
    lambda r: r["new"]["import"]["bootstrap"].pop("identity.impl.agents.registry"),  # a module missing
    lambda r: r["old"].__setitem__("import", "garbage"),                    # wrong type
    lambda r: r["new"]["import"].__setitem__("bytecodeAdopted", 0),         # nothing adopted
])
def test_verdict_rejects_missing_or_invalid_data_without_raising(mutate: Any) -> None:
    suite = _load_suite()
    record = _good_record()
    assert suite.precompile_verdict(record) == {"effective": True, "problems": []}
    mutate(record)
    verdict = suite.precompile_verdict(record)
    assert verdict["effective"] is False and verdict["problems"]


def test_main_runs_scenarios_when_the_probe_returns_garbage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite = _load_suite()
    events: list[Any] = []
    monkeypatch.setattr(suite, "warm_tree", lambda tree: {"tree": str(tree), "compile": {"status": "ok"},
                                                          "import": {"status": "ok"}})
    evidence = _run_main(suite, tmp_path, monkeypatch, events)
    assert events == [("scenario", "run-host-invite.py")]
    assert json.loads((evidence / "precompile.json").read_text())["verdict"]["effective"] is False


def test_reap_shares_one_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R3a: wait, ps and sleep consume one REAP_TIMEOUT budget (was 21.1 s for 1 + 10)."""
    suite = _load_suite()
    clock = _Clock()
    monkeypatch.setattr(suite, "clock", clock.monotonic)
    monkeypatch.setattr(suite, "sleep", clock.sleep)

    class Stuck:
        pid, returncode = 424242, None

        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            clock.sleep(timeout or 0)
            raise subprocess.TimeoutExpired("stuck", timeout)

    def slow_ps(pgid: int, timeout: float) -> list[str]:
        clock.sleep(min(5, timeout))
        return ["424242"]

    monkeypatch.setattr(suite.subprocess, "Popen", Stuck)
    monkeypatch.setattr(suite, "group_members", slow_ps)
    monkeypatch.setattr(suite.os, "killpg", lambda *_: None)
    ran = suite.run_bounded(["x"], {}, tmp_path, 1)
    assert ran["status"] == "reap-failed"
    assert ran["seconds"] <= 1 + suite.REAP_TIMEOUT + 0.2, ran["seconds"]


def test_budget_counts_process_creation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R3b: a Popen that itself took 101 s cannot come back "ok" under a 1 s budget."""
    suite = _load_suite()
    clock = _Clock()
    monkeypatch.setattr(suite, "clock", clock.monotonic)
    monkeypatch.setattr(suite, "sleep", clock.sleep)

    class SlowSpawn:
        pid, returncode = 424243, 0

        def __init__(self, *_: Any, **__: Any) -> None:
            clock.sleep(101)

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return "", ""

    monkeypatch.setattr(suite.subprocess, "Popen", SlowSpawn)
    monkeypatch.setattr(suite, "group_members", lambda pgid, timeout: [])
    monkeypatch.setattr(suite.os, "killpg", lambda *_: None)
    ran = suite.run_bounded(["x"], {}, tmp_path, 1)
    assert ran["status"] == "timeout"


@pytest.mark.parametrize("listing", ["not-a-pid not-a-pgid not-a-status\n", "12 34\n", "", "12 34 \n",
                                     "12 34 5\n"])
def test_unparseable_ps_is_unknown_not_empty(monkeypatch: pytest.MonkeyPatch, listing: str) -> None:
    """R3c"""
    suite = _load_suite()
    monkeypatch.setattr(suite.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, listing, ""))
    assert suite.group_members(1, 5) is None


def test_ps_question_mark_state_is_a_live_member(monkeypatch: pytest.MonkeyPatch) -> None:
    suite = _load_suite()
    monkeypatch.setattr(suite.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "7 7 Ss\n8 7 ?\n9 7 Z\n", ""))
    assert suite.group_members(7, 5) == ["7", "8"]


def test_unconfirmed_reap_stops_the_warm_up_and_the_suite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R3d: survivors after compile are an infrastructure abort, not a cleaned-up failure."""
    suite = _load_suite()
    probes: list[Path] = []
    monkeypatch.setattr(suite, "run_bounded", lambda *a, **k: {"status": "reap-failed", "returncode": None,
                                                                 "survivors": ["909090"], "seconds": 1.0,
                                                                 "stdout": "", "stderr": ""})
    monkeypatch.setattr(suite, "import_probe", lambda tree: probes.append(tree) or {"status": "ok"})
    tree = _fake_tree(tmp_path / "tree")
    report = suite.warm_tree(tree)
    assert report["infraAbort"] and probes == []

    events: list[Any] = []
    evidence = _run_main(suite, tmp_path, monkeypatch, events)
    assert events == [], "no scenario may run over unreaped warm-up processes"
    record = json.loads((evidence / "precompile.json").read_text())
    assert "old" not in record or record["old"].get("skipped")
    suite_rows = json.loads((evidence / "suite.json").read_text())
    assert suite_rows == [{"scenario": "warm-up", "result": "NOT_RUN: warm-up reap unconfirmed",
                           "exitCode": suite.INFRA_ABORT_EXIT}]


def test_empty_targets_never_run_compileall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R4: compileall without targets compiles sys.path (the shared stdlib)."""
    suite = _load_suite()
    calls: list[list[str]] = []
    monkeypatch.setattr(suite, "run_bounded", lambda command, *a, **k: calls.append(command) or {"status": "ok"})
    monkeypatch.setattr(suite, "import_probe", lambda tree: {"status": "ok"})
    (tmp_path / "empty").mkdir()
    report = suite.warm_tree(tmp_path / "empty")
    assert not any("compileall" in command for command in calls)
    assert report["compile"]["status"] == "skipped-no-targets"
    assert not suite.precompile_verdict({"new": report, "old": report})["effective"]


def test_targets_outside_the_tree_are_refused(tmp_path: Path) -> None:
    suite = _load_suite()
    tree = _fake_tree(tmp_path / "tree")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (tree / "src").rename(tmp_path / "moved-src")
    os.symlink(outside, tree / "src")
    assert tree / "src" not in suite.precompile_targets(tree)


# ---------- review round 4 (R3e): the probe's stdout cannot override the parent ----------

def _probe_run(stdout: str, status: str = "reap-failed") -> Any:
    def fake(command: list[str], env: dict[str, str], cwd: Path, timeout: float) -> dict[str, Any]:
        if "compileall" in command:
            return {"status": "ok", "returncode": 0, "seconds": 0.1, "survivors": [], "stdout": "", "stderr": ""}
        return {"status": status, "returncode": None, "seconds": 0.2,
                "survivors": ["909090"] if status == "reap-failed" else [], "stdout": stdout, "stderr": "diag"}
    return fake


@pytest.mark.parametrize("stdout", ["null\n", "3\n", "[1, 2]\n", "", "not json\n",
                                    '{"status": "ok", "returncode": 0, "survivors": [], "sourceCompiles": 0}\n'])
def test_unconfirmed_probe_reap_aborts_whatever_the_probe_printed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    suite = _load_suite()
    monkeypatch.setattr(suite, "run_bounded", _probe_run(stdout))
    probe = suite.import_probe(_fake_tree(tmp_path / "t"))
    assert probe["status"] == "reap-failed" and probe["survivors"] == ["909090"]

    started: list[str] = []
    real_probe = suite.import_probe
    monkeypatch.setattr(suite, "import_probe", lambda tree: started.append(str(tree)) or real_probe(tree))
    events: list[Any] = []
    evidence = _run_main(suite, tmp_path, monkeypatch, events)
    assert started == [str(suite.ROOT)], "only the first tree's probe may start"
    assert events == [], "no scenario may run"
    record = json.loads((evidence / "precompile.json").read_text())
    assert record["new"]["infraAbort"] and record["new"]["import"]["survivors"] == ["909090"]
    assert record["old"]["skipped"]
    assert "precompile.json" in json.loads((evidence / "manifest.json").read_text())
    assert suite.main.__module__  # main returned (6) instead of raising


@pytest.mark.parametrize("stdout", ["null\n", "[1]\n", "not json\n", ""])
def test_confirmed_reap_with_bad_probe_output_is_best_effort(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    suite = _load_suite()
    monkeypatch.setattr(suite, "run_bounded", _probe_run(stdout, status="ok"))
    probe = suite.import_probe(_fake_tree(tmp_path / "t"))
    assert probe["status"] == "ok" and probe["measurement"] == "invalid" and probe["stdoutTail"] == stdout[-500:]
    events: list[Any] = []
    evidence = _run_main(suite, tmp_path, monkeypatch, events)
    assert events == [("scenario", "run-host-invite.py")]
    record = json.loads((evidence / "precompile.json").read_text())
    assert record["verdict"]["effective"] is False and "infraAbort" not in record["new"]


def test_probe_measurement_cannot_overwrite_parent_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite = _load_suite()
    monkeypatch.setattr(suite, "run_bounded", _probe_run(
        '{"status": "ok", "returncode": 0, "survivors": [], "seconds": 9, "sourceCompiles": 0}\n', status="nonzero"))
    probe = suite.import_probe(_fake_tree(tmp_path / "t"))
    assert probe["status"] == "nonzero" and probe["returncode"] is None and probe["seconds"] == 0.2
    assert probe["sourceCompiles"] == 0
