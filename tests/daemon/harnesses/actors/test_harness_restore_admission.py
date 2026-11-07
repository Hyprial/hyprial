"""Restore must not report failures for harnesses it never contacted.

Postmortem 2026-08-28: an ``hyprial daemon upgrade`` on a 34-connector host came back
with connectors missing, and the daemon had marked several of them
quarantined -- retried never again, no self-heal, operator action the only
way back.  None of them was broken.

``_on_restore`` began every start in one loop.  Past the process I/O port's
semaphore the submission was rejected outright ("process I/O port is
overloaded"); past the executor's width it was accepted but sat in the queue
with its start timer already ticking.  Both cohorts ended up reported as
``harness start did not complete within Ns`` -- a statement about a launcher
call that had never been made -- and each was charged a start failure.  Three
such rounds is the failure budget (then called the quarantine threshold).

The measurement that fixed the argument, on a fleet that was entirely healthy
and merely not instant (34 specs, 4 workers, 5s starts, 10s timeout):
4 restored, 30 "failed", 26 of which the launcher was never called for.

These tests pin both directions.  Deleting the admission control turns
``test_slow_healthy_fleet_restores_completely`` red; deleting the failure
accounting instead turns ``test_a_genuinely_broken_harness_is_still_failed``
red.  A fix that only satisfies one of them has removed the protection rather
than corrected the bookkeeping.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.processes import supervisor as supervisor_module
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.processes.supervisor import ManagedHarnessRuntime


class _FakeProcess:
    @property
    def running(self) -> bool:
        return True

    @property
    def pid(self) -> int | None:
        return None

    def stop(self) -> None:
        return None


class _SlowLauncher:
    """Healthy, but not instant -- the regime the fleet actually runs in."""

    def __init__(self, delay: float, *, broken: frozenset[str] = frozenset()) -> None:
        self._delay = delay
        self._broken = broken
        self._lock = threading.Lock()
        self.contacted: list[str] = []

    def start(self, spec: HarnessLaunchSpec) -> _FakeProcess:
        with self._lock:
            self.contacted.append(spec.name)
        time.sleep(self._delay)
        if spec.name in self._broken:
            raise RuntimeError(f"{spec.name} refuses to start")
        return _FakeProcess()


def _specs(count: int) -> tuple[HarnessLaunchSpec, ...]:
    return tuple(
        HarnessLaunchSpec(harness="fake", name=f"c{index:02d}", headless=True)
        for index in range(count)
    )


def _failure_counts(runtime: ManagedHarnessRuntime) -> dict[str, int]:
    return {
        key: record.failures for key, record in runtime._actor._records.items()
    }


# Width 2 puts the port's semaphore at 8 while the fleet is 12, so the fleet
# overruns BOTH bounds: four submissions past the semaphore and six past the
# executor width.  A fleet that only overran one of them would leave half the
# defect untested.
_WIDTH = 2
_FLEET = 12


@pytest.fixture(name="runtime")
def _runtime():
    created: list[ManagedHarnessRuntime] = []

    def build(launcher: _SlowLauncher) -> ManagedHarnessRuntime:
        runtime = ManagedHarnessRuntime(
            launcher,
            start_timeout_seconds=2.0,
            start_max_workers=_WIDTH,
            restart_backoff_seconds=0.0,
        )
        created.append(runtime)
        return runtime

    yield build
    for runtime in created:
        runtime.stop(2.0)


def test_slow_healthy_fleet_restores_completely(runtime) -> None:
    """Every healthy harness comes back, and none is charged a failure."""

    launcher = _SlowLauncher(0.1)
    managed = runtime(launcher)

    summary = managed.restore(_specs(_FLEET))

    assert summary.restored == _FLEET, (
        f"{summary.failed} healthy harnesses were reported failed; "
        f"the launcher was called for {len(launcher.contacted)} of {_FLEET}"
    )
    assert summary.failed == 0
    # The accounting claim, not just the outcome: a restore that got the
    # count right while still charging failures would fail this fleet
    # for good on the third restart.
    assert set(launcher.contacted) == {spec.name for spec in _specs(_FLEET)}
    assert not [key for key, count in _failure_counts(managed).items() if count]
    assert managed._failed == frozenset()


def test_a_genuinely_broken_harness_is_still_failed(runtime) -> None:
    """The other direction: real failures must still stop being retried.

    Without this, "stop charging failures" would read as "stop counting" --
    and the poisoned-worker protection the failure budget exists for would be
    gone while every assertion above still passed.
    """

    broken = "c03"
    launcher = _SlowLauncher(0.1, broken=frozenset({broken}))
    managed = runtime(launcher)

    summary = managed.restore(_specs(_FLEET))

    assert summary.restored == _FLEET - 1
    assert summary.failed == 1
    counts = _failure_counts(managed)
    # U0b (Allen 2026-09-03) retired the OLD assertion `counts[...] == 1`:
    # it pinned "a restore failure is charged one failure and retried by
    # reconcile until the budget trips".  A restore-kind start now gets
    # exactly one attempt -- the row was running before, so a failed
    # bring-back is terminal immediately and the counter is pinned to the
    # budget at settlement.  "Charged" (the assertion's stated purpose)
    # still means a non-zero count, and "still failed" now holds right
    # after the restore round instead of after reconcile retries.
    assert counts[f"fake:{broken}"] >= 1, "the real failure was not charged"
    assert not [key for key, count in counts.items() if count and broken not in key], (
        "a healthy harness was charged for the broken one's failure"
    )
    assert managed._failed == frozenset({f"fake:{broken}"}), (
        "a harness that ran before and did not come back must be failed "
        "after its single restore attempt, with no reconcile retries"
    )

    # The reconcile loop below must keep it failed (never self-heal).  It
    # exits immediately under U0b -- the verdict already happened above.
    deadline = time.monotonic() + 20.0
    while (
        f"fake:{broken}" not in managed._failed
        and time.monotonic() < deadline
    ):
        managed.reconcile()
        time.sleep(0.1)

    assert f"fake:{broken}" in managed._failed, (
        "a harness that fails every start was retried forever; the "
        "poisoned-worker protection is gone"
    )
    assert managed._failed == frozenset({f"fake:{broken}"}), (
        f"healthy harnesses were failed too: {sorted(managed._failed)}"
    )


def test_restore_waits_for_settlement_not_a_wallclock_budget() -> None:
    """Restore's wait tracks the fleet's settlement; there is no budget to trip.

    The predecessor of this test pinned the opposite deal: a caller-side wait
    budget (fleet-scaled, capped by a ceiling held under the CLI's 90s) after
    which restore returned a partial summary and let the fleet finish behind
    it.  Both halves of that design are gone.  Restore left the startup path
    (the daemon serves ping while it runs), so no CLI clock needs protecting
    from it; and progress observation moved to the phase-③ readiness reports,
    so a partial summary had nothing left to say.  What remains: restore
    returns when every admitted start has settled, bounded by the per-start
    timers the actor arms -- a reconcile strategy parameter, never a startup
    clock.
    """

    # 20 specs at width 2 with 0.2s starts settle in ~2s: slow enough that
    # any reintroduced caller budget on the old scale would fire mid-flight,
    # fast enough for the suite.
    launcher = _SlowLauncher(0.2)
    managed = ManagedHarnessRuntime(
        launcher,
        start_timeout_seconds=2.0,
        start_max_workers=2,
        restart_backoff_seconds=0.0,
    )
    try:
        summary = managed.restore(_specs(20))

        # The summary is complete on return -- no partial row, and no
        # reconcile nudge needed afterwards.
        assert summary.attempted == 20
        assert summary.restored == 20
        assert summary.failed == 0
        assert sum(1 for row in managed.status() if row.get("running")) == 20
        assert not [key for key, count in _failure_counts(managed).items() if count]
    finally:
        managed.stop(3.0)


def test_restore_wait_budget_removed() -> None:
    """The budget machinery is gone from the tree, not merely bypassed.

    A deleted caller that keeps its symbols is one import away from coming
    back: this scans every Python source for the wait-budget vocabulary and
    checks the constructor knobs are really gone.  The scan excludes this
    file, which is the one place the names must appear -- as strings to hunt.
    """

    this_file = Path(__file__).resolve()
    root = this_file.parents[1]
    hunted = (
        "_restore_wait_seconds",
        "_restore_wait_ceiling",
        "_RESTORE_WAIT_CEILING",
        "restore_wait_ceiling",
    )
    leftovers: list[str] = []
    for base in ("src", "tests", "contract"):
        for path in (root / base).rglob("*.py"):
            if path.resolve() == this_file:
                continue
            text = path.read_text(encoding="utf-8")
            for name in hunted:
                if name in text:
                    leftovers.append(f"{path.relative_to(root)}: {name}")
    assert not leftovers, "restore wait-budget residue:\n" + "\n".join(leftovers)

    launcher = _SlowLauncher(0.01)
    with pytest.raises(TypeError):
        ManagedHarnessRuntime(
            launcher,
            **{"restore_wait_ceiling": 3.0},  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# retrying / failed: the two-state replacement for quarantine (2026-09-02).
#
# `quarantined` meant "resting": a cooldown elapsed, one probe start went out,
# and the cycle repeated with a doubled wait.  That made a genuinely broken
# harness a permanent low-rate retry storm, and made "recovered on its own"
# indistinguishable from "was merely unlucky".  The replacement splits the
# vocabulary: within the failure budget a harness is `retrying` (normal, no
# alarm); past it the harness is `failed` -- terminal, no automatic path back,
# explicit start is the only revival.
#
# Both directions again: a harness past the budget must never come back on
# its own, and one within the budget must keep being retried.
# ---------------------------------------------------------------------------


def test_failed_harness_never_self_heals() -> None:
    """Past the budget, reconcile never starts the harness again.

    The deleted policy this pins: a quarantined harness got one probe start
    after a cooldown (300s default, doubling per round, capped at 3600s).
    Advancing the clock past every cooldown that policy knew and still
    observing zero new attempts proves the revive path is gone, not merely
    slow -- a reintroduced self-heal, at any interval, turns this red.
    """

    now = 1_000.0

    def clock() -> float:
        return now

    launcher = _SlowLauncher(0.01, broken=frozenset({"c00"}))
    managed = ManagedHarnessRuntime(
        launcher,
        clock=clock,
        start_timeout_seconds=2.0,
        start_max_workers=2,
        restart_backoff_seconds=0.01,
        failure_budget=3,
    )
    key = "fake:c00"
    try:
        managed.restore(_specs(1))

        # U0b (Allen 2026-09-03) retired the OLD assertions "wait until the
        # budget trips" and `len(launcher.contacted) == 3`: they pinned the
        # pre-U0b rhythm where a restore-kind failure retried inside the
        # reconcile budget.  A restore-kind start is now a single attempt --
        # the failure verdict (and the terminal no-self-heal state this test
        # exists to pin) lands at settlement, with exactly one contact.
        assert key in managed._failed, (
            "a restore-kind failure must be terminal at settlement"
        )
        assert len(launcher.contacted) == 1
        assert managed._actor._records[key].restart_after is None
        row = next(item for item in managed.status() if item["id"] == key)
        assert row["state"] == "failed"

        # Past every cooldown the old policy could ever schedule: nothing.
        now += 7_200.0
        for _ in range(5):
            managed.reconcile()
            time.sleep(0.02)
        assert len(launcher.contacted) == 1, (
            "a failed harness was started again; only an explicit start "
            "may revive it"
        )
    finally:
        managed.stop(2.0)


def test_zero_budget_never_fails() -> None:
    'Budget 0 keeps the old disable for reconcile-kind restarts.\n\n    U0b note (Allen 2026-09-03): the OLD vehicle for this test was a broken\n    RESTORE -- restore a permanently broken spec and watch the retries.\n    That vehicle is gone by ruling: a restore-kind start is a single\n    attempt, terminal on failure, regardless of the budget (see\n    tests/daemon/desired_state/state/start/test_desired_state_start_timing.py).  What must survive -- and\n    what this test still pins -- is the reconcile-kind contract: a harness\n    that came up, died, and fails to restart is retried forever at budget 0\n    and is never marked failed.\n    '

    class _DyingLauncher:
        """Healthy exactly once; every restart attempt refuses."""

        def __init__(self) -> None:
            self.contacted: list[str] = []
            self._process: _KillableProcess | None = None

        def start(self, spec: HarnessLaunchSpec) -> "_KillableProcess":
            self.contacted.append(spec.name)
            if self._process is not None:
                raise RuntimeError(f"{spec.name} refuses to restart")
            self._process = _KillableProcess()
            return self._process

    launcher = _DyingLauncher()
    managed = ManagedHarnessRuntime(
        launcher,
        start_timeout_seconds=2.0,
        start_max_workers=2,
        restart_backoff_seconds=0.01,
        failure_budget=0,
    )
    key = "fake:c00"
    try:
        summary = managed.restore(_specs(1))
        assert summary.restored == 1 and summary.failed == 0
        assert len(launcher.contacted) == 1

        # The process dies while the daemon lives: reconcile-kind restarts.
        assert launcher._process is not None
        launcher._process.kill()
        deadline = time.monotonic() + 5.0
        while len(launcher.contacted) < 6 and time.monotonic() < deadline:
            managed.reconcile()
            time.sleep(0.02)
        assert len(launcher.contacted) >= 6, (
            "budget 0 must retry forever; the threshold-based stop came back"
        )
        assert managed._failed == frozenset()
        row = next(item for item in managed.status() if item["id"] == key)
        assert row["state"] == "retrying"
        assert "quarantined" not in row
    finally:
        managed.stop(2.0)


class _KillableProcess:
    """A fake worker whose liveness the test controls."""

    def __init__(self) -> None:
        self._running = True

    @property
    def running(self) -> bool:
        return self._running

    @property
    def pid(self) -> int | None:
        return None

    def kill(self) -> None:
        self._running = False

    def stop(self) -> None:
        self._running = False


def test_failure_budget_env_alias_and_legacy_name(monkeypatch) -> None:
    """HYPRIAL_HARNESS_FAILURE_BUDGET wins; the pre-split name is still honored."""

    monkeypatch.delenv("HYPRIAL_HARNESS_FAILURE_BUDGET", raising=False)
    monkeypatch.setenv("HYPRIAL_HARNESS_QUARANTINE_THRESHOLD", "7")
    assert supervisor_module._failure_budget() == 7
    monkeypatch.setenv("HYPRIAL_HARNESS_FAILURE_BUDGET", "5")
    assert supervisor_module._failure_budget() == 5


def test_cooldown_env_vars_warn_deprecation_once(monkeypatch, caplog) -> None:
    """The removed cooldown knobs are still read -- once, to say they are inert."""

    monkeypatch.setenv("HYPRIAL_HARNESS_QUARANTINE_COOLDOWN", "30")
    monkeypatch.setattr(supervisor_module, "_cooldown_deprecation_warned", False)
    with caplog.at_level(logging.WARNING, logger="hyprial.daemon.impl.processes.supervisor"):
        supervisor_module._warn_cooldown_env_deprecated()
        supervisor_module._warn_cooldown_env_deprecated()
    notices = [
        record.getMessage()
        for record in caplog.records
        if "HYPRIAL_HARNESS_QUARANTINE_COOLDOWN" in record.getMessage()
    ]
    assert len(notices) == 1
    assert "no longer have any effect" in notices[0]
