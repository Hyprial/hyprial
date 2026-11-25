"""Exact-operation probes for the timeout-vs-quota investigation.

This module is loaded as a pytest plugin for the two unit-test seeds.  It times
only the call covered by the candidate budget; pytest startup, fixture setup,
and teardown remain outside the reading.  The e2e mode is a scaled containment
probe for the inherited-pipe failure mechanism, not a substitute for a real
OAuth/Tailscale latency distribution.

Do not run this module directly on the shared host while CI is active.  The
host orchestrator in ``run_timeout_candidate_measurement.py`` owns that gate.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable

_RESULTS_ENV = "HYPRIAL_TIMEOUT_MEASURE_RESULTS"
_CANDIDATE_ENV = "HYPRIAL_TIMEOUT_MEASURE_CANDIDATE"
_ITERATION_ENV = "HYPRIAL_TIMEOUT_MEASURE_ITERATION"
_ORIGINAL_RUN: Callable[..., subprocess.CompletedProcess[Any]] | None = None
_ORIGINAL_INVOKE: Callable[..., Any] | None = None


def _resource_snapshot() -> dict[str, object]:
    snapshot: dict[str, object] = {}
    for name, path in {
        "loadavg": Path("/proc/loadavg"),
        "cpu_max": Path("/sys/fs/cgroup/cpu.max"),
        "cpu_stat": Path("/sys/fs/cgroup/cpu.stat"),
    }.items():
        try:
            snapshot[name] = path.read_text(encoding="utf-8").strip()
        except OSError as error:
            snapshot[name] = f"unavailable:{type(error).__name__}"
    return snapshot


def _append_reading(**fields: object) -> None:
    path = Path(os.environ[_RESULTS_ENV])
    record = {
        "candidate": os.environ.get(_CANDIDATE_ENV),
        "iteration": int(os.environ.get(_ITERATION_ENV, "0")),
        **fields,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()


def _is_blocked_daemon_run(command: object) -> bool:
    if not isinstance(command, (list, tuple)):
        return False
    tail = [str(value) for value in command[-5:]]
    return tail == ["-m", "hyprial.cli", "daemon", "run", "--json"]


def pytest_configure(config: Any) -> None:
    """Install one narrow timer selected by the orchestrator."""
    del config
    candidate = os.environ.get(_CANDIDATE_ENV)
    if candidate == "daemon-ready-15s":
        _install_daemon_ready_timer()
    elif candidate == "home-guard-15s":
        _install_home_guard_timer()


def pytest_unconfigure(config: Any) -> None:
    """Restore process-global methods even if the selected test fails."""
    del config
    global _ORIGINAL_INVOKE, _ORIGINAL_RUN
    if _ORIGINAL_INVOKE is not None:
        from typer.testing import CliRunner

        CliRunner.invoke = _ORIGINAL_INVOKE  # type: ignore[method-assign]
        _ORIGINAL_INVOKE = None
    if _ORIGINAL_RUN is not None:
        subprocess.run = _ORIGINAL_RUN  # type: ignore[assignment]
        _ORIGINAL_RUN = None


def _install_daemon_ready_timer() -> None:
    global _ORIGINAL_INVOKE
    from typer.testing import CliRunner

    original = CliRunner.invoke
    _ORIGINAL_INVOKE = original

    def timed_invoke(self: Any, cli: Any, args: Any = None, **kwargs: Any) -> Any:
        selected = list(args or []) == ["init", "--ready-timeout", "15", "--json"]
        if not selected:
            return original(self, cli, args, **kwargs)
        resource_before = _resource_snapshot()
        started = time.monotonic()
        outcome = "exception"
        try:
            result = original(self, cli, args, **kwargs)
            outcome = f"exit-{result.exit_code}"
            return result
        finally:
            _append_reading(
                operation="CliRunner.invoke(init --ready-timeout 15 --json)",
                budget_seconds=15.0,
                elapsed_seconds=time.monotonic() - started,
                outcome=outcome,
                resource_before=resource_before,
                resource_after=_resource_snapshot(),
            )

    CliRunner.invoke = timed_invoke  # type: ignore[method-assign]


def _install_home_guard_timer() -> None:
    global _ORIGINAL_RUN
    original = subprocess.run
    _ORIGINAL_RUN = original

    def timed_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        command = args[0] if args else kwargs.get("args")
        if not _is_blocked_daemon_run(command):
            return original(*args, **kwargs)
        resource_before = _resource_snapshot()
        started = time.monotonic()
        outcome = "exception"
        try:
            result = original(*args, **kwargs)
            outcome = f"exit-{result.returncode}"
            return result
        except subprocess.TimeoutExpired:
            outcome = "timeout"
            raise
        finally:
            _append_reading(
                operation="blocked daemon run --json subprocess.run",
                budget_seconds=15.0,
                elapsed_seconds=time.monotonic() - started,
                outcome=outcome,
                resource_before=resource_before,
                resource_after=_resource_snapshot(),
            )

    subprocess.run = timed_run  # type: ignore[assignment]


def _load_login_official() -> Any:
    path = Path("/workspace/sidecar/hyprial-tsnet/e2e/login-official.py")
    if not path.exists():
        path = (
            Path(__file__).resolve().parents[1]
            / "sidecar/hyprial-tsnet/e2e/login-official.py"
        )
    spec = importlib.util.spec_from_file_location("login_official_timeout_probe", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_e2e_process_tree_probe(count: int, probe_budget: float, hold: float) -> None:
    """Exercise the exact ``login_official.run`` containment boundary.

    The direct child creates a descendant that inherits its capture pipes.  A
    plain ``subprocess.run(..., timeout=...)`` kills only the direct child and
    can wait for the descendant to close those pipes.  Scaling 420 seconds to
    ``probe_budget`` makes that structural property measurable without thirty
    multi-minute hangs.  These readings classify containment only; they do not
    classify real official-login latency.
    """
    module = _load_login_official()
    command = [
        sys.executable,
        "-c",
        (
            "import subprocess,sys,time;"
            f"subprocess.Popen([sys.executable,'-c','import time;time.sleep({hold})']);"
            "time.sleep(3600)"
        ),
    ]
    os.environ[_CANDIDATE_ENV] = "e2e-process-tree-420s"
    for iteration in range(count):
        os.environ[_ITERATION_ENV] = str(iteration)
        resource_before = _resource_snapshot()
        started = time.monotonic()
        outcome = "unexpected-return"
        try:
            module.run(command, env=dict(os.environ), timeout=probe_budget)
        except subprocess.TimeoutExpired:
            outcome = "timeout"
        _append_reading(
            operation="login_official.run descendant-inherits-capture-pipes",
            original_budget_seconds=420.0,
            probe_budget_seconds=probe_budget,
            descendant_hold_seconds=hold,
            elapsed_seconds=time.monotonic() - started,
            outcome=outcome,
            classification_scope="process-tree containment only",
            resource_before=resource_before,
            resource_after=_resource_snapshot(),
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("e2e-process-tree",))
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--probe-budget", type=float, default=0.05)
    parser.add_argument("--descendant-hold", type=float, default=0.20)
    args = parser.parse_args()
    if (
        args.count < 1
        or args.probe_budget <= 0
        or args.descendant_hold <= args.probe_budget
    ):
        parser.error(
            "count and budgets must be positive; descendant hold must exceed probe budget"
        )
    _run_e2e_process_tree_probe(args.count, args.probe_budget, args.descendant_hold)


if __name__ == "__main__":
    main()
