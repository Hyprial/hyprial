from __future__ import annotations

import json
import sys
import threading
import time
from contextlib import AbstractContextManager
from pathlib import Path
from uuid import uuid4

import pytest

import hyprial.daemon.impl.ipc.server as application_module
import hyprial.daemon.impl.configuration.home_guard as home_guard_module
import hyprial.daemon.impl.mcp.channel.ownership as channel_module
import hyprial.daemon.impl.mcp.unix as unix_module
import hyprial.kernel as kernel_module
import hyprial.kernel.impl.ipc.client as ipc_client
import hyprial.kernel.impl.manysys.ipc.windows as local_ipc_windows
import hyprial.kernel.impl.manysys.process.ports as process_ports_module
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.kernel import (
    LocalIpcListener,
    LocalIpcPeer,
    ProcessLiveness,
    ProcessLivenessState,
    listen_local_ipc,
)
from hyprial.daemon.impl.processes.process_owner import ProcessOwner, own_process
from hyprial.daemon.impl.configuration.home_lease_actor import HomeLeaseAuthority
from hyprial.kernel.impl.manysys.ipc import local_ipc_backend
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from tests.kernel.ipc.local_ipc.memory_backend import MemoryPipeBackend


class _FakePipeListener:
    instances: list["_FakePipeListener"] = []

    def __init__(self, endpoint: Path, *, gui_write_sid: str | None = None) -> None:
        self.endpoint = endpoint
        self.gui_write_sid = gui_write_sid
        self.closed = False
        self.instances.append(self)

    def accept(self):  # type: ignore[no-untyped-def]
        raise TimeoutError

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = timeout

    def close(self) -> None:
        self.closed = True


class _FakeWindowsPipeBackend:
    """The Windows selection simulated: a pipe listener with no credentials."""

    def listen(self, endpoint: Path, *, backlog: int, gui_write_sid: str | None):
        endpoint.write_text("{}", encoding="utf-8")
        return LocalIpcListener(
            _FakePipeListener(endpoint, gui_write_sid=gui_write_sid),
            peer_factory=lambda _connection: LocalIpcPeer(
                pid=None, uid=None, same_user_enforced=True
            ),
        )

    def connect(self, endpoint: Path, timeout: float, *, clock: object | None = None):
        raise NotImplementedError("listener-only fake backend")

    def path_limit(self) -> None:
        return None


def _application(tmp_path: Path) -> DaemonApplication:
    application = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=Path("/tmp") / f"hyprial-platform-{uuid4().hex}.sock",
        node_id="platform-test",
        owner="owner",
    **_deep_module_daemon_dependencies())
    # Direct construction never opens transport; make the isolation contract
    # explicit before any startup method is invoked.
    application.network_isolated = True
    application.zenoh_listen = ("tcp/127.0.0.1:0",)
    application.zenoh_connect = ()
    return application


def _windows_pipe_module(monkeypatch: pytest.MonkeyPatch) -> "AbstractContextManager[object]":
    monkeypatch.setattr(application_module, "_accept_reserve_fd_supported", lambda: False)
    return local_ipc_backend(_FakeWindowsPipeBackend())


def test_windows_listener_starts_common_ipc_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path)
    _FakePipeListener.instances = []
    with _windows_pipe_module(monkeypatch):
        try:
            application._start_server()

            assert isinstance(application._server, LocalIpcListener)
            assert application._server._listener is _FakePipeListener.instances[-1]
            assert application._ipc_request_owner is not None
            assert application._dispatch_diagnostics is not None
            assert application._accept_reserve_fd is None
        finally:
            application._close()
            application._close_home_lease()
    assert _FakePipeListener.instances[-1].closed


def test_windows_listener_rolls_back_owner_when_diagnostics_construction_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path)
    _FakePipeListener.instances = []

    class BrokenDiagnostics:
        def __init__(self, **kwargs: object) -> None:
            raise RuntimeError("injected diagnostics failure")

    monkeypatch.setattr(application_module, "DispatchDiagnostics", BrokenDiagnostics)
    with _windows_pipe_module(monkeypatch):
        try:
            with pytest.raises(RuntimeError, match="injected diagnostics failure"):
                application._start_server()
            assert application._server is None
            assert application._ipc_request_owner is None
            assert application._dispatch_diagnostics is None
            assert _FakePipeListener.instances[-1].closed
        finally:
            application._close()
            application._close_home_lease()


def test_posix_listener_keeps_request_owner_and_reserve_fd(tmp_path: Path) -> None:
    application = _application(tmp_path)
    try:
        application._start_server()
        assert application._server is not None
        assert application._ipc_request_owner is not None
        assert application._dispatch_diagnostics is not None
        assert application._accept_reserve_fd is not None
    finally:
        application._close()
        application._close_home_lease()


class _FakeJobBackedProcess:
    def __init__(self) -> None:
        self.running = True
        self.pid = 4242
        self.last_error = None
        self.session_ref = None
        self.worker_channel = None
        self.endpoint = None
        self.max_in_flight = None
        self.in_flight = None
        self.queue_depth = None
        self.job_stops = 0

    def liveness(self) -> ProcessLiveness:
        return ProcessLiveness(
            ProcessLivenessState.ALIVE if self.running else ProcessLivenessState.DEAD,
            observed=True,
            pid=self.pid,
            marker="win-filetime:1",
        )

    def stop(self) -> None:
        self.job_stops += 1
        self.running = False


def test_job_backed_raw_process_remains_behind_process_owner(actor_host) -> None:
    raw = _FakeJobBackedProcess()
    owner = own_process(raw, actor_host=actor_host)
    assert isinstance(owner, ProcessOwner)
    assert owner.running

    owner.stop()

    assert raw.job_stops == 1
    assert not raw.running


class _FakePipeStream:
    def __init__(self) -> None:
        self.sent = b""
        self.closed = False
        self._response = b'{"version":1,"id":"request","result":{"ok":true}}\n'

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, _size: int) -> bytes:
        response, self._response = self._response, b""
        return response

    def close(self) -> None:
        self.closed = True


def test_mcp_connection_round_trips_without_posix_socket(tmp_path: Path) -> None:
    frames: list[dict] = []
    stop = threading.Event()
    with local_ipc_backend(MemoryPipeBackend()):
        listener = listen_local_ipc(tmp_path / "daemon.sock")

        def serve() -> None:
            listener.settimeout(0.1)
            while not stop.is_set():
                try:
                    connection, _peer = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    return
                try:
                    connection.settimeout(5.0)
                    buffer = bytearray()
                    while b"\n" not in buffer:
                        chunk = connection.recv(64 * 1024)
                        if not chunk:
                            break
                        buffer.extend(chunk)
                    if b"\n" not in buffer:
                        continue
                    frame = json.loads(buffer.partition(b"\n")[0])
                    frames.append(frame)
                    reply = {"version": 1, "id": frame["id"], "result": {"ok": True}}
                    connection.sendall(json.dumps(reply).encode() + b"\n")
                except OSError:
                    pass
                finally:
                    connection.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            result = unix_module.UnixDaemonConnection(
                tmp_path / "daemon.sock", timeout=1.0
            )._request_sync("ping", {}, "request")
        finally:
            stop.set()
            listener.close()
            thread.join(timeout=5)
    assert result == {"ok": True}
    assert frames == [{"version": 1, "id": "request", "method": "ping", "params": {}}]


def test_kernel_client_windows_connection_uses_pipe_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = _FakePipeStream()
    calls = []

    class _FakeWindowsClientBackend:
        def connect(self, endpoint, timeout, *, clock=None):  # type: ignore[no-untyped-def]
            calls.append((endpoint, timeout))
            return stream

    monkeypatch.setattr(local_ipc_windows, "WindowsPipeBackend", _FakeWindowsClientBackend)
    monkeypatch.setattr(sys, "platform", "win32")
    endpoint = tmp_path / "daemon.pipe"
    assert ipc_client._connect_daemon_socket(endpoint, 0.25) is stream
    assert calls == [(endpoint, 0.25)]


def test_windows_owner_status_uses_read_only_process_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kernel_module, "windows_process_identity", lambda pid: f"win-filetime:{pid}")
    monkeypatch.setattr(process_ports_module, "windows_process_identity", lambda pid: f"win-filetime:{pid}")
    monkeypatch.setattr(channel_module.sys, "platform", "win32")

    status = channel_module._owner_process_status(7, "win-filetime:7")
    legacy_status = channel_module._owner_process_status(7, "win32:7")
    reused_status = channel_module._owner_process_status(7, "win32:8")

    assert status.value == "alive"
    assert legacy_status.value == "alive"
    assert reused_status.value == "identity-mismatch"


def test_home_lease_claim_renew_release_uses_platform_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, actor_host, generation_scheduler
) -> None:
    calls: list[int] = []
    original = home_guard_module.lock_exclusive

    def observed(descriptor: int, *, blocking: bool = True) -> None:
        calls.append(descriptor)
        original(descriptor, blocking=blocking)

    monkeypatch.setattr(home_guard_module, "lock_exclusive", observed)
    lease = HomeLeaseAuthority(
        tmp_path,
        keepalive_duration=0.03,
        identity_reader=lambda _pid: "synthetic-identity",
        actor_host=actor_host,
        scheduler=generation_scheduler,
    )
    try:
        lease.claim()
        deadline = time.monotonic() + 1.0
        while len(calls) < 2:
            assert time.monotonic() < deadline
            time.sleep(0.005)
    finally:
        assert lease.close(2.0)
    assert len(calls) >= 3
