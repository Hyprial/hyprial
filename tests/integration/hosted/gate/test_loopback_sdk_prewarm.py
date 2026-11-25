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
    calls = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 2, "", f"no route to index attempt {len(calls)}")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    with pytest.raises(AssertionError, match="pre-warm failed rc=2.*no route to index") as failure:
        runner._prewarm_claude_sdk(daemon, "worker")
    assert len(calls) == 2
    assert "no route to index attempt 1" in str(failure.value)
    assert "no route to index attempt 2" in str(failure.value)


@pytest.mark.parametrize("first_failure", ["timeout", "exit"])
def test_prewarm_retries_in_the_same_cache(
    runner: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_failure: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemon = _daemon(tmp_path, "worker")
    calls: list[dict[str, Any]] = []
    clock = [0.0]
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"command": command, **kwargs})
        cache = Path(kwargs["env"]["XDG_CACHE_HOME"])
        marker = cache / "already-fetched"
        if len(calls) == 1:
            marker.write_text("cached dependency")
            clock[0] += 300.0 if first_failure == "timeout" else 10.0
            if first_failure == "timeout":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"], stderr=b"slow proxy")
            return subprocess.CompletedProcess(command, 2, "", "slow proxy")
        assert marker.read_text() == "cached dependency"
        assert stat.S_IMODE(cache.stat().st_mode) == 0o700
        clock[0] += 20.0
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    assert runner._prewarm_claude_sdk(daemon, "worker") == clock[0]
    assert len(calls) == 2
    first, second = calls
    for key in ("command", "env", "cwd"):
        assert first[key] == second[key]
    assert first["timeout"] == second["timeout"] == 300.0
    assert sum(call["timeout"] for call in calls) == runner._SDK_PREWARM_TIMEOUT_SECONDS == 600.0
    assert "retrying" in capsys.readouterr().err


@pytest.mark.parametrize("stderr", [b"stuck download", "stuck download", None])
def test_prewarm_stuck_download_is_bounded_and_loud(
    runner: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stderr: bytes | str | None,
) -> None:
    daemon = _daemon(tmp_path, "worker")
    clock = [0.0]
    timeouts: list[float] = []
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        timeouts.append(kwargs["timeout"])
        clock[0] += kwargs["timeout"]
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], stderr=stderr)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    with pytest.raises(AssertionError, match="pre-warm failed rc=timeout") as failure:
        runner._prewarm_claude_sdk(daemon, "worker")
    assert timeouts == [300.0, 300.0]
    assert clock[0] == 600.0
    assert "2 attempts" in str(failure.value)
    if stderr is not None:
        assert "stuck download" in str(failure.value)


def test_prewarm_retry_respects_remaining_deadline(
    runner: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = _daemon(tmp_path, "worker")
    clock = [0.0]
    timeouts: list[float] = []
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        timeouts.append(kwargs["timeout"])
        clock[0] += kwargs["timeout"] + (5.0 if len(timeouts) == 1 else 0.0)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], stderr=b"slow cleanup")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    with pytest.raises(AssertionError, match="pre-warm failed rc=timeout"):
        runner._prewarm_claude_sdk(daemon, "worker")
    assert timeouts == [300.0, 295.0]
    assert clock[0] == 600.0


def test_prewarm_does_not_start_retry_after_deadline(
    runner: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = _daemon(tmp_path, "worker")
    clock = [0.0]
    calls = []
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        clock[0] = 600.0
        return subprocess.CompletedProcess(command, 2, "", "no route to index")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    with pytest.raises(AssertionError, match="pre-warm failed rc=2.*no route to index"):
        runner._prewarm_claude_sdk(daemon, "worker")
    assert len(calls) == 1


def test_missing_agent_home_is_loud(runner: Any, tmp_path: Path) -> None:
    daemon = SimpleNamespace(root=tmp_path / "node", env={})
    with pytest.raises(AssertionError, match="agent home for worker not found"):
        runner._prewarm_claude_sdk(daemon, "worker")


def test_only_the_claude_scenario_is_prewarmed() -> None:
    source = RUNNER.read_text()
    assert 'if scenario.harness == "claude":\n        seconds = _prewarm_claude_sdk(daemon, name)' in source
