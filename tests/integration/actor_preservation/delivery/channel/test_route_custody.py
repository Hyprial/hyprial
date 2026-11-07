"""actor_preserved_daemon_channel: route custody scenarios."""

from __future__ import annotations
import threading
import time
from pathlib import Path
import pytest
from hyprial.kernel import (
    CHANNEL_HEARTBEAT_INTERVAL_SECONDS,
    CHANNEL_LIVENESS_TTL_SECONDS,
)
from hyprial.kernel.impl.contracts.channels.channel import (
    CHANNEL_HEARTBEAT_MISS_BUDGET,
    )
from hyprial.daemon.impl.correlation.correlation import CorrelationEventRouter
from hyprial.kernel import LifecycleMutationRequest
from hyprial.daemon.impl.network.route_ports import EnsureRouteCommand, RouteSpec
from hyprial.daemon.impl.route_registration import (
    RouteRegistrationClient,
    RouteRegistrationIo,
)
from hyprial.daemon.impl.operations.session_ports import (
    RefreshSessionCommand,
)

from .support import (
    CANONICAL,
    FakeInbox,
    IPC_WAIT_SECONDS,
    ROUTE_PROGRESS_SECONDS,
    _HeldApplicationRoute,
    _RouteTransport,
    _close_application_domains as _close_application_domains,
    application,
    pending_message,
)


def test_channel_liveness_ttl_is_derived_from_heartbeat_miss_budget() -> None:
    assert CHANNEL_HEARTBEAT_INTERVAL_SECONDS == 1.0
    assert CHANNEL_HEARTBEAT_MISS_BUDGET == 6
    assert CHANNEL_LIVENESS_TTL_SECONDS == (
        CHANNEL_HEARTBEAT_INTERVAL_SECONDS * CHANNEL_HEARTBEAT_MISS_BUDGET
    )


def test_slow_session_call_does_not_block_other_sessions_or_route_expiry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    app._ensure_session_route_coordinator()
    sessions = {
        "alice": "slow-session",
        "bob": "other-session-1",
        "carol": "other-session-2",
        "dave": "other-session-3",
    }
    for actor, session_ref in sessions.items():
        app.handle(
            "session.register",
            {
                "actor": actor,
                "sessionRef": session_ref,
                "cwd": f"/work/{actor}",
                "command": ["claude"],
                "source": "claude-channel",
                "channelConfirmed": True,
            },
        )

    slow_call_entered = threading.Event()
    release_slow_call = threading.Event()
    expiry_applied = threading.Event()
    real_call_session = app._call_session
    real_expire = app._expire_stale_channel_routes_owned

    def controlled_call(command):  # type: ignore[no-untyped-def]
        if (
            isinstance(command, RefreshSessionCommand)
            and command.session_ref == "slow-session"
        ):
            slow_call_entered.set()
            assert release_slow_call.wait(IPC_WAIT_SECONDS)
        return real_call_session(command)

    monkeypatch.setattr(app, "_call_session", controlled_call)

    def observed_expire(now, sessions=None, **kwargs):  # type: ignore[no-untyped-def]
        real_expire(now, sessions, **kwargs)
        expiry_applied.set()

    monkeypatch.setattr(app, "_expire_stale_channel_routes_owned", observed_expire)
    failures: list[BaseException] = []

    def slow_refresh() -> None:
        try:
            app.handle(
                "session.refresh",
                {"actor": "alice", "sessionRef": "slow-session"},
            )
        except BaseException as error:
            failures.append(error)

    slow_worker = threading.Thread(target=slow_refresh, daemon=True)
    slow_worker.start()
    assert slow_call_entered.wait(IPC_WAIT_SECONDS)

    work = tuple(sessions.items())[1:]
    start = threading.Barrier(len(work) + 2)
    completed: set[str] = set()
    completed_all = threading.Event()
    completed_lock = threading.Lock()

    def mark_completed(label: str) -> None:
        with completed_lock:
            completed.add(label)
            if len(completed) == len(work) + 1:
                completed_all.set()

    def heartbeat(actor: str, session_ref: str) -> None:
        try:
            start.wait(IPC_WAIT_SECONDS)
            result = app.handle(
                "session.heartbeat",
                {"actor": actor, "sessionRef": session_ref},
            )
            assert result["alive"] is True
            mark_completed(actor)
        except BaseException as error:
            failures.append(error)

    def expire_routes() -> None:
        try:
            start.wait(IPC_WAIT_SECONDS)
            app._expire_stale_channel_routes(0.0, sessions=())
            assert expiry_applied.wait(ROUTE_PROGRESS_SECONDS)
            mark_completed("routes.expire")
        except BaseException as error:
            failures.append(error)

    workers = [
        threading.Thread(target=heartbeat, args=item, daemon=True)
        for item in work
    ] + [threading.Thread(target=expire_routes, daemon=True)]
    for worker in workers:
        worker.start()
    start.wait(IPC_WAIT_SECONDS)
    try:
        assert completed_all.wait(ROUTE_PROGRESS_SECONDS), (
            "other session heartbeats and route expiry must finish while the "
            f"slow refresh is still blocked; completed={sorted(completed)}"
        )
        assert not release_slow_call.is_set()
    finally:
        release_slow_call.set()

    slow_worker.join(timeout=IPC_WAIT_SECONDS)
    for worker in workers:
        worker.join(timeout=IPC_WAIT_SECONDS)
    assert not slow_worker.is_alive()
    assert all(not worker.is_alive() for worker in workers)
    assert failures == []


def test_late_real_route_receipt_does_not_hold_global_route_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    app._ensure_session_route_coordinator()
    router = CorrelationEventRouter()
    sink = _HeldApplicationRoute(router, CANONICAL)
    route_io = RouteRegistrationIo(
        _RouteTransport(),
        lambda _route: lambda _key: b"ok",
        sink,
        completion_deadline=0.01,
    )
    app._route_registration = route_io
    app._lifecycle_router = router
    app._routes = RouteRegistrationClient(route_io, router, timeout=0.01)
    sessions = {
        "alice": "slow-route-session",
        "bob": "other-route-session-1",
        "carol": "other-route-session-2",
        "dave": "expired-route-session",
    }
    lease_tokens = {
        actor: f"test-route-lease-token-{actor}-000000000000"
        for actor in sessions
    }
    for actor, session_ref in sessions.items():
        app.handle(
            "session.register",
            {
                "actor": actor,
                "sessionRef": session_ref,
                "cwd": f"/work/{actor}",
                "command": ["claude"],
                "source": "claude-channel",
                "channelConfirmed": True,
                "channelBuildVersion": "0.4.1.dev0",
                "channelProtocolVersion": 2,
                "ownerFence": True,
                "channelLeaseToken": lease_tokens[actor],
            },
        )

    app._close_interactive_route(CANONICAL, sessions["alice"])
    sink.block = True
    alice_done = threading.Event()
    failures: list[BaseException] = []

    def refresh_alice() -> None:
        try:
            app.handle(
                "session.refresh",
                {
                    "actor": "alice",
                    "sessionRef": sessions["alice"],
                    "channelLeaseToken": lease_tokens["alice"],
                },
            )
            alice_done.set()
        except BaseException as error:
            failures.append(error)

    alice = threading.Thread(target=refresh_alice, daemon=True)
    alice.start()
    assert sink.held.wait(IPC_WAIT_SECONDS)
    assert not alice_done.is_set()

    completed: set[str] = set()
    completed_lock = threading.Lock()

    def heartbeat(actor: str) -> None:
        try:
            result = app.handle(
                "session.heartbeat",
                {
                    "actor": actor,
                    "sessionRef": sessions[actor],
                    "channelLeaseToken": lease_tokens[actor],
                },
            )
            assert result["alive"] is True
            with completed_lock:
                completed.add(actor)
        except BaseException as error:
            failures.append(error)

    others = [
        threading.Thread(target=heartbeat, args=(actor,), daemon=True)
        for actor in ("bob", "carol")
    ]
    for worker in others:
        worker.start()
    dave = next(
        item
        for item in app._agent_session_domains.session.read_sessions()
        if item.session_ref == sessions["dave"]
    )
    monkeypatch.setattr(app, "_channel_alive", lambda _session, _now: False)
    app._expire_stale_channel_routes(0.0, sessions=(dave,))
    try:
        for worker in others:
            worker.join(ROUTE_PROGRESS_SECONDS)
        deadline = time.monotonic() + ROUTE_PROGRESS_SECONDS
        dave_actor = app._canonical_interactive_actor("dave")
        dave_lease = f"session:{sessions['dave']}"
        while (
            dave_lease in app._routes.owners(dave_actor)
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)
        assert completed == {"bob", "carol"}
        assert dave_lease not in app._routes.owners(dave_actor), (
            app._routes.owners(dave_actor),
            app._session_route_coordinator.projection(),
            failures,
        )
        assert not alice_done.is_set()
    finally:
        sink.release.set()

    alice.join(timeout=IPC_WAIT_SECONDS)
    for worker in others:
        worker.join(timeout=IPC_WAIT_SECONDS)
    assert not alice.is_alive()
    assert all(not worker.is_alive() for worker in others)
    assert alice_done.is_set()
    assert failures == []


def test_route_overload_after_session_commit_retains_exact_reconciliation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    app._ensure_session_route_coordinator()
    router = CorrelationEventRouter()
    sink = _HeldApplicationRoute(router, "blocker")
    sink.block = True
    route_io = RouteRegistrationIo(
        _RouteTransport(),
        lambda _route: lambda _key: b"ok",
        sink,
        capacity=1,
        completion_deadline=0.01,
    )
    client = RouteRegistrationClient(route_io, router, timeout=0.01)
    app._route_registration = route_io
    app._lifecycle_router = router
    app._routes = client
    monkeypatch.setattr(app, "_declare_persona_route", lambda _actor: None)
    blocker_command = EnsureRouteCommand(
        "blocker-completion",
        "blocker-attempt",
        1,
        1,
        RouteSpec("blocker", "live/blocker", "inbox/blocker"),
        "session:blocker",
    )
    blocker_request = LifecycleMutationRequest(
        blocker_command.correlation_id,
        blocker_command.attempt_token,
        "blocker-operation",
        None,
        blocker_command,
    )
    blocker_done = threading.Event()
    blocker = threading.Thread(
        target=lambda: (client.call_settled(blocker_request), blocker_done.set()),
        daemon=True,
    )
    blocker.start()
    assert sink.held.wait(IPC_WAIT_SECONDS)

    registration_done = threading.Event()
    failures: list[BaseException] = []

    def register_alice() -> None:
        try:
            app.handle(
                "session.register",
                {
                    "actor": "alice",
                    "sessionRef": "route-overload-session",
                    "cwd": "/work/alice",
                    "command": ["claude"],
                    "source": "claude-channel",
                    "channelConfirmed": True,
                },
            )
            registration_done.set()
        except BaseException as error:
            failures.append(error)

    registration = threading.Thread(target=register_alice, daemon=True)
    registration.start()
    try:
        deadline = time.monotonic() + ROUTE_PROGRESS_SECONDS
        while (
            not any(
                item.session_ref == "route-overload-session"
                for item in app._agent_session_domains.session.read_sessions()
            )
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)
        assert any(
            item.session_ref == "route-overload-session"
            for item in app._agent_session_domains.session.read_sessions()
        )
        assert not registration_done.is_set()
        assert CANONICAL not in client.registered()
    finally:
        sink.release.set()

    blocker.join(timeout=IPC_WAIT_SECONDS)
    registration.join(timeout=IPC_WAIT_SECONDS)
    assert blocker_done.is_set()
    assert registration_done.is_set()
    assert not blocker.is_alive()
    assert not registration.is_alive()
    assert CANONICAL in client.registered()
    assert failures == []


def test_takeover_during_route_io_withdraws_only_the_old_session_declaration(
    tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    app._ensure_session_route_coordinator()
    router = CorrelationEventRouter()
    sink = _HeldApplicationRoute(router, CANONICAL)
    entered = threading.Event()
    release_io = threading.Event()
    taken_over = threading.Event()
    published_after_takeover = []

    class Transport(_RouteTransport):
        block = False

        def declare_liveliness(self, key):
            if self.block:
                entered.set()
                assert release_io.wait(10)
                published_after_takeover.append(taken_over.is_set())
            return super().declare_liveliness(key)

    transport = Transport()
    io = RouteRegistrationIo(transport, lambda _: lambda _: b"ok", sink,
                             completion_deadline=0.01)
    app._route_registration = io
    app._lifecycle_router = router
    app._routes = RouteRegistrationClient(io, router, timeout=0.01)
    app.handle("agent.create", {"name": "alice"})
    app.handle("session.register", {
        "actor": "alice", "sessionRef": "session-1", "cwd": "/work/alice",
        "command": ["claude"], "source": "claude-channel", "channelConfirmed": True,
    })
    app._close_interactive_route(CANONICAL, "session-1")
    app._drop_persona_route(CANONICAL)
    transport.block = True
    sink.block = True
    failures = []

    def refresh():
        try:
            app.handle("session.refresh", {"actor": "alice", "sessionRef": "session-1"})
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=refresh, daemon=True)
    worker.start()
    try:
        assert entered.wait(10), failures
        app._agent_liveness.release(CANONICAL)
        app._agent_liveness.bind(CANONICAL, harness="codex", runtime="headless",
                                 session_id="headless-1")
        taken_over.set()
        release_io.set()
        assert sink.held.wait(10)
        sink.release.set()
        worker.join(10)
        assert not worker.is_alive()
        assert len(failures) == 1
        assert failures[0].code == "AGENT_ALREADY_RUNNING"
        assert "session-1" not in " ".join(app._routes.owners(CANONICAL))
    finally:
        release_io.set()
        sink.release.set()
        worker.join(10)


def test_headless_lifecycle_route_survives_old_session_cleanup(
    tmp_path: Path,
) -> None:
    from hyprial.kernel import HarnessLaunchSpec

    app = application(tmp_path, FakeInbox(pending_message()))
    app._ensure_session_route_coordinator()
    router = CorrelationEventRouter()
    sink = _HeldApplicationRoute(router, CANONICAL)
    io = RouteRegistrationIo(_RouteTransport(), lambda _: lambda _: b"ok", sink,
                             completion_deadline=0.01)
    app._route_registration = io
    app._lifecycle_router = router
    app._routes = RouteRegistrationClient(io, router, timeout=0.01)
    app.handle("agent.create", {"name": "alice"})
    app.handle("session.register", {
        "actor": "alice", "sessionRef": "session-1", "cwd": "/work/alice",
        "command": ["claude"], "source": "claude-channel", "channelConfirmed": True,
    })
    app._ensure_interactive_route(CANONICAL, "session-1")
    app._drop_persona_route(CANONICAL)

    headless = app._lifecycle_spec(HarnessLaunchSpec("codex", "alice", True))
    app._settled_route_mutation(
        app._routes,
        operation_id="headless-takeover-route",
        generation=0,
        version=0,
        actor=CANONICAL,
        owner_lease=f"managed:{CANONICAL}",
        ensure_spec=headless.route,
    )
    app._close_interactive_route_effect(
        CANONICAL, "session-1",
        operation_id="old-session-cleanup", generation=0, version=0,
    )
    assert app._routes.owners(CANONICAL) == (f"managed:{CANONICAL}",)
