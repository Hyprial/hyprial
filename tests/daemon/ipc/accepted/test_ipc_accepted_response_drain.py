"""Accepted response drain on real socketpairs, without starting a daemon."""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from hyprial.daemon.impl.ipc import server as server_module
from tests.daemon.ipc.response.test_ipc_response_write import SyntheticClientOwner, exchange, expected


def wait_closing(owner):
    deadline = time.monotonic() + 1
    while True:
        with owner._ipc_clients_lock:
            if owner._ipc_closing:
                return
        assert time.monotonic() < deadline
        threading.Event().wait(0.001)


def connect(owner):
    server, client = socket.socketpair()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    client.settimeout(2)
    owner._start_ipc_client(server)
    return client


def read_frame(client):
    data = bytearray()
    while True:
        chunk = client.recv(65536)
        if not chunk:
            return bytes(data)
        data.extend(chunk)


def close_in_thread(owner, errors):
    def close():
        try:
            owner._close_ipc_clients()
        except BaseException as error:
            errors.append(error)
    closer = threading.Thread(target=close)
    closer.start()
    return closer


def test_handler_initiated_stop_keeps_its_accepted_ack():
    owner = SyntheticClientOwner(size=32)
    original = owner._response
    def stop_then_ack(line):
        response = original(line)
        owner.stop_event.set()  # Same lifecycle side effect as shutdown/daemon stop.
        return response
    owner._response = stop_then_ack
    frame, _elapsed = exchange(owner)
    assert owner.stop_event.is_set()
    assert owner.calls == 1
    assert frame == expected(owner)
    assert not owner.logs


def test_shutdown_ack_and_concurrent_accepted_reply_drain_in_existing_grace():
    assert server_module._IPC_CLIENT_SHUTDOWN_GRACE == 0.1
    assert server_module._IPC_CLIENT_SHUTDOWN_TIMEOUT == 2.0
    assert server_module._IPC_RESPONSE_WRITE_TIMEOUT == 15.0
    owner = SyntheticClientOwner(size=0)
    entered, release, shutdown_called = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def handle(line):
        request = json.loads(line)
        calls.append(request["id"])
        if request["id"] == "normal":
            entered.set()
            assert release.wait(3)
        else:
            owner.stop_event.set()
            shutdown_called.set()
        return {"version": 1, "id": request["id"], "result": {"ok": True}}
    owner._response = handle
    normal = connect(owner)
    shutdown = connect(owner)
    closer, errors = None, []
    try:
        normal.sendall(b'{"id":"normal"}\n')
        assert entered.wait(1)  # Normal request is already beyond admission.
        shutdown.sendall(b'{"id":"shutdown"}\n')
        assert shutdown_called.wait(1)
        closer = close_in_thread(owner, errors)
        wait_closing(owner)
        release.set()
        assert json.loads(read_frame(shutdown))["id"] == "shutdown"
        assert json.loads(read_frame(normal))["id"] == "normal"
        closer.join(2)
        assert not closer.is_alive() and not errors
        assert calls == ["normal", "shutdown"]
        assert not owner._ipc_force_closing
        assert not owner._ipc_clients and not owner._ipc_client_threads
    finally:
        release.set()
        normal.close()
        shutdown.close()
        if closer is not None:
            closer.join(2)
        owner._close_ipc_clients()


def test_large_accepted_reply_keeps_partial_progress_during_grace():
    owner = SyntheticClientOwner()
    original = owner._response
    def stop_then_reply(line):
        response = original(line)
        owner.stop_event.set()
        return response
    owner._response = stop_then_reply
    client = connect(owner)
    closer, errors = None, []
    try:
        client.sendall(b'{"method":"synthetic"}\n')
        assert owner.entered.wait(1)
        # No reader until admission is explicitly closed, so a prefix can fill
        # the small socket buffer while the rest still belongs to this request.
        closer = close_in_thread(owner, errors)
        wait_closing(owner)
        frame = read_frame(client)
        closer.join(2)
        assert frame == expected(owner)
        assert frame.count(b"\n") == 1 and owner.calls == 1
        assert not errors and not closer.is_alive()
        assert not owner._ipc_force_closing
    finally:
        client.close()
        if closer is not None:
            closer.join(2)
        owner._close_ipc_clients()


def test_nonreading_accepted_reply_is_forced_closed_after_grace():
    owner = SyntheticClientOwner()
    client = connect(owner)
    errors = []
    try:
        client.sendall(b'{"method":"synthetic"}\n')
        assert owner.entered.wait(1)
        assert client.recv(1, socket.MSG_PEEK)  # Confirm a prefix, without draining it.
        owner.stop_event.set()
        started = time.monotonic()
        closer = close_in_thread(owner, errors)
        closer.join(2)
        elapsed = time.monotonic() - started
        assert not closer.is_alive() and not errors
        assert owner._ipc_closing and owner._ipc_force_closing
        frame = read_frame(client)
        assert 0 < len(frame) < len(expected(owner))
        assert expected(owner).startswith(frame)
        assert owner.calls == 1
        assert elapsed >= server_module._IPC_CLIENT_SHUTDOWN_GRACE
        assert not owner._ipc_clients and not owner._ipc_client_threads
    finally:
        client.close()
        owner._close_ipc_clients()


@pytest.mark.parametrize("partial", [False, True])
def test_closing_never_admits_new_or_partial_request(partial):
    owner = SyntheticClientOwner(size=32)
    errors = []
    if partial:
        client = connect(owner)
        client.sendall(b'{"method":')
        owner.stop_event.set()
        closer = close_in_thread(owner, errors)
        wait_closing(owner)
        try:
            client.sendall(b'"synthetic"}\n')
        except OSError:
            pass
        closer.join(2)
        assert not closer.is_alive() and not errors
    else:
        owner.stop_event.set()
        client = connect(owner)
    try:
        try:
            assert read_frame(client) == b""
        except ConnectionResetError:
            pass
        assert owner.calls == 0
        assert not owner._ipc_client_threads
    finally:
        client.close()
        owner._close_ipc_clients()
