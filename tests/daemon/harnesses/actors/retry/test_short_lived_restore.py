"""A successful launch must not replenish the budget of a crash loop."""
import time

from hyprial.kernel import HarnessLaunchSpec, ProcessLiveness, ProcessLivenessState
from hyprial.daemon.impl.processes.supervisor import ManagedHarnessRuntime


class Process:
    running = True
    pid = None
    last_error = "startup exited"

    def stop(self):
        self.running = False

    def liveness(self):
        return ProcessLiveness(
            ProcessLivenessState.ALIVE if self.running else ProcessLivenessState.DEAD,
            observed=False,
        )


def test_short_lived_successes_stop_at_failure_budget(tmp_path):
    now = [0.0]
    processes = []

    class Launcher:
        def start(self, _spec):
            process = Process()
            processes.append(process)
            return process

    runtime = ManagedHarnessRuntime(
        Launcher(), clock=lambda: now[0], restart_backoff_seconds=1,
        failure_budget=3, orphan_state_path=tmp_path / "orphans.json",
    )
    try:
        runtime.start(HarnessLaunchSpec("pi", "offline", True))
        for _ in range(5):
            processes[-1].running = False
            # Let the asynchronous facts/stop/start operations settle via ticks.
            for _ in range(20):
                runtime.reconcile()
                now[0] += 0.2
                time.sleep(0.005)
        assert len(processes) == 3
        assert runtime.status()[0].get("state") == "failed"
        assert runtime.drain_failed_events() == ("pi:offline",)
    finally:
        runtime.stop()


def test_a_stable_run_does_not_count_its_exit_against_the_budget(tmp_path):
    """Negative arm: the fix must not turn every exit into a failure.

    With a budget of one, a run that proved stable for longer than the
    early-exit window may still exit (a normal stop/crash) without being
    reported failed -- the budget is for crash loops, not for long-lived
    workers that end.
    """

    now = [0.0]
    processes = []

    class Launcher:
        def start(self, _spec):
            process = Process()
            processes.append(process)
            return process

    runtime = ManagedHarnessRuntime(
        Launcher(), clock=lambda: now[0], restart_backoff_seconds=1,
        failure_budget=1, orphan_state_path=tmp_path / "orphans.json",
    )
    try:
        runtime.start(HarnessLaunchSpec("pi", "offline", True))
        # Prove the worker stable for longer than the early-exit window.
        for _ in range(20):
            runtime.reconcile()
            now[0] += 5.0
        assert runtime.status()[0].get("state") is None
        # Now it exits once after that stable run: not a crash-loop failure.
        processes[-1].running = False
        for _ in range(40):
            runtime.reconcile()
            now[0] += 0.2
            time.sleep(0.001)
        assert runtime.status()[0].get("state") != "failed"
        assert runtime.drain_failed_events() == ()
    finally:
        runtime.stop()


def test_budget_zero_keeps_the_infinite_retry_opt_in(tmp_path):
    """Negative arm: budget 0 still means "retry forever under backoff"."""

    now = [0.0]
    processes = []

    class Launcher:
        def start(self, _spec):
            process = Process()
            processes.append(process)
            return process

    runtime = ManagedHarnessRuntime(
        Launcher(), clock=lambda: now[0], restart_backoff_seconds=0,
        failure_budget=0, orphan_state_path=tmp_path / "orphans.json",
    )
    try:
        runtime.start(HarnessLaunchSpec("pi", "offline", True))
        for _ in range(6):
            processes[-1].running = False
            for _ in range(10):
                runtime.reconcile()
                now[0] += 0.2
                time.sleep(0.001)
        assert len(processes) >= 4, "budget 0 must keep restarting"
        assert runtime.status()[0].get("state") != "failed"
        assert runtime.drain_failed_events() == ()
    finally:
        runtime.stop()
