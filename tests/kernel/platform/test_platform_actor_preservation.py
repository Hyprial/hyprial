from __future__ import annotations

import time
import types
from pathlib import Path
from uuid import uuid4

import pytest

import hyprial.daemon.impl.ipc.server as application_module
import hyprial.daemon.impl.configuration.home_guard as home_guard_module
import hyprial.daemon.impl.mcp.channel.ownership as channel_module
import hyprial.daemon.impl.mcp.unix as unix_module
import hyprial.kernel as kernel_module
import hyprial.kernel.impl.ipc.client as ipc_client
import hyprial.kernel.impl.platform.ports as ports_module
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.kernel import ProcessLivenessState
from hyprial.kernel import ProcessLiveness
from hyprial.daemon.impl.processes.process_owner import ProcessOwner, own_process
from hyprial.daemon.impl.configuration.home_lease_actor import HomeLeaseAuthority
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies


class _FakePipeListener:
    instances: list["_FakePipeListener"] = []

    def __init__(self, endpoint: Path, *, gui_write_sid: str | None = None) -> None:
        self.endpoint = endpoint
        self.gui_write_sid = gui_write_sid
        self.closed = False
        self.instances.append(self)

    def accept(self):  # type: ignore[no-untyped-def]
        raise TimeoutError

    def close(self) -> None:
        self.closed = True


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


def _windows_pipe_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kernel_module, "listen_named_pipe", _FakePipeListener)
    monkeypatch.setattr(application_module, "_windows_ipc_enabled", lambda: True)


def test_windows_listener_starts_common_ipc_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path)
    _FakePipeListener.instances = []
    _windows_pipe_module(monkeypatch)
    try:
        application._start_server()

        assert isinstance(application._server, _FakePipeListener)
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
    _windows_pipe_module(monkeypatch)

    class BrokenDiagnostics:
        def __init__(self) -> None:
            raise RuntimeError("injected diagnostics failure")

    monkeypatch.setattr(application_module, "DispatchDiagnostics", BrokenDiagnostics)
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
        self.dsh_home = None
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


def test_job_backed_raw_process_remains_behind_process_owner() -> None:
    raw = _FakeJobBackedProcess()
    owner = own_process(raw)
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


def test_mcp_windows_connection_uses_pipe_stream_without_posix_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = _FakePipeStream()
    monkeypatch.setattr(kernel_module, "connect_named_pipe", lambda path, timeout: stream)
    monkeypatch.setattr(unix_module, "os", types.SimpleNamespace(name="nt"))

    result = unix_module.UnixDaemonConnection(
        tmp_path / "daemon.pipe", timeout=0.25
    )._request_sync("ping", {}, "request")

    assert result == {"ok": True}
    assert b'"method":"ping"' in stream.sent
    assert stream.closed


def test_kernel_client_windows_connection_uses_pipe_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = _FakePipeStream()
    calls = []

    def connect(path, timeout):
        calls.append((path, timeout))
        return stream

    monkeypatch.setattr(ports_module, "connect_named_pipe", connect)
    monkeypatch.setattr(ipc_client, "os", types.SimpleNamespace(name="nt"))
    endpoint = tmp_path / "daemon.pipe"
    assert ipc_client._connect_daemon_socket(endpoint, 0.25) is stream
    assert calls == [(endpoint, 0.25)]


def test_windows_owner_status_uses_read_only_process_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kernel_module, "windows_process_identity", lambda pid: f"win-filetime:{pid}")
    monkeypatch.setattr(channel_module.sys, "platform", "win32")

    status = channel_module._owner_process_status(7, "win-filetime:7")

    assert status.value == "alive"


def test_home_lease_claim_renew_release_uses_platform_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
