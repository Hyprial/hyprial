"""Every isolated-daemon start attempt and every Scenario.cli call leaves timing evidence (AT04 card 3).

The readiness timeout (#899) only describes runs that failed. To see how much of the
45 s budget each start really used, per build tree and per scenario, the harness writes:

- ``attempts.json``: a ledger, appended BEFORE each Popen (what should have a record);
- ``daemon-N-attempt.json``: one terminal record per attempt (ready / timeout / exited /
  spawn-error / error), observed before any evidence write or diagnostics;
- ``daemon-N-timing.json``: BOOTSTRAP's own import wall/CPU (import_module only);
- ``commands.json``: per Scenario.cli call, status, seconds and the 30 s budget.

run-hosted-suite.py joins ledgers and records into ``readiness.json`` with an explicit
completeness verdict. None of this changes any scenario's PASS/FAIL.
"""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

import pytest


RUNNER = Path(__file__).resolve().parents[4] / 'contract/e2e/run-host-invite.py'
LEDGER = Path(__file__).resolve().parents[4] / 'contract/e2e/run-capability-ledger.py'
SUITE = Path(__file__).resolve().parents[4] / 'contract/e2e/run-hosted-suite.py'
CORE = ("shell.impl.cli.runtime.entry", "daemon.impl.application", "daemon.impl.owner_migration", "identity.impl.agents.registry")


def _hyprial_modules() -> set[str]:
    return {name for name in sys.modules if name == "hyprial" or name.startswith("hyprial.")}


@pytest.fixture(autouse=True)
def _drop_hyprial_modules_imported_by_the_test(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Remove ``hyprial*`` modules a test imported from its synthetic tree.

    Some tests here delete every ``hyprial*`` module and re-import from a
    copied tree.  ``monkeypatch.delitem`` restores only the deleted entries,
    so a module first imported during the test (e.g. ``hyprial.cli``) stayed
    behind, bound to the copy; a later test in the same worker then saw
    ``cli.main`` that is not ``shell...main`` (intermittent dev red in
    ``test_shell_cli_entry`` under ``--dist=loadfile``).  This teardown runs
    before monkeypatch's (it depends on monkeypatch), so it removes only the
    names absent from the snapshot; monkeypatch then puts the originals back.
    """

    before = _hyprial_modules()
    yield
    for name in _hyprial_modules() - before:
        sys.modules.pop(name, None)


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stop(process: Any) -> None:
    if isinstance(process, subprocess.Popen) and process.poll() is None:
        process.kill()
        process.wait(timeout=5)


def _fake_tree(root: Path, cli_body: str = "def main():\n    import time\n    time.sleep(60)\n",
               extra: dict[str, str] | None = None) -> Path:
    """A build tree whose src/hyprial has BOOTSTRAP's modules; ``cli.main`` just waits."""
    package = root / "src" / "hyprial"
    files = {"__init__.py": "", "identity/__init__.py": "", "identity/impl/__init__.py": "",
             "identity/impl/agents/__init__.py": "", "identity/impl/agents/registry.py": "VALUE = 1\n",
             "daemon/__init__.py": "", "daemon/impl/__init__.py": "",
             "daemon/impl/application.py": "VALUE = 1\n", "daemon/impl/owner_migration.py": "VALUE = 1\n",
             "shell/__init__.py": "", "shell/impl/__init__.py": "", "shell/impl/cli/runtime/__init__.py": "",
             "shell/impl/cli/runtime/entry.py": cli_body, "cli.py": cli_body, **(extra or {})}
    for rel, text in files.items():
        path = package / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _scenario(runner: Any, tmp_path: Path, tree: Path, **env: str) -> Any:
    root = tmp_path / "scenario-evidence"
    root.mkdir()
    scenario = runner.Scenario(root, tmp_path / "production.sock")
    scenario.env.update(PYTHONPATH=str(tree / "src"), AT01_SOURCE=str(tree), **env)
    return scenario


def _read(path: Path) -> Any:
    return json.loads(path.read_text())


def _ready_after(runner: Any, scenario: Any, marker: Path) -> Any:
    def ping(_socket: Path) -> dict[str, Any]:
        if not marker.exists():
            raise OSError("not yet")
        return {"running": True, "pid": scenario.process.pid, "owner": "host-owner", "nodeId": "host-node"}
    return ping


# ---------- B4: BOOTSTRAP timing covers import_module only, core vs all ----------

def test_real_start_records_bootstrap_timing_and_a_ready_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load(RUNNER, "timing_ready")
    monkeypatch.setattr(runner.os, "nice", lambda _increment: None)
    tree = _fake_tree(tmp_path / "tree")
    scenario = _scenario(runner, tmp_path, tree)
    monkeypatch.setattr(runner, "ping", _ready_after(runner, scenario, scenario.root / "daemon-1-timing.json"))
    try:
        scenario.start()
    finally:
        _stop(scenario.process)

    timing = _read(scenario.root / "daemon-1-timing.json")
    assert list(timing["modules"]) == list(CORE)
    assert timing["coreWall"] == pytest.approx(sum(timing["modules"][n]["wall"] for n in CORE))
    assert timing["allWall"] == pytest.approx(timing["coreWall"]) and timing["sourceCheckWall"] >= 0
    imports = _read(scenario.root / "daemon-1-imports.json")
    assert set(imports) == set(CORE) and all(set(v) == {"path", "sha256"} for v in imports.values())

    ledger = _read(scenario.root / "attempts.json")
    assert [entry["label"] for entry in ledger] == ["daemon-1"] and ledger[0]["startedUtc"].endswith("+00:00")
    record = _read(scenario.root / "daemon-1-attempt.json")
    assert record["status"] == "ready" and record["label"] == "daemon-1"
    assert record["tree"] == str(tree) and record["budgetSeconds"] == 45.0
    assert 0 < record["elapsedSeconds"] == record["popenToReadySeconds"] < 45
    assert record["marginSeconds"] == pytest.approx(45.0 - record["elapsedSeconds"])
    assert record["overrunSeconds"] is None and record["exitCode"] is None
    assert record["import"]["coreWall"] == timing["coreWall"] and record["import"]["note"] is None
    assert record["startedUtc"] <= record["observedUtc"]


def test_at02_fifth_module_is_timed_separately_from_the_core(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run-capability-ledger.py appends the native grants provider to BOOTSTRAP."""
    ledger_module = _load(LEDGER, "timing_at02")
    runner = ledger_module.base
    assert "'identity.impl.agents.runtime.grants')" in runner.BOOTSTRAP, "the AT02 replace must still match"
    monkeypatch.setattr(runner.os, "nice", lambda _increment: None)
    tree = _fake_tree(tmp_path / "tree", extra={"identity/impl/agents/runtime/__init__.py": "",
                                               "identity/impl/agents/runtime/grants.py": "VALUE = 2\n"})
    scenario = _scenario(runner, tmp_path, tree)
    monkeypatch.setattr(runner, "ping", _ready_after(runner, scenario, scenario.root / "daemon-1-timing.json"))
    try:
        scenario.start()
    finally:
        _stop(scenario.process)
    timing = _read(scenario.root / "daemon-1-timing.json")
    assert list(timing["modules"]) == [*CORE, "identity.impl.agents.runtime.grants"]
    assert timing["coreWall"] == pytest.approx(sum(timing["modules"][n]["wall"] for n in CORE))
    assert timing["allWall"] == pytest.approx(timing["coreWall"] + timing["modules"]["identity.impl.agents.runtime.grants"]["wall"])
    assert "identity.impl.agents.runtime.grants" in _read(scenario.root / "daemon-1-imports.json")


def test_source_check_time_is_never_counted_as_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic: run BOOTSTRAP in-process on a virtual clock where each import costs
    exactly 1 s and each post-import read_bytes (the source check) costs exactly 7 s."""
    runner = _load(RUNNER, "timing_sourcecheck")
    tree = _fake_tree(tmp_path / "tree", "def main():\n    pass\n").resolve()
    virtual = {"t": 0.0}
    real_import = importlib.import_module
    real_read = Path.read_bytes

    def timed_import(name: str, *args: Any) -> Any:
        module = real_import(name, *args)
        if name.startswith("hyprial."):
            virtual["t"] += 1.0
        return module

    def timed_read(self: Path) -> bytes:
        if str(self).startswith(str(tree)):
            virtual["t"] += 7.0
        return real_read(self)

    for name in [m for m in sys.modules if m == "hyprial" or m.startswith("hyprial.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.syspath_prepend(str(tree / "src"))
    monkeypatch.setattr(importlib, "import_module", timed_import)
    monkeypatch.setattr(Path, "read_bytes", timed_read)
    monkeypatch.setattr(time, "perf_counter", lambda: virtual["t"])
    monkeypatch.setattr(time, "process_time", lambda: virtual["t"])
    for key, name in (("AT01_SOURCE", str(tree)), ("AT01_IMPORT_EVIDENCE", "imports.json"),
                      ("AT01_FAULTHANDLER_EVIDENCE", "sigusr2.txt"), ("AT01_TIMING_EVIDENCE", "timing.json")):
        monkeypatch.setenv(key, name if key == "AT01_SOURCE" else str(tmp_path / name))
    import faulthandler
    import signal
    try:
        exec(compile(runner.BOOTSTRAP, "<BOOTSTRAP>", "exec"), {"__name__": "__bootstrap__"})
    finally:
        faulthandler.unregister(signal.SIGUSR2)
    timing = _read(tmp_path / "timing.json")
    assert all(timing["modules"][n] == {"wall": 1.0, "cpu": 1.0} for n in CORE)
    assert timing["coreWall"] == 4.0 and timing["allWall"] == 4.0
    assert timing["sourceCheckWall"] == 28.0


# ---------- B1: every attempt ends in exactly one terminal record ----------

def test_early_exit_is_an_exited_attempt_and_keeps_the_original_error(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load(RUNNER, "timing_exited")
    monkeypatch.setattr(runner.os, "nice", lambda _increment: None)
    tree = _fake_tree(tmp_path / "tree", "def main():\n    raise SystemExit(7)\n")
    scenario = _scenario(runner, tmp_path, tree)
    monkeypatch.setattr(runner, "ping", lambda _socket: (_ for _ in ()).throw(OSError("never ready")))
    with pytest.raises(AssertionError, match="daemon exited: daemon-1.stderr"):
        scenario.start()
    record = _read(scenario.root / "daemon-1-attempt.json")
    assert record["status"] == "exited" and record["exitCode"] == 7
    assert record["popenToReadySeconds"] is None and record["marginSeconds"] is None
    assert record["elapsedSeconds"] > 0 and record["import"]["coreWall"] is not None


def test_spawn_error_is_recorded_and_reraised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load(RUNNER, "timing_spawn")
    scenario = _scenario(runner, tmp_path, _fake_tree(tmp_path / "tree"))
    scenario.python = str(tmp_path / "no-such-python")
    with pytest.raises(OSError):
        scenario.start()
    record = _read(scenario.root / "daemon-1-attempt.json")
    assert record["status"] == "spawn-error" and "FileNotFoundError" in record["error"]
    assert [e["label"] for e in _read(scenario.root / "attempts.json")] == ["daemon-1"]


def test_identity_assertion_failure_is_an_error_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load(RUNNER, "timing_error")
    monkeypatch.setattr(runner.os, "nice", lambda _increment: None)
    scenario = _scenario(runner, tmp_path, _fake_tree(tmp_path / "tree"))
    monkeypatch.setattr(runner, "ping", lambda _socket: {"running": True, "pid": -1, "owner": "x", "nodeId": "y"})
    try:
        with pytest.raises(AssertionError):
            scenario.start()
    finally:
        _stop(scenario.process)
    record = _read(scenario.root / "daemon-1-attempt.json")
    assert record["status"] == "error" and "AssertionError" in record["error"]
    assert record["marginSeconds"] is None


def test_evidence_write_failure_never_changes_the_outcome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load(RUNNER, "timing_write_fail")
    monkeypatch.setattr(runner.os, "nice", lambda _increment: None)
    scenario = _scenario(runner, tmp_path, _fake_tree(tmp_path / "tree"))
    monkeypatch.setattr(runner, "ping", _ready_after(runner, scenario, scenario.root / "daemon-1-timing.json"))
    real = runner.write_json

    def failing(path: Path, value: Any) -> None:
        if path.name in ("attempts.json", "daemon-1-attempt.json"):
            raise OSError("disk full")
        real(path, value)

    monkeypatch.setattr(runner, "write_json", failing)
    try:
        state = scenario.start()
    finally:
        _stop(scenario.process)
    assert state["running"] is True


# ---------- B3: the end point is the parent's observation, before diagnostics ----------

class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class _FakeProcess:
    pid, returncode = 424242, None

    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    def poll(self) -> None:
        return None


def _virtual(runner: Any, monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(runner, "clock", clock.now)
    monkeypatch.setattr(runner, "sleep", clock.sleep)
    monkeypatch.setattr(runner.subprocess, "Popen", _FakeProcess)
    return clock


def test_ready_observed_after_the_deadline_keeps_a_negative_margin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load(RUNNER, "timing_cross")
    clock = _virtual(runner, monkeypatch)
    scenario = _scenario(runner, tmp_path, _fake_tree(tmp_path / "tree"))

    def ping(_socket: Path) -> dict[str, Any]:
        clock.t = 1046.0  # the ping itself started before the deadline and returns at 46 s
        return {"running": True, "pid": _FakeProcess.pid, "owner": "host-owner", "nodeId": "host-node"}

    monkeypatch.setattr(runner, "ping", ping)
    scenario.start()
    record = _read(scenario.root / "daemon-1-attempt.json")
    assert record["status"] == "ready"
    assert record["elapsedSeconds"] == 46.0 and record["marginSeconds"] == -1.0


def test_timeout_end_point_excludes_diagnostics_and_snapshots_import_timing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load(RUNNER, "timing_timeout")
    clock = _virtual(runner, monkeypatch)
    scenario = _scenario(runner, tmp_path, _fake_tree(tmp_path / "tree"))
    monkeypatch.setattr(runner, "ping", lambda _socket: (_ for _ in ()).throw(OSError("not ready")))
    timing = scenario.root / "daemon-1-timing.json"

    def diagnostics(*_: Any, **__: Any) -> str:
        clock.t += 15.0  # a 15 s sample
        timing.write_text(json.dumps({"modules": {n: {"wall": 1.0, "cpu": 1.0} for n in CORE},
                                      "coreWall": 4.0, "coreCpu": 4.0, "allWall": 4.0, "allCpu": 4.0,
                                      "sourceCheckWall": 0.1}))
        return "diagnosed"

    monkeypatch.setattr(runner.Scenario, "_capture_readiness_timeout", diagnostics)
    with pytest.raises(AssertionError, match="isolated daemon readiness timeout; diagnosed"):
        scenario.start()
    record = _read(scenario.root / "daemon-1-attempt.json")
    assert record["status"] == "timeout"
    assert 45.0 <= record["elapsedSeconds"] < 45.2, "the end point is before the 15 s of diagnostics"
    assert record["overrunSeconds"] == pytest.approx(record["elapsedSeconds"] - 45.0)
    assert record["diagnosticsSeconds"] == 15.0
    assert record["import"]["coreWall"] is None and "missing" in record["import"]["note"]
    assert record["importTimingSnapshot"] == "pre-diagnostics"
    assert record["importTimingChangedDuringDiagnostics"] is True
    started, finished = record["importTimingReadOffsets"]
    assert 0 <= started <= finished < 15.0, "the snapshot is read after t_obs and before diagnostics"


def test_timing_file_that_lands_between_observation_and_first_read_is_not_backdated(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """t_obs is fixed first; a file completed a moment later is reported with the read
    offset, never as "already there at the deadline"."""
    runner = _load(RUNNER, "timing_race")
    clock = _virtual(runner, monkeypatch)
    scenario = _scenario(runner, tmp_path, _fake_tree(tmp_path / "tree"))
    monkeypatch.setattr(runner, "ping", lambda _socket: (_ for _ in ()).throw(OSError("not ready")))
    monkeypatch.setattr(runner.Scenario, "_capture_readiness_timeout", lambda *a, **k: "diagnosed")
    imports = scenario.root / "daemon-1-imports.json"
    timing = scenario.root / "daemon-1-timing.json"
    real_read = runner.read_import_timing

    def late_file(path: Path, imports_path: Path | None = None) -> dict[str, Any]:
        if not timing.exists():
            clock.t += 0.02  # the child finishes 20 ms after t_obs
            imports.write_text(json.dumps({n: {"path": "p", "sha256": "s"} for n in CORE}))
            timing.write_text(json.dumps({"modules": {n: {"wall": 1.0, "cpu": 1.0} for n in CORE},
                                          "coreWall": 4.0, "coreCpu": 4.0, "allWall": 4.0, "allCpu": 4.0,
                                          "sourceCheckWall": 0.1}))
        return real_read(path, imports_path)

    monkeypatch.setattr(runner, "read_import_timing", late_file)
    with pytest.raises(AssertionError):
        scenario.start()
    record = _read(scenario.root / "daemon-1-attempt.json")
    assert record["import"]["coreWall"] == 4.0
    assert record["importTimingSnapshot"] == "pre-diagnostics"
    started, finished = record["importTimingReadOffsets"]
    assert started == 0.0 and finished >= 0.02 - 1e-9, "the read window after t_obs is recorded, not hidden"
    assert record["importTimingChangedDuringDiagnostics"] is False


def test_real_timeout_links_the_diagnostic_record_and_uses_a_15s_sample_budget(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load(RUNNER, "timing_real_timeout")
    monkeypatch.setattr(runner.os, "nice", lambda _increment: None)
    tree = _fake_tree(tmp_path / "tree", "import time\ntime.sleep(60)\n")
    scenario = _scenario(runner, tmp_path, tree, AT01_READINESS_TIMEOUT_SECONDS="0.5")
    samples: list[Any] = []
    real_run = runner.subprocess.run

    def spy(command: list[str], **kwargs: Any) -> Any:
        if command and command[0] == "/usr/bin/sample":
            samples.append((command, kwargs.get("timeout")))
            return subprocess.CompletedProcess(command, 0, "", "")
        return real_run(command, **kwargs)

    monkeypatch.setattr(runner.subprocess, "run", spy)
    try:
        with pytest.raises(AssertionError, match="isolated daemon readiness timeout"):
            scenario.start()
    finally:
        _stop(scenario.process)
    diagnostic = _read(scenario.root / "daemon-1-readiness-timeout.json")
    assert diagnostic["attempt"] == "daemon-1-attempt.json" and diagnostic["imports"] == "missing"
    assert _read(scenario.root / "daemon-1-attempt.json")["status"] == "timeout"
    if sys.platform == "darwin":
        assert samples and samples[0][1] == 15 and samples[0][0][2] == "3"


@pytest.mark.parametrize("content", [
    "", "{not json", "[]", '{"coreWall": "x", "coreCpu": 1, "allWall": 1, "allCpu": 1}',
    '{"coreWall": true, "coreCpu": 1, "allWall": 1, "allCpu": 1}',
    '{"coreWall": NaN, "coreCpu": 1, "allWall": 1, "allCpu": 1}',
    '{"coreWall": Infinity, "coreCpu": 1, "allWall": 1, "allCpu": 1}',
    '{"coreWall": -1, "coreCpu": 1, "allWall": 1, "allCpu": 1}',
    '{"coreWall": null, "coreCpu": 1, "allWall": 1, "allCpu": 1}',
])
def test_invalid_timing_is_null_with_a_reason_never_zero(tmp_path: Path, content: str) -> None:
    runner = _load(RUNNER, "timing_invalid")
    path = tmp_path / "daemon-1-timing.json"
    path.write_text(content)
    summary = runner.read_import_timing(path)
    assert all(summary[key] is None for key in ("coreWall", "coreCpu", "allWall", "allCpu")) and summary["note"]


def _timing(**overrides: Any) -> dict[str, Any]:
    body = {"modules": {n: {"wall": 1.0, "cpu": 0.5} for n in CORE},
            "coreWall": 4.0, "coreCpu": 2.0, "allWall": 4.0, "allCpu": 2.0, "sourceCheckWall": 0.1}
    body.update(overrides)
    return body


@pytest.mark.parametrize("body, imported", [
    (_timing(coreWall=5.0), CORE),                                             # self-reported sum is wrong
    (_timing(allCpu=9.0), CORE),
    (_timing(modules={n: {"wall": 1.0, "cpu": 0.5} for n in CORE[:3]}), CORE),  # a core module missing
    (_timing(modules={**{n: {"wall": 1.0, "cpu": 0.5} for n in CORE}, "x": {"wall": 1.0, "cpu": 1.0}}), CORE),
    (_timing(), (*CORE, "identity.impl.agents.runtime.grants")),                 # imports.json has a 5th module
    (_timing(modules={**{n: {"wall": 1.0, "cpu": 0.5} for n in CORE[:3]}, "identity.impl.agents.registry": {"wall": -1, "cpu": 0}}), CORE),
])
def test_timing_arithmetic_and_module_set_are_checked(tmp_path: Path, body: dict[str, Any], imported: tuple[str, ...]) -> None:
    runner = _load(RUNNER, "timing_arith")
    path, imports = tmp_path / "t.json", tmp_path / "i.json"
    path.write_text(json.dumps(body))
    imports.write_text(json.dumps({n: {"path": "p", "sha256": "s"} for n in imported}))
    summary = runner.read_import_timing(path, imports)
    assert summary["coreWall"] is None and summary["note"]


def test_valid_timing_with_the_at02_fifth_module(tmp_path: Path) -> None:
    runner = _load(RUNNER, "timing_valid5")
    body = _timing(modules={**{n: {"wall": 1.0, "cpu": 0.5} for n in CORE}, "identity.impl.agents.runtime.grants": {"wall": 2.0, "cpu": 1.0}},
                   allWall=6.0, allCpu=3.0)
    path, imports = tmp_path / "t.json", tmp_path / "i.json"
    path.write_text(json.dumps(body))
    imports.write_text(json.dumps({n: {"path": "p", "sha256": "s"} for n in (*CORE, "identity.impl.agents.runtime.grants")}))
    summary = runner.read_import_timing(path, imports)
    assert summary == {"coreWall": 4.0, "coreCpu": 2.0, "allWall": 6.0, "allCpu": 3.0, "modules": 5, "note": None}


# ---------- B5: Scenario.cli total call time against its 30 s budget ----------

def _cli_scenario(runner: Any, tmp_path: Path) -> Any:
    (tmp_path / "root").mkdir()
    scenario = runner.Scenario(tmp_path / "root", tmp_path / "production.sock")
    scenario.env["AT01_SOURCE"] = str(tmp_path / "tree")
    return scenario


def test_cli_success_and_nonzero_are_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load(RUNNER, "timing_cli_ok")
    scenario = _cli_scenario(runner, tmp_path)
    answers = iter([subprocess.CompletedProcess(["x"], 0, '{"ok": true}', ""),
                    subprocess.CompletedProcess(["x"], 3, '{"ok": false}', "boom")])
    monkeypatch.setattr(runner.subprocess, "run", lambda command, **_: next(answers))
    scenario.cli("agent", "list")
    scenario.cli("agent", "bad", ok=False)
    first, second = _read(scenario.root / "commands.json")
    assert first["seq"] == 1 and first["status"] == "ok" and first["exitCode"] == 0 and first["budgetSeconds"] == 30
    assert first["tree"] == str(tmp_path / "tree") and first["seconds"] >= 0 and first["startedUtc"]
    assert second["seq"] == 2 and second["status"] == "nonzero" and second["exitCode"] == 3


@pytest.mark.parametrize("output", [b"partial \xff bytes", None])
def test_cli_timeout_with_bytes_or_none_is_recorded_then_reraised(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: bytes | None) -> None:
    runner = _load(RUNNER, "timing_cli_timeout")
    scenario = _cli_scenario(runner, tmp_path)

    def hang(command: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=output, stderr=output)

    monkeypatch.setattr(runner.subprocess, "run", hang)
    with pytest.raises(subprocess.TimeoutExpired):
        scenario.cli("agent", "start", "pi")
    entry = _read(scenario.root / "commands.json")[0]
    assert entry["status"] == "timeout" and entry["exitCode"] is None and entry["overrunSeconds"] >= 0
    assert entry["stdout"] == (None if output is None else output.decode("utf-8", "replace"))


def test_cli_spawn_error_is_recorded_then_reraised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load(RUNNER, "timing_cli_spawn")
    scenario = _cli_scenario(runner, tmp_path)
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("python")))
    with pytest.raises(FileNotFoundError):
        scenario.cli("agent", "list")
    entry = _read(scenario.root / "commands.json")[0]
    assert entry["status"] == "spawn-error" and "FileNotFoundError" in entry["error"]


def test_cli_evidence_write_failure_does_not_mask_the_original_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load(RUNNER, "timing_cli_mask")
    scenario = _cli_scenario(runner, tmp_path)
    monkeypatch.setattr(runner.subprocess, "run", lambda command, **k: (_ for _ in ()).throw(
        subprocess.TimeoutExpired(command, 30)))
    monkeypatch.setattr(runner, "write_json", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(subprocess.TimeoutExpired):
        scenario.cli("agent", "list")
    monkeypatch.setattr(runner.subprocess, "run", lambda command, **_: subprocess.CompletedProcess(command, 0, '{"ok": true}', ""))
    assert scenario.cli("agent", "list") == {"ok": True}


# ---------- B2: suite join, identity and completeness ----------

def _attempt(label: str, tree: Path, status: str = "ready", **extra: Any) -> dict[str, Any]:
    record = {"label": label, "status": status, "tree": str(tree), "budgetSeconds": 45.0,
              "startedUtc": "2026-09-29T01:00:00+00:00", "observedUtc": "2026-09-29T01:00:03+00:00",
              "elapsedSeconds": 3.0, "popenToReadySeconds": 3.0 if status == "ready" else None,
              "marginSeconds": 42.0 if status == "ready" else None, "overrunSeconds": None, "exitCode": None,
              "error": None, "diagnosticsSeconds": None, "importTimingAppearedDuringDiagnostics": None,
              "import": {"coreWall": 1.0, "coreCpu": 1.0, "allWall": 1.0, "allCpu": 1.0, "modules": 4, "note": None}}
    record.update(extra)
    return record


def _put(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value))


def _ledger(directory: Path, *labels: str) -> None:
    _put(directory / "attempts.json", [{"label": label, "startedUtc": "2026-09-29T01:00:00+00:00"} for label in labels])


def _result(evidence: Path, scenario: str, *ledgers: str) -> None:
    """run-host-invite main lists every ledger its process created, relative to the scenario root."""
    _put(evidence / scenario / "result.json", {"result": "PASS", "ledgers": list(ledgers)})


def _evidence(tmp_path: Path) -> tuple[Path, Path, Path]:
    evidence, new, old = tmp_path / "evidence", tmp_path / "new", tmp_path / "old"
    new.mkdir()
    old.mkdir()
    return evidence, new, old


def _hosted_upgrade(evidence: Path, new: Path, old: Path) -> None:
    _result(evidence, "hosted-upgrade", "attempts.json", "worker-journey/attempts.json")
    _ledger(evidence / "hosted-upgrade", "daemon-1", "daemon-2", "daemon-3")
    _ledger(evidence / "hosted-upgrade/worker-journey", "daemon-1")
    _put(evidence / "hosted-upgrade/daemon-1-attempt.json", _attempt("daemon-1", old))
    _put(evidence / "hosted-upgrade/daemon-2-attempt.json", _attempt("daemon-2", new))
    _put(evidence / "hosted-upgrade/daemon-3-attempt.json", _attempt("daemon-3", old, "exited", exitCode=1))
    _put(evidence / "hosted-upgrade/worker-journey/daemon-1-attempt.json", _attempt("daemon-1", new))


def _known_trees(suite: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Temporary trees are not git checkouts; give them a valid HEAD sha."""
    monkeypatch.setattr(suite, "tree_identity", lambda trees: {
        name: {"path": str(p), "resolved": str(Path(p).resolve()), "sha": "b" * 40} for name, p in trees.items()})


def test_suite_joins_nested_same_labels_by_path_and_classes_trees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite = _load(SUITE, "readiness_join")
    _known_trees(suite, monkeypatch)
    evidence, new, old = _evidence(tmp_path)
    _hosted_upgrade(evidence, new, old)
    report = suite.collect_readiness(evidence, {"new": new, "old": old}, ["hosted-upgrade"], lambda _: None)
    assert report["complete"] is True and report["expected"] == report["observed"] == 4
    keys = {(r["key"], r["tree"], r["status"]) for r in report["rows"]}
    assert keys == {("hosted-upgrade/daemon-1", "old", "ready"), ("hosted-upgrade/daemon-2", "new", "ready"),
                    ("hosted-upgrade/daemon-3", "old", "exited"),
                    ("hosted-upgrade/worker-journey/daemon-1", "new", "ready")}
    row = next(r for r in report["rows"] if r["key"] == "hosted-upgrade/daemon-2")
    assert row["importCoreWall"] == 1.0 and row["importAllWall"] == 1.0 and row["marginSeconds"] == 42.0
    assert _read(evidence / "readiness.json") == report
    assert set(report["trees"]) == {"new", "old"}


def test_deleting_a_sub_scenario_ledger_and_all_its_records_is_incomplete(tmp_path: Path) -> None:
    suite = _load(SUITE, "readiness_deleted")
    evidence, new, old = _evidence(tmp_path)
    _hosted_upgrade(evidence, new, old)
    for path in (evidence / "hosted-upgrade/worker-journey").iterdir():
        path.unlink()
    report = suite.collect_readiness(evidence, {"new": new, "old": old}, ["hosted-upgrade"], lambda _: None)
    assert report["complete"] is False
    assert any("worker-journey/attempts.json" in item for item in report["missingLedgers"])


def test_nothing_at_all_is_never_complete(tmp_path: Path) -> None:
    suite = _load(SUITE, "readiness_empty")
    evidence, new, old = _evidence(tmp_path)
    evidence.mkdir()
    report = suite.collect_readiness(evidence, {"new": new, "old": old}, [], lambda _: None)
    assert report["expected"] == report["observed"] == 0 and report["complete"] is False


def test_a_scenario_without_result_ledgers_is_not_run_or_unknown(tmp_path: Path) -> None:
    suite = _load(SUITE, "readiness_notrun")
    evidence, new, old = _evidence(tmp_path)
    _hosted_upgrade(evidence, new, old)
    report = suite.collect_readiness(evidence, {"new": new, "old": old}, ["host-invite", "hosted-upgrade"],
                                     lambda _: None)
    assert report["complete"] is False
    assert report["scenarios"]["host-invite"].startswith("unknown") and report["scenarios"]["hosted-upgrade"] == "ok"


@pytest.mark.parametrize("damage, field", [
    ("lost", "missing"), ("extra", "unexpected"), ("dup", "duplicate"), ("bad-json", "bad"),
    ("bool", "bad"), ("nan", "bad"), ("negative", "bad"), ("status", "bad"),
    ("ready-without-margin", "bad"), ("exited-with-margin", "bad"), ("ledger-garbage", "missingLedgers"),
])
def test_suite_marks_timing_evidence_incomplete(tmp_path: Path, damage: str, field: str) -> None:
    suite = _load(SUITE, "readiness_damage")
    evidence, new, old = _evidence(tmp_path)
    _result(evidence, "host-invite", "attempts.json")
    labels = ["daemon-1", "daemon-2"] + (["daemon-1"] if damage == "dup" else [])
    _ledger(evidence / "host-invite", *labels)
    if damage == "ledger-garbage":
        _put(evidence / "host-invite/attempts.json", "{broken")
    _put(evidence / "host-invite/daemon-1-attempt.json", _attempt("daemon-1", new))
    second: Any = _attempt("daemon-2", new)
    if damage == "lost":
        second = None
    elif damage == "bad-json":
        second = "{broken"
    elif damage == "bool":
        second["elapsedSeconds"] = True
    elif damage == "nan":
        second["elapsedSeconds"] = math.nan
    elif damage == "negative":
        second["elapsedSeconds"] = -1.0
    elif damage == "status":
        second["status"] = "maybe"
    elif damage == "ready-without-margin":
        second["marginSeconds"] = None
    elif damage == "exited-with-margin":
        second.update(status="exited", exitCode=1, popenToReadySeconds=None)
    if second is not None:
        _put(evidence / "host-invite/daemon-2-attempt.json", second if isinstance(second, str) else json.dumps(second))
    if damage == "extra":
        _put(evidence / "host-invite/daemon-9-attempt.json", _attempt("daemon-9", new))
    report = suite.collect_readiness(evidence, {"new": new, "old": old}, ["host-invite"], lambda _: None)
    assert report["complete"] is False and report[field]


def test_suite_unknown_tree_carries_a_reason_and_fails_completeness(tmp_path: Path) -> None:
    suite = _load(SUITE, "readiness_unknown")
    evidence, new, old = _evidence(tmp_path)
    _result(evidence, "x", "attempts.json")
    _ledger(evidence / "x", "daemon-1")
    _put(evidence / "x/daemon-1-attempt.json", _attempt("daemon-1", tmp_path / "elsewhere"))
    report = suite.collect_readiness(evidence, {"new": new, "old": old}, ["x"], lambda _: None)
    row = report["rows"][0]
    assert row["tree"] == "unknown" and "elsewhere" in row["treeReason"] and report["complete"] is False


def test_scenario_roots_are_registered_for_the_result_ledger_list(tmp_path: Path) -> None:
    runner = _load(RUNNER, "timing_registry")
    root = tmp_path / "root"
    root.mkdir()
    runner.Scenario(root, tmp_path / "prod.sock")
    (root / "worker-journey").mkdir()
    runner.Scenario(root / "worker-journey", tmp_path / "prod.sock")
    assert runner.ledger_paths(root) == ["attempts.json", "worker-journey/attempts.json"]


def test_suite_readiness_is_in_the_manifest_and_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite = _load(SUITE, "readiness_main")
    monkeypatch.setattr(suite, "warm_trees", lambda trees, evidence, echo: {})
    monkeypatch.setattr(suite.subprocess, "run", lambda command, **_: subprocess.CompletedProcess(command, 1))
    monkeypatch.setattr(suite, "attempt_records", lambda *_: (_ for _ in ()).throw(OSError("disk gone")))
    old = tmp_path / "old"
    old.mkdir()
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(sys, "argv", ["run-hosted-suite.py", "--old-tree", str(old),
                                      "--production-socket", str(tmp_path / "prod.sock"),
                                      "--evidence", str(evidence)])
    suite.main()
    report = _read(evidence / "readiness.json")
    assert report["complete"] is False and "disk gone" in report["notes"][0]
    assert "readiness.json" in _read(evidence / "manifest.json")


# ---------- implementation review round 1 (R1-R4) ----------

def test_timing_write_failure_in_the_child_still_reaches_cli_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R1: the new timing evidence must never stop the daemon from starting."""
    runner = _load(RUNNER, "timing_child_write_fail")
    monkeypatch.setattr(runner.os, "nice", lambda _increment: None)
    marker = tmp_path / "main-reached"
    tree = _fake_tree(tmp_path / "tree", "import os, time\ndef main():\n"
                      f"    open({str(marker)!r}, 'w').write('ok')\n    time.sleep(60)\n")
    scenario = _scenario(runner, tmp_path, tree)
    (scenario.root / "daemon-1-timing.json").mkdir()  # unwritable as a file
    monkeypatch.setattr(runner, "ping", _ready_after(runner, scenario, marker))
    try:
        scenario.start()
    finally:
        _stop(scenario.process)
    assert marker.exists() and _read(scenario.root / "daemon-1-imports.json")
    record = _read(scenario.root / "daemon-1-attempt.json")
    assert record["status"] == "ready" and record["import"]["coreWall"] is None and record["import"]["note"]


def test_missing_timing_note_does_not_claim_a_cause(tmp_path: Path) -> None:
    runner = _load(RUNNER, "timing_neutral_note")
    note = runner.read_import_timing(tmp_path / "absent.json")["note"]
    assert "missing" in note and "had not finished" not in note


def test_source_check_must_be_a_finite_duration(tmp_path: Path) -> None:
    runner = _load(RUNNER, "timing_sourcecheck_nan")
    path = tmp_path / "t.json"
    path.write_text(json.dumps(_timing(sourceCheckWall=float("nan"))))
    assert runner.read_import_timing(path)["coreWall"] is None


def _pass_scenarios(evidence_tmp: Path) -> Any:
    """A subprocess.run stand-in that runs each scenario as a PASS with a real /tmp root."""
    import tempfile

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, "a" * 40 + "\n", "")
        root = Path(tempfile.mkdtemp(prefix="at01-e2e-", dir="/tmp"))
        (root / "attempts.json").write_text(json.dumps([{"label": "daemon-1", "startedUtc": "2026-09-29T01:00:00+00:00"}]))
        (root / "daemon-1-attempt.json").write_text(json.dumps(_attempt("daemon-1", Path(__file__).resolve().parents[4])))
        (root / "result.json").write_text(json.dumps({"result": "PASS", "ledgers": ["attempts.json"]}))
        kwargs["stdout"].write(json.dumps({"root": str(root), "result": "PASS"}))
        return subprocess.CompletedProcess(command, 0)
    return run


def _main(suite: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run: Any) -> tuple[int, Path]:
    monkeypatch.setattr(suite, "warm_trees", lambda trees, evidence, echo: {})
    monkeypatch.setattr(suite.subprocess, "run", run)
    # Synthetic scenarios all PASS; the KNOWN_FAILURES rule (UNEXPECTED-PASS is
    # red) has its own tests in tests/integration/hosted/gate/test_hosted_suite_known_failures.py.
    monkeypatch.setattr(suite, "KNOWN_FAILURES", {})
    old = tmp_path / "old"
    old.mkdir(exist_ok=True)
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(sys, "argv", ["run-hosted-suite.py", "--old-tree", str(old),
                                      "--production-socket", str(tmp_path / "prod.sock"), "--evidence", str(evidence)])
    return suite.main(), evidence


@pytest.mark.parametrize("passing", [True, False])
def test_summary_write_failure_keeps_the_exit_code_and_publishes_evidence(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, passing: bool) -> None:
    """R2: readiness.json and its echo are evidence; failing them changes nothing else."""
    suite = _load(SUITE, "readiness_write_fail")
    real_write = Path.write_text

    def write(self: Path, *args: Any, **kwargs: Any) -> int:
        if self.name == "readiness.json":
            raise OSError("synthetic summary disk full")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write)
    run = _pass_scenarios(tmp_path) if passing else (
        lambda command, **_: subprocess.CompletedProcess(command, 0, "a" * 40 + "\n", "") if command[0] == "git"
        else subprocess.CompletedProcess(command, 3))
    code, evidence = _main(suite, tmp_path, monkeypatch, run)
    assert code == (0 if passing else 3)
    manifest = _read(evidence / "manifest.json")
    assert "suite.json" in manifest and "readiness.json" not in manifest


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(popenToReadySeconds=999.0, marginSeconds=999.0),   # not the elapsed/budget arithmetic
    lambda r: r.update(marginSeconds=0.5),
    lambda r: r.update(import_={}),                                        # every import field absent
    lambda r: r["import"].update(coreWall=None),                           # null without a reason
    lambda r: r.update(startedUtc="not-a-date"),
    lambda r: r.update(observedUtc=""),
    lambda r: r.update(observedUtc="2026-09-29T00:59:00+00:00"),           # observed before started
    lambda r: r.update(label="daemon-999"),                                # not the file's label
])
def test_invalid_ready_records_are_bad(tmp_path: Path, mutate: Any) -> None:
    """R3"""
    suite = _load(SUITE, "readiness_strict")
    evidence, new, old = _evidence(tmp_path)
    _result(evidence, "host-invite", "attempts.json")
    _ledger(evidence / "host-invite", "daemon-1", "daemon-2")
    _put(evidence / "host-invite/daemon-1-attempt.json", _attempt("daemon-1", new))
    record = _attempt("daemon-2", new)
    mutate(record)
    if "import_" in record:
        record["import"] = record.pop("import_")
    _put(evidence / "host-invite/daemon-2-attempt.json", record)
    report = suite.collect_readiness(evidence, {"new": new, "old": old}, ["host-invite"], lambda _: None, planned=["host-invite"])
    assert report["complete"] is False and report["bad"]


@pytest.mark.parametrize("record", [
    _attempt("daemon-2", Path("/x"), "exited", exitCode=True, elapsedSeconds=3.0),
    _attempt("daemon-2", Path("/x"), "timeout", elapsedSeconds=46.0, overrunSeconds=5.0),
])
def test_invalid_non_ready_records_are_bad(tmp_path: Path, record: dict[str, Any]) -> None:
    suite = _load(SUITE, "readiness_strict_nonready")
    evidence, new, old = _evidence(tmp_path)
    record = dict(record, tree=str(new))
    _result(evidence, "host-invite", "attempts.json")
    _ledger(evidence / "host-invite", "daemon-2")
    _put(evidence / "host-invite/daemon-2-attempt.json", record)
    report = suite.collect_readiness(evidence, {"new": new, "old": old}, ["host-invite"], lambda _: None, planned=["host-invite"])
    assert report["complete"] is False and report["bad"]


def test_valid_negative_margin_and_non_ready_nulls_are_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite = _load(SUITE, "readiness_strict_ok")
    _known_trees(suite, monkeypatch)
    evidence, new, old = _evidence(tmp_path)
    _result(evidence, "host-invite", "attempts.json")
    _ledger(evidence / "host-invite", "daemon-1", "daemon-2", "daemon-3")
    _put(evidence / "host-invite/daemon-1-attempt.json",
         _attempt("daemon-1", new, elapsedSeconds=46.0, popenToReadySeconds=46.0, marginSeconds=-1.0))
    _put(evidence / "host-invite/daemon-2-attempt.json", _attempt(
        "daemon-2", new, "timeout", elapsedSeconds=45.1, overrunSeconds=0.1, diagnosticsSeconds=15.0,
        importTimingSnapshot="pre-diagnostics", importTimingReadOffsets=[0.0, 0.01],
        importTimingChangedDuringDiagnostics=False,
        **{"import": {"coreWall": None, "coreCpu": None, "allWall": None, "allCpu": None, "modules": None,
                      "note": "timing missing"}}))
    _put(evidence / "host-invite/daemon-3-attempt.json", _attempt("daemon-3", new, "exited", exitCode=1))
    report = suite.collect_readiness(evidence, {"new": new, "old": old}, ["host-invite"], lambda _: None, planned=["host-invite"])
    assert report["bad"] == [] and report["complete"] is True


def test_main_marks_unrun_planned_scenarios_and_is_not_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R4: the first scenario fails, the other two never run."""
    suite = _load(SUITE, "readiness_notrun_main")
    passing = _pass_scenarios(tmp_path)

    def first_fails(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = passing(command, **kwargs)
        return result if command[0] == "git" else subprocess.CompletedProcess(command, 1)

    code, evidence = _main(suite, tmp_path, monkeypatch, first_fails)
    report = _read(evidence / "readiness.json")
    assert code == 1 and report["complete"] is False
    assert report["scenarios"]["capability-ledger"] == "not-run" and report["scenarios"]["hosted-upgrade"] == "not-run"


def test_duplicate_ledger_reference_across_results_is_reported(tmp_path: Path) -> None:
    suite = _load(SUITE, "readiness_dup_ref")
    evidence, new, old = _evidence(tmp_path)
    _result(evidence, "host-invite", "attempts.json", "attempts.json")
    _ledger(evidence / "host-invite", "daemon-1")
    _put(evidence / "host-invite/daemon-1-attempt.json", _attempt("daemon-1", new))
    report = suite.collect_readiness(evidence, {"new": new, "old": old}, ["host-invite"], lambda _: None, planned=["host-invite"])
    assert report["complete"] is False and report["duplicate"]


def test_emptied_nested_ledger_is_reported(tmp_path: Path) -> None:
    suite = _load(SUITE, "readiness_empty_nested")
    evidence, new, old = _evidence(tmp_path)
    _hosted_upgrade(evidence, new, old)
    _put(evidence / "hosted-upgrade/worker-journey/attempts.json", [])
    (evidence / "hosted-upgrade/worker-journey/daemon-1-attempt.json").unlink()
    report = suite.collect_readiness(evidence, {"new": new, "old": old}, ["hosted-upgrade"], lambda _: None,
                                     planned=["hosted-upgrade"])
    assert report["complete"] is False and report["emptyLedgers"]


@pytest.mark.parametrize("sha", [None, "not-a-sha"])
def test_missing_or_invalid_build_sha_is_not_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sha: str | None) -> None:
    suite = _load(SUITE, "readiness_sha")
    evidence, new, old = _evidence(tmp_path)
    _hosted_upgrade(evidence, new, old)
    monkeypatch.setattr(suite, "tree_identity", lambda trees: {name: {"path": str(p), "resolved": str(p), "sha": sha}
                                                               for name, p in trees.items()})
    report = suite.collect_readiness(evidence, {"new": new, "old": old}, ["hosted-upgrade"], lambda _: None,
                                     planned=["hosted-upgrade"])
    assert report["complete"] is False


# ---------- implementation review round 2 (R1a, R3a) ----------

def test_timing_write_and_its_diagnostic_both_failing_still_reach_cli_main(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R1a: the report about a failed evidence write is itself best effort."""
    runner = _load(RUNNER, "timing_double_fail")
    monkeypatch.setattr(runner.os, "nice", lambda _increment: None)
    marker = tmp_path / "main-reached"
    tree = _fake_tree(tmp_path / "tree", "import time\ndef main():\n"
                      f"    open({str(marker)!r}, 'w').write('ok')\n    time.sleep(60)\n",
                      extra={"__init__.py": "import sys\nclass _Broken:\n"
                             "    def write(self, *_):\n        raise OSError('stderr gone')\n"
                             "    def flush(self):\n        pass\nsys.stderr = _Broken()\n"})
    scenario = _scenario(runner, tmp_path, tree)
    (scenario.root / "daemon-1-timing.json").mkdir()
    monkeypatch.setattr(runner, "ping", _ready_after(runner, scenario, marker))
    try:
        scenario.start()
    finally:
        _stop(scenario.process)
    assert marker.exists()


def test_oversized_json_integer_is_a_missing_measurement_not_an_exception(tmp_path: Path) -> None:
    """R3a: 10**400 is valid JSON but not a representable duration."""
    runner = _load(RUNNER, "timing_bigint")
    body = _timing()
    body["modules"]["shell.impl.cli.runtime.entry"]["wall"] = 10 ** 400
    path = tmp_path / "t.json"
    path.write_text(json.dumps(body))
    summary = runner.read_import_timing(path)
    assert summary["coreWall"] is None and summary["note"]
    assert runner.is_duration(10 ** 400) is False


def test_oversized_integer_at_timeout_keeps_the_timeout_diagnostics_and_record(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load(RUNNER, "timing_bigint_timeout")
    _virtual(runner, monkeypatch)
    scenario = _scenario(runner, tmp_path, _fake_tree(tmp_path / "tree"))
    monkeypatch.setattr(runner, "ping", lambda _socket: (_ for _ in ()).throw(OSError("not ready")))
    body = _timing()
    body["modules"]["shell.impl.cli.runtime.entry"]["wall"] = 10 ** 400
    (scenario.root / "daemon-1-timing.json").write_text(json.dumps(body))
    (scenario.root / "daemon-1-imports.json").write_text(json.dumps({n: {"path": "p", "sha256": "s"} for n in CORE}))
    called: list[bool] = []
    monkeypatch.setattr(runner.Scenario, "_capture_readiness_timeout", lambda *a, **k: called.append(True) or "diagnosed")
    with pytest.raises(AssertionError, match="isolated daemon readiness timeout; diagnosed"):
        scenario.start()
    assert called == [True]
    record = _read(scenario.root / "daemon-1-attempt.json")
    assert record["status"] == "timeout" and record["import"]["coreWall"] is None and record["import"]["note"]


@pytest.mark.parametrize("failing_call", [1, 2, 3])
def test_timing_reader_failure_never_replaces_the_timeout(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_call: int) -> None:
    """The first read, the post-diagnostics read and the final record read are all guarded."""
    runner = _load(RUNNER, "timing_reader_raises")
    _virtual(runner, monkeypatch)
    scenario = _scenario(runner, tmp_path, _fake_tree(tmp_path / "tree"))
    monkeypatch.setattr(runner, "ping", lambda _socket: (_ for _ in ()).throw(OSError("not ready")))
    called: list[bool] = []
    monkeypatch.setattr(runner.Scenario, "_capture_readiness_timeout", lambda *a, **k: called.append(True) or "diagnosed")
    calls = {"n": 0}
    real = runner.read_import_timing

    def reader(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == failing_call:
            raise OverflowError("synthetic")
        return real(*args, **kwargs)

    monkeypatch.setattr(runner, "read_import_timing", reader)
    with pytest.raises(AssertionError, match="isolated daemon readiness timeout; diagnosed"):
        scenario.start()
    assert called == [True]
    assert _read(scenario.root / "daemon-1-attempt.json")["status"] == "timeout"


def test_suite_rejects_unrepresentable_numbers_without_raising(tmp_path: Path) -> None:
    suite = _load(SUITE, "readiness_bigint")
    evidence, new, old = _evidence(tmp_path)
    _result(evidence, "host-invite", "attempts.json")
    _ledger(evidence / "host-invite", "daemon-1")
    _put(evidence / "host-invite/daemon-1-attempt.json",
         json.dumps(_attempt("daemon-1", new)).replace('"elapsedSeconds": 3.0', '"elapsedSeconds": ' + str(10 ** 400)))
    report = suite.collect_readiness(evidence, {"new": new, "old": old}, ["host-invite"], lambda _: None,
                                     planned=["host-invite"])
    assert report["complete"] is False and report["bad"] and not report["notes"]
