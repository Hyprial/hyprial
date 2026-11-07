"""Upgrade and repeated-lifecycle regressions from the independent custody review."""
from dataclasses import asdict
import json
import weakref

import pytest

from hyprial.daemon.impl.processes.orphan.models import _OrphanEntry
from hyprial.daemon.impl.processes.orphan_processes import OrphanProcessRegistry
from hyprial.daemon.impl.processes.storage.journal import CustodyJournal
from hyprial.kernel import ProcessLiveness, ProcessLivenessState, atomic_json_write


class Alive:
    def liveness(self):
        return ProcessLiveness(
            ProcessLivenessState.ALIVE, observed=True, pid=111, marker="birth"
        )

    def stop(self):
        pass


class Handle:
    pass


def row(index, *, harness=None, legacy=False, **fields):
    harness = harness or f"pi:worker-{index}"
    key = f"{harness}:unidentified"
    if legacy:
        key += f":old-owner:{index}"
    return asdict(_OrphanEntry(
        orphan_id=key, harness_id=harness, pid=None, marker=None,
        observed=False, first_seen_ms=index + 1, owner_id="old-owner",
    )) | fields


def store(path, rows, source):
    if source == "snapshot":
        path.write_text(json.dumps({
            "schema": "hyprial.orphan-processes/v1", "orphans": rows,
        }))
    else:
        if source == "snapshot+journal":
            store(path, [], "snapshot")
        # Also exercise journal-only recovery (no snapshot yet).
        path.with_suffix(".json.jsonl").write_text(json.dumps({
            "seq": 1, "upsert": rows, "delete": [],
        }) + "\n")


def persisted(path):
    return CustodyJournal(path, atomic_json_write).rows


def test_self_evicted_handle_can_be_observed_again(tmp_path):
    path = tmp_path / "orphans.json"
    registry = OrphanProcessRegistry(
        path, identity_reader=lambda _: "birth", max_entries=1, cleanup_async=False
    )
    old = Alive()
    registry.observe_stop("pi:old", old, pid=111, marker="birth")
    registry.collect_once()
    assert registry.status()[0]["state"] == "alive"
    for index in range(100):
        new = Handle()
        ref = weakref.ref(new)
        assert registry.observe_start(f"pi:new-{index}", new, pid=None, marker=None) == ""
        assert registry.observe_stop(f"pi:new-{index}", new, pid=None, marker=None) == ""
        assert set(registry._processes) == set(registry._entries)
        assert set(registry._entry_keys) == set(registry._entries)
        assert registry._process_keys == {id(old): "pi:old:111:birth"}
        del new
        assert ref() is None
    assert set(persisted(path)) == {"pi:old:111:birth"}


@pytest.mark.parametrize("source", ["snapshot", "journal", "snapshot+journal"])
def test_restore_enforces_capacity_and_persists_deletions(tmp_path, source):
    path = tmp_path / "orphans.json"
    rows = [row(i, state="alive" if i in (0, 2, 4) else "unknown") for i in range(5)]
    store(path, rows, source)
    registry = OrphanProcessRegistry(
        path, identity_reader=lambda _: None, max_entries=2, clock_ms=lambda: 1000,
    )
    expected = {rows[2]["orphan_id"], rows[4]["orphan_id"]}
    assert set(registry._entries) == expected
    registry.collect_once()  # unidentified entries remain inside their grace period
    assert len(registry.status()) == 2
    assert set(persisted(path)) == expected
    assert all(item["owner_id"] is None for item in persisted(path).values())
    # Reopening without a cap must not resurrect the deleted rows.
    reopened = OrphanProcessRegistry(path, identity_reader=lambda _: None)
    assert set(reopened._entries) == expected


@pytest.mark.parametrize("source", ["snapshot", "journal", "snapshot+journal"])
def test_restore_coalesces_legacy_unidentified_keys(tmp_path, source):
    path = tmp_path / "orphans.json"
    rows = [row(i, harness="pi:worker", legacy=True) for i in range(5)]
    store(path, rows, source)
    registry = OrphanProcessRegistry(path, identity_reader=lambda _: None, clock_ms=lambda: 1000)
    assert len(registry.status()) == 1
    registry.observe_stop("pi:worker", object(), pid=None, marker=None)
    assert len(registry.status()) == 1
    assert set(persisted(path)) == {"pi:worker:unidentified"}
    assert registry.status()[0]["firstSeenAtMs"] == 1
    assert len(OrphanProcessRegistry(path, identity_reader=lambda _: None).status()) == 1


@pytest.mark.parametrize("source", ["snapshot", "journal", "snapshot+journal"])
def test_legacy_merge_preserves_known_identity_and_custody(tmp_path, source):
    path = tmp_path / "orphans.json"
    rows = [row(0, harness="pi:worker", legacy=True), row(
        1, harness="pi:worker", legacy=True, pid=111, marker="birth", observed=True,
        orphan_since_ms=10, last_probe_ms=20, gc_attempts=7,
        state="probe-failed", probe_failures=3, next_probe_ms=3000,
        last_probe_error="denied", detail="denied",
    )]
    store(path, rows, source)
    registry = OrphanProcessRegistry(path, identity_reader=lambda _: None, clock_ms=lambda: 1000)
    registry.observe_stop("pi:worker", object(), pid=None, marker=None)
    assert len(registry.status()) == 1
    entry = registry._entries["pi:worker:unidentified"]
    assert (entry.pid, entry.marker, entry.observed) == (111, "birth", True)
    assert (entry.first_seen_ms, entry.orphan_since_ms, entry.owner_id) == (1, 10, None)
    assert (entry.gc_attempts, entry.probe_failures, entry.next_probe_ms) == (7, 3, 3000)
    assert (entry.state, entry.last_probe_ms, entry.last_probe_error) == ("probe-failed", 20, "denied")
    assert set(persisted(path)) == {entry.orphan_id}


@pytest.mark.parametrize("source", ["snapshot", "journal", "snapshot+journal"])
def test_known_observation_reuses_restored_harness_key(tmp_path, source):
    path = tmp_path / "orphans.json"
    store(path, [row(0, harness="pi:worker", legacy=True, pid=111, marker="birth")], source)
    registry = OrphanProcessRegistry(path, identity_reader=lambda _: None)
    handle = Handle()
    assert registry.observe_start("pi:worker", handle, pid=111, marker="birth") == "pi:worker:unidentified"
    registry.observe_stop("pi:worker", handle, pid=111, marker="birth")
    assert len(registry.status()) == 1
    assert set(persisted(path)) == {"pi:worker:unidentified"}


def test_conflicting_legacy_identities_are_not_discarded(tmp_path):
    path = tmp_path / "orphans.json"
    rows = [row(i, harness="pi:worker", legacy=True, pid=111 + i, marker="birth")
            for i in range(2)]
    store(path, rows, "snapshot")
    registry = OrphanProcessRegistry(path, identity_reader=lambda _: None)
    assert {(r["pid"], r["marker"]) for r in registry.status()} == {(111, "birth"), (112, "birth")}
    assert not set(persisted(path)).intersection(r["orphan_id"] for r in rows)


def test_unknown_legacy_probe_cannot_erase_known_live_custody(tmp_path):
    path = tmp_path / "orphans.json"
    rows = [
        row(0, harness="pi:worker", pid=111, marker="birth", state="alive", last_probe_ms=10),
        row(1, harness="pi:worker", legacy=True, last_probe_ms=20),
        row(2),
    ]
    store(path, rows, "snapshot")
    registry = OrphanProcessRegistry(path, identity_reader=lambda _: None, max_entries=1)
    assert [(item["harnessId"], item["pid"], item["state"]) for item in registry.status()] == [
        ("pi:worker", 111, "alive")
    ]
    assert set(persisted(path)) == {"pi:worker:unidentified"}


def test_conflicting_partial_identities_are_not_combined(tmp_path):
    path = tmp_path / "orphans.json"
    rows = [
        row(0, harness="pi:worker", legacy=True, pid=111),
        row(1, harness="pi:worker", legacy=True, marker="birth"),
        row(2, harness="pi:worker", legacy=True),
    ]
    store(path, rows, "snapshot")
    registry = OrphanProcessRegistry(path, identity_reader=lambda _: None)
    assert {(r["pid"], r["marker"]) for r in registry.status()} == {
        (111, None), (None, "birth"), (None, None),
    }
    assert not set(persisted(path)).intersection(r["orphan_id"] for r in rows)
    assert len(OrphanProcessRegistry(path, identity_reader=lambda _: None).status()) == 3


def test_retired_harness_tombstones_are_bounded_and_refresh_recency():
    registry = OrphanProcessRegistry(identity_reader=lambda _: None, max_entries=2)
    for index in range(100):
        registry.retire_harness(f"pi:gone-{index}")
        assert len(registry._retired_harnesses) <= 2
    assert set(registry._retired_harnesses) == {"pi:gone-98", "pi:gone-99"}
    registry.retire_harness("pi:gone-98")
    registry.retire_harness("pi:gone-100")
    assert set(registry._retired_harnesses) == {"pi:gone-98", "pi:gone-100"}
    registry.observe_stop("pi:gone-98", Handle(), pid=None, marker=None)
    assert registry.collect_once() == 1
    registry.observe_start("pi:gone-100", Handle(), pid=None, marker=None)
    assert "pi:gone-100" not in registry._retired_harnesses
