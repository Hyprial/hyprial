"""Registry-level backfill: a late-discovered pid must reach the custody entry.

Production signature this pins: async-spawn harnesses (pi rpc) are captured at
launch with ``unidentified`` custody (pid=None); once the handle exposes the
real pid, ``refresh_pid`` must backfill the entry so liveness probes and the
stop fence read a real PID instead of an unanswerable UNKNOWN.
"""

from __future__ import annotations

from types import SimpleNamespace

from hyprial.daemon.impl.processes.orphan_processes import OrphanProcessRegistry


def test_refresh_pid_backfills_an_unidentified_entry(tmp_path) -> None:
    registry = OrphanProcessRegistry(
        tmp_path / "orphan-processes.json", identity_reader=lambda _pid: None
    )
    process = SimpleNamespace(running=True, pid=None)
    registry.observe_start("pi:worker", process, pid=None, marker=None)

    # Owned (current-daemon) entries are live custody, not orphans: the status
    # listing deliberately hides them.
    assert registry.status() == ()

    registry.refresh_pid("pi:worker", process, pid=4242, marker="m")

    # The backfilled pid becomes visible the moment custody is handed off
    # (observe_stop) -- the stop fence and probes now read a real PID.
    registry.observe_stop("pi:worker", process, pid=None, marker=None)
    [row] = registry.status()
    assert row["pid"] == 4242
    assert row["marker"] == "m"


def test_refresh_pid_ignores_unknown_processes_and_known_pids(tmp_path) -> None:
    registry = OrphanProcessRegistry(
        tmp_path / "orphan-processes.json", identity_reader=lambda _pid: None
    )
    known = SimpleNamespace(running=True, pid=111)
    registry.observe_start("pi:known", known, pid=111, marker="origin")

    # An unknown handle: nothing to backfill, no entry created, no crash.
    stranger = SimpleNamespace(running=True, pid=222)
    registry.refresh_pid("pi:stranger", stranger, pid=222, marker="m")

    # A known handle whose pid is already recorded: refresh must not rewrite.
    registry.refresh_pid("pi:known", known, pid=999, marker="later")

    registry.observe_stop("pi:known", known, pid=None, marker=None)
    [row] = registry.status()
    assert row["pid"] == 111
    assert row["marker"] == "origin"


def test_authority_refresh_pid_end_to_end(tmp_path) -> None:
    """The production composition (Authority lane) must backfill too.

    Pins the exact bug the #1370 verdict caught: the authority's
    ``refresh-pid`` dispatch dropped ``harness_id`` on the floor, so the
    production path raised TypeError while the direct-registry path (what a
    registry-injected test sees) silently worked.
    """

    from hyprial.daemon.impl.processes.orphan_processes import OrphanProcessAuthority

    authority = OrphanProcessAuthority(
        tmp_path / "orphan-processes.json", identity_reader=lambda _pid: None
    )
    process = SimpleNamespace(running=True, pid=None)
    authority.observe_start("pi:worker", process, pid=None, marker=None)
    authority.refresh_pid("pi:worker", process, pid=4242, marker="m")
    authority.observe_stop("pi:worker", process, pid=None, marker=None)
    [row] = authority.status()
    assert row["pid"] == 4242
    assert row["marker"] == "m"
    assert authority.close(5.0)
