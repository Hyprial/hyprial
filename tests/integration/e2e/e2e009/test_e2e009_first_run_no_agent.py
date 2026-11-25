"""Safety and orchestration contracts for E2E-009's clean-first-run path."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[4]
RUNNER = ROOT / "contract" / "e2e" / "run-first-run-no-agent.py"


def load_runner(name: str):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(RUNNER.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(RUNNER.parent))
    return module


@pytest.fixture()
def runner():  # type: ignore[no-untyped-def]
    return load_runner("e2e009_first_run_under_test")


class FakeDaemon:
    def __init__(self, calls: list[Any]) -> None:
        self.calls = calls
        self.env = {"HYPRIAL_HOME": "/nonexistent", "HARNESS_STATE_DIR": "/nonexistent"}

    def start(self) -> dict[str, object]:
        self.calls.append("start")
        return {"ok": True, "running": True, "phase": "serving"}

    def stop(self) -> None:
        self.calls.append("stop")

    def command(self, *arguments: str) -> dict[str, object]:
        self.calls.append(arguments)
        if arguments[:2] == ("agent", "list"):
            return {"ok": True, "agents": []}
        if arguments[:2] == ("adapter", "list"):
            return {"ok": True, "adapters": []}
        if arguments[0] == "init":
            return {"ok": True, "running": True, "alreadyRunning": True}
        raise AssertionError(f"unexpected command: {arguments}")


def _empty_agent_list() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        [], 0, json.dumps({"ok": True, "agents": []}), ""
    )


def _refusal(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    if argv[-1] == "--json":
        return subprocess.CompletedProcess(
            list(argv),
            2,
            json.dumps(
                {
                    "ok": False,
                    "code": "INVALID_ARGUMENT",
                    "error": "No such command 'onboarding'.",
                }
            ),
            "",
        )
    return subprocess.CompletedProcess(
        list(argv), 2, "", "Error: No such command 'onboarding'.\n"
    )


def test_a_clean_run_drives_init_restart_and_the_retired_surface(
    runner, monkeypatch, tmp_path
) -> None:
    calls: list[Any] = []
    daemon = FakeDaemon(calls)
    monkeypatch.setattr(runner, "IsolatedDaemon", lambda *_args: daemon)
    monkeypatch.setattr(runner, "_raw", lambda _hyprial, _env, *argv, **_kw: (
        _empty_agent_list() if argv[:2] == ("agent", "list") else _refusal(argv)
    ))

    report = runner.Report()
    runner.run(report, tmp_path, tmp_path / "hyprial")

    assert [check[0] for check in report.checks] == ["PASS"] * 4
    assert [check[1] for check in report.checks] == [
        "first-run.no-agent",
        "first-run.idempotent",
        "first-run.after-restart",
        "first-run.retired-surface",
    ]
    assert calls.count("start") == 2 and calls.count("stop") == 2


def test_an_agent_created_by_first_run_fails_the_scenario(runner, monkeypatch, tmp_path) -> None:
    class SquireDaemon(FakeDaemon):
        def command(self, *arguments: str) -> dict[str, object]:
            if arguments[:2] == ("agent", "list"):
                return {"ok": True, "agents": [{"actor": "squire"}]}
            return super().command(*arguments)

    monkeypatch.setattr(runner, "IsolatedDaemon", lambda *_args: SquireDaemon([]))

    report = runner.Report()
    with pytest.raises(AssertionError, match="init created agent\\(s\\): \\['squire'\\]"):
        runner.run(report, tmp_path, tmp_path / "hyprial")


def test_a_fixed_first_run_name_on_disk_fails_the_scenario(
    runner, monkeypatch, tmp_path
) -> None:
    daemon = FakeDaemon([])
    monkeypatch.setattr(runner, "IsolatedDaemon", lambda *_args: daemon)
    monkeypatch.setattr(
        runner,
        "_raw",
        lambda _hyprial, _env, *argv, **_kw: subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {"ok": True, "agents": [{"actor": "default", "uri": "agent:e2e:n:default"}]}
            ),
            "",
        ),
    )

    with pytest.raises(AssertionError, match="first-run agent"):
        runner._assert_nothing_was_created(daemon, "first init")


def test_a_retired_command_that_still_runs_fails_the_scenario(
    runner, monkeypatch
) -> None:
    monkeypatch.setattr(
        runner,
        "_raw",
        lambda _hyprial, _env, *argv, **_kw: subprocess.CompletedProcess(
            list(argv),
            0,
            json.dumps(
                {
                    "ok": False,
                    "code": "INVALID_ARGUMENT",
                    "error": "No such command 'onboarding'.",
                }
            ),
            "",
        ),
    )

    with pytest.raises(AssertionError, match="expected 2"):
        runner._assert_retired_surface(
            Path("hyprial"), {"HYPRIAL_HOME": "/nonexistent"}
        )


def test_an_old_step_id_that_is_still_accepted_fails_the_scenario(
    runner, monkeypatch
) -> None:
    def accepted(_hyprial, _env, *argv, **_kwargs):  # type: ignore[no-untyped-def]
        if argv[:3] == ("init", "onboarding", "apply") and argv[3] in runner.OLD_STEP_IDS:
            return subprocess.CompletedProcess(
                list(argv), 0, json.dumps({"ok": True, "step": argv[3]}), ""
            )
        return _refusal(argv)

    monkeypatch.setattr(runner, "_raw", accepted)

    with pytest.raises(AssertionError, match="old step id"):
        runner._assert_retired_surface(
            Path("hyprial"), {"HYPRIAL_HOME": "/nonexistent"}
        )


def test_the_runner_declares_the_scenario_and_reads_no_credential(runner) -> None:
    assert runner.SCENARIO == "E2E-009"
    assert "squire" in runner.FIRST_RUN_NAMES
    assert "lark-roundtrip" in runner.OLD_STEP_IDS
    source = RUNNER.read_text(encoding="utf-8")
    assert "HYPRIAL_E2E_LARK" not in source
    assert "~/.hyprial" not in source
