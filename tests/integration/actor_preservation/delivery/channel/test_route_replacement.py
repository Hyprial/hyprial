"""actor_preserved_daemon_channel: route replacement scenarios."""

from __future__ import annotations
import threading
from pathlib import Path
import pytest
from hyprial.daemon.impl.correlation.correlation import CorrelationEventRouter
from hyprial.daemon.impl.route_registration import (
    RouteRegistrationClient,
)
from hyprial.daemon.impl.operations.session_ports import (
    RegisterSessionCommand,
    UnregisterSessionCommand,
)

from .support import (
    CANONICAL,
    FakeInbox,
    IPC_WAIT_SECONDS,
    ROUTE_PROGRESS_SECONDS,
    _FailOnceCloseRouteTransport,
    _RecordingRouteIo,
    _close_application_domains as _close_application_domains,
    application,
    official_address,
    pending_message,
    register,
)


def _fail_once_close_application(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: str
):  # type: ignore[no-untyped-def]
    app = application(tmp_path, FakeInbox(pending_message()))
    app._ensure_session_route_coordinator()
    router = CorrelationEventRouter()
    transport = _FailOnceCloseRouteTransport(target)
    route_io = _RecordingRouteIo(
        transport,
        lambda _route: lambda _key: b"ok",
        router,
    )
    app._route_registration = route_io
    app._lifecycle_router = router
    app._routes = RouteRegistrationClient(route_io, router, timeout=0.01)
    monkeypatch.setattr(app, "_declare_persona_route", lambda _actor: None)
    return app, transport, route_io


def _register_alice(app, session_ref: str) -> None:  # type: ignore[no-untyped-def]
    app.handle(
        "session.register",
        {
            "actor": "alice",
            "sessionRef": session_ref,
            "cwd": "/work/alice",
            "command": ["claude"],
            "source": "claude-channel",
            "channelConfirmed": True,
        },
    )


def _assert_unregister_retries_the_failed_close(  # type: ignore[no-untyped-def]
    app, transport, route_io, address: str, session_ref: str
) -> None:
    result = app.handle(
        "session.unregister",
        {"actor": "alice", "sessionRef": session_ref},
    )

    assert result["unregistered"] is True
    assert transport.failed_registration is not None
    assert transport.failed_registration.close_calls == 2
    session_drops = [
        token for token in route_io.drop_attempts if ":unregister:" in token
    ]
    assert len(session_drops) >= 2
    assert len(set(session_drops)) == 1
    assert f"session:{session_ref}" not in app._routes.owners(address)


def test_app_last_lease_close_failure_retries_when_the_first_register_creates_the_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """No record before the register: its route is still the id address's.

    The fail-once transport targets the id-address prefix, which the name
    URI ``CANONICAL`` does not contain, so a session route declared at the
    name URI would never take the failing close.
    """

    app, transport, route_io = _fail_once_close_application(
        monkeypatch, tmp_path, "agent:op:node-1:a."
    )
    assert app.agents.get("alice") is None
    assert "agent:op:node-1:a." not in CANONICAL

    _register_alice(app, "first-session")

    agent = app.agents.require("alice")
    address = f"agent:op:node-1:{agent.agent_id}"
    assert agent.uri == address
    assert app._routes.owners(CANONICAL) == ()
    _assert_unregister_retries_the_failed_close(
        app, transport, route_io, address, "first-session"
    )


def test_app_last_lease_close_failure_retries_exact_route_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    app, transport, route_io = _fail_once_close_application(
        monkeypatch, tmp_path, "alice"
    )
    app.handle("agent.create", {"name": "alice"})
    address = official_address(app)
    transport.target = address
    _register_alice(app, "close-retry-session")

    _assert_unregister_retries_the_failed_close(
        app, transport, route_io, address, "close-retry-session"
    )


def test_delayed_older_register_result_cannot_reopen_superseded_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    route_owners: dict[str, set[str]] = {}

    class Routes:
        def ensure(self, spec, *, owner_lease=None):  # type: ignore[no-untyped-def]
            assert owner_lease is not None
            owners = route_owners.setdefault(spec.route_id, set())
            changed = owner_lease not in owners
            owners.add(owner_lease)
            return type("Completion", (), {"changed": changed})()

        def drop(self, actor: str, *, owner_lease=None):  # type: ignore[no-untyped-def]
            assert owner_lease is not None
            owners = route_owners.setdefault(actor, set())
            changed = owner_lease in owners
            owners.discard(owner_lease)
            return type("Completion", (), {"changed": changed})()

        def owners(self, actor: str) -> tuple[str, ...]:
            return tuple(sorted(route_owners.get(actor, set())))

    app._routes = Routes()  # type: ignore[assignment]
    first_result_ready = threading.Event()
    release_first_result = threading.Event()
    second_finished = threading.Event()
    failures: list[BaseException] = []
    real_call_session = app._call_session

    def delayed_call(command):  # type: ignore[no-untyped-def]
        completed = real_call_session(command)
        if (
            isinstance(command, RegisterSessionCommand)
            and command.session_ref == "session-1"
        ):
            first_result_ready.set()
            assert release_first_result.wait(IPC_WAIT_SECONDS)
        return completed

    monkeypatch.setattr(app, "_call_session", delayed_call)

    def register_session(session_ref: str, finished: threading.Event | None) -> None:
        try:
            app.handle(
                "session.register",
                {
                    "actor": "alice",
                    "sessionRef": session_ref,
                    "cwd": "/work/alice",
                    "command": ["claude"],
                    "source": "claude-channel",
                    "channelConfirmed": True,
                },
            )
            if finished is not None:
                finished.set()
        except BaseException as error:
            failures.append(error)

    first = threading.Thread(
        target=register_session, args=("session-1", None), daemon=True
    )
    second = threading.Thread(
        target=register_session,
        args=("session-2", second_finished),
        daemon=True,
    )
    first.start()
    assert first_result_ready.wait(IPC_WAIT_SECONDS)
    address = official_address(app)
    second.start()
    assert second_finished.wait(ROUTE_PROGRESS_SECONDS)
    try:
        session_owners = {
            owner
            for owner in route_owners.get(address, set())
            if owner.startswith("session:")
        }
        assert session_owners == {"session:session-2"}
    finally:
        release_first_result.set()

    first.join(timeout=IPC_WAIT_SECONDS)
    second.join(timeout=IPC_WAIT_SECONDS)
    assert not first.is_alive()
    assert not second.is_alive()
    session_owners = {
        owner
        for owner in route_owners.get(address, set())
        if owner.startswith("session:")
    }
    assert session_owners == {"session:session-2"}
    assert failures == []


def test_repeated_session_replacement_bounds_exact_route_versions(
    tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    app._ensure_session_route_coordinator()
    replacements = 24
    for index in range(replacements):
        app.handle(
            "session.register",
            {
                "actor": "alice",
                "sessionRef": f"replacement-{index}",
                "cwd": "/work/alice",
                "command": ["claude"],
                "source": "claude-channel",
                "channelConfirmed": True,
            },
        )

    current_ref = f"replacement-{replacements - 1}"
    address = official_address(app)
    actor_keys = {
        key
        for key in app._interactive_route_versions
        if key[0] == address
    }
    assert actor_keys == {(address, current_ref)}
    assert not any(key[0] == CANONICAL for key in app._interactive_route_versions)
    actor_high_water = app._interactive_route_actor_versions[address]

    result = app.handle(
        "session.unregister",
        {"actor": "alice", "sessionRef": current_ref},
    )
    assert result["unregistered"] is True
    assert not any(
        key[0] == address for key in app._interactive_route_versions
    )
    assert app._interactive_route_actor_versions[address] > actor_high_water


def test_delayed_older_unregister_result_cannot_close_newer_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    route_owners: dict[str, set[str]] = {}

    class Routes:
        def ensure(self, spec, *, owner_lease=None):  # type: ignore[no-untyped-def]
            assert owner_lease is not None
            owners = route_owners.setdefault(spec.route_id, set())
            changed = owner_lease not in owners
            owners.add(owner_lease)
            return type("Completion", (), {"changed": changed})()

        def drop(self, actor: str, *, owner_lease=None):  # type: ignore[no-untyped-def]
            assert owner_lease is not None
            owners = route_owners.setdefault(actor, set())
            changed = owner_lease in owners
            owners.discard(owner_lease)
            return type("Completion", (), {"changed": changed})()

        def owners(self, actor: str) -> tuple[str, ...]:
            return tuple(sorted(route_owners.get(actor, set())))

    app._routes = Routes()  # type: ignore[assignment]
    register(app, "old-session")
    address = official_address(app)
    old_result_ready = threading.Event()
    release_old_result = threading.Event()
    failures: list[BaseException] = []
    real_call_session = app._call_session

    def delayed_call(command):  # type: ignore[no-untyped-def]
        completed = real_call_session(command)
        if isinstance(command, UnregisterSessionCommand):
            old_result_ready.set()
            assert release_old_result.wait(IPC_WAIT_SECONDS)
        return completed

    monkeypatch.setattr(app, "_call_session", delayed_call)

    def unregister_old() -> None:
        try:
            app.handle(
                "session.unregister",
                {"actor": "alice", "sessionRef": "old-session"},
            )
        except BaseException as error:
            failures.append(error)

    old = threading.Thread(target=unregister_old, daemon=True)
    old.start()
    assert old_result_ready.wait(IPC_WAIT_SECONDS)
    app.handle(
        "session.register",
        {
            "actor": "alice",
            "sessionRef": "new-session",
            "cwd": "/work/alice",
            "command": ["claude"],
            "source": "claude-channel",
            "channelConfirmed": True,
        },
    )
    try:
        assert {
            owner
            for owner in route_owners[address]
            if owner.startswith("session:")
        } == {"session:new-session"}
    finally:
        release_old_result.set()

    old.join(timeout=IPC_WAIT_SECONDS)
    assert not old.is_alive()
    assert {
        owner
        for owner in route_owners[address]
        if owner.startswith("session:")
    } == {"session:new-session"}
    assert failures == []
