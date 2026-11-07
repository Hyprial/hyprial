"""Regression pins for unidentified twins and bounded durable writes."""
from __future__ import annotations

import json

from hyprial.kernel import ProcessLiveness, ProcessLivenessState
from hyprial.daemon.impl.processes.orphan_processes import OrphanProcessRegistry
from hyprial.daemon.impl.processes.process_owner import own_process


class Process:
    running = False
    pid = None

    def liveness(self):
        return ProcessLiveness(ProcessLivenessState.DEAD, observed=False)

    def stop(self):
        pass


def test_raw_owned_unidentified_share_custody_and_retire_together(tmp_path):
    path = tmp_path / "orphans.json"
    registry = OrphanProcessRegistry(path, identity_reader=lambda _: None)
    raw = Process()
    owner = own_process(raw)
    try:
        first = registry.observe_start("pi:worker", raw, pid=None, marker=None)
        second = registry.observe_start("pi:worker", owner, pid=None, marker=None)
        assert second == first
        assert registry.collect_once() == 0  # active raw twin cannot be collected
        assert registry.observe_stop("pi:worker", owner, pid=None, marker=None) == first
        assert registry.collect_once() == 1
        assert registry.status() == ()
        recovered = OrphanProcessRegistry(path, identity_reader=lambda _: None)
        assert recovered.status() == ()
    finally:
        assert owner.detach()


def test_identical_observation_storm_does_not_write(tmp_path, monkeypatch):
    import hyprial.daemon.impl.processes.orphan_processes as module
    writes = []
    original = module.atomic_json_write
    monkeypatch.setattr(module, "atomic_json_write",
                        lambda *args: (writes.append(1), original(*args))[1])
    path = tmp_path / "orphans.json"
    registry = OrphanProcessRegistry(path, identity_reader=lambda _: None)
    raw = Process()
    first = registry.observe_start("pi:worker", raw, pid=None, marker=None)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    count = len(writes)
    for _ in range(1000):
        assert registry.observe_start("pi:worker", raw, pid=None, marker=None) == first
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert len(writes) == count


def test_distinct_generations_use_incremental_writes_and_survive_restart(tmp_path, monkeypatch):
    import hyprial.daemon.impl.processes.orphan_processes as module

    writes = []
    original = module.atomic_json_write

    def counted(path, value):
        writes.append(len(json.dumps(value)))
        return original(path, value)

    monkeypatch.setattr(module, "atomic_json_write", counted)
    path = tmp_path / "orphans.json"
    registry = OrphanProcessRegistry(path, identity_reader=lambda _: None)
    for index in range(120):
        registry.observe_start(f"pi:worker-{index}", Process(), pid=None, marker=None)
    assert len(writes) < 5, "observe must not rewrite the whole registry"
    restored = OrphanProcessRegistry(path, identity_reader=lambda _: None)
    assert len(restored.status()) == 120


def test_collection_tick_is_bounded_and_fair(tmp_path):
    registry = OrphanProcessRegistry(
        tmp_path / "orphans.json", identity_reader=lambda _: None,
        collect_batch_size=2,
    )
    for index in range(5):
        registry.observe_stop(f"pi:worker-{index}", Process(), pid=None, marker=None)
    assert registry.collect_once() == 2
    assert registry.collect_once() == 2
    assert registry.collect_once() == 1


def test_unreachable_pidless_entry_expires_only_after_grace(tmp_path):
    now = [1000]
    path = tmp_path / "orphans.json"
    first = OrphanProcessRegistry(path, identity_reader=lambda _: None, clock_ms=lambda: now[0])
    first.observe_start("pi:worker", Process(), pid=None, marker=None)
    restored = OrphanProcessRegistry(path, identity_reader=lambda _: None, clock_ms=lambda: now[0])
    assert restored.collect_once() == 0
    now[0] += 61_000
    assert restored.collect_once() == 1
