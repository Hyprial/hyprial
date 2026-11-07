"""Executable E1 current-run refresh contract.

``RunLedger.refresh`` is the daemon-side boundary of ``session.turn.refresh``:
a session-fenced, authoritative current open-run set with producer sequence
ordering. It adds no business event and no durable authority. The second
vector set pins the contact-gap acknowledgement owned by the session actor.
"""

from hyprial.daemon.impl.cells.observability.projection import RunLedger
from hyprial.daemon.impl.session_actor.internals import _SessionRuntimeState

FENCE = ("agent:o:n:a", "entity", 1, "session-1")
CAPABILITIES = {
    "wholeSessionTurns": True,
    "preparation": True,
    "permissionWait": False,
    "producerActivity": False,
}


def _refresh(
    ledger: RunLedger,
    *,
    runs=(),
    seq: int = 0,
    producer_epoch: str = "boot-1",
    capabilities=None,
    at: int = 1_000,
    fence=FENCE,
):
    return ledger.refresh(
        fence,
        producer_epoch=producer_epoch,
        seq=seq,
        runs=list(runs),
        capabilities=dict(capabilities or CAPABILITIES),
        at_ms=at,
        monotonic_ms=at,
    )


def exercise_turn_refresh() -> int:
    cases = 0

    ledger = RunLedger()
    assert ledger.snapshot(FENCE).known is False
    applied, cleared = _refresh(ledger)
    facts = ledger.snapshot(FENCE)
    assert applied is True and cleared is False
    assert (facts.known, facts.running, facts.open_count) == (True, True, 0)
    assert (facts.since_ms, facts.since_monotonic_ms) == (None, None)
    cases += 1

    ledger = RunLedger()
    _refresh(ledger, runs=[("r1", "executing", 400)], at=1_000)
    facts = ledger.snapshot(FENCE)
    assert (facts.open_count, facts.since_ms, facts.since_monotonic_ms) == (1, 600, 600)
    # A second refresh keeps the first known instant, even without elapsed.
    _refresh(ledger, runs=[("r1", "executing", None)], seq=1, at=5_000)
    facts = ledger.snapshot(FENCE)
    assert (facts.open_count, facts.since_ms) == (1, 600)
    cases += 1

    ledger = RunLedger()
    _refresh(ledger, runs=[("p1", "prepared", None)])
    facts = ledger.snapshot(FENCE)
    assert (facts.open_count, facts.since_ms, facts.since_monotonic_ms) == (1, None, None)
    cases += 1

    ledger = RunLedger()
    ledger.record(FENCE, "stale", "start", at_ms=10, monotonic_ms=10)
    assert ledger.snapshot(FENCE).open_count == 1
    _refresh(ledger, runs=[], seq=1, at=2_000)
    facts = ledger.snapshot(FENCE)
    assert (facts.open_count, facts.known) == (0, True)
    assert "stale" not in ledger.rows
    cases += 1

    ledger = RunLedger()
    ledger.record(FENCE, "ended", "start", at_ms=10, monotonic_ms=10)
    ledger.record(FENCE, "ended", "end", at_ms=20, monotonic_ms=20)
    _refresh(ledger, runs=[("ended", "executing", 5)], seq=1)
    assert ledger.snapshot(FENCE).open_count == 0
    cases += 1

    ledger = RunLedger()
    assert _refresh(ledger, runs=[("r1", "executing", None)], seq=5) == (True, False)
    assert _refresh(ledger, seq=5) == (False, False)
    assert ledger.snapshot(FENCE).open_count == 1
    assert _refresh(ledger, seq=4) == (False, False)
    assert ledger.snapshot(FENCE).open_count == 1
    assert _refresh(ledger, seq=6) == (True, False)
    assert ledger.snapshot(FENCE).open_count == 0
    assert _refresh(
        ledger, producer_epoch="boot-2", seq=0, runs=[("r2", "executing", None)]
    ) == (True, False)
    assert ledger.snapshot(FENCE).open_count == 1
    cases += 1

    ledger = RunLedger(capacity=2)
    for index in range(3):
        ledger.record(FENCE, f"r{index}", "start", at_ms=index, monotonic_ms=index)
    assert ledger.snapshot(FENCE).resync_required is True
    applied, cleared = _refresh(ledger, runs=[("r9", "executing", 10)], seq=1, at=1_000)
    facts = ledger.snapshot(FENCE)
    assert (applied, cleared) == (True, True)
    assert facts.resync_required is False and facts.open_count == 1
    cases += 1

    ledger = RunLedger()
    _refresh(ledger, runs=[("r1", "executing", 100)], at=1_000)
    other = ("agent:o:n:a", "entity", 2, "session-1")
    facts = ledger.snapshot(other)
    assert facts.known is False and facts.open_count == 0
    cases += 1

    ledger = RunLedger()
    _refresh(
        ledger,
        runs=[("r1", "executing", 0)],
        capabilities={**CAPABILITIES, "wholeSessionTurns": False},
    )
    facts = ledger.snapshot(FENCE)
    assert facts.open_count == 1
    assert ledger.capabilities["wholeSessionTurns"] is False
    cases += 1

    ledger = RunLedger()
    _refresh(ledger, producer_epoch="boot-A", seq=5,
             runs=[("old-run", "executing", None)])
    _refresh(ledger, producer_epoch="boot-B")
    for seq in (4, 5, 6):
        assert _refresh(ledger, producer_epoch="boot-A", seq=seq,
                        runs=[("old-run", "executing", None)]) == (False, False)
        assert ledger.refresh_rejection_reason == "retired_producer"
        assert ledger.snapshot(FENCE).open_count == 0
    cases += 1

    ledger = RunLedger(producer_capacity=1)
    _refresh(ledger, producer_epoch="boot-A")
    _refresh(ledger, producer_epoch="boot-B")
    assert _refresh(ledger, producer_epoch="boot-C") == (False, False)
    assert ledger.refresh_rejection_reason == "producer_history_exhausted"
    assert not ledger.snapshot(FENCE).known
    assert ledger.snapshot(FENCE).resync_required
    assert _refresh(ledger, producer_epoch="boot-B", seq=1) == (False, False)
    assert _refresh(ledger, producer_epoch="boot-A", seq=10) == (False, False)
    assert ledger.refresh_rejection_reason == "retired_producer"
    assert ledger.retired_producers == {"boot-A"}
    assert _refresh(ledger, fence=(*FENCE, "replacement")) == (True, False)
    assert not ledger.retired_producers
    cases += 1

    ledger = RunLedger(producer_capacity=1)
    _refresh(ledger, producer_epoch="boot-A", seq=5)
    _refresh(ledger, producer_epoch="boot-B", seq=5)
    _refresh(ledger, producer_epoch="boot-C")
    ledger.advance_mount(FENCE)
    assert ledger.producer_seq == 5
    assert ledger.retired_producers == {"boot-A"}
    assert ledger.producer_history_exhausted and ledger.gap
    assert _refresh(ledger, producer_epoch="boot-A", seq=6) == (False, False)
    assert ledger.refresh_rejection_reason == "retired_producer"
    assert _refresh(ledger, producer_epoch="boot-B", seq=6) == (False, False)
    assert ledger.refresh_rejection_reason == "producer_history_exhausted"
    cases += 1

    return cases


def exercise_contact_gap_acknowledgement() -> int:
    """Only a current-state refresh may clear a recovered contact gap."""

    owner = _SessionRuntimeState("daemon", lease_ttl_seconds=1)
    try:
        cases = 0
        owner.register("actor", "session", 0, confirmed=True)
        owner.register("actor", "session", 2_000, confirmed=True)
        assert owner.read("actor", "session", now_ms=2_000, ttl_seconds=1).observation_gap
        gap_revision = owner.read(
            "actor", "session", now_ms=2_000, ttl_seconds=1
        ).observation_gap_revision
        owner.register("actor", "session", 2_100, confirmed=True)
        assert owner.read("actor", "session", now_ms=2_100, ttl_seconds=1).observation_gap
        assert owner.acknowledge_observation_gap(
            "actor", "session", now_ms=2_100, ttl_seconds=1,
            gap_revision=gap_revision,
        )
        assert not owner.read("actor", "session", now_ms=2_100, ttl_seconds=1).observation_gap
        cases += 1

        # No gap, a superseded session or stale contact never clears anything.
        assert not owner.acknowledge_observation_gap(
            "actor", "session", now_ms=2_100, ttl_seconds=1,
            gap_revision=gap_revision,
        )
        owner.register("actor", "session", 5_000, confirmed=True)
        assert owner.read("actor", "session", now_ms=5_000, ttl_seconds=1).observation_gap
        assert not owner.acknowledge_observation_gap(
            "actor", "session", now_ms=5_000, ttl_seconds=1,
            gap_revision=gap_revision,
        )
        gap_revision = owner.read(
            "actor", "session", now_ms=5_000, ttl_seconds=1
        ).observation_gap_revision
        cases += 1
        assert not owner.acknowledge_observation_gap(
            "actor", "session", now_ms=9_000, ttl_seconds=1,
            gap_revision=gap_revision,
        )
        assert owner.read("actor", "session", now_ms=9_000, ttl_seconds=1).observation_gap
        assert not owner.acknowledge_observation_gap(
            "actor", "other-session", now_ms=5_000, ttl_seconds=1,
            gap_revision=gap_revision,
        )
        assert owner.read("actor", "session", now_ms=5_000, ttl_seconds=1).observation_gap
        cases += 1

        # A replacement session starts without the old session's gap.
        owner.register("actor", "replacement", 5_100, confirmed=True)
        assert not owner.read(
            "actor", "replacement", now_ms=5_100, ttl_seconds=1
        ).observation_gap
        assert not owner.acknowledge_observation_gap(
            "actor", "session", now_ms=5_100, ttl_seconds=1,
            gap_revision=gap_revision,
        )
        cases += 1
        return cases
    finally:
        owner.lifecycle_observations.close()
