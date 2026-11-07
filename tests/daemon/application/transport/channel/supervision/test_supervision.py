from __future__ import annotations

import threading
from pathlib import Path
import pytest
from hyprial.kernel import CHANNEL_HEARTBEAT_INTERVAL_SECONDS, CHANNEL_LIVENESS_TTL_SECONDS
from hyprial.kernel.impl.contracts.channels.channel import CHANNEL_HEARTBEAT_MISS_BUDGET
from hyprial.daemon.impl.application import DaemonRequestError, _LocalPresence
from hyprial.daemon.impl.application.actors.sessions.routes import _INTERACTIVE_HEARTBEAT_TTL_SECONDS

from ..support import (
    CANONICAL,
    FakeInbox,
    IPC_WAIT_SECONDS,
    ROUTE_PROGRESS_SECONDS,
    _FakeLivelinessDirectory,
    _close_application_domains as _close_application_domains,
    _register_channel_session,
    _wire_real_presence,
    application,
    pending_message,
    register,
)


def test_channel_liveness_ttl_is_derived_from_heartbeat_miss_budget() -> None:
    assert CHANNEL_HEARTBEAT_INTERVAL_SECONDS == 1.0
    assert CHANNEL_HEARTBEAT_MISS_BUDGET == 6
    assert CHANNEL_LIVENESS_TTL_SECONDS == (
        CHANNEL_HEARTBEAT_INTERVAL_SECONDS * CHANNEL_HEARTBEAT_MISS_BUDGET
    )


def test_persisted_lease_digest_alone_does_not_mark_liveness_after_restart(
    tmp_path: Path,
) -> None:
    token = "lease-token-that-is-long-enough-0004"
    first = application(tmp_path, FakeInbox(pending_message()))
    first.handle(
        "session.register",
        {
            "actor": "alice",
            "sessionRef": "session-live",
            "cwd": str(tmp_path),
            "command": ["claude"],
            "source": "claude-channel",
            "channelBuildVersion": "0.4.1.dev0",
            "channelProtocolVersion": 2,
            "ownerFence": True,
            "channelLeaseToken": token,
        },
    )
    persisted = first.desired_state.load().to_json()["interactiveSessions"][0]
    assert persisted["channelLeaseDigest"] != token
    assert "channelLeaseToken" not in persisted

    second = application(tmp_path, FakeInbox(pending_message()))
    restored = second.handle("ps", {})["interactiveSessions"][0]
    assert restored["channelRegistered"] is True
    assert restored["channelCurrentThisGeneration"] is False
    assert restored["channelRecentlyObserved"] is False
    assert restored["channelAlive"] is False

    with pytest.raises(DaemonRequestError) as rejected:
        second.handle(
            "session.refresh",
            {
                "actor": "alice",
                "sessionRef": "session-live",
                "channelLeaseToken": "wrong-token-that-is-long-enough-0000",
            },
        )
    assert rejected.value.code == "INVALID_CHANNEL_LEASE"
    assert second.handle("ps", {})["interactiveSessions"][0]["channelAlive"] is False

    second.handle(
        "session.refresh",
        {
            "actor": "alice",
            "sessionRef": "session-live",
            "channelLeaseToken": token,
        },
    )
    current = second.handle("ps", {})["interactiveSessions"][0]
    assert current["channelCurrentThisGeneration"] is True
    assert current["channelRecentlyObserved"] is True
    assert current["channelAlive"] is True


def test_ps_marks_legacy_channel_generation_without_guessing_liveness(
    tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))

    class Providers:
        def status(self) -> tuple[()]:
            return ()

    app._harnesses = Providers()  # type: ignore[assignment]
    register(app)

    status = app.handle("ps", {})

    session = status["interactiveSessions"][0]
    assert session["channelGeneration"] == "legacy_or_unknown"
    assert "channelBuildVersion" not in session
    assert "ownerFence" not in session


def test_dead_connector_stops_reporting_online_after_heartbeat_expires(
    tmp_path: Path,
) -> None:
    """Acceptance case: register, then go silent (process killed, no more
    polls) -- ps.online and targets must flip to dead once the heartbeat
    goes stale, even though the daemon-owned token never explicitly closed."""

    app = application(tmp_path, FakeInbox(pending_message()))

    class Providers:
        def status(self) -> tuple[()]:
            return ()

    app._harnesses = Providers()  # type: ignore[assignment]
    directory = _wire_real_presence(app)
    clock = {"now": 1_000.0}
    app._clock = lambda: clock["now"]  # type: ignore[assignment]

    _register_channel_session(app, directory)

    fresh_ps = app.handle("ps", {})["interactiveSessions"][0]
    assert fresh_ps["online"] is True
    assert fresh_ps["channelConfirmed"] is True
    fresh_targets = app.handle("targets", {"kind": "agent"})["targets"]
    assert CANONICAL in [item["actor"] for item in fresh_targets]

    # Simulate the connector process dying: time passes, nothing ever calls
    # the daemon again (no poll, no refresh).
    clock["now"] += _INTERACTIVE_HEARTBEAT_TTL_SECONDS + 1.0

    stale_ps = app.handle("ps", {})["interactiveSessions"][0]
    assert stale_ps["online"] is False
    # channelConfirmed is deliberately untouched: it stays persisted
    # registration/generation telemetry, not liveness (see
    # _interactive_session_status's docstring). A downstream consumer that
    # inferred liveness from channelConfirmed must migrate to `online`.
    assert stale_ps["channelConfirmed"] is True
    stale_targets = app.handle("targets", {"kind": "agent"})["targets"]
    (stale_actor,) = [
        item for item in stale_targets if item["actor"] == CANONICAL
    ]
    assert stale_actor["status"] == "offline"


def test_periodic_expiry_cannot_revoke_a_route_after_heartbeat_returns_alive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    clock = {"now": 0.0}
    app._clock = lambda: clock["now"]  # type: ignore[method-assign]
    lease_token = "test-channel-lease-token-000000000001"
    app.handle(
        "session.register",
        {
            "actor": "alice",
            "sessionRef": "session-1",
            "cwd": "/work/alice",
            "command": ["claude"],
            "source": "claude-channel",
            "channelBuildVersion": "0.4.1.dev0",
            "channelProtocolVersion": 2,
            "ownerFence": True,
            "channelLeaseToken": lease_token,
        },
    )

    class Runtime:
        def stop(self) -> None:
            return None

        def on_timer(self, _observed_at_ms=None):  # type: ignore[no-untyped-def]
            return type(
                "Outcome",
                (),
                {"harness_restarts": 0, "inbox_results": 0},
            )()

    app._runtime = Runtime()  # type: ignore[assignment]
    active_routes = {CANONICAL}

    class Routes:
        def ensure(self, spec, *, owner_lease=None):  # type: ignore[no-untyped-def]
            del owner_lease
            changed = spec.route_id not in active_routes
            active_routes.add(spec.route_id)
            return type("Completion", (), {"changed": changed})()

        def drop(self, actor: str, *, owner_lease: str | None = None):  # type: ignore[no-untyped-def]
            del owner_lease
            active_routes.discard(actor)
            return type("Completion", (), {"changed": True})()

        def owners(self, actor: str) -> tuple[str, ...]:
            return ("session:session-1",) if actor in active_routes else ()

    app._routes = Routes()  # type: ignore[assignment]
    clock["now"] = CHANNEL_LIVENESS_TTL_SECONDS + 1.0
    stale_observation_checked = threading.Event()
    release_expiry = threading.Event()
    heartbeat_finished = threading.Event()
    real_channel_alive = app._channel_alive

    def paused_channel_alive(session, now):  # type: ignore[no-untyped-def]
        alive = real_channel_alive(session, now)
        assert not alive
        stale_observation_checked.set()
        assert release_expiry.wait(IPC_WAIT_SECONDS)
        return alive

    monkeypatch.setattr(app, "_channel_alive", paused_channel_alive)

    periodic = threading.Thread(
        target=app._run_scheduled_domains,
        args=(CHANNEL_LIVENESS_TTL_SECONDS + 1.0,),
        daemon=True,
    )

    def heartbeat() -> None:
        result = app.handle(
            "session.heartbeat",
            {
                "actor": "alice",
                "sessionRef": "session-1",
                "channelLeaseToken": lease_token,
            },
        )
        assert result["alive"] is True
        heartbeat_finished.set()

    heartbeat_worker = threading.Thread(target=heartbeat, daemon=True)
    periodic.start()
    assert stale_observation_checked.wait(IPC_WAIT_SECONDS)
    heartbeat_worker.start()
    try:
        # The newer heartbeat completes while expiry still holds its stale
        # observation.  Its Session version then fences the delayed close.
        assert heartbeat_finished.wait(ROUTE_PROGRESS_SECONDS)
        assert CANONICAL in active_routes
    finally:
        release_expiry.set()

    periodic.join(timeout=IPC_WAIT_SECONDS)
    heartbeat_worker.join(timeout=IPC_WAIT_SECONDS)
    assert not periodic.is_alive()
    assert not heartbeat_worker.is_alive()
    assert heartbeat_finished.is_set()
    assert CANONICAL in active_routes


def test_explicit_heartbeat_keeps_connector_online_past_registration_ttl(
    tmp_path: Path,
) -> None:
    """Only the explicit carrier heartbeat renews its liveness lease."""

    app = application(tmp_path, FakeInbox(pending_message()))

    class Providers:
        def status(self) -> tuple[()]:
            return ()

    app._harnesses = Providers()  # type: ignore[assignment]
    directory = _wire_real_presence(app)
    clock = {"now": 0.0}
    app._clock = lambda: clock["now"]  # type: ignore[assignment]

    _register_channel_session(app, directory)

    # Several poll cycles, each inside the TTL of the *previous* contact, but
    # together spanning far more wall-clock time than one TTL window alone.
    for _ in range(3):
        clock["now"] += _INTERACTIVE_HEARTBEAT_TTL_SECONDS - 1.0
        app.handle(
            "session.heartbeat", {"actor": "alice", "sessionRef": "session-1"}
        )

    assert clock["now"] > _INTERACTIVE_HEARTBEAT_TTL_SECONDS
    still_online = app.handle("ps", {})["interactiveSessions"][0]
    assert still_online["online"] is True
    assert CANONICAL in [
        item["actor"] for item in app.handle("targets", {"kind": "agent"})["targets"]
    ]


def test_non_interactive_online_actor_is_not_heartbeat_gated(
    tmp_path: Path,
) -> None:
    """A daemon-managed harness worker or a genuinely remote peer's raw zenoh
    token really does track that other process's own session (see
    `_reconcile_harness_actors`) -- these must keep reporting online exactly
    as before, with no heartbeat required, since they were never the actors
    `_ensure_interactive_route` registers on the daemon's own behalf."""

    app = application(tmp_path, FakeInbox(pending_message()))
    directory = _FakeLivelinessDirectory()
    directory.add("agent:owner:host:remote-peer")
    app._presence = _LocalPresence(  # type: ignore[assignment]
        directory, app.node_id, interactive_liveness=app._interactive_actor_liveness
    )
    clock = {"now": 0.0}
    app._clock = lambda: clock["now"]  # type: ignore[assignment]

    clock["now"] += _INTERACTIVE_HEARTBEAT_TTL_SECONDS * 10

    assert app._presence.actor_online("agent:owner:host:remote-peer") is True
    assert "agent:owner:host:remote-peer" in app._presence.online_actors()


def test_current_channel_protocol_requires_operational_liveness_lease(
    tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    with pytest.raises(DaemonRequestError) as rejected:
        app.handle(
            "session.register",
            {
                "actor": "worker-node",
                "sessionRef": "session-1",
                "cwd": str(tmp_path),
                "command": ["claude"],
                "source": "claude-channel",
                "channelBuildVersion": "0.4.1.dev0",
                "channelProtocolVersion": 2,
                "ownerFence": True,
            },
        )
    assert rejected.value.code == "INVALID_CHANNEL_LEASE"
