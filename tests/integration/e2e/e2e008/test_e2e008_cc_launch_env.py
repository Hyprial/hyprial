"""E2E-008 red line: the CC launch carries DeepSeek's official env mapping.

Baseline (2026-09-03, CC 2.1.259, origin/dev@244cd55): the scenarios launched
CC through ``hyprial agent start claude --provider deepseek --model deepseek-flash``,
whose provider mapping pins ANTHROPIC_MODEL (and every DEFAULT_* variable) to
the raw ``deepseek-flash`` id. That id is absent from CC's model catalog,
so the TUI printed the "isn't described by this version's model catalog"
banner and exited (status 1) before Channel registration — the red E2E-008
controlled-gate signature.

The fix (spec 2026-09-03): launch CC with DeepSeek's official integration
mapping (ANTHROPIC_BASE_URL / AUTH_TOKEN / MODEL / DEFAULT_OPUS / DEFAULT_SONNET
/ DEFAULT_HAIKU / CLAUDE_CODE_SUBAGENT_MODEL, per
https://api-docs.deepseek.com/zh-cn/quick_start/agent_integrations/claude_code)
and select the catalog-known ``haiku`` alias, which
ANTHROPIC_DEFAULT_HAIKU_MODEL resolves to deepseek-flash at request time.
These tests pin that launch construction against drift. The expected table
below IS the official one — it is deliberately spelled out here so a change
to the mapping cannot pass silently.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCENARIOS = ROOT / "contract" / "e2e"
RUN_008 = SCENARIOS / "run-cc-tui-worker.py"
RUN_ATTACH = SCENARIOS / "run-tui-attach-manual.py"

TEST_KEY = "e2e008-unit-not-a-real-key"

# DeepSeek's official CC integration table (the spec's 事实 section).
OFFICIAL_BASE_URL = "https://api.deepseek.com/anthropic"
OFFICIAL_TABLE = {
    "ANTHROPIC_BASE_URL": OFFICIAL_BASE_URL,
    "ANTHROPIC_MODEL": "deepseek-flash",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-flash",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-flash",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-flash",
    "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-flash",
}
OFFICIAL_KEYS = ("ANTHROPIC_AUTH_TOKEN", *OFFICIAL_TABLE)


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
def run008(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DEEPSEEK_API_KEY", TEST_KEY)
    return load_module(RUN_008, "e2e008_runner_cc_launch_env")


@pytest.fixture()
def run_attach(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DEEPSEEK_API_KEY", TEST_KEY)
    return load_module(RUN_ATTACH, "e2e008_attach_runner_cc_launch_env")


def env_entries(command: list[str]) -> dict[str, str]:
    """Parse the ``env``-prefixed KEY=VALUE pairs of a tmux launch command."""

    entries: dict[str, str] = {}
    for item in command:
        key, separator, value = item.partition("=")
        if separator and key.replace("_", "").isalpha():
            entries[key] = value
    return entries


# -- the shared official mapping (contract/e2e/_deepseek.py) --------


def test_claude_code_environment_matches_official_table(
    run008, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """The mapping is exactly the official table and never prints the key."""

    with contextlib.redirect_stdout(io.StringIO()) as captured:
        environment = run008.claude_code_environment()
    assert captured.getvalue() == ""
    assert set(environment) == set(OFFICIAL_KEYS)
    for key, value in OFFICIAL_TABLE.items():
        assert environment[key] == value
    assert environment["ANTHROPIC_AUTH_TOKEN"] == TEST_KEY
    # The key exists only in AUTH_TOKEN's value, never in another entry.
    for key, value in environment.items():
        if key != "ANTHROPIC_AUTH_TOKEN":
            assert TEST_KEY not in value


# -- E2E-008 run-cc-tui-worker.py launch construction -------------------------


def cc_launch_command(run008) -> list[str]:  # type: ignore[no-untyped-def]
    return run008.build_cc_launch_command(
        session="hyprial-cc-worker-test",
        workspace=Path("/tmp/e2e008-cc-workspace"),
        worker_env={"HYPRIAL_HOME": "/tmp/e2e008-home", "HARNESS_STATE_DIR": "/tmp/e2e008-state"},
        claude_config_dir=Path("/Users/ci-runner/.claude-ci"),
        executable=Path("/tmp/e2e008-hyprial"),
        actor="agent:o:n:cc-tui-test",
        busy_sentinel=Path("/tmp/e2e008-cc-workspace/busy-started"),
        debug_log=Path("/tmp/e2e008-cc-debug.log"),
        cc_environment=run008.claude_code_environment(),
    )


def test_cc_launch_command_carries_official_mapping_and_haiku(
    run008,
) -> None:  # type: ignore[no-untyped-def]
    command = cc_launch_command(run008)
    entries = env_entries(command)
    for key, value in OFFICIAL_TABLE.items():
        assert entries.get(key) == value, f"{key} missing or wrong in env line"
    assert entries.get("ANTHROPIC_AUTH_TOKEN") == TEST_KEY
    # hyprial's own provider mapping must not run: it would overwrite the official
    # table and pin the selection to the catalog-fatal deepseek id.
    assert "--provider" not in command
    model = command[command.index("--model") + 1]
    assert "deepseek-" not in model
    assert model == "haiku"


def test_start_tui_launches_the_real_command_with_the_mapping(
    run008, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """start_tui must spawn the constructed command, not a parallel shape."""

    captured: list[list[str]] = []

    def fake_run(*args: str, **_kwargs: object):
        if not captured:
            captured.append(list(args))
        raise KeyboardInterrupt  # stop start_tui right after the spawn

    monkeypatch.setattr(run008, "run", fake_run)
    worker = SimpleNamespace(
        root=tmp_path,
        env={"HYPRIAL_HOME": str(tmp_path / "home"), "HARNESS_STATE_DIR": str(tmp_path / "state")},
    )
    with pytest.raises(KeyboardInterrupt):
        run008.start_tui(
            Path("/tmp/e2e008-hyprial"),
            worker,  # type: ignore[arg-type]
            "agent:o:n:cc-tui-test",
            tmp_path / "cc-workspace" / "busy-started",
            tmp_path / "claude-config",
            suffix="unit",
            attended=False,
        )
    command = captured[0]
    entries = env_entries(command)
    for key, value in OFFICIAL_TABLE.items():
        assert entries.get(key) == value, f"{key} missing or wrong in env line"
    assert entries.get("ANTHROPIC_AUTH_TOKEN") == TEST_KEY
    assert "--provider" not in command
    model = command[command.index("--model") + 1]
    assert "deepseek-" not in model
    assert model == "haiku"


# -- run-tui-attach-manual.py CC leg ------------------------------------------


def test_attach_manual_cc_leg_carries_official_mapping_and_haiku(
    run_attach,
) -> None:  # type: ignore[no-untyped-def]
    command = run_attach.claude_leg_launch_command(
        session="claude-attach-leg-test",
        workspace=Path("/tmp/e2e008-attach-workspace"),
        root=Path("/tmp/e2e008-attach-home"),
        config_dir=Path("/Users/ci-runner/.claude-ci"),
        hyprial=["uv", "run", "--project", "/tmp/repo", "hyprial"],
        name="claude-leg-test",
        cc_environment=run_attach.claude_code_environment(),
    )
    entries = env_entries(command)
    for key, value in OFFICIAL_TABLE.items():
        assert entries.get(key) == value, f"{key} missing or wrong in env line"
    assert entries.get("ANTHROPIC_AUTH_TOKEN") == TEST_KEY
    assert "--provider" not in command
    model = command[command.index("--model") + 1]
    assert "deepseek-" not in model
    assert model == "haiku"
