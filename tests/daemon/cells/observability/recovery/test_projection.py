"""Bounded diagnostic receipts, not a turn or message authority."""
from dataclasses import replace

from hyprial.daemon.impl.cells.observability.model import RunFacts
from hyprial.daemon.impl.cells.observability.projection import RunLedger, RunProjection
from hyprial.daemon.impl.cells.observability.policy import RunPolicy


def test_duplicate_and_end_before_start_and_eviction():
    ledger = RunLedger(capacity=2)
    ledger.record("e", "a", "start", at_ms=10, monotonic_ms=20)
    ledger.record("e", "a", "start", at_ms=30, monotonic_ms=40)
    facts = ledger.snapshot("e")
    assert facts.open_count == 1 and facts.since_ms == 10 and facts.since_monotonic_ms == 20
    ledger.record("e", "b", "end", at_ms=50, monotonic_ms=60)
    ledger.record("e", "b", "start", at_ms=70, monotonic_ms=80)
    assert ledger.snapshot("e").open_count == 1
    ledger.record("e", "a", "end", at_ms=90, monotonic_ms=100)
    assert ledger.snapshot("e").known and ledger.snapshot("e").open_count == 0
    ledger.record("e", "c", "end", at_ms=110, monotonic_ms=120)
    assert not ledger.snapshot("e").known
    ledger.record("e", "a", "start", at_ms=130, monotonic_ms=140)
    assert not ledger.snapshot("e").known  # Evicted receipts never recreate idle/busy certainty.
    row = RunProjection().publish({"a": ("cell", ledger.snapshot("e"), RunPolicy())},
                                  now_ms=150, monotonic_ms=150)["a"]
    assert row["state"] == "unknown" and row["observationHealth"] == "resync-required"


def test_old_binding_read_and_restart_require_current_evidence():
    ledger = RunLedger()
    ledger.record("old", "a", "start", at_ms=1, monotonic_ms=1)
    assert not ledger.snapshot("new").known
    ledger.record("new", "b", "start", at_ms=2, monotonic_ms=2)
    assert not ledger.snapshot("old").known
    assert ledger.snapshot("new").open_count == 1
    assert not RunLedger().snapshot("new").known


def test_projection_epoch_revision_shared_cut_and_unknown_after_rebind():
    projection = RunProjection(capacity=2)
    facts = RunFacts(source_epoch="e", known=True, running=True, open_count=1,
                     since_ms=1, since_monotonic_ms=1)
    rows = projection.publish({"a": ("cell-a", facts, RunPolicy()),
                               "b": ("cell-b", facts, RunPolicy())}, now_ms=2, monotonic_ms=2)
    assert rows["a"]["revision"] == rows["b"]["revision"]
    newer = projection.publish({"a": ("cell-a", replace(facts, source_epoch="new", known=False), RunPolicy())},
                               now_ms=3, monotonic_ms=3)["a"]
    assert newer["state"] == "unknown" and newer["turnAgeMs"] is None
    assert newer["runtimeIncarnation"] != rows["a"]["runtimeIncarnation"]
    restarted = RunProjection().publish({"a": ("cell-a", RunFacts(), RunPolicy())}, now_ms=4, monotonic_ms=4)["a"]
    assert restarted["epoch"] != rows["a"]["epoch"]
    assert restarted["state"] == "unknown"


def test_unavailable_keeps_tagged_last_evidence_not_a_current_claim():
    projection = RunProjection()
    first = projection.publish({"a": ("cell", RunFacts(source_epoch="e", known=True, running=True), RunPolicy())},
                               now_ms=1, monotonic_ms=1)["a"]
    lost = projection.publish({"a": ("cell", RunFacts(source_epoch="e", contact_current=False), RunPolicy())},
                              now_ms=2, monotonic_ms=2)["a"]
    assert lost["state"] == "unknown" and lost["sinceMs"] is None
    assert lost["lastKnown"]["state"] == "idle"
    assert lost["lastKnown"]["runtimeIncarnation"] == first["runtimeIncarnation"]
    assert lost["lastKnown"]["observedAtMs"] == 1


def test_snapshot_does_not_invent_an_idle_transition_time():
    projection = RunProjection()
    row = projection.publish({"a": ("cell", RunFacts(source_epoch="e", known=True, running=True), RunPolicy())},
                             now_ms=50, monotonic_ms=50)["a"]
    assert row["state"] == "idle" and row["sinceMs"] is None
