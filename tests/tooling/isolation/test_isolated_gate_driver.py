"""Guard tests: the isolated-macos gate driver runs every scenario regardless.

spec-e2e001 追加 2 (hq-adjutant ruling): the scenario loop lives in the runner
layer, not in workflow semantics.  These tests lock in the three properties
that ruling requires, using stub scenario scripts:

  1. a FAILing first scenario never stops the later ones — each still runs
     and still writes its own diag log;
  2. the summary row lists every scenario PASS/FAIL/SKIP and the driver
     exits 1 on ANY FAIL (later greens cannot dilute the red);
  3. a scenario whose declared dependency did not pass is SKIPped with
     reason 前置失败 and NEVER executed (no fake greens).

The stubs are real subprocesses, exactly how the driver consumes the real
runners, so the streaming/log plumbing is exercised too.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
DRIVER_PATH = ROOT / "contract/e2e" / "run-isolated-gate.py"


def load_driver():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("e2e001_isolated_gate_driver", DRIVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def stub(tmp_path: Path, name: str, code: str) -> str:
    path = tmp_path / name
    path.write_text(code, encoding="utf-8")
    return name


@pytest.fixture()
def gate(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    module = load_driver()
    scripts = tmp_path / "scenarios"
    scripts.mkdir()
    monkeypatch.setattr(module, "SCENARIO_DIR", scripts)
    monkeypatch.setattr(module, "HYPRIAL_E2E_ROOT", None, raising=False)
    monkeypatch.setenv("HYPRIAL_E2E_ROOT", str(tmp_path / "runroot"))
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    return module, tmp_path


def diag(gate_module) -> Path:  # type: ignore[no-untyped-def]
    return gate_module.diag_dir()


def test_env_differences_match_the_old_workflow_steps(gate, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """批复 §3a: the per-scenario env the old workflow steps set must survive
    the move into one driver process.  E2E-005/E2E-010 never saw HYPRIAL_BIN;
    every other scenario did."""

    module, _tmp = gate
    monkeypatch.setenv("HYPRIAL_BIN", "/workspace/.venv/bin/hyprial")
    without = {scenario for scenario, _ in module.SCENARIOS if scenario in module.NO_HYPRIAL_BIN}
    assert without == {"E2E-005", "E2E-010"}
    for scenario, _script in module.SCENARIOS:
        environment = module.scenario_environment(scenario)
        if scenario in without:
            assert "HYPRIAL_BIN" not in environment, scenario
        else:
            assert environment.get("HYPRIAL_BIN") == "/workspace/.venv/bin/hyprial", scenario


def test_first_failure_does_not_stop_later_scenarios(gate, capsys) -> None:  # type: ignore[no-untyped-def]
    module, tmp_path = gate
    scripts = tmp_path / "scenarios"
    module.SCENARIOS = (
        ("E2E-001", stub(scripts, "a.py", "print('RESULT FAIL E2E-001 boom')\nraise SystemExit(1)\n")),
        ("E2E-002", stub(scripts, "b.py", "print('RESULT PASS E2E-002')\n")),
        ("E2E-003", stub(scripts, "c.py", "print('RESULT PASS E2E-003')\n")),
    )
    module.DEPENDS_ON = {}
    rc = module.main([])
    out = capsys.readouterr().out
    assert rc == 1
    # Every later scenario ran and logged...
    assert (diag(module) / "E2E-002.log").read_text(encoding="utf-8") == "RESULT PASS E2E-002\n"
    assert (diag(module) / "E2E-003.log").read_text(encoding="utf-8") == "RESULT PASS E2E-003\n"
    # ...the summary is complete, and the red is not diluted:
    assert "E2E-001=FAIL" in out and "E2E-002=PASS" in out and "E2E-003=PASS" in out
    assert "RESULT FAIL GATE isolated-macos" in out
    # steps.tsv keeps one row per scenario, same shape as before.
    rows = (diag(module) / "steps.tsv").read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 3 and rows[0].split("\t")[1] == "E2E-001"


def test_all_green_passes(gate, capsys) -> None:  # type: ignore[no-untyped-def]
    module, tmp_path = gate
    scripts = tmp_path / "scenarios"
    module.SCENARIOS = (
        ("E2E-001", stub(scripts, "a.py", "print('RESULT PASS E2E-001')\n")),
        ("E2E-002", stub(scripts, "b.py", "print('RESULT PASS E2E-002')\n")),
    )
    module.DEPENDS_ON = {}
    assert module.main([]) == 0
    assert "RESULT PASS GATE isolated-macos" in capsys.readouterr().out


def test_dependent_scenario_skipped_not_fake_green(gate, capsys) -> None:  # type: ignore[no-untyped-def]
    module, tmp_path = gate
    scripts = tmp_path / "scenarios"
    module.SCENARIOS = (
        ("E2E-001", stub(scripts, "a.py", "print('RESULT FAIL E2E-001 boom')\nraise SystemExit(1)\n")),
        ("E2E-002", stub(scripts, "b.py", "print('should never run')\n")),
    )
    module.DEPENDS_ON = {"E2E-002": ("E2E-001",)}
    rc = module.main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert "RESULT SKIP E2E-002 前置失败:E2E-001" in out
    assert "E2E-002=SKIP" in out
    # Never started: no log file, no execution trace.
    assert not (diag(module) / "E2E-002.log").exists()
    assert "should never run" not in out


def test_dependent_runs_when_dependency_passed(gate, capsys) -> None:  # type: ignore[no-untyped-def]
    module, tmp_path = gate
    scripts = tmp_path / "scenarios"
    module.SCENARIOS = (
        ("E2E-001", stub(scripts, "a.py", "print('RESULT PASS E2E-001')\n")),
        ("E2E-002", stub(scripts, "b.py", "print('RESULT PASS E2E-002')\n")),
    )
    module.DEPENDS_ON = {"E2E-002": ("E2E-001",)}
    assert module.main([]) == 0
    assert (diag(module) / "E2E-002.log").exists()


def test_scenario_selection_and_unknown_ids(gate, capsys) -> None:  # type: ignore[no-untyped-def]
    module, tmp_path = gate
    scripts = tmp_path / "scenarios"
    module.SCENARIOS = (
        ("E2E-001", stub(scripts, "a.py", "print('RESULT PASS E2E-001')\n")),
        ("E2E-002", stub(scripts, "b.py", "print('RESULT PASS E2E-002')\n")),
    )
    module.DEPENDS_ON = {}
    assert module.main(["E2E-002"]) == 0
    out = capsys.readouterr().out
    assert "E2E-002=PASS" in out and "E2E-001=" not in out
    assert not (diag(module) / "E2E-001.log").exists()
    with pytest.raises(SystemExit):
        module.main(["E2E-999"])
