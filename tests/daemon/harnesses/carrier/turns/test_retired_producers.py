"""Retired producer epochs cannot restore run state or acknowledge contact gaps."""

import pytest

from hyprial.daemon.impl.cells.observability.projection import RunLedger
from tests.daemon.harnesses.carrier.turns.test_turn_refresh import (
    A_URI,
    CAPABILITIES,
    SESSION,
    _app,
    _facts,
    _open_contact_gap,
    _published,
    _refresh,
    _register,
)


@pytest.mark.parametrize("seq", [0, 4, 5, 6, 1000])
@pytest.mark.parametrize("gap", [False, True])
def test_retired_epoch_is_rejected_even_with_a_higher_sequence(tmp_path, seq, gap):
    app = _app(tmp_path, {"now": 5000})
    try:
        _register(app)
        _refresh(app, producer_epoch="boot-A", seq=5,
                 open_runs=[{"reportId": "old-run", "phase": "executing"}])
        _refresh(app, producer_epoch="boot-B", seq=0)
        if gap:
            _open_contact_gap(app)
        answer = _refresh(app, producer_epoch="boot-A", seq=seq,
                          open_runs=[{"reportId": "old-run", "phase": "executing"}])
        assert answer["applied"] is False
        assert answer["rejectionReason"] == "retired_producer"
        assert answer["observationGapCleared"] is False
        assert answer["resyncCleared"] is False
        assert _facts(app).open_count == 0
        assert _published(app)["state"] == ("unknown" if gap else "idle")
        current = _refresh(app, producer_epoch="boot-B", seq=1)
        assert current["applied"] is True
        assert current["rejectionReason"] is None
        assert current["observationGapCleared"] is gap
        assert _published(app)["state"] == "idle"
    finally:
        app.close()


def test_epoch_history_saturation_fails_closed_until_mount_changes(tmp_path):
    app = _app(tmp_path, {"now": 5000})
    try:
        _register(app)
        _refresh(app, producer_epoch="boot-A", seq=5)
        ledger = app.app._carrier_turn_ledgers[(A_URI, SESSION)].observation
        ledger.producer_capacity = 2
        for boot in ("boot-B", "boot-C"):
            assert _refresh(app, producer_epoch=boot)["applied"] is True
        _open_contact_gap(app)
        for boot, reason in (
            ("boot-D", "producer_history_exhausted"),
            ("boot-A", "retired_producer"),
            ("boot-C", "producer_history_exhausted"),
            ("boot-E", "producer_history_exhausted"),
        ):
            answer = _refresh(app, producer_epoch=boot, seq=100,
                              open_runs=[{"reportId": "stale", "phase": "executing"}])
            assert answer["applied"] is False
            assert answer["rejectionReason"] == reason
            assert answer["observationGapCleared"] is False
            assert _facts(app).open_count == 0
            assert _published(app)["state"] == "unknown"
            assert len(ledger.retired_producers) == 2
        assert ledger.snapshot(ledger.epoch).resync_required is True
        fence = (*ledger.epoch, "new-mount")
        ledger.record(fence, "new", "start", at_ms=1, monotonic_ms=1)
        assert not ledger.retired_producers
        assert ledger.refresh(
            fence, producer_epoch="boot-A", seq=0, runs=[],
            capabilities=CAPABILITIES, at_ms=2, monotonic_ms=2,
        ) == (True, False)
        assert ledger.snapshot(fence).known is True
    finally:
        app.close()


def test_retired_epoch_does_not_clear_receipt_eviction_gap():
    ledger = RunLedger(capacity=1)
    def refresh(boot, seq=0):
        return ledger.refresh(
            "mount", producer_epoch=boot, seq=seq, runs=[],
            capabilities=CAPABILITIES, at_ms=1, monotonic_ms=1,
        )
    refresh("boot-A", 5)
    refresh("boot-B")
    ledger.record("mount", "r1", "start", at_ms=1, monotonic_ms=1)
    ledger.record("mount", "r2", "start", at_ms=2, monotonic_ms=2)
    assert refresh("boot-A", 6) == (False, False)
    assert ledger.snapshot("mount").resync_required is True
    assert refresh("boot-B", 1) == (True, True)
