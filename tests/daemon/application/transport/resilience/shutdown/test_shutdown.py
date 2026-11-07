from __future__ import annotations

import errno
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any
import pytest

from ..support import (
    _IdleRuntime,
    _app,
    _close_application_domains as _close_application_domains,
    _connect_with_backlog_retry,
    _request,
    _response,
    short_state_root as short_state_root,
)


def test_client_closing_before_response_does_not_stop_dispatcher(
    monkeypatch: pytest.MonkeyPatch, short_state_root: Path
) -> None:
    app = _app(short_state_root)
    app._runtime = _IdleRuntime()  # type: ignore[assignment]
    response_started = threading.Event()
    release_response = threading.Event()

    def delayed_response(line: bytes) -> dict[str, Any]:
        request = json.loads(line)
        if request["id"] == "abandoned":
            response_started.set()
            assert release_response.wait(1.0)
        return _response(line)

    monkeypatch.setattr(app, "_response", delayed_response)
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    abandoned = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        abandoned.connect(str(app.socket_path))
        abandoned.sendall(
            b'{"version":1,"id":"abandoned","method":"ps","params":{}}\n'
        )
        assert response_started.wait(1.0)
        abandoned.shutdown(socket.SHUT_RDWR)
        abandoned.close()
        release_response.set()

        response = _request(app.socket_path, "after-abandon")
        assert response["id"] == "after-abandon"
        assert response["result"]["daemon"]["running"] is True
        assert dispatcher.is_alive()
    finally:
        release_response.set()
        abandoned.close()
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(timeout=1.0)
        app._close()


def test_partial_request_survives_shutdown_poll_interval(
    monkeypatch: pytest.MonkeyPatch, short_state_root: Path
) -> None:
    app = _app(short_state_root)
    app._runtime = _IdleRuntime()  # type: ignore[assignment]
    monkeypatch.setattr(app, "_response", _response)
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(1.0)
    try:
        client.connect(str(app.socket_path))
        client.sendall(b'{"version":1,"id":"polled"')
        time.sleep(0.25)
        client.sendall(b',"method":"ps","params":{}}\n')

        response = json.loads(client.makefile("rb").readline())
        assert response["id"] == "polled"
        assert response["result"]["daemon"]["running"] is True
    finally:
        client.close()
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(timeout=2.0)
        app._close()


def test_many_early_closes_leave_dispatcher_healthy(
    monkeypatch: pytest.MonkeyPatch, short_state_root: Path
) -> None:
    app = _app(short_state_root)
    app._runtime = _IdleRuntime()  # type: ignore[assignment]
    monkeypatch.setattr(app, "_response", _response)
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    try:
        connect_deadline = time.monotonic() + 5.0
        for _ in range(500):
            client = _connect_with_backlog_retry(
                app.socket_path, deadline=connect_deadline
            )
            client.close()

        deadline = time.monotonic() + 2.0
        while True:
            try:
                response = _request(app.socket_path, "after-early-closes")
                break
            except (BlockingIOError, ConnectionResetError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        assert response["id"] == "after-early-closes"
        assert dispatcher.is_alive()
    finally:
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(timeout=2.0)
        app._close()


def test_shutdown_wakes_partial_clients_and_reaps_workers(
    monkeypatch: pytest.MonkeyPatch, short_state_root: Path
) -> None:
    app = _app(short_state_root)
    app._runtime = _IdleRuntime()  # type: ignore[assignment]
    monkeypatch.setattr(app, "_response", _response)
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    partial = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    partial.connect(str(app.socket_path))
    partial.sendall(b"partial")
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with app._ipc_clients_lock:
                if app._ipc_client_threads:
                    break
            time.sleep(0.01)
        else:
            pytest.fail("partial client was not assigned a worker")

        app.stop_event.set()
        app._close()

        dispatcher.join(timeout=1.0)
        assert not dispatcher.is_alive()
        with app._ipc_clients_lock:
            assert app._ipc_clients == set()
            assert app._ipc_client_threads == set()
    finally:
        partial.close()
        app.stop_event.set()
        app._close()


def test_close_finishes_all_teardown_after_active_ipc_worker_times_out(
    monkeypatch: pytest.MonkeyPatch, short_state_root: Path
) -> None:
    app = _app(short_state_root)
    events: list[str] = []
    response_started = threading.Event()
    release_response = threading.Event()

    class CloseResource:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            events.append(self.name)

        def shutdown(self) -> None:
            events.append(self.name)

    class FailingAdapters:
        def stop(self) -> None:
            events.append("adapters")
            raise RuntimeError("adapter cleanup sentinel")

    class Runtime:
        def stop(self) -> None:
            events.append("runtime")

    class RouteAuthority:
        def close_registrations(self) -> None:
            events.extend(("route-endpoint", "route-token"))

        def drain(self, _timeout: float) -> bool:
            return True

    def blocked_response(line: bytes) -> dict[str, Any]:
        response_started.set()
        assert release_response.wait(2.0)
        return _response(line)

    # IPC client lifecycle constants moved with the IPC server cluster
    # (impl/ipc/server.py owns them since the application split).
    monkeypatch.setattr("hyprial.daemon.impl.ipc.server._IPC_CLIENT_SHUTDOWN_GRACE", 0.0)
    monkeypatch.setattr("hyprial.daemon.impl.ipc.server._IPC_CLIENT_SHUTDOWN_TIMEOUT", 0.02)
    monkeypatch.setattr(app, "_response", blocked_response)
    app._start_server()
    app._write_pid_file()
    app._adapters = FailingAdapters()  # type: ignore[assignment]
    app._route_registration = RouteAuthority()  # type: ignore[assignment]
    app._routes = object()  # type: ignore[assignment]
    app._actor_token = CloseResource("actor-token")
    app._user_endpoint = CloseResource("user-endpoint")  # type: ignore[assignment]
    app._endpoint = CloseResource("endpoint")  # type: ignore[assignment]
    app._directory = CloseResource("directory")  # type: ignore[assignment]
    app._inbox = CloseResource("inbox")  # type: ignore[assignment]
    app._transport = CloseResource("transport")  # type: ignore[assignment]
    app._runtime = Runtime()  # type: ignore[assignment]
    app._lock_stream = CloseResource("lock")
    server, peer = socket.socketpair()
    app._start_ipc_client(server)
    peer.sendall(b'{"version":1,"id":"blocked","method":"ps","params":{}}\n')
    assert response_started.wait(1.0)

    try:
        with pytest.raises(RuntimeError, match="IPC client workers did not stop"):
            app._close()
        # A handler still owns these dependencies; shutdown cannot tear them
        # down merely because its socket was closed.
        assert events == []
        assert app._runtime is not None
        assert app._inbox is not None
        assert app._transport is not None
    finally:
        release_response.set()
        peer.close()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with app._ipc_clients_lock:
                if not app._ipc_client_threads:
                    break
            time.sleep(0.01)
        else:
            pytest.fail("blocked IPC worker leaked after test release")

    with pytest.raises(RuntimeError, match="adapter cleanup sentinel"):
        app._close()
    assert not app.socket_path.exists()
    assert not (app.state_dir / "daemon.json").exists()
    assert events == [
        "runtime",
        "route-endpoint",
        "route-token",
        "adapters",
        "inbox",
        "actor-token",
        "user-endpoint",
        "endpoint",
        "directory",
        "transport",
        "lock",
    ]


def test_accept_completed_after_ipc_closing_is_closed_without_worker(
    monkeypatch: pytest.MonkeyPatch, short_state_root: Path
) -> None:
    app = _app(short_state_root)
    app._runtime = _IdleRuntime()  # type: ignore[assignment]
    server_side, peer = socket.socketpair()

    class ClosingAfterAcceptServer:
        attempts = 0

        def accept(self) -> tuple[socket.socket, object]:
            self.attempts += 1
            if self.attempts == 1:
                # Deterministic accept -> admission race: shutdown linearizes
                # after the kernel returned a peer but before registration.
                app.stop_event.set()
                return server_side, object()
            raise OSError(errno.EBADF, "stopped")

    app._server = ClosingAfterAcceptServer()  # type: ignore[assignment]
    monkeypatch.setattr(app, "_log", lambda *_args, **_kwargs: None)
    try:
        app._serve()

        peer.settimeout(1.0)
        assert peer.recv(1) == b""
        with app._ipc_clients_lock:
            assert app._ipc_clients == set()
            assert app._ipc_client_threads == set()
    finally:
        server_side.close()
        peer.close()
        app.stop_event.set()
        app._close_ipc_clients()


def test_shutdown_linearizes_between_registration_and_thread_start(
    monkeypatch: pytest.MonkeyPatch, short_state_root: Path
) -> None:
    app = _app(short_state_root)
    monkeypatch.setattr(app, "_log", lambda *_args, **_kwargs: None)
    server_side, peer = socket.socketpair()
    real_start = threading.Thread.start
    close_attempted = threading.Event()
    closer: threading.Thread | None = None

    def racing_start(worker: threading.Thread) -> None:
        nonlocal closer
        if worker.name != "hyprial-daemon-ipc-client":
            real_start(worker)
            return

        def close_during_start() -> None:
            close_attempted.set()
            app._close_ipc_clients()

        closer = threading.Thread(target=close_during_start, name="ipc-closer")
        real_start(closer)
        assert close_attempted.wait(1.0)
        # Give an unlocked implementation time to miss the registered but not
        # yet alive worker before starting it.
        time.sleep(0.05)
        real_start(worker)

    monkeypatch.setattr(threading.Thread, "start", racing_start)
    try:
        app._start_ipc_client(server_side)
        assert closer is not None
        closer.join(timeout=2.0)
        assert not closer.is_alive()

        peer.settimeout(1.0)
        assert peer.recv(1) == b""
        with app._ipc_clients_lock:
            assert app._ipc_clients == set()
            assert app._ipc_client_threads == set()
    finally:
        peer.close()
        app.stop_event.set()
        app._close_ipc_clients()


def test_independent_request_is_not_convoyed_by_active_handle_before_shutdown(
    monkeypatch: pytest.MonkeyPatch, short_state_root: Path
) -> None:
    app = _app(short_state_root)
    active_started = threading.Event()
    release_active = threading.Event()
    queued_executed = threading.Event()

    def scheduled_response(line: bytes) -> dict[str, Any]:
        request = json.loads(line)
        if request["id"] == "active":
            active_started.set()
            assert release_active.wait(2.0)
        else:
            queued_executed.set()
        return _response(line)

    monkeypatch.setattr(app, "_response", scheduled_response)
    peers: list[socket.socket] = []
    for request_id in ("active", "queued"):
        server, peer = socket.socketpair()
        peers.append(peer)
        peer.sendall(
            json.dumps(
                {"version": 1, "id": request_id, "method": "ps", "params": {}}
            ).encode()
            + b"\n"
        )
        app._start_ipc_client(server)
        if request_id == "active":
            assert active_started.wait(1.0)

    assert queued_executed.wait(1.0)
    closer = threading.Thread(target=app._close_ipc_clients, daemon=True)
    closer.start()
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with app._ipc_clients_lock:
                if app._ipc_closing:
                    break
            time.sleep(0.01)
        else:
            pytest.fail("shutdown did not reach its closing linearization point")
        release_active.set()
        closer.join(timeout=2.0)

        assert not closer.is_alive()
        assert queued_executed.is_set()
        with app._ipc_clients_lock:
            assert app._ipc_clients == set()
            assert app._ipc_client_threads == set()
    finally:
        release_active.set()
        for peer in peers:
            peer.close()
        closer.join(timeout=2.0)


def test_shutdown_fails_loud_when_active_handle_exceeds_bound(
    monkeypatch: pytest.MonkeyPatch, short_state_root: Path
) -> None:
    app = _app(short_state_root)
    active_started = threading.Event()
    release_active = threading.Event()

    def blocked_response(line: bytes) -> dict[str, Any]:
        active_started.set()
        assert release_active.wait(2.0)
        return _response(line)

    monkeypatch.setattr(app, "_response", blocked_response)
    monkeypatch.setattr("hyprial.daemon.impl.ipc.server._IPC_CLIENT_SHUTDOWN_GRACE", 0.0)
    monkeypatch.setattr("hyprial.daemon.impl.ipc.server._IPC_CLIENT_SHUTDOWN_TIMEOUT", 0.05)
    server, peer = socket.socketpair()
    peer.sendall(b'{"version":1,"id":"active","method":"ps","params":{}}\n')
    app._start_ipc_client(server)
    assert active_started.wait(1.0)
    try:
        with pytest.raises(RuntimeError, match="IPC client workers did not stop"):
            app._close_ipc_clients()
    finally:
        release_active.set()
        peer.close()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with app._ipc_clients_lock:
                if not app._ipc_client_threads:
                    break
            time.sleep(0.01)


def test_client_worker_count_is_capped_and_overflow_is_closed(
    monkeypatch: pytest.MonkeyPatch, short_state_root: Path
) -> None:
    app = _app(short_state_root)
    monkeypatch.setattr(app, "_log", lambda *_args, **_kwargs: None)
    peers: list[socket.socket] = []
    try:
        for _ in range(64):
            server, peer = socket.socketpair()
            app._start_ipc_client(server)
            peers.append(peer)

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with app._ipc_clients_lock:
                if len(app._ipc_client_threads) == 64:
                    break
            time.sleep(0.01)
        else:
            pytest.fail("worker pool did not reach its configured capacity")

        overflow, overflow_peer = socket.socketpair()
        try:
            app._start_ipc_client(overflow)
            overflow_peer.settimeout(1.0)
            assert overflow_peer.recv(1) == b""
        finally:
            overflow.close()
            overflow_peer.close()

        with app._ipc_clients_lock:
            assert len(app._ipc_client_threads) == 64
    finally:
        app.stop_event.set()
        app._close_ipc_clients()
        for peer in peers:
            peer.close()
        with app._ipc_clients_lock:
            assert app._ipc_clients == set()
            assert app._ipc_client_threads == set()
