"""The harness never kills ``hyprial init`` before the product's own wait ends.

2026-09-27, task 36275: the E2E gates set HYPRIAL_INIT_READY_TIMEOUT=45 (#907)
while ``IsolatedDaemon.command`` still killed every command at a flat 30 s, so
E2E-002/E2E-003 ended in subprocess.TimeoutExpired with no product verdict.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[5]


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "_daemon_runner", REPO / "contract/e2e/_daemon_runner.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO / "contract/e2e"))
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


@pytest.mark.parametrize("ready", ["", "15", "45", "120", "garbage", "0"])
def test_init_harness_timeout_is_strictly_above_the_product_wait(ready: str) -> None:
    environment = {"HYPRIAL_INIT_READY_TIMEOUT": ready} if ready else {}
    product = runner.product_init_ready_seconds(environment)
    harness = runner.init_harness_timeout(environment)
    assert harness > product
    assert harness >= 60.0


def _captured_timeouts(monkeypatch: pytest.MonkeyPatch, daemon: Any) -> list[float]:
    seen: list[float] = []

    def fake_run(argv, **kwargs):
        seen.append(kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    return seen


def test_init_command_waits_longer_than_the_gate_readiness_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = runner.IsolatedDaemon(Path("/bin/echo"), Path("/tmp"), "test-node")
    daemon.env["HYPRIAL_INIT_READY_TIMEOUT"] = "45"
    seen = _captured_timeouts(monkeypatch, daemon)

    daemon.command("init", "--json")
    daemon.command("init", "--json", timeout=60)
    daemon.command("ping", "--json")

    assert seen[0] > 45 and seen[1] > 45
    assert seen[0] >= 90
    # Other commands keep their own budget.
    assert seen[2] == 30.0


def test_stop_harness_timeout_outlasts_the_teardown_budget_and_the_stall_dump() -> None:
    from hyprial.kernel import TEARDOWN_BUDGETED_SECONDS
    from hyprial.daemon.impl.processes.shutdown_stall import STALL_SECONDS

    harness = runner.stop_harness_timeout()
    assert harness > TEARDOWN_BUDGETED_SECONDS
    assert harness > STALL_SECONDS


def test_daemon_stop_waits_longer_than_the_teardown_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hyprial.kernel import TEARDOWN_BUDGETED_SECONDS

    daemon = runner.IsolatedDaemon(Path("/bin/echo"), Path("/tmp"), "test-node")
    seen = _captured_timeouts(monkeypatch, daemon)

    daemon.command("daemon", "stop", "--json")
    daemon.command("daemon", "status", "--json")

    assert seen[0] > TEARDOWN_BUDGETED_SECONDS
    assert seen[1] == 30.0
