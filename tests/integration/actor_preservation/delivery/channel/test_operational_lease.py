"""actor_preserved_daemon_channel: operational lease scenarios."""

from __future__ import annotations
import json
import threading
from pathlib import Path
import pytest
import hyprial.daemon.impl.application as daemon_application
from hyprial import __version__
from hyprial.kernel import (
    CHANNEL_LIVENESS_TTL_SECONDS,
)
from hyprial.daemon.impl.application.actors.sessions.routes import _INTERACTIVE_HEARTBEAT_TTL_SECONDS
from hyprial.daemon.impl.application import DaemonRequestError,_LocalPresence

from .support import (
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

STALE_CHANNEL_BUILD = f"{__version__}.stale"


@pytest.mark.parametrize(
    ("method", "extra"),
    (
        ("message.pending.list", {}),
        ("message.reply", {"messageId": "message-1", "message": "late"}),
        ("message.ack", {"messageId": "message-1"}),
        ("message.send", {"to": ["agent:bob"], "message": "late"}),
        (
            "session.heartbeat",
            {"channelLeaseToken": "lease-token-that-is-long-enough-0000"},
        ),
    ),
)
def test_superseded_interactive_session_is_fenced_from_inbox_mutations(
    tmp_path: Path, method: str, extra: dict[str, object]
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    register(app, "new-session")

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            method,
            {"actor": "alice", "sessionRef": "old-session", **extra},
        )

    # A *different* live session now owns the actor: this is the terminal
    # supersede verdict, distinct from STALE_SESSION (no session registered).
    assert failure.value.code == "SESSION_SUPERSEDED"


def test_targets_preserve_canonical_interactive_actor_uri(tmp_path: Path) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))

    class Presence:
        def online_actors(self) -> tuple[str, ...]:
            return ("agent:owner:host:hq-adjutant",)

    app._presence = Presence()  # type: ignore[assignment]

    result = app.handle("targets", {})

    assert result["targets"] == [
        {
            "targetKind": "agent",
            "targetUri": "agent:owner:host:hq-adjutant",
            "actor": "agent:owner:host:hq-adjutant",
            "status": "online",
            "deliverable": True,
        }
    ]


def test_ps_exposes_unknown_orphan_process_for_operator_retry(
    tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    orphan = {
        "orphanId": "worker:4242:birth",
        "harnessId": "worker",
        "state": "unknown",
        "observed": True,
        "pid": 4242,
        "marker": "birth",
        "gcAttempts": 3,
        "unknownForMs": 61_000,
        "unknownOverThreshold": True,
    }

    class Providers:
        def status(self) -> tuple[()]:
            return ()

        def orphan_status(self) -> tuple[dict[str, object], ...]:
            return (orphan,)

    app._harnesses = Providers()  # type: ignore[assignment]

    assert app.handle("ps", {})["orphanProcesses"] == [orphan]


def test_ps_exposes_interactive_channel_registration(tmp_path: Path) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))

    class Providers:
        def status(self) -> tuple[()]:
            return ()

    app._harnesses = Providers()  # type: ignore[assignment]
    app.handle(
        "session.register",
        {
            "actor": "worker-node",
            "sessionRef": "session-1",
            "cwd": str(tmp_path),
            "command": ["claude"],
            "source": "claude-channel",
            "runtime": "claude_interactive",
            "channelConfirmed": True,
            "channelBuildVersion": "0.4.1.dev0",
            "channelProtocolVersion": 2,
            "ownerFence": True,
            "channelLeaseToken": "lease-token-that-is-long-enough-0001",
        },
    )

    status = app.handle("ps", {})
    observed_age = status["interactiveSessions"][0].pop(
        "channelLastObservedAgeMs"
    )
    assert isinstance(observed_age, int) and observed_age >= 0

    assert status["interactiveSessions"] == [
        {
            "actor": "agent:op:node-1:worker-node",
            "sessionRef": "session-1",
            "cwd": str(tmp_path),
            "command": ["claude"],
            "source": "claude-channel",
            "runtime": "claude_interactive",
            "online": True,
            "channelConfirmed": True,
            "channelBuildVersion": "0.4.1.dev0",
            "channelProtocolVersion": 2,
            "ownerFence": True,
            "channelRegistered": True,
            "channelGeneration": "current",
            "channelBuildStale": True,
            "channelCurrentThisGeneration": True,
            "channelCurrentEpoch": app.epoch,
            "channelRecentlyObserved": True,
            "channelAlive": True,
            "channelLiveness": "alive",
        }
    ]


def test_persisted_channel_is_not_current_until_guarded_refresh(
    tmp_path: Path,
) -> None:
    first = application(tmp_path, FakeInbox(pending_message()))

    class Providers:
        def status(self) -> tuple[()]:
            return ()

    first._harnesses = Providers()  # type: ignore[assignment]
    params = {
        "actor": "worker-node",
        "sessionRef": "session-1",
        "cwd": str(tmp_path),
        "command": ["claude"],
        "source": "claude-channel",
        "runtime": "claude_interactive",
        "channelConfirmed": True,
        "channelBuildVersion": "0.4.1.dev0",
        "channelProtocolVersion": 2,
        "ownerFence": True,
        "channelLeaseToken": "lease-token-that-is-long-enough-0002",
    }
    registered = first.handle("session.register", params)
    assert registered["channelCurrentThisGeneration"] is True
    assert registered["channelCurrentEpoch"] == first.epoch

    second = application(tmp_path, FakeInbox(pending_message()))
    second._harnesses = Providers()  # type: ignore[assignment]
    assert second.epoch != first.epoch

    restored = second.handle("ps", {})["interactiveSessions"][0]
    assert restored["channelCurrentThisGeneration"] is False
    assert restored["channelCurrentEpoch"] is None
    stale_identity = second.handle(
        "identity.whoami", {"actor": "worker-node", "sessionRef": "session-1"}
    )
    assert stale_identity["sessionRegistered"] is True
    assert stale_identity["channelCurrentThisGeneration"] is False
    assert stale_identity["channelCurrentEpoch"] is None
    assert stale_identity["daemonEpoch"] == second.epoch

    # A normal IPC/tool call with matching identity cannot mark the persisted
    # Channel current in this daemon generation.
    second._response(json.dumps({
        "version": 1,
        "id": "ordinary-ipc",
        "method": "identity.whoami",
        "params": {"actor": "worker-node", "sessionRef": "session-1"},
    }).encode())
    assert second.handle("ps", {})["interactiveSessions"][0][
        "channelCurrentThisGeneration"
    ] is False

    refresh_response = second._response(json.dumps({
        "version": 1,
        "id": "channel-refresh",
        "method": "session.refresh",
        "params": {
            "actor": "worker-node",
            "sessionRef": "session-1",
            "channelLeaseToken": "lease-token-that-is-long-enough-0002",
        },
    }).encode())
    assert "error" not in refresh_response
    current = second.handle("ps", {})["interactiveSessions"][0]
    assert current["channelCurrentThisGeneration"] is True
    assert current["channelCurrentEpoch"] == second.epoch


def test_operational_channel_lease_expires_without_losing_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 100.0
    monkeypatch.setattr(daemon_application.time, "monotonic", lambda: now)
    app = application(tmp_path, FakeInbox(pending_message()))
    token = "lease-token-that-is-long-enough-0003"
    app.handle(
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

    fresh = app.handle("ps", {})["interactiveSessions"][0]
    assert fresh["channelRegistered"] is True
    assert fresh["channelCurrentThisGeneration"] is True
    assert fresh["channelRecentlyObserved"] is True
    assert fresh["channelAlive"] is True
    assert "channelLeaseToken" not in fresh
    assert "channelLeaseDigest" not in fresh

    now += CHANNEL_LIVENESS_TTL_SECONDS + 0.1
    # Identity and inbox IPC know actor/sessionRef, but possess no unexported
    # lease marker and therefore cannot renew liveness as a side effect. This
    # is accidental-misuse isolation inside a trusted same-user boundary.
    app.handle(
        "identity.whoami", {"actor": "alice", "sessionRef": "session-live"}
    )
    app.handle(
        "message.pending.list", {"actor": "alice", "sessionRef": "session-live"}
    )
    stale = app.handle("ps", {})["interactiveSessions"][0]
    assert stale["channelRegistered"] is True
    assert stale["channelCurrentThisGeneration"] is True
    assert stale["channelRecentlyObserved"] is False
    assert stale["channelAlive"] is False
    assert stale["channelLiveness"] == "stale"

    with pytest.raises(DaemonRequestError) as rejected:
        app.handle(
            "session.heartbeat",
            {
                "actor": "alice",
                "sessionRef": "session-live",
                "channelLeaseToken": "wrong-token-that-is-long-enough-0000",
            },
        )
    assert rejected.value.code == "INVALID_CHANNEL_LEASE"
    assert app.handle("ps", {})["interactiveSessions"][0]["channelAlive"] is False

    renewed = app.handle(
        "session.heartbeat",
        {
            "actor": "alice",
            "sessionRef": "session-live",
            "channelLeaseToken": token,
        },
    )
    assert renewed["alive"] is True
    assert app.handle("ps", {})["interactiveSessions"][0]["channelAlive"] is True


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


def test_expired_operational_channel_withdraws_route_but_keeps_offline_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 100.0
    monkeypatch.setattr(daemon_application.time, "monotonic", lambda: now)
    app = application(tmp_path, FakeInbox(pending_message()))
    token = "lease-token-that-is-long-enough-0005"
    app.handle(
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

    class Resource:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    endpoint = Resource()
    liveliness = Resource()

    class Routes:
        active = True

        def drop(self, _actor: str, *, owner_lease: str | None = None):  # type: ignore[no-untyped-def]
            del owner_lease
            self.active = False
            endpoint.close()
            liveliness.close()
            return type("Completion", (), {"changed": True})()

        def owners(self, _actor: str) -> tuple[str, ...]:
            return ("session:session-live",) if self.active else ()

    routes = Routes()
    app._routes = routes  # type: ignore[assignment]

    class Presence:
        def online_actors(self) -> tuple[str, ...]:
            return (CANONICAL,) if routes.active else ()

    app._presence = Presence()  # type: ignore[assignment]
    assert app.handle("targets", {})["targets"][0]["actor"] == CANONICAL

    now += CHANNEL_LIVENESS_TTL_SECONDS + 0.1
    # Status reads are projection-only (f4a9e5f16); route expiry is owned by
    # the maintenance tick (messaging/maintenance.py), so run that step here.
    app._expire_stale_channel_routes(now)
    assert app.handle("targets", {})["targets"] == [
        {
            "targetKind": "agent",
            "targetUri": CANONICAL,
            "actor": CANONICAL,
            "status": "offline",
            "deliverable": True,
            "hosted": False,
            "hostedBy": None,
        }
    ]
    assert endpoint.closed is True
    assert liveliness.closed is True


def test_daemon_restore_does_not_publish_persisted_channel_as_online(
    tmp_path: Path,
) -> None:
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
            "channelLeaseToken": "lease-token-that-is-long-enough-0006",
        },
    )
    second = application(tmp_path, FakeInbox(pending_message()))
    restored: list[str] = []
    second._ensure_interactive_route = (  # type: ignore[method-assign]
        lambda actor: restored.append(actor) or True
    )

    second._restore_interactive_routes()

    assert restored == []


def test_non_channel_and_wrong_session_cannot_refresh_channel_generation(
    tmp_path: Path,
) -> None:
    first = application(tmp_path, FakeInbox(pending_message()))
    for actor, source in (("pi-worker", "e2e"), ("channel", "claude-channel")):
        first.handle("session.register", {
            "actor": actor,
            "sessionRef": "session-1",
            "cwd": str(tmp_path),
            "command": ["claude"],
            "source": source,
            "runtime": "pi" if source == "e2e" else "claude_interactive",
        })

    app = application(tmp_path, FakeInbox(pending_message()))

    class Providers:
        def status(self) -> tuple[()]:
            return ()

    app._harnesses = Providers()  # type: ignore[assignment]
    failures = []
    for actor, session_ref in (("pi-worker", "session-1"), ("channel", "wrong")):
        failures.append(app._response(json.dumps({
            "version": 1,
            "id": actor,
            "method": "session.refresh",
            "params": {
                "actor": actor,
                "sessionRef": session_ref,
            },
        }).encode()))

    statuses = {
        item["actor"]: item for item in app.handle("ps", {})["interactiveSessions"]
    }
    assert [item["error"]["code"] for item in failures] == [
        "INVALID_SESSION_SOURCE",
        "SESSION_SUPERSEDED",
    ]
    assert (
        "channelCurrentThisGeneration"
        not in statuses["agent:op:node-1:pi-worker"]
    )
    assert statuses["agent:op:node-1:channel"][
        "channelCurrentThisGeneration"
    ] is False
    assert statuses["agent:op:node-1:channel"]["channelCurrentEpoch"] is None


def test_guarded_refresh_cannot_steal_from_superseding_channel(
    tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    base = {
        "actor": "alice",
        "cwd": str(tmp_path),
        "command": ["claude"],
        "source": "claude-channel",
        "runtime": "claude_interactive",
    }
    app.handle("session.register", {**base, "sessionRef": "old-session"})
    app.handle("session.register", {**base, "sessionRef": "new-session"})

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "session.refresh",
            {"actor": "alice", "sessionRef": "old-session"},
        )

    assert failure.value.code == "SESSION_SUPERSEDED"
    persisted = app.desired_state.load().interactive_sessions[0]
    assert persisted.session_ref == "new-session"

    class Providers:
        def status(self) -> tuple[()]:
            return ()

    app._harnesses = Providers()  # type: ignore[assignment]
    status = app.handle("ps", {})["interactiveSessions"][0]
    assert status["sessionRef"] == "new-session"
    assert status["channelCurrentThisGeneration"] is True
    assert status["channelCurrentEpoch"] == app.epoch


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


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("channelBuildVersion", "not a version", "safe package-version"),
        ("channelProtocolVersion", False, "positive integer"),
        ("ownerFence", 1, "boolean"),
    ],
)
def test_registration_rejects_unsafe_channel_telemetry(
    tmp_path: Path, field: str, value: object, match: str
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    params: dict[str, object] = {
        "actor": "worker-node",
        "sessionRef": "session-1",
        "cwd": str(tmp_path),
        "command": ["claude"],
        "source": "claude-channel",
        field: value,
    }

    with pytest.raises(DaemonRequestError, match=match):
        app.handle("session.register", params)


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
        # The newer heartbeat completes while expiry holds its stale
        # observation. Its Session version then fences the delayed close.
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


def test_actor_online_and_online_actors_agree_for_interactive_session(
    tmp_path: Path,
) -> None:
    """`ps`, `targets` and the delivery path (inbox/service.is_online) all
    read through the same `_LocalPresence.actor_online` / `online_actors` --
    they must never disagree about the same actor at the same instant. That
    is the "ps says confirmed, targets says online, delivery disagrees"
    defect this fix closes: one shared judge, not three independent ones."""

    app = application(tmp_path, FakeInbox(pending_message()))

    class Providers:
        def status(self) -> tuple[()]:
            return ()

    app._harnesses = Providers()  # type: ignore[assignment]
    directory = _wire_real_presence(app)
    presence = app._presence
    clock = {"now": 0.0}
    app._clock = lambda: clock["now"]  # type: ignore[assignment]

    def agrees() -> bool:
        return presence.actor_online(CANONICAL) == (  # type: ignore[union-attr]
            CANONICAL in presence.online_actors()  # type: ignore[union-attr]
        )

    _register_channel_session(app, directory)
    assert presence.actor_online(CANONICAL) is True  # type: ignore[union-attr]
    assert agrees()

    clock["now"] += _INTERACTIVE_HEARTBEAT_TTL_SECONDS + 1.0
    assert presence.actor_online(CANONICAL) is False  # type: ignore[union-attr]
    assert agrees()

    app.handle("session.refresh", {"actor": "alice", "sessionRef": "session-1"})
    assert presence.actor_online(CANONICAL) is True  # type: ignore[union-attr]
    assert agrees()


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


def test_registration_flags_a_channel_build_from_another_process(
    tmp_path: Path,
) -> None:
    """The daemon names the skew and the restart path, not just a build token."""

    app = application(tmp_path, FakeInbox(pending_message()))

    class Providers:
        def status(self) -> tuple[()]:
            return ()

    assert STALE_CHANNEL_BUILD != __version__
    app._harnesses = Providers()  # type: ignore[assignment]
    registered = app.handle(
        "session.register",
        {
            "actor": "worker-node",
            "sessionRef": "session-1",
            "cwd": str(tmp_path),
            "command": ["claude"],
            "source": "claude-channel",
            "runtime": "claude_interactive",
            "channelConfirmed": True,
            "channelBuildVersion": STALE_CHANNEL_BUILD,
            "channelProtocolVersion": 2,
            "ownerFence": True,
            "channelLeaseToken": "lease-token-that-is-long-enough-0003",
        },
    )

    assert registered["channelStale"] == {
        "code": "MCP_CHANNEL_STALE",
        "channelBuildVersion": STALE_CHANNEL_BUILD,
        "daemonVersion": registered["channelStale"]["daemonVersion"],
        "restart": (
            "restart the Claude session so the stdio channel reloads this "
            "hyprial build"
        ),
    }
    status = app.handle("ps", {})["interactiveSessions"][0]
    assert status["channelBuildStale"] is True
