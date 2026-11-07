from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl import ManagedHarnessRuntime
from hyprial.daemon.impl.api import (
    ProcessLivenessProbeError,
)
from hyprial.kernel import ProcessLiveness, ProcessLivenessState
from hyprial.daemon.impl.composition import HarnessPortClient
from hyprial.daemon.impl.harnesses.actor.contracts import HarnessRuntimeClosed, _default_identity_reader
from hyprial.daemon.impl.processes.orphan_processes import OrphanProcessRegistry
from hyprial.daemon.impl.harnesses.codex.process import _OwnedProcessGroup
from hyprial.daemon.impl.harnesses.protocol.common import PtyHarnessProcess


def _observed_group(*, pid: int = 4242, marker: str | None) -> _OwnedProcessGroup:
    group = _OwnedProcessGroup()
    with group._lock:
        group._observed = True
        group._pid = pid
        group._identity = marker
    return group


def test_production_harness_facade_keeps_orphan_gc_wiring() -> None:
    assert callable(getattr(HarnessPortClient, "collect_orphans", None))
    assert callable(getattr(HarnessPortClient, "orphan_status", None))


def test_unknown_orphan_is_retained_retried_listed_and_aged() -> None:
    now = [1_000]
    registry = OrphanProcessRegistry(
        identity_reader=lambda _pid: None,
        clock_ms=lambda: now[0],
        unknown_threshold_seconds=1.0,
    )
    process = _observed_group(marker=None)
    registry.observe_stop("worker", process, pid=4242, marker=None)

    assert registry.collect_once() == 0
    now[0] = 2_100
    assert registry.collect_once() == 0

    assert registry.status() == (
        {
            "orphanId": registry.status()[0]["orphanId"],
            "harnessId": "worker",
            "state": ProcessLivenessState.UNKNOWN.value,
            "observed": True,
            "pid": 4242,
            "marker": None,
            "firstSeenAtMs": 1_000,
            "orphanSinceMs": 1_000,
            "lastProbeAtMs": 2_100,
            "gcAttempts": 2,
            "unknownSinceMs": 1_000,
            "unknownForMs": 1_100,
            "unknownOverThreshold": True,
            "detail": "process identity criterion is unavailable",
        },
    )


def test_never_observed_orphan_is_unknown_and_retained() -> None:
    registry = OrphanProcessRegistry(identity_reader=lambda _pid: None)
    process = _OwnedProcessGroup()
    registry.observe_stop("worker", process, pid=None, marker=None)

    assert registry.collect_once() == 0

    row = registry.status()[0]
    assert row["state"] == ProcessLivenessState.UNKNOWN.value
    assert row["observed"] is False
    assert row["gcAttempts"] == 1


def test_current_daemon_ownership_is_hidden_until_the_next_daemon_inherits_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "orphan-processes.json"
    current = OrphanProcessRegistry(path, identity_reader=lambda _pid: None)
    process = _observed_group(marker=None)
    current.observe_start("worker", process, pid=4242, marker=None)

    assert current.status() == ()

    restarted = OrphanProcessRegistry(path, identity_reader=lambda _pid: None)
    assert len(restarted.status()) == 1
    assert restarted.status()[0]["harnessId"] == "worker"
    assert restarted.status()[0]["state"] == ProcessLivenessState.UNKNOWN.value


def test_steady_state_collection_does_not_probe_current_daemon_processes() -> None:
    """The one-second tick must not rescan every healthy managed process."""

    probes = 0

    class CurrentProcess:
        def liveness(self) -> ProcessLiveness:
            nonlocal probes
            probes += 1
            return ProcessLiveness(
                ProcessLivenessState.ALIVE,
                observed=True,
                pid=4_000,
                marker="birth",
            )

    registry = OrphanProcessRegistry(identity_reader=lambda _pid: "birth")
    for index in range(100):
        registry.observe_start(
            f"worker-{index}", CurrentProcess(), pid=4_000 + index, marker="birth"
        )

    for _ in range(10):
        assert registry.collect_once() == 0

    assert probes == 0


def test_only_confirmed_dead_orphan_is_retired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = OrphanProcessRegistry(identity_reader=lambda _pid: "birth")
    process = _observed_group(marker="birth")
    registry.observe_stop("worker", process, pid=4242, marker="birth")
    monkeypatch.setattr(
        os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()),
    )

    assert registry.collect_once() == 1
    assert registry.status() == ()


def test_probe_failure_is_retained_and_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, str, str, dict[str, object]]] = []

    def log(level: str, component: str, event: str, **fields: object) -> None:
        events.append((level, component, event, fields))

    registry = OrphanProcessRegistry(
        identity_reader=lambda _pid: "birth",
        logger=log,
    )
    process = _observed_group(marker="birth")
    registry.observe_stop("worker", process, pid=4242, marker="birth")
    monkeypatch.setattr(
        os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(PermissionError("denied")),
    )

    assert registry.collect_once() == 0

    row = registry.status()[0]
    assert row["state"] == "probe-failed"
    assert row["gcAttempts"] == 1
    assert "denied" in str(row["lastProbeError"])
    assert events[0][0:3] == ("error", "daemon", "harness.orphan.probe_failed")


def test_cleanup_identity_race_is_visible_and_never_signals_reused_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "orphan-processes.json"
    original = OrphanProcessRegistry(path, identity_reader=lambda _pid: "birth")
    original.observe_start(
        "worker",
        _observed_group(marker="birth"),
        pid=4242,
        marker="birth",
    )
    identity_reads = 0

    def identity_reader(_pid: int) -> str:
        nonlocal identity_reads
        identity_reads += 1
        return "birth" if identity_reads == 1 else "reused"

    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    events: list[tuple[str, str, str, dict[str, object]]] = []

    def log(level: str, component: str, event: str, **fields: object) -> None:
        events.append((level, component, event, fields))

    restarted = OrphanProcessRegistry(
        path,
        identity_reader=identity_reader,
        logger=log,
    )
    assert restarted.collect_once() == 0
    deadline = time.monotonic() + 1.0
    while restarted.status()[0]["state"] != "cleanup-failed":
        assert time.monotonic() < deadline
        time.sleep(0.01)

    row = restarted.status()[0]
    assert signals == []
    assert row["state"] == "cleanup-failed"
    assert row["gcAttempts"] == 2
    assert "identity changed before cleanup" in str(row["lastProbeError"])
    assert events[-1][0:3] == ("error", "daemon", "harness.orphan.cleanup_failed")


def test_corrupt_persistent_registry_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "orphan-processes.json"
    path.write_text(json.dumps({"schema": "wrong", "orphans": []}), encoding="utf-8")

    with pytest.raises(ProcessLivenessProbeError, match="invalid schema"):
        OrphanProcessRegistry(path, identity_reader=lambda _pid: None)


def test_actor_takes_persistent_custody_before_real_process_stop_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = PtyHarnessProcess.spawn(
        "test",
        [sys.executable, "-c", "import time; time.sleep(30)"],
        startup_probe_seconds=0.02,
        stop_grace_seconds=0.2,
    )
    original_stop = process.stop

    class Launcher:
        def start(self, _spec: HarnessLaunchSpec) -> PtyHarnessProcess:
            return process

    supervisor = ManagedHarnessRuntime(
        Launcher(),
        orphan_state_path=tmp_path / "orphan-processes.json",
    )
    try:
        assert supervisor.start(HarnessLaunchSpec("test", "worker", True))
        registered = json.loads(
            (tmp_path / "orphan-processes.json").read_text(encoding="utf-8")
        )
        assert len(registered["orphans"]) == 1
        assert registered["orphans"][0]["owner_id"] is not None
        assert supervisor.orphan_status() == ()
        monkeypatch.setattr(
            process,
            "stop",
            lambda: (_ for _ in ()).throw(RuntimeError("blocked stop")),
        )

        with pytest.raises(RuntimeError, match="blocked stop"):
            supervisor.remove("test", "worker")

        from hyprial.daemon.impl.processes.storage.journal import CustodyJournal
        from hyprial.kernel import atomic_json_write
        persisted = {"orphans": list(CustodyJournal(
            tmp_path / "orphan-processes.json", atomic_json_write
        ).rows.values())}
        assert len(persisted["orphans"]) == 1
        assert persisted["orphans"][0]["harness_id"] == "test:worker"
        assert persisted["orphans"][0]["owner_id"] is None
        assert persisted["orphans"][0]["orphan_since_ms"] is not None
        assert len(supervisor.orphan_status()) == 1
    finally:
        monkeypatch.setattr(process, "stop", original_stop)
        original_stop()
        try:
            supervisor.stop()
        except HarnessRuntimeClosed:
            pass
        assert process._child.stderr is not None
        process._child.stderr.close()


def test_persistent_collector_does_not_accumulate_or_kill_live_controls_across_restarts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "orphan-processes.json"
    control = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    readings: list[tuple[int, int, int, bool]] = []
    control_group = _OwnedProcessGroup()
    control_group.register(control.pid)
    try:
        control_marker = _default_identity_reader(control.pid)
        assert control_marker is not None
        for index in range(3):
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                start_new_session=True,
            )
            driver_cleanup_count = 0
            try:
                marker = _default_identity_reader(child.pid)
                assert marker is not None
                before_restart = OrphanProcessRegistry(
                    path, identity_reader=_default_identity_reader
                )
                before_restart.observe_start(
                    "control", control_group, pid=control.pid, marker=control_marker
                )
                before_restart.observe_start(
                    f"worker-{index}", child, pid=child.pid, marker=marker
                )
                assert before_restart.status() == ()

                after_restart = OrphanProcessRegistry(
                    path, identity_reader=_default_identity_reader
                )
                after_restart.observe_start(
                    "control", control_group, pid=control.pid, marker=control_marker
                )
                assert len(after_restart.status()) == 1
                assert after_restart.collect_once() == 0
                deadline = time.monotonic() + 3.0
                while child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)

                orphan_count_before_driver_cleanup = int(child.poll() is None)
                control_alive_from_os = _pid_exists(control.pid)
                if orphan_count_before_driver_cleanup:
                    os.killpg(child.pid, 9)
                    child.wait(timeout=3.0)
                    driver_cleanup_count = 1

                next_restart = OrphanProcessRegistry(
                    path, identity_reader=_default_identity_reader
                )
                next_restart.observe_start(
                    "control", control_group, pid=control.pid, marker=control_marker
                )
                next_restart.collect_once()
                readings.append(
                    (
                        len(next_restart.status()),
                        orphan_count_before_driver_cleanup,
                        driver_cleanup_count,
                        control_alive_from_os,
                    )
                )
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, 9)
                    child.wait(timeout=3.0)
        assert readings == [(0, 0, 0, True)] * 3
    finally:
        if control.poll() is None:
            os.killpg(control.pid, 9)
            control.wait(timeout=3.0)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_unprobeable_inherited_orphans_are_retired_once_and_loudly(
    tmp_path: Path,
) -> None:
    """An inherited ownerless entry with no live handle and no pid+marker can
    never be observed.  Production 0.5.0 kept 336 such pi entries since 09-18,
    re-probing each every second (gcAttempts up to 652894).  They are retired
    after a conservative grace period, with a warning naming the entry."""

    path = tmp_path / "orphan-processes.json"
    original = OrphanProcessRegistry(path, identity_reader=lambda _pid: None)
    original.observe_stop("never-observed", _OwnedProcessGroup(), pid=None, marker=None)
    original.observe_stop(
        "pid-only", _observed_group(pid=4242, marker=None), pid=4242, marker=None
    )
    logs: list[tuple[str, str]] = []
    restarted = OrphanProcessRegistry(
        path,
        identity_reader=lambda _pid: None,
        unknown_threshold_seconds=0,
        logger=lambda level, _component, event, **_fields: logs.append((level, event)),
    )
    assert len(restarted.status()) == 2

    assert restarted.collect_once() == 2

    assert restarted.status() == ()
    assert ("warn", "harness.orphan.unprobeable_retired") in logs
    assert len([event for _level, event in logs if event == "harness.orphan.unprobeable_retired"]) == 2
    # Durable: the next daemon does not inherit them again.
    assert OrphanProcessRegistry(path, identity_reader=lambda _pid: None).status() == ()


def test_collection_writes_the_registry_once_per_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One durable write per collection pass, not one per entry: each write is
    a full atomic JSON rewrite plus fsync (the 2026-10-05 production hotspot)."""

    path = tmp_path / "orphan-processes.json"
    original = OrphanProcessRegistry(path, identity_reader=lambda _pid: "birth")
    for index in range(5):
        original.observe_stop(
            f"worker-{index}",
            _observed_group(pid=4_000_000 + index, marker="birth"),
            pid=4_000_000 + index,
            marker="birth",
        )
    # A restarted daemon inherits them without handles; the pids do not exist,
    # so each probe sees DEAD and retires it.
    restarted = OrphanProcessRegistry(path, identity_reader=lambda _pid: None)
    saves = []
    original_save = restarted._save_locked
    monkeypatch.setattr(
        restarted, "_save_locked", lambda *keys: (saves.append(1), original_save(*keys))[1]
    )

    assert restarted.collect_once() == 5
    assert len(saves) == 1
    assert OrphanProcessRegistry(path, identity_reader=lambda _pid: None).status() == ()


def test_retirement_records_why_the_process_died(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retirement must carry the managed process's own last error.

    ``harness.orphan.retired`` otherwise says only that the probe observed
    ``dead``; without the child's last error a reader cannot tell a harness
    that crashed from one that never launched, and the whole failure shows up
    in the log as a bare retirement.
    """

    events: list[dict[str, object]] = []
    registry = OrphanProcessRegistry(
        identity_reader=lambda _pid: "birth",
        logger=lambda _level, _component, event, **fields: events.append(
            {"event": event, **fields}
        ),
    )
    process = _observed_group(marker="birth")
    process.last_error = "pi RPC process exited with status 137"
    registry.observe_stop("worker", process, pid=4242, marker="birth")
    monkeypatch.setattr(
        os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()),
    )

    assert registry.collect_once() == 1
    retired = [e for e in events if e["event"] == "harness.orphan.retired"]
    assert len(retired) == 1
    entry = retired[0]
    assert entry["processError"] == "pi RPC process exited with status 137"
    assert entry["state"] == ProcessLivenessState.DEAD.value
    assert entry["pidKnown"] is True
    assert entry["markerKnown"] is True
    assert isinstance(entry["lifetimeMs"], int)


def test_retirement_marks_a_process_without_a_reported_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counterexample: no process error must stay null, never a guess."""

    events: list[dict[str, object]] = []
    registry = OrphanProcessRegistry(
        identity_reader=lambda _pid: "birth",
        logger=lambda _level, _component, event, **fields: events.append(
            {"event": event, **fields}
        ),
    )
    process = _observed_group(marker="birth")
    registry.observe_stop("worker", process, pid=4242, marker="birth")
    monkeypatch.setattr(
        os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()),
    )

    assert registry.collect_once() == 1
    retired = [e for e in events if e["event"] == "harness.orphan.retired"]
    assert retired and retired[0]["processError"] is None


# --- 2026-10-09 production storm: component-wise identity, not raw strings ---
#
# Production logged `harness.orphan.probe_failed` once a second for ~24.8h
# (83,865 events, 99.7% one `dsh` process) because the stored marker predated a
# new component and was compared to the live marker with `!=`. The process had
# never died. These are the positive and negative controls for that comparison.

#: The exact shapes measured in production: the live marker carries
#: `darwin-startsec`, the stored one does not.
_STORM_STORED = (
    "darwin-starttime:1791435913:62590;ps-lstart:Thu Oct  8 05:05:13 2026"
)
_STORM_LIVE = (
    "darwin-starttime:1791435913:62590;darwin-startsec:1791435913;"
    "ps-lstart:Thu Oct  8 05:05:13 2026"
)
#: A genuine pid reuse: a shared fine component differs, and the coarse
#: second differs by exactly one, so the +/-1s tolerance must NOT rescue it.
_REUSED_STORED = (
    "darwin-starttime:1791435913:62590;darwin-startsec:1791435913;"
    "ps-lstart:Thu Oct  8 05:05:13 2026"
)
_REUSED_LIVE = (
    "darwin-starttime:1791435913:62591;darwin-startsec:1791435914;"
    "ps-lstart:Thu Oct  8 05:05:14 2026"
)


def test_stored_marker_missing_a_component_matches_the_live_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POSITIVE CONTROL: stored has fewer components, live has more.

    A shared finer component (`darwin-starttime`) must still match, so the
    record is the same live process: no `probe_failed`, and orphan GC proceeds
    to signal it.
    """

    path = tmp_path / "orphan-processes.json"
    original = OrphanProcessRegistry(path, identity_reader=lambda _pid: _STORM_STORED)
    original.observe_stop(
        "dsh:worker",
        _observed_group(pid=19238, marker=_STORM_STORED),
        pid=19238,
        marker=_STORM_STORED,
    )

    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    events: list[tuple[str, str]] = []

    restarted = OrphanProcessRegistry(
        path,
        identity_reader=lambda _pid: _STORM_LIVE,
        logger=lambda level, _component, event, **_fields: events.append((level, event)),
    )

    for _ in range(5):
        restarted.collect_once()

    assert not [event for _level, event in events if event == "harness.orphan.probe_failed"]
    assert restarted.status()[0]["state"] == ProcessLivenessState.ALIVE.value
    deadline = time.monotonic() + 1.0
    while not signals and time.monotonic() < deadline:
        time.sleep(0.01)
    assert signals[0] == (19238, signal.SIGTERM)


def test_pid_reuse_retires_the_record_instead_of_probing_forever(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NEGATIVE CONTROL: a shared fine component differs => pid reuse.

    The record is retired once, loudly, and never re-probed: no `probe_failed`
    and no `harness.orphan.retired`-by-mistake signalling of the new pid.
    """

    path = tmp_path / "orphan-processes.json"
    original = OrphanProcessRegistry(path, identity_reader=lambda _pid: _REUSED_STORED)
    original.observe_stop(
        "dsh:worker",
        _observed_group(pid=19238, marker=_REUSED_STORED),
        pid=19238,
        marker=_REUSED_STORED,
    )

    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    events: list[tuple[str, str]] = []

    restarted = OrphanProcessRegistry(
        path,
        identity_reader=lambda _pid: _REUSED_LIVE,
        logger=lambda level, _component, event, **_fields: events.append((level, event)),
    )

    assert restarted.collect_once() == 1
    assert restarted.status() == ()
    assert signals == []
    assert [event for _level, event in events if event == "harness.orphan.probe_failed"] == []
    assert ("warn", "harness.orphan.identity_replaced") in events

    # Terminal: a later pass does not resurrect it.
    assert restarted.collect_once() == 0
    assert OrphanProcessRegistry(path, identity_reader=lambda _pid: _REUSED_LIVE).status() == ()


def test_component_identity_match_semantics_and_the_second_tolerance() -> None:
    """Direct controls for the shared comparator itself."""

    from hyprial.daemon.impl.mcp.channel.ownership import _process_identities_match

    # Positive: fewer stored components, more live ones, one shared equal.
    assert _process_identities_match(_STORM_STORED, _STORM_LIVE)
    # Negative: a shared fine component differs.
    assert not _process_identities_match(_REUSED_STORED, _REUSED_LIVE)
    assert not _process_identities_match(
        "darwin-starttime:1000:1", "darwin-starttime:1000:2"
    )
    # The +/-1s tolerance applies ONLY when the coarse second is the sole
    # shared component; a differing finer component must not be rescued.
    assert _process_identities_match("darwin-startsec:1000", "darwin-startsec:1001")
    assert not _process_identities_match(
        "darwin-starttime:1000:1;darwin-startsec:1000",
        "darwin-starttime:1000:2;darwin-startsec:1001",
    )
