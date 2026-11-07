"""E1e: reviewer replay probes plus fence ownership and restart regressions."""

import pytest

from hyprial.kernel import DaemonRequestError

from tests.daemon.harnesses.carrier.turns.test_turn_refresh import (
    A_URI,
    LEASE,
    SESSION,
    _app,
    _facts,
    _open_contact_gap,
    _published,
    _refresh,
    _register,
)


@pytest.mark.parametrize("source", ["pi-extension", "claude-channel"])
@pytest.mark.parametrize("operation", ["session.refresh", "session.heartbeat", "session.register"])
def test_retired_producer_stays_retired_across_same_mount_contact(tmp_path, operation, source):
    app = _app(tmp_path, {"now": 5000})
    try:
        lease = LEASE if source == "claude-channel" else None
        runtime = "claude_interactive" if lease else "pi_interactive"
        _register(app, source=source, runtime=runtime, lease=lease)
        _refresh(app, producer_epoch="boot-A", seq=5, lease=lease,
                 open_runs=[{"reportId": "old-run", "phase": "executing"}])
        _refresh(app, producer_epoch="boot-B", seq=0, lease=lease)
        fence = app.app._cell_binding_fence(A_URI, SESSION)
        _open_contact_gap(app)
        if operation == "session.register":
            _register(app, source=source, runtime=runtime, lease=lease)
        else:
            params = {"actor": A_URI, "sessionRef": SESSION}
            if lease:
                params["channelLeaseToken"] = lease
            app.app.handle(operation, params)
        assert app.app._cell_binding_fence(A_URI, SESSION) == fence
        assert app.app._agent_session_domains.session.read_runtime(A_URI).observation_gap
        answer = _refresh(app, producer_epoch="boot-A", seq=4, lease=lease,
                          open_runs=[{"reportId": "old-run", "phase": "executing"}])
        assert answer["applied"] is False, (answer, _published(app))
        assert answer["rejectionReason"] == "retired_producer"
        assert answer["observationGapCleared"] is False
        assert not _facts(app).known
    finally:
        app.close()


@pytest.mark.parametrize("source", ["pi-extension", "claude-channel"])
@pytest.mark.parametrize("saturated", [False, True])
@pytest.mark.parametrize("seq", [4, 5])
def test_same_mount_registration_preserves_sequence_and_saturation_fences(tmp_path, saturated, source, seq):
    app = _app(tmp_path, {"now": 5000})
    try:
        lease = LEASE if source == "claude-channel" else None
        runtime = "claude_interactive" if lease else "pi_interactive"
        _register(app, source=source, runtime=runtime, lease=lease)
        _refresh(app, producer_epoch="boot-A", seq=5, lease=lease)
        ledger = app.app._carrier_turn_ledgers[(A_URI, SESSION)].observation
        if saturated:
            ledger.producer_capacity = 1
            assert _refresh(app, producer_epoch="boot-B", seq=5, lease=lease)["applied"]
            assert not _refresh(app, producer_epoch="boot-C", lease=lease)["applied"]
            assert ledger.producer_history_exhausted
        fence = app.app._cell_binding_fence(A_URI, SESSION)
        _open_contact_gap(app)
        _register(app, source=source, runtime=runtime, lease=lease)
        assert app.app._cell_binding_fence(A_URI, SESSION) == fence
        assert app.app._agent_session_domains.session.read_runtime(A_URI).observation_gap
        answer = _refresh(app, producer_epoch="boot-B" if saturated else "boot-A", seq=seq, lease=lease,
                          open_runs=[{"reportId": "stale", "phase": "executing"}])
        assert not answer["applied"], (answer, _published(app))
        assert answer["rejectionReason"] == (
            "producer_history_exhausted" if saturated else "stale_sequence"
        )
        assert not answer["observationGapCleared"]
        assert _published(app)["state"] == "unknown"
    finally:
        app.close()


@pytest.mark.parametrize("reporting", [False, True])
def test_display_reset_preserves_observation_fence_and_gap(tmp_path, reporting):
    app = _app(tmp_path, {"now": 5000})
    try:
        _register(app)
        _refresh(app, producer_epoch="boot-A", seq=5)
        _refresh(app, producer_epoch="boot-B", seq=5)
        ledger = app.app._carrier_turn_ledgers[(A_URI, SESSION)].observation
        ledger.gap = True
        app.app._reset_interactive_turn_stats(A_URI, SESSION, reporting=reporting)
        answer = _refresh(app, producer_epoch="boot-A", seq=4)
        assert answer["rejectionReason"] == "retired_producer"
        assert not answer["resyncCleared"]
        assert _facts(app).resync_required
        assert app.app._carrier_turn_ledgers[(A_URI, SESSION)].observation is ledger
        assert _refresh(app, producer_epoch="boot-B", seq=6)["resyncCleared"]
    finally:
        app.close()


@pytest.mark.parametrize("source", ["pi-extension", "claude-channel"])
def test_daemon_restart_rejects_old_epoch_even_after_same_session_register(tmp_path, source):
    lease = LEASE if source == "claude-channel" else None
    runtime = "claude_interactive" if lease else "pi_interactive"
    app = _app(tmp_path, {"now": 5000})
    try:
        _register(app, source=source, runtime=runtime, lease=lease)
        _refresh(app, producer_epoch="boot-A", seq=5, lease=lease,
                 open_runs=[{"reportId": "old-run", "phase": "executing"}])
        old_epoch = app.app.epoch
    finally:
        app.close()
    app = _app(tmp_path, {"now": 6000})
    try:
        assert app.app.epoch != old_epoch
        unconfirmed = _refresh(app, producer_epoch="boot-A", seq=500, lease=lease)
        assert unconfirmed["rejectionReason"] == "stale_daemon_epoch"
        assert not unconfirmed["applied"]
        _register(app, source=source, runtime=runtime, lease=lease)
        _open_contact_gap(app)
        answer = _refresh(app, producer_epoch="boot-A", seq=4, lease=lease,
                          daemonEpoch=old_epoch,
                          open_runs=[{"reportId": "old-run", "phase": "executing"}])
        assert not answer["applied"]
        assert answer["rejectionReason"] == "stale_daemon_epoch"
        assert not answer["observationGapCleared"]
        assert _published(app)["state"] == "unknown"
        fresh = _refresh(app, producer_epoch="boot-A", seq=6, lease=lease)
        assert fresh["applied"] and fresh["observationGapCleared"]
        assert _published(app)["state"] == "idle"
        replay = _refresh(app, producer_epoch="boot-A", seq=500, lease=lease,
                          daemonEpoch=old_epoch,
                          open_runs=[{"reportId": "old-run", "phase": "executing"}])
        assert replay["rejectionReason"] == "stale_daemon_epoch"
        assert _published(app)["state"] == "idle"
    finally:
        app.close()


@pytest.mark.parametrize("session_ref", [SESSION, "stale-session"])
def test_unregister_cannot_delete_observation_fences(tmp_path, session_ref):
    app = _app(tmp_path, {"now": 5000})
    try:
        _register(app)
        _refresh(app, producer_epoch="boot-A", seq=5)
        _refresh(app, producer_epoch="boot-B", seq=5)
        ledger = app.app._carrier_turn_ledgers[(A_URI, SESSION)].observation
        ledger.gap = True
        old_fence = ledger.epoch
        app.app.handle("session.unregister", {"actor": A_URI, "sessionRef": session_ref})
        assert app.app._carrier_run_ledgers[A_URI] is ledger
        assert ledger.epoch == old_fence
        assert ledger.retired_producers == {"boot-A"}
        assert ledger.producer_seq == 5 and ledger.gap
        if session_ref == SESSION:
            with pytest.raises(DaemonRequestError):
                _refresh(app, producer_epoch="boot-A", seq=4)
            _register(app, session_ref="replacement")
            assert app.app._cell_binding_fence(A_URI, "replacement") != old_fence
            assert not _facts(app).known
            assert ledger.epoch == old_fence
            assert _refresh(app, session_ref="replacement")["applied"]
            assert ledger.epoch != old_fence
        else:
            assert _refresh(app, producer_epoch="boot-A", seq=4)["rejectionReason"] == "retired_producer"
            assert not _facts(app).known
    finally:
        app.close()


def test_only_identity_mount_advancement_emits_fence_audit(tmp_path, monkeypatch):
    app = _app(tmp_path, {"now": 5000})
    events = []
    original = app.app._log

    def observe(level, component, event, **fields):
        if event == "observation.mount.advanced":
            events.append(fields)
        original(level, component, event, **fields)

    monkeypatch.setattr(app.app, "_log", observe)
    try:
        _register(app)
        _refresh(app)
        _register(app)
        _refresh(app, seq=1)
        assert len(events) == 1
        assert set(events[0]) == {"actor", "bindingRevision", "daemonEpoch"}
        assert events[0]["daemonEpoch"] == app.app.epoch
        _register(app, session_ref="replacement")
        assert not _facts(app).known
        assert len(events) == 1
        _refresh(app, session_ref="replacement")
        assert len(events) == 2
        assert events[1]["bindingRevision"] > events[0]["bindingRevision"]
    finally:
        app.close()


def test_boundary_reports_after_stats_reset_cannot_clear_either_gap(tmp_path):
    app = _app(tmp_path, {"now": 5000})
    try:
        _register(app)
        _refresh(app, seq=5)
        _open_contact_gap(app)
        ledger = app.app._carrier_turn_ledgers[(A_URI, SESSION)].observation
        ledger.gap = True
        app.app._reset_interactive_turn_stats(A_URI, SESSION, reporting=True)
        app.started("boundary-run")
        app.ended(["boundary-run"])
        assert ledger.gap
        assert app.app._agent_session_domains.session.read_runtime(A_URI).observation_gap
        assert not _facts(app).known
        assert _refresh(app, seq=6)["observationGapCleared"]
        assert not ledger.gap
    finally:
        app.close()


def test_missing_daemon_epoch_cannot_acknowledge_gap(tmp_path):
    from tests.daemon.harnesses.carrier.turns.test_turn_refresh import CAPABILITIES

    app = _app(tmp_path, {"now": 5000})
    try:
        _register(app)
        _open_contact_gap(app)
        answer = app.app.handle("session.turn.refresh", {
            "actor": A_URI, "sessionRef": SESSION, "schemaVersion": 1,
            "producerEpoch": "boot-A", "seq": 0, "openRuns": [],
            "capabilities": CAPABILITIES,
        })
        assert answer["rejectionReason"] == "stale_daemon_epoch"
        assert not answer["applied"] and not answer["observationGapCleared"]
        assert not _facts(app).known
    finally:
        app.close()
