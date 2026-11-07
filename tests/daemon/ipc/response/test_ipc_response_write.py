"""Real local socketpair response controls; no daemon construction or listener.

Deflake (2026-11-03, workflow wf-ff332cef #implDF): the write-budget probe
went red once on #2319 CI (run 45309) with a truncated frame: it patched
_IPC_RESPONSE_WRITE_TIMEOUT down to 0.08s and simultaneously demanded that
the default 379KB response flush through the 4KB socketpair inside that
budget while a 0.2s handler held the other end -- on the CI shard (3-CPU
cgroup quota, -n 2) the flush is a wall-clock bet, and the deadline fired
mid-frame. The budget-must-begin-after-the-handler discrimination is kept
intact (0.08s < 0.2s handler, so an early-starting budget must still expire
before any write); only the accidental bulk flush inside the patched budget
is removed by using the small frame. Large-frame response writing stays
covered with the product-default budget by
test_large_response_survives_reader_pause_past_shutdown_poll and
test_fast_large_and_delayed_small_frames_stay_exact[379000-0].
"""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from hyprial.daemon.impl import application as module
from hyprial.daemon.impl.ipc import server as server_module


class SyntheticClientOwner:
    _serve_client = module.DaemonApplication._serve_client
    _serve_connection = module.DaemonApplication._serve_connection
    _close_ipc_clients = module.DaemonApplication._close_ipc_clients
    _start_ipc_client = module.DaemonApplication._start_ipc_client
    _new_ipc_client_worker = module.DaemonApplication._new_ipc_client_worker
    _ipc_request_metadata = staticmethod(module.DaemonApplication._ipc_request_metadata)
    _ipc_peer_disconnected = staticmethod(module.DaemonApplication._ipc_peer_disconnected)
    _ipc_timeout_response = staticmethod(module.DaemonApplication._ipc_timeout_response)
    _dispatch_ipc_response = module.DaemonApplication._dispatch_ipc_response

    def __init__(self, size=379000, handler_delay=0):
        self._ipc_clients_lock = threading.Lock()
        self._ipc_closing = False
        self._ipc_force_closing = False
        self._ipc_client_slots = threading.BoundedSemaphore(module._IPC_MAX_CLIENTS)
        self._ipc_clients = set()
        self._ipc_clients_high_water = 0
        self._ipc_client_states = {}
        self._ipc_client_threads = set()
        self._ipc_peer = threading.local()
        self.stop_event = threading.Event()
        self.entered = threading.Event()
        self.calls = 0
        self.logs = []
        self.response = {"version": 1, "id": "response-probe", "result": {"data": "x" * size + "结束"}}
        self.handler_delay = handler_delay

    def _response(self, line):
        assert json.loads(line)["method"] == "synthetic"
        self.calls += 1
        self.entered.set()
        if self.handler_delay:
            threading.Event().wait(self.handler_delay)
        return self.response

    def _log(self, _level, _component, event, **fields):
        self.logs.append((event, fields))


def exchange(owner, *, read_delay=0, per_read_delay=0, close=False):
    server, client = socket.socketpair()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    client.settimeout(2)
    ended = []
    started = time.monotonic()
    def serve():
        owner._serve_client(server)
        ended.append(time.monotonic() - started)
    worker = threading.Thread(target=serve)
    worker.start()
    received = bytearray()
    try:
        client.sendall(b'{"method":"synthetic"}\n')
        assert owner.entered.wait(1)
        threading.Event().wait(read_delay)
        if close:
            owner.stop_event.set()
            with owner._ipc_clients_lock:
                owner._ipc_force_closing = True
            worker.join(1)
        while True:
            data = client.recv(4096)
            if not data:
                break
            received.extend(data)
            if per_read_delay:
                threading.Event().wait(per_read_delay)
        worker.join(1)
        assert not worker.is_alive()
        assert owner.calls == 1
        return bytes(received), ended[0]
    finally:
        owner.stop_event.set()
        client.close()
        server.close()
        worker.join(1)
        assert not worker.is_alive()


def expected(owner):
    return json.dumps(owner.response, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"


def test_large_response_survives_reader_pause_past_shutdown_poll():
    owner = SyntheticClientOwner()
    received, _elapsed = exchange(owner, read_delay=0.25)
    assert received == expected(owner)
    assert received.count(b"\n") == 1
    assert not owner.logs


@pytest.mark.parametrize("size,delay", [(379000, 0), (64, 0.25)])
def test_fast_large_and_delayed_small_frames_stay_exact(size, delay):
    owner = SyntheticClientOwner(size)
    received, _ = exchange(owner, read_delay=delay)
    assert received == expected(owner)
    assert not owner.logs


def test_write_budget_begins_after_single_handler(monkeypatch):
    monkeypatch.setattr(server_module, "_IPC_RESPONSE_WRITE_TIMEOUT", 0.08)
    owner = SyntheticClientOwner(size=64, handler_delay=0.2)
    received, elapsed = exchange(owner)
    assert received == expected(owner)
    assert elapsed >= 0.2
    assert not owner.logs


def test_partial_progress_does_not_extend_absolute_response_budget(monkeypatch):
    assert server_module._IPC_RESPONSE_WRITE_TIMEOUT == 15.0
    monkeypatch.setattr(server_module, "_IPC_RESPONSE_WRITE_TIMEOUT", 0.25)
    owner = SyntheticClientOwner()
    received, elapsed = exchange(owner, per_read_delay=0.07)
    assert 0 < len(received) < len(expected(owner))
    assert expected(owner).startswith(received)
    assert 0.2 <= elapsed < 0.7
    assert owner.logs == [("daemon.ipc.client_error", {"errorType": "TimeoutError"})]


def test_forced_close_during_partial_write_exits_without_reissuing_handler():
    owner = SyntheticClientOwner()
    received, elapsed = exchange(owner, read_delay=0.03, close=True)
    assert len(received) < len(expected(owner))
    assert expected(owner).startswith(received)
    assert elapsed < 0.4
    assert not owner.logs


def test_unknown_sendall_only_stream_is_refused_without_sending():
    class Unknown:
        def sendall(self, _data):
            pytest.fail("unsafe sendall fallback")
    with pytest.raises(TypeError, match="response"):
        module._write_ipc_response(Unknown(), b"{}\n", timeout=1, poll_interval=0.1, should_stop=lambda: False)


def test_zero_byte_socket_write_is_broken_pipe():
    class ZeroSocket(socket.socket):
        def send(self, _data, *_args):
            return 0
    left, right = socket.socketpair()
    left = ZeroSocket(fileno=left.detach())
    try:
        with pytest.raises(BrokenPipeError):
            module._write_ipc_response(left, b"{}\n", timeout=1, poll_interval=0.1, should_stop=lambda: False)
    finally:
        left.close()
        right.close()


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.005)


def _client_count(owner):
    with owner._ipc_clients_lock:
        return len(owner._ipc_clients)


def test_request_deadline_returns_typed_timeout_and_releases_client_slot(monkeypatch):
    monkeypatch.setattr(server_module, "_IPC_REQUEST_TIMEOUT", 0.08)
    owner = SyntheticClientOwner(size=0)
    release = threading.Event()

    def blocked(_line):
        owner.entered.set()
        release.wait()
        return owner.response

    owner._response = blocked
    server, client = socket.socketpair()
    try:
        owner._start_ipc_client(server)
        client.sendall(b'{"version":1,"id":"blocked","method":"synthetic"}\n')
        assert owner.entered.wait(1)
        frame = json.loads(client.makefile("rb").readline())
        assert frame == {
            "version": 1,
            "id": "blocked",
            "error": {
                "code": "IPC_TIMEOUT",
                "message": "daemon IPC request deadline expired for method 'synthetic'",
            },
        }
        _wait_for(lambda: _client_count(owner) == 0)
    finally:
        release.set()
        client.close()
        owner._close_ipc_clients()


def test_peer_hangup_releases_slot_while_request_is_still_settling(monkeypatch):
    monkeypatch.setattr(server_module, "_IPC_REQUEST_TIMEOUT", 5.0)
    owner = SyntheticClientOwner(size=0)
    release = threading.Event()

    def blocked(_line):
        owner.entered.set()
        release.wait()
        return owner.response

    owner._response = blocked
    server, client = socket.socketpair()
    try:
        owner._start_ipc_client(server)
        client.sendall(b'{"version":1,"id":"gone","method":"synthetic"}\n')
        assert owner.entered.wait(1)
        client.close()
        _wait_for(lambda: _client_count(owner) == 0, timeout=0.5)
        assert not owner._ipc_client_threads
    finally:
        release.set()
        owner._close_ipc_clients()


@pytest.mark.perf_bound
def test_64_stuck_handlers_do_not_keep_the_65th_client_slot(monkeypatch):
    monkeypatch.setattr(server_module, "_IPC_REQUEST_TIMEOUT", 5.0)
    owner = SyntheticClientOwner(size=0)
    release = threading.Event()
    burn_stop = threading.Event()
    entered = 0
    entered_lock = threading.Lock()
    all_entered = threading.Event()

    def handle(line):
        nonlocal entered
        request = json.loads(line)
        if request["method"] == "fresh":
            return {"version": 1, "id": request["id"], "result": {"ok": True}}
        with entered_lock:
            entered += 1
            if entered == server_module._IPC_MAX_CLIENTS:
                all_entered.set()
        release.wait()
        return {"version": 1, "id": request["id"], "result": {"ok": True}}

    def burn_in_bursts():
        while not burn_stop.is_set():
            started = time.thread_time()
            while time.thread_time() - started < 0.02:
                pass
            burn_stop.wait(0.002)

    owner._response = handle
    clients = []
    burner = threading.Thread(target=burn_in_bursts, name="ipc-gil-burst")
    burner.start()
    try:
        for index in range(server_module._IPC_MAX_CLIENTS):
            server, client = socket.socketpair()
            clients.append(client)
            owner._start_ipc_client(server)
            client.sendall(
                json.dumps({"version": 1, "id": str(index), "method": "blocked"}).encode()
                + b"\n"
            )
        assert all_entered.wait(2)
        for client in clients:
            client.close()
        _wait_for(lambda: _client_count(owner) == 0, timeout=0.75)

        latencies = []
        for index in range(5):
            server, fresh = socket.socketpair()
            clients.append(fresh)
            owner._start_ipc_client(server)
            started = time.monotonic()
            fresh.sendall(
                json.dumps({"version": 1, "id": f"fresh-{index}", "method": "fresh"}).encode()
                + b"\n"
            )
            assert json.loads(fresh.makefile("rb").readline())["result"] == {"ok": True}
            latencies.append(time.monotonic() - started)
            fresh.close()
        assert max(latencies) < 2.0, latencies
    finally:
        burn_stop.set()
        burner.join(1)
        release.set()
        for client in clients:
            client.close()
        owner._close_ipc_clients()


def test_capacity_log_identifies_the_live_slot_holder(monkeypatch):
    monkeypatch.setattr(server_module, "_IPC_MAX_CLIENTS", 1)
    owner = SyntheticClientOwner(size=0)
    owner._ipc_client_slots = threading.BoundedSemaphore(1)
    release = threading.Event()

    def blocked(_line):
        owner.entered.set()
        release.wait()
        return owner.response

    owner._response = blocked
    server, client = socket.socketpair()
    overflow_server, overflow_client = socket.socketpair()
    try:
        owner._start_ipc_client(server)
        client.sendall(b'{"version":1,"id":"held","method":"synthetic"}\n')
        assert owner.entered.wait(1)
        owner._start_ipc_client(overflow_server)
        event, fields = owner.logs[-1]
        assert event == "daemon.ipc.client_capacity_exhausted"
        assert fields["clientCount"] == fields["clientLimit"] == 1
        assert fields["saturated"] is True
        assert len(fields["activeClients"]) == 1
        assert set(fields["activeClients"][0]) == {
            "peerPid",
            "connectedSinceMs",
            "lastRequestMs",
            "currentMethod",
        }
        assert fields["activeClients"][0]["currentMethod"] == "synthetic"
    finally:
        release.set()
        client.close()
        overflow_client.close()
        owner._close_ipc_clients()
