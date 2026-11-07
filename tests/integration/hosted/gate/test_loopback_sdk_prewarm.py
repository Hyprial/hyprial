"""The loopback gate pre-warms the Claude SDK exactly where the worker reads it.

Regression for run 50553: with the proxy route fixed, the real Claude worker
still downloaded claude-agent-sdk (80.2 MiB) inside the daemon's fixed 60/70 s
start deadline and was killed mid-download. These tests start no harness and
download nothing: the uv subprocess is replaced.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.harnesses.agent_sdk import AGENT_SDK_VERSION, sdk_worker_command
from hyprial.identity.impl.agents.runtime.context import derive_runtime_roots


RUNNER = Path(__file__).resolve().parents[4] / "contract/e2e/run-real-harness-loopback.py"


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> Any:
    spec = importlib.util.spec_from_file_location("loopback_sdk_prewarm", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec.loader.exec_module(module)
    return module


def _daemon(tmp_path: Path, name: str) -> SimpleNamespace:
    home = tmp_path / "node" / "home" / "agents" / name
    (home / "state").mkdir(parents=True, mode=0o700)
    return SimpleNamespace(root=tmp_path / "node", env={"PATH": "/usr/bin", "UV_CACHE_DIR": "/elsewhere"})


def test_prewarm_runs_the_product_command_into_the_worker_cache(
    runner: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = _daemon(tmp_path, "worker")
    calls: list[dict[str, Any]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    runner._prewarm_claude_sdk(daemon, "worker")

    (call,) = calls
    product = list(sdk_worker_command())
    assert call["command"][:-2] == product[:-1]
    assert call["command"][-2:] == ["-c", "import claude_agent_sdk"]
    assert f"claude-agent-sdk=={AGENT_SDK_VERSION}" in call["command"]
    agent_home = daemon.root / "home" / "agents" / "worker"
    expected = derive_runtime_roots(
        agent_home=agent_home, entity_token="x", harness="claude",
        config_revision="x", tool_profile_id="x", config_source=agent_home,
    ).xdg_cache_home
    assert call["env"]["XDG_CACHE_HOME"] == str(expected)
    assert "UV_CACHE_DIR" not in call["env"], "uv must resolve the cache the worker will read"
    for directory in (expected.parent, expected):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory


def test_prewarm_failure_is_loud(runner: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = _daemon(tmp_path, "worker")
    monkeypatch.setattr(
        runner.subprocess, "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 2, "", "no route to index"),
    )
    with pytest.raises(AssertionError, match="pre-warm failed rc=2.*no route to index"):
        runner._prewarm_claude_sdk(daemon, "worker")


def test_missing_agent_home_is_loud(runner: Any, tmp_path: Path) -> None:
    daemon = SimpleNamespace(root=tmp_path / "node", env={})
    with pytest.raises(AssertionError, match="agent home for worker not found"):
        runner._prewarm_claude_sdk(daemon, "worker")


def test_only_the_claude_scenario_is_prewarmed() -> None:
    source = RUNNER.read_text()
    assert 'if scenario.harness == "claude":\n        seconds = _prewarm_claude_sdk(daemon, name)' in source
