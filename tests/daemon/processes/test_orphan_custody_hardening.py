"""Residual orphan-custody hardening: backoff, capacity, dedup, convergence.

Each test pins one residual defect from the 2026-10-16 orphan mint/retire
investigation that ``837226cd7`` did not cover. The production symptom was
unbounded ``unidentified`` growth with probe attempts in the tens of thousands.
"""

from __future__ import annotations

from pathlib import Path

from hyprial.kernel import ProcessLiveness, ProcessLivenessState
from hyprial.daemon.impl.processes.orphan_processes import OrphanProcessRegistry


class _Process:
    """A handle that reports an unknown-but-observed identity."""

    def __init__(self, pid: int = 4242, marker: str | None = "birth") -> None:
        self._pid = pid
        self._marker = marker

    def liveness(self) -> ProcessLiveness:
        return ProcessLiveness(
            ProcessLivenessState.UNKNOWN,
            observed=True,
            pid=self._pid,
            marker=self._marker,
        )


def _denied(_pid: int) -> str | None:
    raise PermissionError("denied")


def test_probe_failures_back_off_exponentially_with_a_cap(tmp_path: Path) -> None:
    now = [0]
    registry = OrphanProcessRegistry(
        tmp_path / "orphans.json",
        identity_reader=_denied,
        clock_ms=lambda: now[0],
        probe_backoff_base_ms=1000,
        probe_backoff_cap_ms=8000,
    )
    registry.observe_stop("worker", _Process(), pid=4242, marker="birth")

    def attempts() -> int:
        return int(registry.status()[0]["gcAttempts"])

    assert registry.collect_once() == 0  # probe 1 fails at t=0
    assert attempts() == 1

    # (arrival time of each following probe, attempts after it). The delay
    # doubles 1s, 2s, 4s then stays at the 8s cap.
    schedule = ((999, 1), (1000, 2), (2999, 2), (3000, 3), (6999, 3),
                (7000, 4), (14999, 4), (15000, 5), (23000, 6))
    for at, expected in schedule:
        now[0] = at
        assert registry.collect_once() == 0
        assert attempts() == expected, at


def test_registry_capacity_evicts_oldest_and_logs(tmp_path: Path) -> None:
    events: list[tuple[str, str, str, dict[str, object]]] = []

    def log(level: str, component: str, event: str, **fields: object) -> None:
        events.append((level, component, event, fields))

    now = [0]
    path = tmp_path / "orphans.json"
    handles: list[object] = []
    registry = OrphanProcessRegistry(
        path,
        identity_reader=lambda _pid: None,
        clock_ms=lambda: now[0],
        max_entries=3,
        logger=log,
    )
    for index in range(5):
        handles.append(object())
        registry.observe_stop(
            f"pi:worker-{index}", handles[-1], pid=None, marker=None
        )
        now[0] += 1

    remaining = {row["harnessId"] for row in registry.status()}
    assert remaining == {"pi:worker-2", "pi:worker-3", "pi:worker-4"}
    assert sum(
        event == "harness.orphan.capacity_evicted" for _l, _c, event, _f in events
    ) == 2
    restored = OrphanProcessRegistry(path, identity_reader=lambda _pid: None)
    assert {row["harnessId"] for row in restored.status()} == remaining


def test_one_unidentified_entry_per_harness_id(tmp_path: Path) -> None:
    registry = OrphanProcessRegistry(
        tmp_path / "orphans.json", identity_reader=lambda _pid: None
    )
    handles = [object(), object(), object()]
    first = registry.observe_stop("pi:worker", handles[0], pid=None, marker=None)
    second = registry.observe_stop("pi:worker", handles[1], pid=None, marker=None)
    assert second == first
    assert len(registry.status()) == 1

    other = registry.observe_stop("pi:other", handles[2], pid=None, marker=None)
    assert other != first
    assert len(registry.status()) == 2


def test_disappeared_harness_entries_do_not_resurrect(tmp_path: Path) -> None:
    path = tmp_path / "orphans.json"
    registry = OrphanProcessRegistry(path, identity_reader=lambda _pid: None)
    handles = [object(), object()]
    registry.observe_stop("pi:worker", handles[0], pid=None, marker=None)
    registry.retire_harness("pi:worker")
    assert registry.status() == ()

    # The harness is gone but a late observation mints a fresh entry.
    registry.observe_stop("pi:worker", handles[1], pid=None, marker=None)
    assert len(registry.status()) == 1
    assert registry.collect_once() == 1
    assert registry.status() == ()
    assert OrphanProcessRegistry(path, identity_reader=lambda _pid: None).status() == ()


def test_confirmed_remove_signals_orphan_retirement(tmp_path: Path) -> None:
    from hyprial.kernel import HarnessLaunchSpec
    from hyprial.daemon.impl import ManagedHarnessRuntime

    class Process:
        running = True
        pid = 909090

        def stop(self) -> None:
            self.running = False

    class Launcher:
        def start(self, _spec: HarnessLaunchSpec) -> Process:
            return Process()

    supervisor = ManagedHarnessRuntime(
        Launcher(), orphan_state_path=tmp_path / "orphans.json"
    )
    try:
        assert supervisor.start(HarnessLaunchSpec("test", "worker", True))
        assert supervisor.remove("test", "worker")
        # The remove signal marks the harness gone; a late observation for it
        # is pruned by the next collection pass.
        authority = supervisor._actor._orphan_processes
        late = object()
        authority.observe_stop("test:worker", late, pid=None, marker=None)
        assert len(authority.status()) == 1
        assert authority.collect_once() == 1
        assert authority.status() == ()
    finally:
        try:
            supervisor.stop()
        except Exception:
            pass


def test_defaults_are_the_documented_bounds(tmp_path: Path) -> None:
    """The effective bounds must match the documented defaults."""

    registry = OrphanProcessRegistry(
        tmp_path / "orphans.json", identity_reader=lambda _pid: None
    )
    assert registry._max_entries == 4096
    assert registry._probe_backoff_base_ms == 1000
    assert registry._probe_backoff_cap_ms == 900_000


class _AliveProcess:
    """A handle that reports a live, identity-matching process."""

    def __init__(self, pid: int = 111, marker: str = "birth") -> None:
        self._pid = pid
        self._marker = marker

    def liveness(self) -> ProcessLiveness:
        return ProcessLiveness(
            ProcessLivenessState.ALIVE,
            observed=True,
            pid=self._pid,
            marker=self._marker,
        )

    def stop(self) -> None:
        pass


def test_capacity_evicts_non_live_before_an_older_live_entry(tmp_path: Path) -> None:
    """Eviction order must prefer non-live entries over older live ones."""

    now = [0]
    registry = OrphanProcessRegistry(
        tmp_path / "orphans.json",
        identity_reader=lambda _pid: "birth",
        clock_ms=lambda: now[0],
        max_entries=1,
        cleanup_async=False,
    )
    alive = _AliveProcess(pid=111, marker="birth")
    registry.observe_stop("pi:old", alive, pid=111, marker="birth")
    now[0] = 1
    assert registry.collect_once() == 0
    assert registry.status()[0]["state"] == ProcessLivenessState.ALIVE.value

    now[0] = 2
    registry.observe_stop("pi:new", _Process(pid=222), pid=222, marker="birth")
    # The older live entry survives; the newer non-live entry is evicted first.
    assert {row["harnessId"] for row in registry.status()} == {"pi:old"}


def test_unidentified_key_is_harness_scoped(tmp_path: Path) -> None:
    registry = OrphanProcessRegistry(
        tmp_path / "orphans.json", identity_reader=lambda _pid: None
    )
    assert (
        registry.observe_stop("pi:worker", object(), pid=None, marker=None)
        == "pi:worker:unidentified"
    )
