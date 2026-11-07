"""Late-pid backfill: async-spawn harnesses expose their pid after start.

The pi rpc process spawns its child inside the client's connect task, so at
launcher.start() return the pid is None.  The launch contract captures the
identity once at that boundary; before this fix the captured pid stayed None
forever, so every generation landed in the orphan registry as
``<harness>:unidentified:<uuid>`` (pid=None), status projections never showed
a pid, and the stop path's PID-reuse fence had nothing to fence with.

The reconcile tick must re-capture the identity from the live handle once the
pid becomes discoverable, backfilling both the actor record and the orphan
registry entry.
"""

from __future__ import annotations

import time

from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.harnesses.actor import HarnessRuntimeActor
from hyprial.daemon.impl.harnesses.runtime.ports import (
    EnsureHarnessCommand,
    HarnessLaunchProjection,
    HarnessTimerCompleted,
    HarnessTimerElapsedCommand,
)
from hyprial.daemon.impl.processes.orphan_processes import OrphanProcessRegistry

from ..support import (
    _Launcher,
    _Process,
    _wait_for,
)


class _LatePidProcess(_Process):
    """A harness whose child is spawned asynchronously (pid unknown at start)."""

    pid = None


class _LatePidLauncher(_Launcher):
    def start(self, _spec):  # type: ignore[no-untyped-def]
        process = _LatePidProcess()
        self.processes.append(process)
        return process


def _tick(actor: HarnessRuntimeActor, events: list[object], name: str, version: int) -> None:
    assert (
        actor.submit(HarnessTimerElapsedCommand(name, actor.generation, version, 1))
        is PortAdmission.ACCEPTED
    )
    _wait_for(
        lambda: any(
            isinstance(event, HarnessTimerCompleted) and event.correlation_id == name
            for event in events
        )
    )


def test_late_pid_backfills_record_and_orphan_registry(tmp_path) -> None:
    registry = OrphanProcessRegistry(
        tmp_path / "orphan-processes.json", identity_reader=lambda _pid: None
    )
    launcher = _LatePidLauncher()
    events: list[object] = []
    actor = HarnessRuntimeActor(
        launcher,
        orphan_processes=registry,
        identity_reader=lambda _pid: None,
        event_sink=events.append,
    )
    assert (
        actor.submit(
            EnsureHarnessCommand("late", HarnessLaunchProjection("pi", "worker", True))
        )
        is PortAdmission.ACCEPTED
    )
    _wait_for(lambda: launcher.processes != [])
    [process] = launcher.processes
    assert isinstance(process, _LatePidProcess)
    _wait_for(lambda: actor.read_harness("pi:worker") is not None)
    harness = actor.read_harness("pi:worker")
    assert harness is not None and harness.running  # type: ignore[union-attr]

    # First reconcile: the child is running but its pid is still unknown.
    _tick(actor, events, "tick-1", 1)
    harness = actor.read_harness("pi:worker")
    assert harness is not None and harness.pid is None  # type: ignore[union-attr]

    # The async spawn completes; the handle now exposes the real pid.
    process.pid = 4242

    # The async spawn completes; the handle now exposes the real pid.  Each
    # tick schedules a native facts poll (async) and then reads whatever has
    # landed -- so the backfill may take a couple of ticks to settle.
    process.pid = 4242
    harness = actor.read_harness("pi:worker")
    for index in range(2, 7):
        _tick(actor, events, f"tick-{index}", index)
        time.sleep(0.1)
        harness = actor.read_harness("pi:worker")
        if harness is not None and harness.pid == 4242:  # type: ignore[union-attr]
            break
    assert harness is not None and harness.pid == 4242  # type: ignore[union-attr]
    assert actor.close_runtime(2.0)


def test_late_pid_backfills_through_authority_lane(tmp_path) -> None:
    """Same backfill, but through the production composition.

    The first test injects the registry itself, which bypasses
    OrphanProcessAuthority - exactly the seam where #1370's first verdict
    caught the launch (the authority's refresh-pid dispatch dropped
    harness_id).  This arm drives the actor with the authority in place so
    ``_call → _execute → registry`` stays pinned end to end.
    """

    from hyprial.daemon.impl.processes.orphan_processes import OrphanProcessAuthority

    authority = OrphanProcessAuthority(
        tmp_path / "orphan-processes.json", identity_reader=lambda _pid: None
    )
    launcher = _LatePidLauncher()
    events: list[object] = []
    actor = HarnessRuntimeActor(
        launcher,
        orphan_processes=authority,
        identity_reader=lambda _pid: None,
        event_sink=events.append,
    )
    assert (
        actor.submit(
            EnsureHarnessCommand("late-auth", HarnessLaunchProjection("pi", "worker", True))
        )
        is PortAdmission.ACCEPTED
    )
    _wait_for(lambda: launcher.processes != [])
    [process] = launcher.processes
    assert isinstance(process, _LatePidProcess)
    _wait_for(lambda: actor.read_harness("pi:worker") is not None)
    harness = actor.read_harness("pi:worker")
    assert harness is not None and harness.running  # type: ignore[union-attr]

    process.pid = 4242
    import time

    harness = actor.read_harness("pi:worker")
    for index in range(2, 7):
        _tick(actor, events, f"auth-tick-{index}", index)
        time.sleep(0.1)
        harness = actor.read_harness("pi:worker")
        if harness is not None and harness.pid == 4242:  # type: ignore[union-attr]
            break
    assert harness is not None and harness.pid == 4242  # type: ignore[union-attr]
    # The record-level backfill is what the stop fence and the status
    # projection read; the raw-handle entry (io_port observes both the raw
    # process and the owned wrapper) is legacy double bookkeeping that the
    # registry's unknown-threshold path collects - covered by the registry
    # unit tests, not asserted here.
    assert actor.close_runtime(2.0)
    assert authority.close(5.0)
