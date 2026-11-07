"""F1 (PR #332): a restore batch must settle even when a lifecycle effect
owns one of its targets.

The defect, on baseline f6598d1: ``_on_restore`` reported a lifecycle-owned
target as ``deferred`` and moved on, but left the key in ``batch.pending``;
``_finish_start_attempt`` -- the only place that empties ``pending`` -- runs
only for admitted starts, so the batch never completed, ``HarnessRestoreCompleted``
never fired, and the facade's then-unbounded settlement wait blocked forever.
The daemon's gate never opened: ``handle()``
admitted only ping/shutdown and answered everything else ``DAEMON_RESTORING``.

Two doors reached that state, both pinned here:

* door 1 -- the lifecycle claim lands before the restore command is
  processed (``_on_restore`` sees the key in ``_lifecycle_effect_resources``);
* door 2 -- the claim lands while a key sits in ``batch.queued`` (admission
  drains one settlement at a time), so ``_begin_start``'s lifecycle guard
  silently returned for a key the batch still counted as pending.

And the wait ceiling: U7 removed the flat 45s wall clock; its replacement is
derived, not constant -- every key now has a disposition owner (immediate
deferred settle, or a start bounded by the per-start settlement timer), so
the facade waits at most ``ceil(targets / width) + 1`` rounds of one start
timeout, and converts a lost settlement into ``TimeoutError`` on the
degraded-start path instead of a gate that never opens.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Iterator

import pytest

from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.harnesses.runtime.ports import (
    HarnessLaunchProjection,
    HarnessRestoreCompleted,
    RestoreHarnessesCommand,
)
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


class _StubDesiredState:
    """Only the startup read the actor performs; anything else is a bug here.

    Same seam the repro (``docs/notes/repro/test_repro_restore_hang_pr332.py``)
    uses: the actor constructor seeds its lifecycle-claim set from these two
    methods, which is exactly how a claim present at restore time is reached
    in production.
    """

    def __init__(self, incomplete: set[str]) -> None:
        self._incomplete = incomplete

    def incomplete_harness_lifecycle_resources(self) -> set[str]:
        return set(self._incomplete)

    def incomplete_harness_lifecycle_receipts(self) -> tuple[(), ...]:
        return ()


class _GateLauncher:
    """First-named start blocks until released; the rest return immediately.

    The block is what holds a fleet of three at admission width one with the
    second target still sitting in ``batch.queued`` -- the window door 2
    needs, reached deterministically instead of by racing the actor.
    """

    def __init__(self, gated: str) -> None:
        self._gated = gated
        self.entered = threading.Event()
        self.release = threading.Event()
        self.contacted: list[str] = []
        self._lock = threading.Lock()

    def start(self, spec: HarnessLaunchSpec) -> _FakeProcess:
        with self._lock:
            self.contacted.append(spec.name)
        if spec.name == self._gated:
            self.entered.set()
            self.release.wait(timeout=10.0)
        return _FakeProcess()


def _specs(count: int) -> tuple[HarnessLaunchSpec, ...]:
    return tuple(
        HarnessLaunchSpec(harness="fake", name=f"c{index:02d}", headless=True)
        for index in range(count)
    )


def _wait_for(predicate: Any, timeout: float = 5.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"{what} never happened"
        time.sleep(0.02)


@pytest.fixture(name="runtimes")
def _runtimes() -> Iterator[list[ManagedHarnessRuntime]]:
    created: list[ManagedHarnessRuntime] = []
    yield created
    for runtime in created:
        runtime.stop(2.0)


# --------------------------------------------------------------------------- #
# Door 1: the claim precedes the restore command.
# --------------------------------------------------------------------------- #


def test_restore_returns_when_a_lifecycle_effect_owns_one_target(
    runtimes: list[ManagedHarnessRuntime],
) -> None:
    """Spec's named test: 3 targets, 1 lifecycle-owned => bounded return.

    ``restore()`` must come back (the repro hung here for 5s+ on f6598d1),
    the deferred target must appear in the readiness reports, and the
    summary must account for every attempted target -- no unexplained
    remainder between attempted and restored+failed+deferred.
    """

    managed = ManagedHarnessRuntime(
        _GateLauncher(gated="none"),
        start_timeout_seconds=1.0,
        start_max_workers=2,
        restart_backoff_seconds=0.0,
        desired_state=_StubDesiredState({"fake:c01"}),
    )
    runtimes.append(managed)

    started = time.monotonic()
    summary = managed.restore(_specs(3))
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, "restore() must return in bounded time"
    assert summary.attempted == 3
    assert summary.restored == 2
    assert summary.failed == 0
    assert summary.deferred == 1
    reports = managed.drain_readiness_reports()
    by_source = {report.source: report for report in reports}
    assert len(reports) == 3, "one report per desired target, deferred included"
    assert by_source["fake:c01"].verdict == "deferred"
    assert by_source["fake:c01"].phase == "connector-up"
    assert by_source["fake:c00"].verdict == "ready"
    assert by_source["fake:c02"].verdict == "ready"
    running = sum(1 for row in managed.status() if row.get("running"))
    assert running == 2


def test_a_batch_of_only_deferred_targets_still_completes() -> None:
    """The all-deferred edge: no admitted start exists to drain the batch.

    Before F1 this emitted no ``HarnessRestoreCompleted`` at all -- the
    deferred loop skipped every key, ``queued`` stayed empty, and the
    pending set never drained.  The batch owes completion however it is
    dispositioned.
    """

    events: list[object] = []
    actor = HarnessRuntimeActor(_GateLauncher(gated="none"), event_sink=events.append)
    try:
        actor._lifecycle_effect_resources.update({"fake:c00", "fake:c01"})
        assert (
            actor.submit(
                RestoreHarnessesCommand(
                    "restore-all-deferred",
                    tuple(
                        HarnessLaunchProjection("fake", f"c{index:02d}", True)
                        for index in range(2)
                    ),
                )
            )
            is PortAdmission.ACCEPTED
        )

        def completed() -> bool:
            return any(
                isinstance(event, HarnessRestoreCompleted)
                and event.correlation_id == "restore-all-deferred"
                for event in events
            )

        _wait_for(completed, what="all-deferred batch completion")
        (event,) = [
            item
            for item in events
            if isinstance(item, HarnessRestoreCompleted)
            and item.correlation_id == "restore-all-deferred"
        ]
        assert (event.result.attempted, event.result.restored, event.result.failed) == (
            2,
            0,
            0,
        )
        assert event.result.deferred == 2
    finally:
        actor.close_runtime(2.0)


# --------------------------------------------------------------------------- #
# Door 2: the claim lands while the key sits in the batch queue.
# --------------------------------------------------------------------------- #


def test_queued_target_claimed_by_a_lifecycle_effect_settles_deferred(
    runtimes: list[ManagedHarnessRuntime],
) -> None:
    """Width 1: c01 is claimed while it waits in ``batch.queued``.

    ``_begin_start``'s lifecycle guard used to return silently for such a
    key -- out of ``queued``, still in ``pending``, no start, no report, no
    completion.  The claim's own saga owns the target now, so the round
    hands it over as deferred and never starts it here.
    """

    launcher = _GateLauncher(gated="c00")
    managed = ManagedHarnessRuntime(
        launcher,
        start_timeout_seconds=2.0,
        start_max_workers=1,
        restart_backoff_seconds=0.0,
    )
    runtimes.append(managed)

    result: dict[str, Any] = {}

    def run() -> None:
        try:
            result["summary"] = managed.restore(_specs(3))
        except BaseException as error:  # noqa: BLE001
            result["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        # c00 is admitted and blocked inside the launcher; c01 and c02 sit
        # in batch.queued.  Claim c01 exactly now -- before any settlement
        # can admit it.
        assert launcher.entered.wait(timeout=5.0), "gated start never began"
        managed._actor._lifecycle_effect_resources.add("fake:c01")
        launcher.release.set()

        thread.join(timeout=5.0)
        assert not thread.is_alive(), (
            "restore() still blocked: the queued-then-claimed target was "
            "stranded in batch.pending by _begin_start's silent return"
        )
        assert "error" not in result
        summary = result["summary"]
        assert (summary.attempted, summary.restored, summary.failed) == (3, 2, 0)
        assert summary.deferred == 1
        assert launcher.contacted == ["c00", "c02"], (
            "the lifecycle-claimed target must not be started by the "
            "restore round; the claiming saga starts it through the "
            "lifecycle path"
        )
        by_source = {
            report.source: report for report in managed.drain_readiness_reports()
        }
        assert by_source["fake:c01"].verdict == "deferred"
    finally:
        launcher.release.set()
        thread.join(timeout=5.0)


# --------------------------------------------------------------------------- #
# The settlement ceiling: derived, not the flat 45s constant U7 removed.
# --------------------------------------------------------------------------- #


def test_settlement_budget_is_derived_from_fleet_width_and_start_timeout(
    runtimes: list[ManagedHarnessRuntime],
) -> None:
    """``(ceil(targets / width) + 1) x start timeout`` -- and no ceiling when
    start timers are disabled (that is the operator's opt-out; no deadline
    is invented for them)."""

    managed = ManagedHarnessRuntime(
        _GateLauncher(gated="none"),
        start_timeout_seconds=2.0,
        start_max_workers=4,
    )
    runtimes.append(managed)
    budget = managed._actor.restore_settlement_budget

    assert budget(7) == 6.0  # ceil(7/4)=2 rounds, +1 margin, x2s
    assert budget(8) == 6.0
    assert budget(1) == 4.0
    assert budget(0) == 2.0  # an empty round still owes the margin round

    untimed = ManagedHarnessRuntime(
        _GateLauncher(gated="none"),
        start_timeout_seconds=0.0,
    )
    runtimes.append(untimed)
    assert untimed._actor.restore_settlement_budget(5) is None


def test_lost_settlement_times_out_within_the_derived_budget(
    runtimes: list[ManagedHarnessRuntime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backstop's whole job: a settlement that never arrives (a bug --
    after F1 every key has a disposition owner) surfaces as a
    ``TimeoutError`` feeding the daemon's degraded-start path, not as a
    gate that stays closed forever.

    The lost settlement is produced by dropping the start-timer command --
    the one signal that would have dispositioned the blocked start -- which
    is exactly the class of loss the backstop exists for.
    """

    launcher = _GateLauncher(gated="c00")
    managed = ManagedHarnessRuntime(
        launcher,
        start_timeout_seconds=0.5,
        start_max_workers=1,
        restart_backoff_seconds=0.0,
    )
    runtimes.append(managed)

    real_emit = managed._actor._emit_completion

    def drop_start_timers(command: object) -> None:
        from hyprial.daemon.impl.harnesses.runtime.ports import HarnessStartTimerElapsedCommand

        if isinstance(command, HarnessStartTimerElapsedCommand):
            return  # the settlement signal is lost
        real_emit(command)

    monkeypatch.setattr(managed._actor, "_emit_completion", drop_start_timers)

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        managed.restore((_specs(1)))
    elapsed = time.monotonic() - started

    # Derived budget: (ceil(1/1) + 1) x 0.5s = 1.0s.  Well under the old
    # 5s repro bound, and bounded at all -- which is the replacement for
    # the ceiling U7 deleted.
    assert elapsed < 3.0


def test_restore_wait_is_locally_bounded_when_start_timers_are_disabled(
    runtimes: list[ManagedHarnessRuntime],
) -> None:
    """Disabling effect timers does not permit an unbounded facade wait."""

    launcher = _GateLauncher(gated="c00")
    managed = ManagedHarnessRuntime(
        launcher,
        start_timeout_seconds=0.0,
        start_max_workers=1,
        restart_backoff_seconds=0.0,
    )
    managed._facade._reply_timeout = 0.05
    runtimes.append(managed)
    assert managed._actor.restore_settlement_budget(1) is None

    with pytest.raises(TimeoutError) as raised:
        managed.restore(_specs(1))
    receipt = raised.value.receipt
    assert receipt.targets == ("fake:c00",)

    launcher.release.set()
    deadline = time.monotonic() + 5.0
    while managed._facade.recover_restore(receipt) is None:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert managed._facade.recover_restore(receipt).restored == 1
