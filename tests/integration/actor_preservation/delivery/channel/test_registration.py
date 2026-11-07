"""actor_preserved_daemon_channel: registration scenarios."""

from __future__ import annotations
import threading
from pathlib import Path
import pytest
import hyprial.daemon.impl.application as daemon_application
from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.operations.session_ports import (
    RegisterSessionCommand,
    UnregisterSessionCommand,
)

from .support import (
    CANONICAL,
    FakeInbox,
    IPC_WAIT_SECONDS,
    _close_application_domains as _close_application_domains,
    _finish_ipc_request,
    _ipc_session_request,
    application,
    pending_message,
    register,
    system_notice,
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


def test_ipc_register_requests_execute_in_arrival_order_atomically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = application(tmp_path, FakeInbox(pending_message()))
    app._ensure_session_route_coordinator()
    first_call_started = threading.Event()
    release_first = threading.Event()
    second_route_started = threading.Event()
    second_session_started = threading.Event()
    real_call_session = app._call_session
    real_call_session_route = app._call_session_route

    def observed_session_call(command):  # type: ignore[no-untyped-def]
        if (
            isinstance(command, RegisterSessionCommand)
            and command.session_ref == "session-1"
        ):
            first_call_started.set()
            assert release_first.wait(IPC_WAIT_SECONDS)
        elif (
            isinstance(command, RegisterSessionCommand)
            and command.session_ref == "session-2"
        ):
            second_session_started.set()
        return real_call_session(command)

    def observed_route_call(kind, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("session_ref") == "session-2":
            second_route_started.set()
        return real_call_session_route(kind, **kwargs)

    monkeypatch.setattr(app, "_call_session", observed_session_call)
    monkeypatch.setattr(app, "_call_session_route", observed_route_call)
    first, first_client = _ipc_session_request(
        app, "register-1", "session.register", "session-1"
    )
    assert first_call_started.wait(IPC_WAIT_SECONDS)
    second, second_client = _ipc_session_request(
        app, "register-2", "session.register", "session-2"
    )
    try:
        assert second_route_started.wait(IPC_WAIT_SECONDS)
        assert not second_session_started.is_set()
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
    app._ensure_session_route_coordinator()
    unregister_call_started = threading.Event()
    release_unregister = threading.Event()
    register_route_started = threading.Event()
    register_session_started = threading.Event()
    real_call_session = app._call_session
    real_call_session_route = app._call_session_route

    def observed_session_call(command):  # type: ignore[no-untyped-def]
        if isinstance(command, UnregisterSessionCommand):
            unregister_call_started.set()
            assert release_unregister.wait(IPC_WAIT_SECONDS)
        elif (
            isinstance(command, RegisterSessionCommand)
            and command.session_ref == "new-session"
        ):
            register_session_started.set()
        return real_call_session(command)

    def observed_route_call(kind, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("session_ref") == "new-session":
            register_route_started.set()
        return real_call_session_route(kind, **kwargs)

    monkeypatch.setattr(app, "_call_session", observed_session_call)
    monkeypatch.setattr(app, "_call_session_route", observed_route_call)
    first, first_client = _ipc_session_request(
        app, "unregister-old", "session.unregister", "old-session"
    )
    assert unregister_call_started.wait(IPC_WAIT_SECONDS)
    second, second_client = _ipc_session_request(
        app, "register-new", "session.register", "new-session"
    )
    try:
        assert register_route_started.wait(IPC_WAIT_SECONDS)
        assert not register_session_started.is_set()
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
        def __init__(self) -> None:
            self.puts: list[tuple[str, bytes]] = []
            self.entered = threading.Event()
            self.release = threading.Event()
            self.published = threading.Event()

        def put(self, key: str, payload: bytes) -> None:
            assert app._inbox.fetches == [CANONICAL]
            self.entered.set()
            assert self.release.wait(IPC_WAIT_SECONDS)
            self.puts.append((key, payload))
            self.published.set()

    transport = RecordingTransport()
    app._transport = transport  # type: ignore[assignment]
    app._fetch_receipt_publisher = daemon_application.FetchReceiptPublisher(transport)

    try:
        fetched = app.handle(
            "message.pending.list",
            {"actor": "alice", "sessionRef": "session-1", "fetched": True},
        )
        assert fetched["messages"] == result["messages"]
        assert app._inbox.fetches == [CANONICAL]
        assert transport.entered.wait(IPC_WAIT_SECONDS)
        assert transport.puts == []  # fetch returned while native publication is blocked
    finally:
        transport.release.set()
    assert transport.published.wait(IPC_WAIT_SECONDS)
    assert transport.puts == [
        ("hyprial/v1/fetch-receipt/agent:bob/message-1", b"ack")
    ]

    # The durable body is no longer a pending-row field; ``message.read_body``
    # carries it on demand.  Detach the receipt publisher first: this test's
    # RecordingTransport asserts the exact fetch list on every put, and a body
    # read legitimately records one more fetch of the same recipient.
    app._fetch_receipt_publisher = None
    body = app.handle(
        "message.read_body",
        {"actor": "alice", "sessionRef": "session-1", "messageId": "message-1"},
    )
    assert body["message"] == "durable body"
    assert body["summary"] is None
    assert body["bodyBytes"] == len("durable body".encode())


def test_system_notice_stays_listed_until_its_own_ack_without_fifo_settlement(
    tmp_path: Path,
) -> None:
    inbox = FakeInbox(pending_message(), (system_notice(),))
    app = application(tmp_path, inbox)
    register(app)

    first = app.handle(
        "message.pending.list", {"actor": "alice", "sessionRef": "session-1"}
    )
    second = app.handle(
        "message.pending.list", {"actor": "alice", "sessionRef": "session-1"}
    )

    assert [item["intent"] for item in first["messages"]] == ["system", "request"]
    assert first["messages"][0]["messageId"] == "notice-1"
    assert "original-1" in first["messages"][0]["summary"]
    assert [item["intent"] for item in second["messages"]] == ["system", "request"]
    assert second["messages"][0]["messageId"] == "notice-1"
    assert inbox.acks == []
