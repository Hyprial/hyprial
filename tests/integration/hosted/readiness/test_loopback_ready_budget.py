"""Loopback readiness budgets use the supplied 156 s Claude cold-start reading.

The clock and daemon status are controlled; these tests do not start a harness
or establish live mock-model-harness-e2e acceptance.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from typing import Any

import pytest


RUNNER = Path(__file__).resolve().parents[4] / "contract/e2e/run-real-harness-loopback.py"


class Clock:
    def __init__(self) -> None:
        self.seconds = 0.0

    def monotonic(self) -> float:
        return self.seconds

    def sleep(self, seconds: float) -> None:
        self.seconds += seconds


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> Any:
    spec = importlib.util.spec_from_file_location("loopback_ready_budget", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def clock(runner: Any, monkeypatch: pytest.MonkeyPatch) -> Clock:
    controlled = Clock()
    monkeypatch.setattr(runner, "time", controlled)
    return controlled


def _daemon(clock: Clock, harness: str, ready_at: float) -> Any:
    def ps() -> dict[str, Any]:
        ready = clock.seconds >= ready_at
        return {
            "connectors": [
                {"name": "worker", "runtime": harness, "running": ready, "pid": 123}
            ],
            "agents": [{"actor": "worker", "status": "online" if ready else "starting"}],
        }

    return SimpleNamespace(ps=ps)


def test_claude_ready_after_observed_cold_start(runner: Any, clock: Clock) -> None:
    runner._wait_headless_ready(_daemon(clock, "claude", 156.0), "worker", "claude")
    assert clock.seconds == 156.0
    print(f"READY claude-headless elapsed={clock.seconds:.2f}s")


@pytest.mark.parametrize("harness", ["pi", "codex"])
def test_other_headless_budgets_stay_at_75_seconds(
    runner: Any, clock: Clock, harness: str
) -> None:
    with pytest.raises(AssertionError, match="timed out"):
        runner._wait_headless_ready(_daemon(clock, harness, 156.0), "worker", harness)
    assert clock.seconds == 75.0


@pytest.mark.parametrize("harness", ["pi", "codex"])
def test_interactive_budgets_stay_at_75_seconds(
    runner: Any, clock: Clock, harness: str
) -> None:
    daemon = SimpleNamespace(ps=lambda: {"interactiveSessions": []})
    with pytest.raises(AssertionError, match="timed out"):
        runner._wait_interactive_ready(daemon, "worker", harness)
    assert clock.seconds == 75.0


def test_reply_wait_stays_at_75_seconds(runner: Any, clock: Clock) -> None:
    with pytest.raises(AssertionError, match="timed out"):
        runner._wait_until(lambda: False, "reply")
    assert clock.seconds == 75.0


def test_timeout_reports_actual_elapsed_and_budget(runner: Any, clock: Clock) -> None:
    def slow_status() -> bool:
        clock.sleep(0.4)
        return False

    with pytest.raises(AssertionError) as caught:
        runner._wait_until(slow_status, "slow status", timeout_seconds=1.0)
    assert str(caught.value) == (
        "timed out after 1.30s (budget 1.00s): slow status; last=False"
    )


def test_never_ready_exits_nonzero_with_diagnostics_and_cleanup(tmp_path: Path) -> None:
    bootstrap = textwrap.dedent(
        """
        from contextlib import nullcontext
        import importlib.util
        import json
        from pathlib import Path
        import sys
        from types import SimpleNamespace

        spec = importlib.util.spec_from_file_location("loopback_bounded", sys.argv[1])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        trace = Path(sys.argv[2])

        class Clock:
            seconds = 0.0
            def monotonic(self):
                return self.seconds
            def sleep(self, seconds):
                self.seconds += seconds

        def record(value):
            with trace.open("a") as stream:
                stream.write(json.dumps(value) + "\\n")

        class Daemon:
            def __init__(self, *args):
                self.env = {}
            def start(self):
                record({"event": "daemon.start"})
            def stop(self):
                record({"event": "daemon.stop", "seconds": module.time.seconds})
            def ps(self):
                return {"agents": [{"actor": "worker-claude-headless",
                         "uri": "agent:test:isolated:worker", "status": "starting"}]}
            def request(self, *args):
                return {"messages": []}
            def command(self, *args, **kwargs):
                record({"args": args, "kwargs": kwargs})
                return {"ok": True}

        provider = SimpleNamespace(requests=[], request_bodies=[],
                                   openai_base_url="http://127.0.0.1/unused",
                                   anthropic_base_url="http://127.0.0.1/unused")
        module.time = Clock()
        module._require_harnesses = lambda: None
        module.hyprial_executable = lambda: "hyprial-test"
        module._git_workspace = lambda path: None
        module._configure_agent = lambda *args: None
        # The SDK pre-warm has its own tests (tests/integration/hosted/gate/);
        # this budget test drives a stub daemon with no agent home.
        module._prewarm_claude_sdk = lambda *args: 0.0
        module.FakeProvider = lambda **kwargs: nullcontext(provider)
        module.IsolatedDaemon = Daemon
        module.SCENARIOS = (module.Scenario("claude", "headless", "/v1/messages"),)
        raise SystemExit(module.main())
        """
    )
    trace = tmp_path / "commands.jsonl"
    result = subprocess.run(
        [sys.executable, "-c", bootstrap, str(RUNNER), str(trace)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 1
    assert "timed out after 240.00s (budget 240.00s)" in result.stderr
    assert "loopback diagnostics:" in result.stderr
    assert '"driverPending"' in result.stderr and '"workerPending"' in result.stderr
    assert result.stdout == ""
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    start = next(event for event in events if event.get("args", [])[:2] == ["agent", "start"])
    assert start["kwargs"] == {"timeout": 90}
    assert events[-2] == {
        "args": ["agent", "stop", "worker-claude-headless", "--json"],
        "kwargs": {"exits": [0, 1]},
    }
    assert events[-1] == {"event": "daemon.stop", "seconds": 240.0}
    print("BOUNDED exit=1 elapsed=240.00s budget=240.00s start_command=90s cleanup=agent+daemon")
