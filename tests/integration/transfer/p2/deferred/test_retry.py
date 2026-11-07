"""Recovery uses newer custody evidence and preserves activity policy."""
from dataclasses import replace

import pytest

from tests.integration.actor_preservation.lifecycle.test_actor_preserved_restore_threshold_states import (
    HOUR_MS, _app, _close,
)


@pytest.mark.parametrize("age, suppressed", [(HOUR_MS, False), (17 * HOUR_MS, True)])
def test_query_recovery_reclassifies_without_bypassing_activity_policy(tmp_path, monkeypatch, age, suppressed):
    app, spec = _app(tmp_path, now_ms=2_000_000_000_000, age_ms=age)
    try:
        original = app._transfer_p2._store.journal.custody_snapshot
        def fail(*_):
            raise OSError("temporary journal failure")
        monkeypatch.setattr(app._transfer_p2._store.journal, "custody_snapshot", fail)
        assert app._classify_restore_specs((spec,)) == ()
        previous = app._harnesses.eligibility[-1]
        assert previous.suppressed
        monkeypatch.setattr(app._transfer_p2._store.journal, "custody_snapshot", original)
        app._restore_wake_scan(0)
        latest = app._harnesses.eligibility[-1]
        assert latest.source_version > previous.source_version, "deferred source was never reconsidered"
        assert latest.suppressed is suppressed
        # The scan publishes eligibility; it does not introduce another launcher.
        assert not app._harnesses.started
    finally:
        _close(app)


def test_retry_is_bounded_and_never_unsuppresses_unavailable(tmp_path, monkeypatch):
    app, spec = _app(tmp_path, now_ms=2_000_000_000_000, age_ms=HOUR_MS)
    reads = []
    try:
        def fail(*_):
            reads.append(1)
            raise OSError("persistent journal failure")
        monkeypatch.setattr(app._transfer_p2._store.journal, "custody_snapshot", fail)
        assert app._classify_restore_specs((spec,)) == ()
        for _ in range(8):
            app._restore_wake_scan(0)
        assert len(reads) == 4, "initial read plus three retries must be the bound"
        assert all(e.suppressed for e in app._harnesses.eligibility)
    finally:
        _close(app)


def test_stale_observation_cannot_release_deferred_entity(tmp_path, monkeypatch):
    app, spec = _app(tmp_path, now_ms=2_000_000_000_000, age_ms=HOUR_MS)
    try:
        token = app.agents.require("worker").entity_token
        fact = app._transfer_p2.hydrate_custody("worker", expected_entity_token=token).result(5)
        monkeypatch.setattr(app, "_hydrate_transfer_source", lambda *_a, **_k: None)
        assert app._classify_restore_specs((spec,)) == ()
        monkeypatch.setattr(app, "_hydrate_transfer_source", lambda *_a, **_k: replace(fact, state="clear"))
        before = len(app._harnesses.eligibility)
        app._restore_wake_scan(0)
        assert len(app._harnesses.eligibility) == before
        assert app._harnesses.eligibility[-1].suppressed
    finally:
        _close(app)


def test_fleet_budget_is_shared_and_timed_out_read_is_not_resubmitted(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hyprial.kernel import HarnessLaunchSpec
    from hyprial.daemon.impl.application.actors import transfer_p2
    app, spec = _app(tmp_path, now_ms=2_000_000_000_000, age_ms=HOUR_MS)
    now, waits, submissions = [0.0], [], []
    class Pending:
        def result(self, timeout):
            waits.append(timeout)
            now[0] += timeout
            raise TimeoutError("deliberately unresolved read")
        def done(self):
            return False
    try:
        app.agents.create("other")
        other = HarnessLaunchSpec("codex", "other", True)
        app.desired_state.upsert_harness(other)
        monkeypatch.setattr(transfer_p2, "time", SimpleNamespace(monotonic=lambda: now[0]))
        def submit(actor, **_):
            submissions.append(actor)
            return Pending()
        monkeypatch.setattr(app._transfer_p2, "hydrate_custody", submit)
        assert app._filter_transfer_restore_specs((spec, other)) == ()
        assert waits == [10.0], "the second entity received a fresh per-entity budget"
        assert submissions == ["worker"]
        # A later cadence has another bounded observer budget, but must attach
        # to the already accepted read rather than submit it a second time.
        token = app.agents.require("worker").entity_token
        assert app._hydrate_transfer_source("worker", token) is None
        assert waits == [10.0, 10.0]
        assert submissions == ["worker"]
    finally:
        _close(app)


def test_changed_spec_is_not_released_by_old_deferred_evidence(tmp_path, monkeypatch):
    app, spec = _app(tmp_path, now_ms=2_000_000_000_000, age_ms=HOUR_MS)
    try:
        monkeypatch.setattr(app, "_hydrate_transfer_source", lambda *_a, **_k: None)
        assert app._classify_restore_specs((spec,)) == ()
        app.desired_state.upsert_harness(replace(spec, cwd=str(tmp_path)))
        before = len(app._harnesses.eligibility)
        app._restore_wake_scan(0)
        assert len(app._harnesses.eligibility) == before
        assert not app._transfer_deferred
    finally:
        _close(app)


def test_retry_bounds_are_declared():
    import json
    from pathlib import Path
    from hyprial.daemon.impl.application.actors.transfer_p2 import (
        SOURCE_CUSTODY_RETRIES, SOURCE_CUSTODY_RETRY_BATCH, SOURCE_CUSTODY_DEFERRED_CAPACITY,
    )
    bounds = json.loads(Path("contract/daemon-lifecycle/p2-source-bounds.json").read_text())
    assert bounds["source_custody_retry_attempts"] == SOURCE_CUSTODY_RETRIES == 3
    assert bounds["source_custody_retry_batch"] == SOURCE_CUSTODY_RETRY_BATCH == 32
    assert bounds["source_custody_deferred_capacity"] == SOURCE_CUSTODY_DEFERRED_CAPACITY == 10000


def test_accepted_wake_is_polled_without_resubmission(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hyprial.kernel import PortAdmission
    from hyprial.daemon.impl.lifecycle_manager import LifecycleState
    app, spec = _app(tmp_path, now_ms=2_000_000_000_000, age_ms=HOUR_MS)
    operations, waits = [], []
    complete = [False]
    class Manager:
        def submit(self, operation):
            operations.append(operation)
            return PortAdmission.ACCEPTED
        def wait(self, operation_id, timeout):
            waits.append((operation_id, timeout))
            if not complete[0]:
                raise TimeoutError("accepted start still pending")
            return SimpleNamespace(state=LifecycleState.COMPLETED)
    try:
        original = app._transfer_p2._store.journal.custody_snapshot
        def fail(*_):
            raise OSError("temporary query fault")
        monkeypatch.setattr(app._transfer_p2._store.journal, "custody_snapshot", fail)
        assert app._classify_restore_specs((spec,)) == ()
        monkeypatch.setattr(app._transfer_p2._store.journal, "custody_snapshot", original)
        app._lifecycle_manager = Manager()
        app._restore_wake_scan(0)
        assert len(operations) == 1
        assert operations[0].operation_id.startswith("resume-wake:")
        assert operations[0].target.expected_entity_token == app.agents.require("worker").entity_token
        for _ in range(4):
            app._restore_wake_scan(0)
        assert len(operations) == 1, "pending lifecycle effect was resubmitted"
        assert waits == [(operations[0].operation_id, 0)] * 4
        assert app._transfer_deferred
        complete[0] = True
        app._restore_wake_scan(0)
        assert not app._transfer_deferred
    finally:
        app._lifecycle_manager = None
        _close(app)
