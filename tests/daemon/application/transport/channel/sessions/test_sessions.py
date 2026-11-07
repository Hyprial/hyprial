from __future__ import annotations

import json
import threading
from pathlib import Path
import pytest
from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.application.actors.sessions.routes import _INTERACTIVE_HEARTBEAT_TTL_SECONDS
from hyprial.daemon.impl.composition.views import AgentIdentityProjectionView
from hyprial.daemon.impl.operations.session_ports import RefreshSessionCommand, RegisterSessionCommand

from ..support import (
    CANONICAL,
    FakeInbox,
    IPC_WAIT_SECONDS,
    ROUTE_PROGRESS_SECONDS,
    _close_application_domains as _close_application_domains,
    _finish_ipc_request,
    _ipc_session_request,
    _register_channel_session,
    _wire_real_presence,
    application,
    pending_message,
    register,
)


def test_codex_session_register_persists_complete_process_fence(
    tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))

    app.handle(
        "session.register",
        {
            "actor": "alice",
            "sessionRef": "codex-session",
            "cwd": "/work/alice",
            "command": ["codex", "--remote"],
            "source": "codex-app-server",
            "runtime": "codex_interactive",
            "processPid": 4321,
            "processIdentity": "proc-starttime:99",
        },
    )

    session = app.desired_state.load().interactive_sessions[0]
    assert session.process_pid == 4321
    assert session.process_identity == "proc-starttime:99"


@pytest.mark.parametrize(
    ("source", "runtime", "command"),
    [
        ("claude-channel", "claude_interactive", ["claude"]),
        ("pi-extension", "pi_interactive", ["pi"]),
    ],
)
def test_every_interactive_carrier_can_register_a_complete_process_fence(
    tmp_path: Path, source: str, runtime: str, command: list[str]
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))

    app.handle(
        "session.register",
        {
            "actor": "alice",
            "sessionRef": f"{source}-session",
            "cwd": "/work/alice",
            "command": command,
            "source": source,
            "runtime": runtime,
            "processPid": 4321,
            "processIdentity": "proc-starttime:99",
        },
    )

    session = app.desired_state.load().interactive_sessions[0]
    assert session.process_pid == 4321
    assert session.process_identity == "proc-starttime:99"


def test_session_register_rejects_partial_process_fence(tmp_path: Path) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))

    with pytest.raises(
        DaemonRequestError,
        match="processPid and processIdentity must be provided together",
    ):
        app.handle(
            "session.register",
            {
                "actor": "alice",
                "sessionRef": "codex-session",
                "cwd": "/work/alice",
                "command": ["codex", "--remote"],
                "source": "codex-app-server",
                "runtime": "codex_interactive",
                "processPid": 4321,
            },
        )

    assert app.desired_state.load().interactive_sessions == ()


def test_slow_session_call_does_not_block_other_sessions_or_route_expiry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
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
    real_call_session = app._call_session

    def controlled_call(command):  # type: ignore[no-untyped-def]
        if (
            isinstance(command, RefreshSessionCommand)
            and command.session_ref == "slow-session"
        ):
            slow_call_entered.set()
            assert release_slow_call.wait(IPC_WAIT_SECONDS)
        return real_call_session(command)

    monkeypatch.setattr(app, "_call_session", controlled_call)
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


def test_steady_heartbeat_skips_route_ensure_but_repairs_a_lost_route(
    tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))

    class Routes:
        def __init__(self) -> None:
            self.leases: dict[str, set[str]] = {}
            self.ensures: list[tuple[str, str]] = []

        def ensure(self, spec, *, owner_lease=None):  # type: ignore[no-untyped-def]
            assert owner_lease is not None
            self.ensures.append((spec.route_id, owner_lease))
            owners = self.leases.setdefault(spec.route_id, set())
            changed = owner_lease not in owners
            owners.add(owner_lease)
            return type("Completion", (), {"changed": changed})()

        def drop(self, actor, *, owner_lease=None):  # type: ignore[no-untyped-def]
            assert owner_lease is not None
            owners = self.leases.setdefault(actor, set())
            changed = owner_lease in owners
            owners.discard(owner_lease)
            return type("Completion", (), {"changed": changed})()

        def owners(self, actor: str) -> tuple[str, ...]:
            return tuple(sorted(self.leases.get(actor, set())))

    routes = Routes()
    app._routes = routes  # type: ignore[assignment]
    register(app)
    owner_lease = "session:session-1"
    assert owner_lease in routes.owners(CANONICAL)
    routes.ensures.clear()

    result = app.handle(
        "session.heartbeat", {"actor": "alice", "sessionRef": "session-1"}
    )
    assert result["alive"] is True
    assert routes.ensures == []

    routes.drop(CANONICAL, owner_lease=owner_lease)
    assert owner_lease not in routes.owners(CANONICAL)
    result = app.handle(
        "session.heartbeat", {"actor": "alice", "sessionRef": "session-1"}
    )
    assert result["alive"] is True
    assert routes.ensures == [(CANONICAL, owner_lease)]
    assert owner_lease in routes.owners(CANONICAL)


def test_displaced_session_heartbeat_cannot_restore_liveness_or_route(
    tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))

    class Routes:
        def __init__(self) -> None:
            self.leases: dict[str, set[str]] = {}
            self.ensures: list[tuple[str, str]] = []

        def ensure(self, spec, *, owner_lease=None):  # type: ignore[no-untyped-def]
            assert owner_lease is not None
            self.ensures.append((spec.route_id, owner_lease))
            owners = self.leases.setdefault(spec.route_id, set())
            changed = owner_lease not in owners
            owners.add(owner_lease)
            return type("Completion", (), {"changed": changed})()

        def drop(self, actor, *, owner_lease=None):  # type: ignore[no-untyped-def]
            assert owner_lease is not None
            owners = self.leases.setdefault(actor, set())
            changed = owner_lease in owners
            owners.discard(owner_lease)
            return type("Completion", (), {"changed": changed})()

        def owners(self, actor: str) -> tuple[str, ...]:
            return tuple(sorted(self.leases.get(actor, set())))

    routes = Routes()
    app._routes = routes  # type: ignore[assignment]
    app.handle("agent.create", {"name": "alice"})
    register(app)
    owner_lease = "session:session-1"
    app._agent_liveness.release(CANONICAL)
    app._agent_liveness.bind(
        CANONICAL,
        harness="codex",
        runtime="headless",
        session_id="other-session",
    )
    routes.drop(CANONICAL, owner_lease=owner_lease)
    routes.ensures.clear()

    with pytest.raises(DaemonRequestError) as captured:
        app.handle(
            "session.heartbeat", {"actor": "alice", "sessionRef": "session-1"}
        )

    assert captured.value.code == "AGENT_ALREADY_RUNNING"
    binding = app._agent_liveness.binding(CANONICAL)
    assert binding is not None
    assert (binding.harness, binding.runtime, binding.session_id) == (
        "codex",
        "headless",
        "other-session",
    )
    assert routes.ensures == []
    assert owner_lease not in routes.owners(CANONICAL)


@pytest.mark.parametrize("takeover", [False, True], ids=["release", "takeover"])
def test_binding_change_after_heartbeat_observation_is_fenced_atomically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, takeover: bool
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))

    class Routes:
        def __init__(self) -> None:
            self.leases: dict[str, set[str]] = {}
            self.ensures: list[tuple[str, str]] = []

        def ensure(self, spec, *, owner_lease=None):  # type: ignore[no-untyped-def]
            assert owner_lease is not None
            self.ensures.append((spec.route_id, owner_lease))
            owners = self.leases.setdefault(spec.route_id, set())
            changed = owner_lease not in owners
            owners.add(owner_lease)
            return type("Completion", (), {"changed": changed})()

        def drop(self, actor, *, owner_lease=None):  # type: ignore[no-untyped-def]
            assert owner_lease is not None
            owners = self.leases.setdefault(actor, set())
            changed = owner_lease in owners
            owners.discard(owner_lease)
            return type("Completion", (), {"changed": changed})()

        def owners(self, actor: str) -> tuple[str, ...]:
            return tuple(sorted(self.leases.get(actor, set())))

    routes = Routes()
    app._routes = routes  # type: ignore[assignment]
    app.handle("agent.create", {"name": "alice"})
    register(app)
    owner_lease = "session:session-1"
    routes.drop(CANONICAL, owner_lease=owner_lease)
    routes.ensures.clear()

    binding_observed = threading.Event()
    resume_heartbeat = threading.Event()
    real_binding_key = AgentIdentityProjectionView.binding_key

    def paused_binding_key(self, actor):  # type: ignore[no-untyped-def]
        result = real_binding_key(self, actor)
        if actor == CANONICAL and not binding_observed.is_set():
            binding_observed.set()
            assert resume_heartbeat.wait(IPC_WAIT_SECONDS)
        return result

    monkeypatch.setattr(AgentIdentityProjectionView, "binding_key", paused_binding_key)
    result: list[dict[str, object]] = []
    failures: list[BaseException] = []

    def heartbeat() -> None:
        try:
            result.append(
                app.handle(
                    "session.heartbeat",
                    {"actor": "alice", "sessionRef": "session-1"},
                )
            )
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=heartbeat, name="racing-heartbeat", daemon=True)
    worker.start()
    assert binding_observed.wait(IPC_WAIT_SECONDS)
    app._agent_liveness.release(CANONICAL)
    takeover_heartbeat = None
    if takeover:
        app._agent_liveness.bind(
            CANONICAL,
            harness="codex",
            runtime="headless",
            session_id="headless-1",
        )
        takeover_heartbeat = (
            app._agent_session_domains.agent._liveness._heartbeats[CANONICAL]
        )
    try:
        resume_heartbeat.set()
        worker.join(timeout=IPC_WAIT_SECONDS)
    finally:
        resume_heartbeat.set()
    assert not worker.is_alive()

    binding = app._agent_liveness.binding(CANONICAL)
    assert binding is not None
    if takeover:
        assert result == []
        assert len(failures) == 1
        assert isinstance(failures[0], DaemonRequestError)
        assert failures[0].code == "AGENT_ALREADY_RUNNING"
        assert (binding.harness, binding.runtime, binding.session_id) == (
            "codex",
            "headless",
            "headless-1",
        )
        assert routes.ensures == []
        assert owner_lease not in routes.owners(CANONICAL)
        assert (
            app._agent_session_domains.agent._liveness._heartbeats[CANONICAL]
            == takeover_heartbeat
        )
    else:
        assert failures == []
        assert result[0]["alive"] is True
        assert (binding.harness, binding.runtime, binding.session_id) == (
            "claude",
            "interactive",
            "session-1",
        )
        assert routes.ensures == [(CANONICAL, owner_lease)]
        assert owner_lease in routes.owners(CANONICAL)
        assert (
            app._agent_session_domains.agent.read_runtime(
                CANONICAL
            ).heartbeat_age_seconds
            is not None
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
    second.start()
    assert second_finished.wait(ROUTE_PROGRESS_SECONDS)
    try:
        session_owners = {
            owner
            for owner in route_owners.get(CANONICAL, set())
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
        for owner in route_owners.get(CANONICAL, set())
        if owner.startswith("session:")
    }
    assert session_owners == {"session:session-2"}
    assert failures == []


def test_ipc_register_requests_execute_in_arrival_order_atomically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    first_in_store = threading.Event()
    release_first = threading.Event()
    second_call_started = threading.Event()
    second_entered_store = threading.Event()
    real_register = app._state_persistence._desired_store.claim_interactive_with_agent_effects
    real_call_session = app._call_session

    def scheduled_register(session, effects):  # type: ignore[no-untyped-def]
        if session.session_ref == "session-1":
            first_in_store.set()
            assert release_first.wait(IPC_WAIT_SECONDS)
        else:
            second_entered_store.set()
        return real_register(session, effects)

    monkeypatch.setattr(
        app._state_persistence._desired_store, "claim_interactive_with_agent_effects", scheduled_register
    )

    def observed_call(command):  # type: ignore[no-untyped-def]
        if (
            isinstance(command, RegisterSessionCommand)
            and command.session_ref == "session-2"
        ):
            second_call_started.set()
        return real_call_session(command)

    monkeypatch.setattr(app, "_call_session", observed_call)
    first, first_client = _ipc_session_request(
        app, "register-1", "session.register", "session-1"
    )
    assert first_in_store.wait(IPC_WAIT_SECONDS)
    second, second_client = _ipc_session_request(
        app, "register-2", "session.register", "session-2"
    )
    try:
        # Both IPC threads may wait on the Session actor concurrently.  The
        # actor's mailbox, not the route lock, preserves mutation order.
        assert second_call_started.wait(IPC_WAIT_SECONDS)
        assert not second_entered_store.is_set()
    finally:
        release_first.set()

    assert _finish_ipc_request(first, first_client)["id"] == "register-1"
    assert _finish_ipc_request(second, second_client)["id"] == "register-2"
    current = app.desired_state.load().interactive_sessions[0]
    assert current.session_ref == "session-2"
    assert app._channel_current_epoch(CANONICAL, "session-2") == app.epoch
    assert app._agent_session_domains.session.read_runtime(CANONICAL).alive


def test_ipc_unregister_then_register_preserves_new_session_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    register(app, "old-session")
    unregister_mutated_store = threading.Event()
    release_unregister = threading.Event()
    register_call_started = threading.Event()
    register_entered_store = threading.Event()
    real_unregister = app._state_persistence._desired_store.unregister_interactive_if_current
    real_register = app._state_persistence._desired_store.claim_interactive_with_agent_effects
    real_call_session = app._call_session

    def scheduled_unregister(actor: str, session_ref: str, effects):  # type: ignore[no-untyped-def]
        result = real_unregister(actor, session_ref, effects)
        unregister_mutated_store.set()
        assert release_unregister.wait(IPC_WAIT_SECONDS)
        return result

    def scheduled_register(session, effects):  # type: ignore[no-untyped-def]
        register_entered_store.set()
        return real_register(session, effects)

    monkeypatch.setattr(
        app._state_persistence._desired_store, "unregister_interactive_if_current", scheduled_unregister
    )
    monkeypatch.setattr(
        app._state_persistence._desired_store, "claim_interactive_with_agent_effects", scheduled_register
    )

    def observed_call(command):  # type: ignore[no-untyped-def]
        if (
            isinstance(command, RegisterSessionCommand)
            and command.session_ref == "new-session"
        ):
            register_call_started.set()
        return real_call_session(command)

    monkeypatch.setattr(app, "_call_session", observed_call)
    first, first_client = _ipc_session_request(
        app, "unregister-old", "session.unregister", "old-session"
    )
    assert unregister_mutated_store.wait(IPC_WAIT_SECONDS)
    second, second_client = _ipc_session_request(
        app, "register-new", "session.register", "new-session"
    )
    try:
        assert register_call_started.wait(IPC_WAIT_SECONDS)
        assert not register_entered_store.is_set()
    finally:
        release_unregister.set()

    assert _finish_ipc_request(first, first_client)["id"] == "unregister-old"
    assert _finish_ipc_request(second, second_client)["id"] == "register-new"
    current = app.desired_state.load().interactive_sessions[0]
    assert current.session_ref == "new-session"
    assert app._channel_current_epoch(CANONICAL, "new-session") == app.epoch
    assert app._agent_session_domains.session.read_runtime(CANONICAL).alive


def test_channel_session_registration_and_pending_read_are_daemon_authoritative(
    tmp_path: Path,
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))

    registered = app.handle(
        "session.register",
        {
            "actor": "alice",
            "sessionRef": "session-1",
            "cwd": "/work/alice",
            "command": ["claude", "--resume", "session-1"],
            "source": "claude-channel",
            "channelConfirmed": True,
        },
    )
    result = app.handle(
        "message.pending.list", {"actor": "alice", "sessionRef": "session-1"}
    )

    assert registered["registered"] is True
    assert app.desired_state.load().interactive_sessions[0].actor == CANONICAL
    assert result == {
        "ok": True,
        "daemonEpoch": app.epoch,
        "messages": [
            {
                "messageId": "message-1",
                "deliveryId": "message-1",
                "conversationId": "conversation-1",
                "from": "agent:bob",
                    "to": CANONICAL,
                    "intent": "request",
                    "kind": "message",
                    "summary": None,
                    "bodyBytes": len("durable body".encode()),
                    "origin": {"via": "daemon"},
                }
        ],
    }
    assert app._inbox.fetches == []

    class RecordingTransport:
        def close(self) -> None:
            return None

        def __init__(self) -> None:
            self.puts: list[tuple[str, bytes]] = []

        def put(self, key: str, payload: bytes) -> None:
            self.puts.append((key, payload))

    transport = RecordingTransport()
    app._transport = transport  # type: ignore[assignment]
    from hyprial.daemon.impl.inbox.tracking.fetch_receipts import FetchReceiptPublisher

    app._fetch_receipt_publisher = FetchReceiptPublisher(transport)

    fetched = app.handle(
        "message.pending.list",
        {"actor": "alice", "sessionRef": "session-1", "fetched": True},
    )
    assert fetched["messages"] == result["messages"]
    assert app._inbox.fetches == [CANONICAL]
    assert app._fetch_receipt_publisher.close()
    assert transport.puts == [
        ("hyprial/v1/fetch-receipt/agent:bob/message-1", b"ack")
    ]

    # The body is no longer a pending-row field; ``message.read_body`` carries
    # it on demand.  Read after the fetch assertions so the receiver-side
    # stamps this read records do not disturb them.
    body = app.handle(
        "message.read_body",
        {"actor": "alice", "sessionRef": "session-1", "messageId": "message-1"},
    )
    assert body["message"] == "durable body"
    assert body["summary"] is None
    assert body["bodyBytes"] == len("durable body".encode())


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


def test_confirmation_cannot_overwrite_a_concurrent_session_takeover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confirm paused after ownership lookup must not write session-1."""

    from dataclasses import replace
    from hyprial.daemon.impl.session_actor import SessionActor
    from hyprial.daemon.impl.desired_state import DesiredStateStore
    from tests.daemon.sessions.actors.domains.support import (
        ACTOR, _ScriptedAgentCommands, _register, _registered, _wait_until,
    )

    events = []
    commands = _ScriptedAgentCommands(())
    actor = SessionActor(
        DesiredStateStore(tmp_path / "state.json"), daemon_epoch="review",
        event_sink=events.append, agent_commands=commands,
    )
    commands.attach(actor)
    checked = threading.Event()
    release = threading.Event()
    result = []
    real_check = actor._session_projection.still_owned

    def paused_check(*args):
        owned = real_check(*args)
        checked.set()
        assert release.wait(10)
        return owned

    try:
        actor.submit(_register("register", "session-1"))
        _wait_until(lambda: _registered(events, "register"))
        monkeypatch.setattr(actor._session_projection, "still_owned", paused_check)
        worker = threading.Thread(target=lambda: result.append(
            actor.commit_liveness_if_owned(ACTOR, "session-1", lambda confirm: confirm())
        ), daemon=True)
        worker.start()
        assert checked.wait(10)
        actor.submit(replace(_register("takeover", "session-2"), manage_agent=False))
        _wait_until(lambda: _registered(events, "takeover"))
        assert actor.read_session(ACTOR).session_ref == "session-2"
        release.set()
        worker.join(10)
        assert not worker.is_alive()
        assert actor._runtime_projection.confirmed(ACTOR) == "session-2", result
    finally:
        release.set()
        assert actor.drain(2).complete
