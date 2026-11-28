"""Closed E2E release inventory and the runner contracts behind it.

The objects are repository entities that outlive the workflows that once
scheduled them: ``contract/e2e/validate.py`` (the release inventory),
``contract/e2e/run-pac-workflow-unified.py`` (the E2E-015 home guard) and
``contract/e2e/run-isolated-gate.py`` (the pre-compilation pass).  The
mutation checks are deliberate: they prove a guard rejects the regression it
exists to catch instead of merely asserting the current text.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCENARIOS = ROOT / "contract" / "e2e"
VALIDATE = SCENARIOS / "validate.py"
RUN_015 = SCENARIOS / "run-pac-workflow-unified.py"
GATE_DRIVER = SCENARIOS / "run-isolated-gate.py"

EXPECTED_IDS = (
    {f"E2E-{number:03d}" for number in range(1, 19) if number != 14}
) | {
    "E2E-020",
    "E2E-021",
    "E2E-024",
    "E2E-025",
    "E2E-028",
    "E2E-029",
    "E2E-030",
    "E2E-031",
    "E2E-032",
}


def _load_validate(name: str):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, VALIDATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_gate_driver():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("e2e_gate_driver_wiring", GATE_DRIVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _assert_e2e015_home_guard(source: str) -> None:
    refusal = source.index('refused.get("code") == "HYPRIAL_HOME_NOT_INITIALIZED"')
    untouched = source.index('assert not (root / "home").exists()')
    initialise = source.index('result = daemon.command("init"')
    successful_plan = source.index('plan = daemon.command("workflow", "plan"')
    assert refusal < untouched < initialise < successful_plan


def test_inventory_is_exactly_the_registered_release_set() -> None:
    module = _load_validate("e2e_inventory_exact")
    assert module.REQUIRED_IDS == EXPECTED_IDS
    assert module.main() == 0


def test_inventory_omission_mutation_is_rejected(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    module = _load_validate("e2e_inventory_omission_mutation")
    document = json.loads((SCENARIOS / "scenarios.json").read_text(encoding="utf-8"))
    document["scenarios"] = [
        row for row in document["scenarios"] if row["id"] != "E2E-017"
    ]
    (tmp_path / "scenarios.json").write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    with pytest.raises(AssertionError, match="missing.*E2E-017"):
        module.main()


def test_e2e015_runner_refuses_then_prepares_disposable_home() -> None:
    """The actual runner proves the guard before it creates its own home.

    This is deliberately an ordering contract: weakening the product guard
    or moving the mkdir before the refusal probe makes the real E2E go red.
    """

    _assert_e2e015_home_guard(RUN_015.read_text(encoding="utf-8"))


def test_e2e015_home_guard_mutation_is_rejected() -> None:
    source = RUN_015.read_text(encoding="utf-8")
    mutated = source.replace("HYPRIAL_HOME_NOT_INITIALIZED", "WRONG_CODE", 1)
    with pytest.raises(ValueError):
        _assert_e2e015_home_guard(mutated)


def test_gate_precompiles_the_tree_and_the_hyprial_venv_before_any_scenario(
    tmp_path, monkeypatch
) -> None:
    """E2E-002 (2026-09-27, task 35833) was the first daemon start from a CI
    venv built without bytecode, so it compiled everything inside `hyprial
    init`'s 15 s readiness wait.  The gate compiles once up front instead."""

    gate = _load_gate_driver()
    venv = tmp_path / "venv"
    site = venv / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (site / "sample_dep.py").write_text("VALUE = 1\n")
    (venv / "bin").mkdir()
    monkeypatch.setenv("HYPRIAL_BIN", str(venv / "bin" / "hyprial"))

    targets = gate.precompile_bytecode_targets()
    assert site.resolve() in [target.resolve() for target in targets]
    assert gate.ROOT / "src" in targets

    monkeypatch.setattr(gate, "precompile_bytecode_targets", lambda: [site])
    lines: list[str] = []
    gate.precompile_bytecode(lines.append)
    assert any((site / "__pycache__").glob("sample_dep.*.pyc"))
    assert lines and lines[0].startswith("RESULT NOTE gate precompile ok")

    def broken() -> list:
        raise OSError("disk gone")

    monkeypatch.setattr(gate, "precompile_bytecode_targets", broken)
    lines.clear()
    gate.precompile_bytecode(lines.append)  # must not raise
    assert lines == ["RESULT NOTE gate precompile skipped: OSError: disk gone\n"]
