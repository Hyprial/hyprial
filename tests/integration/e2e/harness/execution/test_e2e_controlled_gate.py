"""Controlled-gate red lines, pinned as tests (feat/e2e-controlled-gate).

The CI controlled gate injects the approved test App's credential through
environment variables instead of reading production ~/.hyprial files.  These
tests pin the refusal semantics: a foreign injected App id (a production
App above all), a half-set injection, or an env-mode run without an explicit
chat pin must fail loudly before any file or network access — never fall
back to another credential source and never run against a production App.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[5]
SCENARIOS = ROOT / "contract" / "e2e"
RUN_004 = SCENARIOS / "run-feishu-roundtrip.py"
RUN_013 = SCENARIOS / "run-all-harness-lifecycle.py"
VERDICT = SCENARIOS / "gate-verdict.py"

TEST_APP_ID = "cli_aaf9ea19d4b8dbe4"
# hq-adjutant-feishu: one of the production Apps that must never be tested.
PRODUCTION_APP_ID = "cli_aafe80206e22dbe8"


def load_module(path: Path, name: str):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
    return module


@pytest.fixture()
def run004():  # type: ignore[no-untyped-def]
    return load_module(RUN_004, "e2e004_runner_controlled_gate")


@pytest.fixture()
def run013():  # type: ignore[no-untyped-def]
    return load_module(RUN_013, "e2e013_runner_controlled_gate")


# -- E2E-004 injected credential guard ----------------------------------------


def test_004_unset_injection_keeps_file_mode(run004, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("HYPRIAL_E2E_LARK_APP_ID", raising=False)
    monkeypatch.delenv("HYPRIAL_E2E_LARK_APP_SECRET", raising=False)
    assert run004.injected_app_credentials() is None


def test_004_half_set_injection_is_blocked(run004, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("HYPRIAL_E2E_LARK_APP_ID", TEST_APP_ID)
    monkeypatch.delenv("HYPRIAL_E2E_LARK_APP_SECRET", raising=False)
    with pytest.raises(run004.ContractBlocked, match="must be set together"):
        run004.injected_app_credentials()


def test_004_foreign_injected_app_id_is_blocked(run004, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Injecting a production App id must refuse loudly, never run."""

    monkeypatch.setenv("HYPRIAL_E2E_LARK_APP_ID", PRODUCTION_APP_ID)
    monkeypatch.setenv("HYPRIAL_E2E_LARK_APP_SECRET", "irrelevant")
    with pytest.raises(run004.ContractBlocked, match="approved E2E test App"):
        run004.injected_app_credentials()


def test_004_env_mode_requires_chat_pin_and_never_reads_production_files(  # type: ignore[no-untyped-def]
    run004, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("HYPRIAL_E2E_LARK_APP_ID", TEST_APP_ID)
    monkeypatch.setenv("HYPRIAL_E2E_LARK_APP_SECRET", "injected-secret")
    monkeypatch.delenv("HYPRIAL_E2E_LARK_CHAT_ID", raising=False)
    # If env mode touched the production config/secret files, these missing
    # paths would surface a different error than the intended refusal.
    monkeypatch.setattr(run004, "CHANNELS_PATH", tmp_path / "absent-channels.json")
    monkeypatch.setattr(run004, "SECRET_PATH", tmp_path / "absent-secret.json")
    with pytest.raises(run004.ContractBlocked, match="HYPRIAL_E2E_LARK_CHAT_ID"):
        run004.load_approved_target(need_identity=False)


def test_004_env_mode_resolves_without_production_files(  # type: ignore[no-untyped-def]
    run004, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("HYPRIAL_E2E_LARK_APP_ID", TEST_APP_ID)
    monkeypatch.setenv("HYPRIAL_E2E_LARK_APP_SECRET", "injected-secret")
    monkeypatch.setenv("HYPRIAL_E2E_LARK_CHAT_ID", "oc_test_chat")
    monkeypatch.setattr(run004, "CHANNELS_PATH", tmp_path / "absent-channels.json")
    monkeypatch.setattr(run004, "SECRET_PATH", tmp_path / "absent-secret.json")
    target = run004.load_approved_target(need_identity=False)
    assert target.app_id == TEST_APP_ID
    assert target.app_secret == "injected-secret"
    assert target.chat_id == "oc_test_chat"


# -- E2E-013 injected credential guard ----------------------------------------


def test_013_env_secret_wins_and_never_reads_production_file(  # type: ignore[no-untyped-def]
    run013, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("HYPRIAL_E2E_LARK_APP_ID", TEST_APP_ID)
    monkeypatch.setenv("HYPRIAL_E2E_LARK_APP_SECRET", "injected-secret")
    monkeypatch.setattr(run013, "LARK_SECRET_PATH", tmp_path / "absent.json")
    assert run013.load_test_app_secret() == "injected-secret"


def test_013_foreign_injected_app_id_raises(run013, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("HYPRIAL_E2E_LARK_APP_ID", PRODUCTION_APP_ID)
    monkeypatch.setenv("HYPRIAL_E2E_LARK_APP_SECRET", "irrelevant")
    with pytest.raises(ValueError, match="approved E2E test App"):
        run013.load_test_app_secret()


def test_013_file_mode_reads_approved_local_secret(  # type: ignore[no-untyped-def]
    run013, monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("HYPRIAL_E2E_LARK_APP_ID", raising=False)
    monkeypatch.delenv("HYPRIAL_E2E_LARK_APP_SECRET", raising=False)
    secret_file = tmp_path / "secret.json"
    secret_file.write_text(json.dumps({"appSecret": "file-secret"}))
    monkeypatch.setattr(run013, "LARK_SECRET_PATH", secret_file)
    assert run013.load_test_app_secret() == "file-secret"


def test_013_channel_config_writes_injected_secret_at_0600(  # type: ignore[no-untyped-def]
    run013, tmp_path
) -> None:
    home = tmp_path / "home"
    run013.write_channel_config(
        home,
        include_real_lark=True,
        lark_chat_id="oc_test_chat",
        lark_secret="injected-secret",
    )
    credential = home / "secrets" / f"lark-{run013.LARK_APP_NAME}.json"
    assert json.loads(credential.read_text()) == {"appSecret": "injected-secret"}
    assert credential.stat().st_mode & 0o777 == 0o600
    channels = json.loads((home / "channels.json").read_text())
    gateway = next(
        item for item in channels["gateways"] if item["name"] == run013.LARK_APP_NAME
    )
    assert gateway["appId"] == TEST_APP_ID
    assert gateway["routes"][0]["nativeId"] == "oc_test_chat"


def test_013_channel_config_requires_secret_for_real_lark(run013, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(AssertionError, match="test-App secret"):
        run013.write_channel_config(
            tmp_path / "home",
            include_real_lark=True,
            lark_chat_id="oc_test_chat",
            lark_secret=None,
        )


# -- gate verdict aggregation --------------------------------------------------


def test_verdict_all_pass(verdict_module, tmp_path) -> None:  # type: ignore[no-untyped-def]
    for scenario in verdict_module.REQUIRED_SCENARIOS:
        (tmp_path / f"{scenario}.code").write_text("0\n")
    lines, all_passed = verdict_module.render_matrix(tmp_path)
    assert all_passed is True
    assert all("PASS" in line for line in lines)


def test_verdict_missing_record_is_not_pass(verdict_module, tmp_path) -> None:  # type: ignore[no-untyped-def]
    lines, all_passed = verdict_module.render_matrix(tmp_path)
    assert all_passed is False
    assert any("absence is not PASS" in line for line in lines)


def test_verdict_blocked_and_fail_are_distinguished(verdict_module, tmp_path) -> None:  # type: ignore[no-untyped-def]
    codes = {
        "E2E-004": "4",
        "E2E-006": "1",
        "E2E-012": "3",
    }
    for scenario in verdict_module.REQUIRED_SCENARIOS:
        (tmp_path / f"{scenario}.code").write_text(codes.get(scenario, "0"))
    lines, all_passed = verdict_module.render_matrix(tmp_path)
    assert all_passed is False
    assert "E2E-006: FAIL" in lines
    assert "E2E-004: AWAITING-USER (bounded attended handoff)" in lines
    assert "E2E-012: NOT-RUN (preflight refused)" in lines


# -- gating layer-2 smoke (promoted 2026-08-19: #94 proved it catches real
# -- regressions; a red here must never again be waved away) ----------------


def test_verdict_does_not_read_the_isolated_e2e009_record(verdict_module) -> None:  # type: ignore[no-untyped-def]
    assert "E2E-009" not in verdict_module.REQUIRED_SCENARIOS


def test_verdict_gating_smoke_failure_blocks(verdict_module, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A red real-TUI smoke now flips the verdict even when every other
    gating scenario passed — the #94 lesson."""

    for scenario in verdict_module.REQUIRED_SCENARIOS:
        (tmp_path / f"{scenario}.code").write_text("0")
    assert "E2E-008" in verdict_module.REQUIRED_SCENARIOS
    assert "E2E-008" not in verdict_module.NON_GATING_SCENARIOS
    (tmp_path / "E2E-008.code").write_text("1")
    lines, all_passed = verdict_module.render_matrix(tmp_path)
    assert all_passed is False
    assert "E2E-008: FAIL" in lines


def test_verdict_gating_smoke_absence_blocks(verdict_module, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Absence is not PASS: a missing 008 record fails the verdict loudly."""

    for scenario in verdict_module.REQUIRED_SCENARIOS:
        if scenario != "E2E-008":
            (tmp_path / f"{scenario}.code").write_text("0")
    lines, all_passed = verdict_module.render_matrix(tmp_path)
    assert all_passed is False
    assert any(line.startswith("E2E-008: MISSING RECORD") for line in lines)


def test_verdict_gating_smoke_pass(verdict_module, tmp_path) -> None:  # type: ignore[no-untyped-def]
    for scenario in verdict_module.REQUIRED_SCENARIOS:
        (tmp_path / f"{scenario}.code").write_text("0")
    lines, all_passed = verdict_module.render_matrix(tmp_path)
    assert all_passed is True
    assert "E2E-008: PASS" in lines


@pytest.fixture()
def verdict_module():  # type: ignore[no-untyped-def]
    return load_module(VERDICT, "gate_verdict_under_test")


# -- workflow wiring ------------------------------------------------------------


def test_controlled_workflow_pins_every_red_line() -> None:
    """Every credentialed scenario is wired; E2E-009 is credential-free and
    runs in the isolated gate, so reading a record this job never writes could
    only ever be a false green or red."""

    import yaml

    controlled = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "e2e-macos-credentialed.yml").read_text()
    )
    assert controlled[True]["workflow_dispatch"] is None or True  # dispatchable
    job = controlled["jobs"]["controlled-real-env"]
    env = job["env"]
    assert env["HYPRIAL_E2E_LARK_APP_ID"] == "${{ secrets.HYPRIAL_E2E_LARK_APP_ID }}"
    assert env["HYPRIAL_E2E_LARK_APP_SECRET"] == "${{ secrets.HYPRIAL_E2E_LARK_APP_SECRET }}"
    assert env["HYPRIAL_E2E_LARK_CHAT_ID"] == "${{ secrets.HYPRIAL_E2E_LARK_CHAT_ID }}"
    assert job["env"]["HYPRIAL_HOME"] != "~/.hyprial"

    step_text = "\n".join(str(step.get("run", "")) for step in job["steps"])
    # Second-line defense: refuse a foreign injected App id before any runner.
    assert "not the approved E2E test App" in step_text
    assert TEST_APP_ID in step_text
    for scenario in ("004", "006", "008", "012", "013"):
        assert f"E2E-{scenario}.code" in step_text
    assert "E2E-009.code" not in step_text
    assert "run-first-run-no-agent.py" not in step_text
    assert "gate-verdict.py" in step_text
    for step in job["steps"]:
        if step.get("name") != "Upload CI conditions artifact":
            assert "continue-on-error" not in step
    # 006 must point at the repository fixture skill, not a machine workspace.
    assert "fixtures/resident-skill" in yaml.dump(controlled)
    # 008 is the layer-2 real-TUI smoke, gating since 2026-08-19: wired as
    # --smoke, and its exit code fails the step like every gating scenario —
    # the "stays green by design" exemption is gone for good.
    assert "run-cc-tui-worker.py --attended --smoke" in step_text
    assert "stays green by design" not in step_text


def _assert_isolated_gate_presence_discipline(text: str, wiring: dict[str, str]) -> None:
    """The isolated gate must still run its named scenarios and keep the
    absence discipline.  The runner names moved into the gate driver's
    SCENARIOS table (spec-e2e001 追加 2), so they are pinned there; the
    workflow keeps naming the driver and the exclusion discipline."""

    assert wiring["E2E-001"] == "run-two-machine-roundtrip.py"
    assert wiring["E2E-009"] == "run-first-run-no-agent.py"
    assert wiring["E2E-011"] == "run-lark-app-onboarding.py"
    assert wiring["E2E-015"] == "run-pac-workflow.py"
    assert wiring["E2E-016"] == "run-selfdrive-routine.py"
    assert wiring["E2E-017"] == "run-outbox-deadletter.py"
    assert "run-isolated-gate.py" in text
    assert "never a PASS" in text
    assert "e2e-macos-credentialed.yml" in text
    # The attended-only exclusions stay named, with reasons.
    assert "two-Tailnet-host" in text
    assert "tenant admin" in text


def test_e2e_workflow_runs_001_and_011_and_keeps_absence_discipline() -> None:
    text = (ROOT / ".github" / "workflows" / "e2e-macos-isolated.yml").read_text()
    spec = importlib.util.spec_from_file_location(
        "e2e_gate_driver_presence",
        ROOT / "contract" / "e2e" / "run-isolated-gate.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _assert_isolated_gate_presence_discipline(text, dict(module.SCENARIOS))


def test_isolated_gate_presence_discipline_mutation_is_rejected() -> None:
    """A driver table that silently drops or renames a named scenario must
    redden the presence guard."""

    text = (ROOT / ".github" / "workflows" / "e2e-macos-isolated.yml").read_text()
    wiring = {
        "E2E-001": "run-two-machine-roundtrip.py",
        "E2E-009": "run-first-run-no-agent.py",
        "E2E-011": "run-someone-else.py",
    }
    with pytest.raises(AssertionError):
        _assert_isolated_gate_presence_discipline(text, wiring)
