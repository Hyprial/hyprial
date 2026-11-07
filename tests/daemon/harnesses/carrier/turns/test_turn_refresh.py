"""E1: a session-fenced current-run refresh replaces stale turn observation.

``session.turn.refresh`` is observation only.  It is fenced exactly like the
turn boundary reports, it never publishes ``turn.*`` business events, and it
never reads, acknowledges or replies to an inbox row.  The daemon receipt
clock stays authoritative; producer elapsed time is provenance, not a start
authority.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hyprial.kernel import CHANNEL_PROTOCOL_VERSION, DaemonRequestError
from tests.daemon.harnesses.carrier.turns.test_turn_reports import (
    A_NAME,
    A_URI,
    _TurnApp,
)

SESSION = "session-1"
CAPABILITIES = {
    "wholeSessionTurns": True,
    "preparation": True,
    "permissionWait": False,
    "producerActivity": False,
}
LEASE = "lease-0123456789012345678901234567890123"


def _app(tmp_path: Path, clock: dict[str, int]) -> _TurnApp:
    return _TurnApp(tmp_path, clock)


def _register(
    app: _TurnApp,
    *,
    session_ref: str = SESSION,
    source: str = "pi-extension",
    runtime: str = "pi_interactive",
    lease: str | None = None,
) -> None:
    params = {
        "actor": A_NAME,
        "sessionRef": session_ref,
        "cwd": "/work",
        "command": ["test"],
        "source": source,
        "runtime": runtime,
        "channelConfirmed": True,
    }
    if lease is not None:
        params["channelLeaseToken"] = lease
        params["channelProtocolVersion"] = CHANNEL_PROTOCOL_VERSION
    app.app.handle("session.register", params)


def _refresh(
    app: _TurnApp,
    *,
    open_runs: list[dict[str, object]] | None = None,
    capabilities: dict[str, bool] | None = None,
    session_ref: str = SESSION,
    seq: int = 0,
    producer_epoch: str = "boot-1",
    lease: str | None = None,
    **extra: object,
) -> dict[str, object]:
    params: dict[str, object] = {
        "actor": A_NAME,
        "sessionRef": session_ref,
        "schemaVersion": 1,
        "producerEpoch": producer_epoch,
        "daemonEpoch": app.app.epoch,
        "seq": seq,
        "openRuns": list(open_runs or ()),
        "capabilities": dict(capabilities or CAPABILITIES),
        **extra,
    }
    if lease is not None:
        params["channelLeaseToken"] = lease
    return app.app.handle("session.turn.refresh", params)


def _facts(app: _TurnApp):
    rows = app.app._cell_interactive_facts()
    assert len(rows) == 1, rows
    return next(iter(rows.values()))


def _published(app: _TurnApp, *, monotonic_ms: int | None = None) -> dict:
    # Elapsed age is measured on the daemon monotonic clock, never wall time.
    monotonic_ms = (
        time.monotonic_ns() // 1_000_000 if monotonic_ms is None else monotonic_ms
    )
    rows = app.app._agent_session_domains.cells.run_states(
        app.app._cell_interactive_facts(), {}, now_ms=monotonic_ms,
        monotonic_ms=monotonic_ms,
    )
    assert A_URI in rows, rows
    return rows[A_URI]


def _pending(app: _TurnApp) -> list[str]:
    listed = app.app.handle(
        "message.pending.list", {"actor": A_NAME, "sessionRef": SESSION}
    )
    return sorted(row["messageId"] for row in listed["messages"])


def test_refresh_is_versioned_and_bounded(tmp_path: Path) -> None:
    app = _app(tmp_path, {"now": 5_000})
    try:
        _register(app)
        with pytest.raises(DaemonRequestError) as version:
            _refresh(app, schemaVersion=2)
        assert version.value.code == "INVALID_ARGUMENT"
        with pytest.raises(DaemonRequestError):
            _refresh(app, openRuns=[{"reportId": "r", "phase": "thinking"}])
        with pytest.raises(DaemonRequestError):
            _refresh(app, openRuns=[{"reportId": "r" * 129, "phase": "executing"}])
        with pytest.raises(DaemonRequestError):
            _refresh(app, openRuns=[{"reportId": f"r{i}", "phase": "prepared"} for i in range(65)])
        with pytest.raises(DaemonRequestError):
            _refresh(app, capabilities={"wholeSessionTurns": True})
        with pytest.raises(DaemonRequestError) as epoch:
            _refresh(app, producerEpoch="")
        assert epoch.value.code == "INVALID_ARGUMENT"
        # A malformed diagnostic never changes the observable run state.
        assert _facts(app).known is False
    finally:
        app.close()


def test_initial_idle_refresh_is_current_instead_of_unknown(tmp_path: Path) -> None:
    app = _app(tmp_path, {"now": 5_000})
    try:
        _register(app)
        answer = _refresh(app)
        assert answer["ok"] is True
        assert answer["schemaVersion"] == 1
        assert answer["applied"] is True
        facts = _facts(app)
        assert (facts.known, facts.running, facts.open_count) == (True, True, 0)
        assert facts.scope == "whole-session"
        row = _published(app)
        assert row["state"] == "idle"
        assert row["reason"] == "no-open-turn"
        assert row["certainty"] == "observed"
        assert row["coverage"]["turnBoundaries"] is True
        assert row["turnAgeMs"] is None
    finally:
        app.close()


def test_refresh_without_capability_negotiation_stays_partial(tmp_path: Path) -> None:
    app = _app(tmp_path, {"now": 5_000})
    try:
        _register(app)
        _refresh(app, capabilities={**CAPABILITIES, "wholeSessionTurns": False})
        row = _published(app)
        assert row["scope"] == "carrier-deliveries"
        assert row["state"] == "unknown"
        assert row["reason"] == "partial-turn-coverage"
    finally:
        app.close()


def test_refresh_drops_an_open_turn_lost_while_disconnected(tmp_path: Path) -> None:
    clock = {"now": 5_000}
    app = _app(tmp_path, clock)
    try:
        _register(app)
        app.started("s1")
        assert _facts(app).open_count == 1
        assert app.recorder.wait_for(lambda: len(app.recorder.of("turn.started")) == 1)
        published_before = list(app.recorder.events)
        clock["now"] = 30_000
        answer = _refresh(app, seq=1)
        assert answer["applied"] is True
        facts = _facts(app)
        assert (facts.open_count, facts.known) == (0, True)
        assert _published(app)["state"] == "idle"
        # Removing a stale observation is not a business turn transition.
        time.sleep(0.1)
        assert app.recorder.events == published_before
    finally:
        app.close()


def test_refresh_restores_in_turn_after_a_daemon_restart(tmp_path: Path) -> None:
    app = _app(tmp_path, {"now": 5_000})
    try:
        _register(app)
        app.started("s1")
    finally:
        app.close()
    app = _app(tmp_path, {"now": 6_000})
    try:
        _register(app)
        assert _facts(app).known is False
        _refresh(app, open_runs=[{"reportId": "s1", "phase": "executing"}], seq=3)
        facts = _facts(app)
        assert (facts.open_count, facts.known, facts.scope) == (1, True, "whole-session")
        row = _published(app)
        assert row["state"] == "in-turn"
        assert row["reason"] == "turn-open"
        # No producer elapsed time: the start instant stays unknown, never 0.
        assert row["sinceMs"] is None
        assert row["turnAgeMs"] is None
    finally:
        app.close()


def test_refresh_elapsed_is_provenance_for_the_age(tmp_path: Path) -> None:
    now_ms = int(time.time() * 1_000)
    app = _app(tmp_path, {"now": now_ms})
    try:
        _register(app)
        _refresh(
            app,
            open_runs=[
                {"reportId": "s1", "phase": "executing", "elapsedMs": 120_000}
            ],
        )
        facts = _facts(app)
        assert facts.open_count == 1
        assert facts.since_ms == now_ms - 120_000
        row = _published(app)
        assert row["state"] == "in-turn"
        assert 120_000 <= row["turnAgeMs"] < 121_000
        assert row["sinceMs"] == now_ms - 120_000
    finally:
        app.close()


def test_queued_preparation_is_an_open_run(tmp_path: Path) -> None:
    app = _app(tmp_path, {"now": 5_000})
    try:
        _register(app)
        _refresh(app, open_runs=[{"reportId": "p1", "phase": "prepared"}])
        assert _facts(app).open_count == 1
        row = _published(app)
        assert row["state"] == "in-turn"
        assert row["reason"] == "turn-open"
    finally:
        app.close()


def test_duplicate_and_stale_refresh_cannot_rewrite_current_state(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path, {"now": 5_000})
    try:
        _register(app)
        assert _refresh(app, open_runs=[{"reportId": "s1", "phase": "executing"}], seq=5)["applied"] is True
        assert _facts(app).open_count == 1

        duplicate = _refresh(app, seq=5)
        assert duplicate["applied"] is False
        assert _facts(app).open_count == 1

        stale = _refresh(app, seq=4)
        assert stale["applied"] is False
        assert _facts(app).open_count == 1

        newer = _refresh(app, seq=6)
        assert newer["applied"] is True
        assert _facts(app).open_count == 0

        # A carrier restart is a new producer epoch: its sequence restarts.
        after_restart = _refresh(
            app,
            producer_epoch="boot-2",
            seq=0,
            open_runs=[{"reportId": "s2", "phase": "executing"}],
        )
        assert after_restart["applied"] is True
        assert _facts(app).open_count == 1
    finally:
        app.close()


def test_an_ended_report_id_is_never_reopened_by_a_late_refresh(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path, {"now": 5_000})
    try:
        _register(app)
        app.started("s1")
        app.ended(["s1"])
        _refresh(app, open_runs=[{"reportId": "s1", "phase": "executing"}], seq=1)
        assert _facts(app).open_count == 0
        time.sleep(0.1)
        assert len(app.recorder.of("turn.completed")) == 1
    finally:
        app.close()


def test_refresh_is_fenced_exactly_like_turn_reports(tmp_path: Path) -> None:
    app = _app(tmp_path, {"now": 5_000})
    try:
        with pytest.raises(DaemonRequestError) as unregistered:
            _refresh(app)
        assert unregistered.value.code == "STALE_SESSION"

        _register(app, session_ref=SESSION)
        with pytest.raises(DaemonRequestError) as superseded:
            _refresh(app, session_ref="session-gone")
        assert superseded.value.code == "SESSION_SUPERSEDED"

        _register(app, session_ref="session-2")
        with pytest.raises(DaemonRequestError):
            _refresh(app, session_ref=SESSION)
    finally:
        app.close()


def test_an_old_channel_lease_cannot_publish_run_state(tmp_path: Path) -> None:
    app = _app(tmp_path, {"now": 5_000})
    try:
        _register(
            app,
            source="claude-channel",
            runtime="claude_interactive",
            lease=LEASE,
        )
        assert _refresh(
            app,
            lease=LEASE,
            open_runs=[{"reportId": "c1", "phase": "executing"}],
        )["applied"] is True
        assert _facts(app).open_count == 1

        with pytest.raises(DaemonRequestError) as wrong:
            _refresh(app, lease=LEASE[::-1])
        assert wrong.value.code == "INVALID_CHANNEL_LEASE"
        with pytest.raises(DaemonRequestError):
            _refresh(app)
        assert _facts(app).open_count == 1
    finally:
        app.close()


def test_refresh_never_touches_the_inbox_or_publishes_turn_events(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path, {"now": 5_000})
    try:
        _register(app)
        app.seed_request("r1")
        before = _pending(app)
        answer = _refresh(app, open_runs=[{"reportId": "s1", "phase": "executing"}])
        assert answer["ok"] is True
        assert _pending(app) == before == ["r1"]
        time.sleep(0.1)
        assert app.recorder.events == []
        assert app.inbox.system_notices(A_URI) == ()
        # A later real start report publishes normally: the refresh did not
        # occupy the report id or suppress the business event.
        app.started("s1")
        assert app.recorder.wait_for(
            lambda: len(app.recorder.of("turn.started")) == 1
        )
    finally:
        app.close()


def test_gap_is_cleared_only_by_an_authoritative_refresh(tmp_path: Path) -> None:
    app = _app(tmp_path, {"now": 5_000})
    try:
        _register(app)
        app.app._record_carrier_turn_started(A_URI, SESSION, "s1")
        ledger = app.app._carrier_turn_ledgers[(A_URI, SESSION)]
        for index in range(ledger.observation.capacity + 1):
            ledger.observation.record(
                app.app._cell_binding_fence(A_URI, SESSION),
                f"filler-{index}",
                "start",
                at_ms=5_000,
                monotonic_ms=5_000,
            )
        assert _facts(app).resync_required is True
        answer = _refresh(app, open_runs=[{"reportId": "s1", "phase": "executing"}])
        assert answer["resyncCleared"] is True
        facts = _facts(app)
        assert facts.resync_required is False
        assert facts.open_count == 1
        assert _published(app)["state"] == "in-turn"
    finally:
        app.close()


def _open_contact_gap(app: _TurnApp, session_ref: str = SESSION) -> None:
    """Make the session owner itself record a real missed-lease gap."""

    from hyprial.kernel import CHANNEL_LIVENESS_TTL_SECONDS

    session = app.app._agent_session_domains.session
    now_ms = session._clock_ms()
    runtime = session._runtime_projection
    runtime.register(
        A_URI,
        session_ref,
        now_ms - int(CHANNEL_LIVENESS_TTL_SECONDS * 1_000) - 1,
        confirmed=True,
    )
    runtime.register(A_URI, session_ref, now_ms, confirmed=True)
    assert app.app._agent_session_domains.session.read_runtime(A_URI).observation_gap


def test_current_refresh_acknowledges_a_recovered_contact_gap(tmp_path: Path) -> None:
    app = _app(tmp_path, {"now": 5_000})
    try:
        _register(app)
        _open_contact_gap(app)
        assert _facts(app).known is False
        assert _facts(app).resync_required is True

        answer = _refresh(app)
        assert answer["applied"] is True
        assert answer["observationGapCleared"] is True
        facts = _facts(app)
        assert (facts.known, facts.resync_required, facts.open_count) == (True, False, 0)
        assert _published(app)["state"] == "idle"
    finally:
        app.close()


def test_current_refresh_acknowledges_a_recovered_gap_with_an_open_run(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path, {"now": 5_000})
    try:
        _register(app)
        app.started("s1")
        _open_contact_gap(app)
        answer = _refresh(
            app,
            open_runs=[{"reportId": "s1", "phase": "executing", "elapsedMs": 1_000}],
        )
        assert answer["observationGapCleared"] is True
        facts = _facts(app)
        assert (facts.known, facts.open_count) == (True, 1)
        assert _published(app)["state"] == "in-turn"
    finally:
        app.close()


def test_a_heartbeat_or_stale_contact_cannot_clear_the_gap(tmp_path: Path) -> None:
    app = _app(tmp_path, {"now": 5_000})
    try:
        _register(app)
        _open_contact_gap(app)
        # Ordinary contact traffic alone is not a current-state statement.
        app.app.handle(
            "session.heartbeat", {"actor": A_NAME, "sessionRef": SESSION}
        )
        assert app.app._agent_session_domains.session.read_runtime(A_URI).observation_gap
        assert _facts(app).known is False

        # Stale contact cannot claim recovery either: age the heartbeat past TTL.
        from hyprial.kernel import CHANNEL_LIVENESS_TTL_SECONDS

        session = app.app._agent_session_domains.session
        runtime = session._runtime_projection
        stale_ms = (
            session._clock_ms()
            - int(CHANNEL_LIVENESS_TTL_SECONDS * 1_000)
            - 5_000
        )
        runtime.register(A_URI, SESSION, stale_ms, confirmed=True)
        stale = runtime.read(
            A_URI,
            SESSION,
            now_ms=session._clock_ms(),
            ttl_seconds=CHANNEL_LIVENESS_TTL_SECONDS,
        )
        assert stale.observation_gap and not stale.alive
        answer = _refresh(app)
        assert answer["observationGapCleared"] is False
        assert runtime.read(
            A_URI,
            SESSION,
            now_ms=session._clock_ms(),
            ttl_seconds=CHANNEL_LIVENESS_TTL_SECONDS,
        ).observation_gap
    finally:
        app.close()


def test_a_superseded_session_cannot_clear_the_new_session_gap(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path, {"now": 5_000})
    try:
        _register(app)
        _open_contact_gap(app)
        _register(app, session_ref="session-2")
        # A generation change drops the old session's gap with its ledger.
        assert not app.app._agent_session_domains.session.read_runtime(A_URI).observation_gap
        with pytest.raises(DaemonRequestError):
            _refresh(app, session_ref=SESSION)
    finally:
        app.close()


@pytest.mark.parametrize("existing_gap", [False, True])
def test_refresh_cannot_acknowledge_a_gap_created_during_admission(
    tmp_path: Path, monkeypatch, existing_gap: bool,
) -> None:
    app = _app(tmp_path, {"now": 5_000})
    try:
        _register(app)
        if existing_gap:
            _open_contact_gap(app)
        record = app.app._record_carrier_turn_refresh

        def race(*args, **kwargs):
            result = record(*args, **kwargs)
            _open_contact_gap(app)
            return result

        monkeypatch.setattr(app.app, "_record_carrier_turn_refresh", race)
        answer = _refresh(app, seq=5)
        assert answer["applied"] is True
        assert answer["observationGapCleared"] is False
        assert not _facts(app).known
        monkeypatch.setattr(app.app, "_record_carrier_turn_refresh", record)
        assert not _refresh(app, seq=5)["observationGapCleared"]
        assert _refresh(app, seq=6)["observationGapCleared"]
        assert _facts(app).known
    finally:
        app.close()


def test_heartbeat_exposes_gap_without_clearing_it(tmp_path: Path) -> None:
    app = _app(tmp_path, {"now": 5_000})
    try:
        _register(app)
        _open_contact_gap(app)
        answer = app.app.handle(
            "session.heartbeat", {"actor": A_NAME, "sessionRef": SESSION}
        )
        assert answer["observationGap"] is True
        assert not _facts(app).known
        _refresh(app)
        answer = app.app.handle(
            "session.heartbeat", {"actor": A_NAME, "sessionRef": SESSION}
        )
        assert answer["observationGap"] is False
    finally:
        app.close()


def test_the_refresh_is_served_by_the_declared_request_catalog(tmp_path: Path) -> None:
    """R3: the method is a declared MethodSpec, never a legacy router branch."""

    from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY

    assert "session.turn.refresh" in METHOD_REGISTRY
    binding = METHOD_REGISTRY.get("session.turn.refresh")
    assert binding is not None
    assert binding.spec.mutates is False
    assert binding.spec.retry == "safe"
    assert binding.spec.idempotency is not None

    app = _app(tmp_path, {"now": 5_000})
    try:
        _register(app)
        # The registry validates params before the handler: an undeclared key
        # can never reach the observation path.
        with pytest.raises(DaemonRequestError) as rejected:
            _refresh(app, unexpected="not-in-schema")
        assert rejected.value.code == "INVALID_ARGUMENT"
        assert _facts(app).known is False
    finally:
        app.close()
