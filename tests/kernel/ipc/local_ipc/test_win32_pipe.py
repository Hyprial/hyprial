"""The Windows named-pipe backend behind the local IPC seam.

The four end-to-end cases need a real Windows host: the default backend there
is the current-user named pipe (DACL-restricted, no OS peer credentials), and
the endpoint file keeps its ``windows-pipe-v1`` document format.

Decision record (2026-11-26): the deterministic ``PipeListener``
accept/close concurrency tests load ``windows_pipe.py`` in isolation with fake
Win32 modules and ``threading.Event`` barriers so they run on Linux as well as
Windows without sleeping for races.
"""

from __future__ import annotations

import errno
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
import types
from typing import Any

import pytest

from hyprial.kernel import (
    LocalIpcPeer,
    listen_local_ipc,
    local_ipc_path_limit,
)

from .test_contract import close_pair, connect_pair, read_frame

_WIN32_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="the real named-pipe backend is Windows only"
)
_CLOSE_BOUND_SECONDS = 2.0
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_INFINITE = 0xFFFFFFFF


class _FakeWinError(Exception):
    def __init__(self, code: int) -> None:
        self.winerror = code
        super().__init__(code)


class _FakeEvent:
    def __init__(self, io: _FakePipeWin32, *, initial_state: bool = False) -> None:
        self._io = io
        self._signal = threading.Event()
        if initial_state:
            self._signal.set()
        self._operation: _FakeOverlapped | None = None
        self.closed = False
        self.close_count = 0

    def Close(self) -> None:
        with self._io._lock:
            if self._io.listener_closed:
                self._io._record("event closed after listener.close returned")
            self.close_count += 1
            if self.closed:
                self._io._record("event closed more than once")
                raise AssertionError("event closed more than once")
            if self._operation is not None and self._operation.pending:
                self._io._record("event closed while overlapped operation was pending")
                raise AssertionError(
                    "event closed while overlapped operation was pending"
                )
            self.closed = True


class _FakeOverlapped:
    def __init__(self) -> None:
        self.hEvent: _FakeEvent | None = None
        self.pending = False
        self.canceled = False
        self.connected = False
        self.result_calls = 0


class _FakeHandle:
    def __init__(self, io: _FakePipeWin32) -> None:
        self._io = io
        self.operation: _FakeOverlapped | None = None
        self.closed = False
        self.close_count = 0
        self.accepted_stream = False

    def Close(self) -> None:
        with self._io._lock:
            if self._io.listener_closed and not self.accepted_stream:
                self._io._record(
                    "listener handle closed after listener.close returned"
                )
            self.close_count += 1
            if self.closed:
                self._io._record("handle closed more than once")
                raise AssertionError("handle closed more than once")
            if self.operation is not None and self.operation.pending:
                self._io._record(
                    "handle closed while overlapped operation was pending"
                )
                raise AssertionError(
                    "handle closed while overlapped operation was pending"
                )
            self.closed = True


class _FakeToken:
    def Close(self) -> None:
        return None


class _FakeAcl:
    def AddAccessAllowedAce(self, _rev: int, _access: int, _sid: object) -> None:
        return None


class _FakeSecurityDescriptor:
    def SetSecurityDescriptorDacl(
        self, _present: bool, _dacl: object, _defaulted: bool
    ) -> None:
        return None


class _FakeSecurityAttributes:
    def __init__(self) -> None:
        self.SECURITY_DESCRIPTOR: object = None


class _FakePipeWin32:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.handles: list[_FakeHandle] = []
        self.operations: list[_FakeOverlapped] = []
        self.events: list[_FakeEvent] = []
        self.violations: list[str] = []
        self.listener_closed = False
        self.on_wait_enter: Any = None
        self.on_wait_exit: Any = None
        self.on_get_result: Any = None
        self.on_cancel_exit: Any = None

    def _record(self, message: str) -> None:
        with self._lock:
            self.violations.append(message)

    def mark_listener_closed(
        self, accepted_handles: tuple[_FakeHandle, ...] = ()
    ) -> None:
        with self._lock:
            for handle in accepted_handles:
                handle.accepted_stream = True
            self.listener_closed = True

    def create_event(
        self,
        _sa: object,
        _manual_reset: bool,
        initial_state: bool,
        _name: object,
    ) -> _FakeEvent:
        event = _FakeEvent(self, initial_state=bool(initial_state))
        with self._lock:
            self.events.append(event)
        return event

    def set_event(self, event: _FakeEvent | None) -> None:
        with self._lock:
            if self.listener_closed:
                self._record("SetEvent called after listener.close returned")
                raise AssertionError("SetEvent called after listener.close returned")
            if event is None or event.closed:
                self._record("SetEvent called on closed or None event")
                raise AssertionError("SetEvent called on closed or None event")
            event._signal.set()

    def create_overlapped(self) -> _FakeOverlapped:
        operation = _FakeOverlapped()
        with self._lock:
            self.operations.append(operation)
        return operation

    def create_named_pipe(self, *_args: object) -> _FakeHandle:
        handle = _FakeHandle(self)
        with self._lock:
            self.handles.append(handle)
        return handle

    def connect_named_pipe(
        self, handle: _FakeHandle | None, operation: _FakeOverlapped | None
    ) -> int:
        with self._lock:
            if handle is None or handle.closed:
                self._record("ConnectNamedPipe called on closed or None handle")
                raise AssertionError("ConnectNamedPipe called on closed or None handle")
            if operation is None or operation.hEvent is None or operation.hEvent.closed:
                self._record("ConnectNamedPipe called on closed or None event")
                raise AssertionError("ConnectNamedPipe called on closed or None event")
            handle.operation = operation
            operation.hEvent._operation = operation
            operation.pending = True
        return 997

    def complete_connect(self, operation: _FakeOverlapped) -> None:
        with self._lock:
            operation.connected = True
            assert operation.hEvent is not None
            operation.hEvent._signal.set()

    def cancel(self, handle: _FakeHandle | None) -> None:
        with self._lock:
            if self.listener_closed:
                self._record("CancelIoEx called after listener.close returned")
                raise AssertionError(
                    "CancelIoEx called after listener.close returned"
                )
            if handle is None or handle.closed:
                self._record("CancelIoEx called on closed or None handle")
                raise AssertionError("CancelIoEx called on closed or None handle")
            operation = handle.operation
            if operation is not None and operation.pending and not operation.connected:
                operation.canceled = True
                assert operation.hEvent is not None
                operation.hEvent._signal.set()
        if self.on_cancel_exit is not None:
            self.on_cancel_exit(handle)

    def wait(self, event: _FakeEvent | None, milliseconds: int) -> int:
        with self._lock:
            if self.listener_closed:
                self._record("WaitForSingleObject called after listener.close returned")
                raise AssertionError(
                    "WaitForSingleObject called after listener.close returned"
                )
            if event is None or event.closed:
                self._record("WaitForSingleObject called on closed or None event")
                raise AssertionError(
                    "WaitForSingleObject called on closed or None event"
                )
        if self.on_wait_enter is not None:
            self.on_wait_enter(event)
        timeout_s = (
            10.0 if milliseconds == _INFINITE else max(0.0, milliseconds / 1000.0)
        )
        signaled = event._signal.wait(timeout_s)
        state = _WAIT_OBJECT_0 if signaled else _WAIT_TIMEOUT
        if self.on_wait_exit is not None:
            state = self.on_wait_exit(event, state)
        with self._lock:
            if self.listener_closed:
                self._record(
                    "WaitForSingleObject finished after listener.close returned"
                )
                raise AssertionError(
                    "WaitForSingleObject finished after listener.close returned"
                )
            if event.closed:
                self._record("event closed while WaitForSingleObject was active")
                raise AssertionError(
                    "event closed while WaitForSingleObject was active"
                )
        return state

    def get_overlapped_result(
        self,
        handle: _FakeHandle | None,
        operation: _FakeOverlapped | None,
        wait: bool,
    ) -> int:
        assert wait is True
        with self._lock:
            if self.listener_closed:
                self._record(
                    "GetOverlappedResult called after listener.close returned"
                )
                raise AssertionError(
                    "GetOverlappedResult called after listener.close returned"
                )
            if handle is None or handle.closed:
                self._record("GetOverlappedResult called on closed or None handle")
                raise AssertionError(
                    "GetOverlappedResult called on closed or None handle"
                )
            if operation is None or operation.hEvent is None or operation.hEvent.closed:
                self._record("GetOverlappedResult called on closed or None event")
                raise AssertionError(
                    "GetOverlappedResult called on closed or None event"
                )
        if self.on_get_result is not None:
            self.on_get_result(handle, operation)
        with self._lock:
            if self.listener_closed:
                self._record(
                    "GetOverlappedResult settled after listener.close returned"
                )
                raise AssertionError(
                    "GetOverlappedResult settled after listener.close returned"
                )
            if handle.closed:
                self._record("handle closed during GetOverlappedResult")
                raise AssertionError("handle closed during GetOverlappedResult")
            assert operation.hEvent is not None
            if operation.hEvent.closed:
                self._record("event closed during GetOverlappedResult")
                raise AssertionError("event closed during GetOverlappedResult")
            operation.result_calls += 1
            if operation.result_calls > 1:
                self._record(
                    "GetOverlappedResult called more than once on same OVERLAPPED"
                )
                raise AssertionError(
                    "GetOverlappedResult called more than once on same OVERLAPPED"
                )
            if operation.canceled:
                operation.pending = False
                raise _FakeWinError(995)
            if operation.connected:
                operation.pending = False
                return 0
            signal = operation.hEvent._signal
        assert signal.wait(2.0), "GetOverlappedResult waited on an unsignaled operation"
        with self._lock:
            if self.listener_closed:
                self._record(
                    "GetOverlappedResult completed after listener.close returned"
                )
                raise AssertionError(
                    "GetOverlappedResult completed after listener.close returned"
                )
            if handle.closed:
                self._record("handle closed while GetOverlappedResult waited")
                raise AssertionError("handle closed while GetOverlappedResult waited")
            assert operation.hEvent is not None
            if operation.hEvent.closed:
                self._record("event closed while GetOverlappedResult waited")
                raise AssertionError("event closed while GetOverlappedResult waited")
            if operation.canceled:
                operation.pending = False
                raise _FakeWinError(995)
            if operation.connected:
                operation.pending = False
                return 0
            raise AssertionError("operation signaled without connect or cancel")


@pytest.fixture
def fake_pipe_module(monkeypatch: pytest.MonkeyPatch):
    io = _FakePipeWin32()
    for name in (
        "pywintypes",
        "win32api",
        "win32con",
        "win32event",
        "win32file",
        "win32pipe",
        "win32security",
    ):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))

    pywintypes: Any = sys.modules["pywintypes"]
    pywintypes.error = _FakeWinError
    pywintypes.OVERLAPPED = io.create_overlapped
    pywintypes.SECURITY_ATTRIBUTES = _FakeSecurityAttributes

    win32api: Any = sys.modules["win32api"]
    win32api.GetCurrentProcess = lambda: 1

    win32con: Any = sys.modules["win32con"]
    win32con.TOKEN_QUERY = 8
    win32con.GENERIC_ALL = 0x10000000
    win32con.GENERIC_READ = 0x80000000
    win32con.GENERIC_WRITE = 0x40000000
    win32con.OPEN_EXISTING = 3

    win32event: Any = sys.modules["win32event"]
    win32event.INFINITE = _INFINITE
    win32event.WAIT_OBJECT_0 = _WAIT_OBJECT_0
    win32event.WAIT_TIMEOUT = _WAIT_TIMEOUT
    win32event.CreateEvent = io.create_event
    win32event.SetEvent = io.set_event
    win32event.WaitForSingleObject = io.wait

    win32file: Any = sys.modules["win32file"]
    win32file.FILE_FLAG_OVERLAPPED = 0x40000000
    win32file.GetOverlappedResult = io.get_overlapped_result

    win32pipe: Any = sys.modules["win32pipe"]
    win32pipe.PIPE_ACCESS_DUPLEX = 3
    win32pipe.PIPE_TYPE_BYTE = 0
    win32pipe.PIPE_READMODE_BYTE = 0
    win32pipe.PIPE_WAIT = 0
    win32pipe.CreateNamedPipe = io.create_named_pipe
    win32pipe.ConnectNamedPipe = io.connect_named_pipe

    win32security: Any = sys.modules["win32security"]
    win32security.ACL_REVISION = 2
    win32security.TokenUser = 1
    win32security.OpenProcessToken = lambda _proc, _access: _FakeToken()
    win32security.GetTokenInformation = lambda _token, _cls: ("S-1-5-21-fake", 0)
    win32security.ACL = _FakeAcl
    win32security.SECURITY_DESCRIPTOR = _FakeSecurityDescriptor
    win32security.ConvertStringSidToSid = lambda sid: sid

    path = (
        Path(__file__).resolve().parents[4]
        / "src/hyprial/kernel/impl/manysys/ipc/windows_pipe.py"
    )
    spec = importlib.util.spec_from_file_location("_isolated_windows_pipe_listener", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_cancel", io.cancel)
    return module, io


@_WIN32_ONLY
def test_one_frame_round_trips_over_the_named_pipe(tmp_path) -> None:
    pair = connect_pair(tmp_path / "daemon.sock")
    try:
        pair.client.sendall(b'{"version":1,"id":"w1","method":"ping"}\n')
        assert read_frame(pair.stream)["id"] == "w1"
        reply = b'{"version":1,"id":"w1","result":{"ok":true}}\n'
        assert pair.stream.send_response(
            reply, timeout=2.0, poll_interval=0.05, should_stop=lambda: False
        ) is True
        assert read_frame(pair.client)["result"] == {"ok": True}
    finally:
        close_pair(pair)


@_WIN32_ONLY
def test_the_endpoint_file_keeps_the_pipe_document_format(tmp_path) -> None:
    listener = listen_local_ipc(tmp_path / "daemon.sock")
    try:
        document = json.loads((tmp_path / "daemon.sock").read_text(encoding="utf-8"))
        assert document["transport"] == "windows-pipe-v1"
        assert isinstance(document["pipe"], str)
    finally:
        listener.close()


@_WIN32_ONLY
def test_the_pipe_peer_has_no_credentials_but_enforces_same_user(tmp_path) -> None:
    pair = connect_pair(tmp_path / "daemon.sock")
    try:
        assert pair.peer == LocalIpcPeer(pid=None, uid=None, same_user_enforced=True)
    finally:
        close_pair(pair)


@_WIN32_ONLY
def test_the_pipe_backend_has_no_path_limit() -> None:
    assert local_ipc_path_limit() is None


def test_close_interrupts_waiting_accept_within_bound(
    fake_pipe_module, tmp_path: Path
) -> None:
    module, io = fake_pipe_module
    listener = module.PipeListener(tmp_path / "daemon.sock")
    listener.settimeout(0.25)
    accept_in_wait = threading.Event()
    io.on_wait_enter = lambda _event: accept_in_wait.set()

    errors: list[BaseException] = []

    def run_accept() -> None:
        try:
            listener.accept()
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=run_accept)
    worker.start()
    try:
        assert accept_in_wait.wait(_CLOSE_BOUND_SECONDS)
        started = time.monotonic()
        listener.close()
        elapsed = time.monotonic() - started
        io.mark_listener_closed()
        worker.join(_CLOSE_BOUND_SECONDS)
        assert not worker.is_alive()
        assert elapsed < _CLOSE_BOUND_SECONDS
        assert len(errors) == 1
        assert isinstance(errors[0], OSError)
        assert errors[0].errno == errno.EBADF
        assert not isinstance(errors[0], TimeoutError)
        assert io.violations == []
        assert len(io.handles) == 1 and io.handles[0].close_count == 1
        assert len(io.events) == 1 and io.events[0].close_count == 1
        assert io.operations[0].result_calls == 1
    finally:
        if worker.is_alive():
            listener.close()
            worker.join(_CLOSE_BOUND_SECONDS)


def test_close_waits_for_in_flight_accept_settlement_before_returning(
    fake_pipe_module, tmp_path: Path
) -> None:
    module, io = fake_pipe_module
    listener = module.PipeListener(tmp_path / "daemon.sock")
    listener.settimeout(0.25)
    first_handle = io.handles[0]
    first_event = io.events[0]

    accept_in_wait = threading.Event()
    in_get_result = threading.Event()
    wake_main = threading.Event()
    release_get_result = threading.Event()
    close_done = threading.Event()
    accept_errors: list[BaseException] = []
    close_errors: list[BaseException] = []

    def run_accept() -> None:
        try:
            listener.accept()
        except BaseException as error:
            accept_errors.append(error)
        finally:
            wake_main.set()

    def run_close() -> None:
        try:
            listener.close()
        except BaseException as error:
            close_errors.append(error)
        finally:
            close_done.set()

    accept_thread = threading.Thread(target=run_accept)
    close_thread = threading.Thread(target=run_close)

    def on_get_result(_handle: _FakeHandle, _operation: _FakeOverlapped) -> None:
        if threading.current_thread() is accept_thread:
            in_get_result.set()
            wake_main.set()
            assert release_get_result.wait(_CLOSE_BOUND_SECONDS)

    io.on_wait_enter = lambda _event: accept_in_wait.set()
    io.on_get_result = on_get_result

    accept_thread.start()
    try:
        assert accept_in_wait.wait(_CLOSE_BOUND_SECONDS)
        started = time.monotonic()
        close_thread.start()
        assert wake_main.wait(_CLOSE_BOUND_SECONDS)
        assert in_get_result.is_set(), (
            f"accept did not reach settlement cleanly: violations={io.violations!r}, "
            f"errors={accept_errors!r}"
        )
        assert not first_handle.closed
        assert not first_event.closed
        assert not close_done.is_set()
        release_get_result.set()
        close_thread.join(_CLOSE_BOUND_SECONDS)
        elapsed = time.monotonic() - started
        io.mark_listener_closed()
        accept_thread.join(_CLOSE_BOUND_SECONDS)
        assert not close_thread.is_alive()
        assert not accept_thread.is_alive()
        assert close_done.is_set()
        assert elapsed < _CLOSE_BOUND_SECONDS
        assert close_errors == []
        assert len(accept_errors) == 1
        assert isinstance(accept_errors[0], OSError)
        assert accept_errors[0].errno == errno.EBADF
        assert io.violations == []
        assert first_handle.close_count == 1
        assert first_event.close_count == 1
    finally:
        release_get_result.set()
        close_thread.join(_CLOSE_BOUND_SECONDS)
        accept_thread.join(_CLOSE_BOUND_SECONDS)


def test_close_racing_successful_accept_never_uses_closed_handles(
    fake_pipe_module, tmp_path: Path
) -> None:
    module, io = fake_pipe_module
    listener = module.PipeListener(tmp_path / "daemon.sock")
    io.complete_connect(io.operations[0])

    in_get_result = threading.Event()
    release_get_result = threading.Event()
    cancel_called = threading.Event()
    accepted_streams: list[Any] = []
    accept_errors: list[BaseException] = []
    close_errors: list[BaseException] = []

    def run_accept() -> None:
        try:
            stream, _addr = listener.accept()
            accepted_streams.append(stream)
        except BaseException as error:
            accept_errors.append(error)

    def run_close() -> None:
        try:
            listener.close()
        except BaseException as error:
            close_errors.append(error)

    accept_thread = threading.Thread(target=run_accept)
    close_thread = threading.Thread(target=run_close)

    def on_get_result(_handle: _FakeHandle, _operation: _FakeOverlapped) -> None:
        if threading.current_thread() is accept_thread:
            in_get_result.set()
            assert release_get_result.wait(_CLOSE_BOUND_SECONDS)

    io.on_get_result = on_get_result
    io.on_cancel_exit = lambda _handle: cancel_called.set()

    accept_thread.start()
    try:
        assert in_get_result.wait(_CLOSE_BOUND_SECONDS)
        close_thread.start()
        assert cancel_called.wait(_CLOSE_BOUND_SECONDS)
        release_get_result.set()
        close_thread.join(_CLOSE_BOUND_SECONDS)
        accept_thread.join(_CLOSE_BOUND_SECONDS)
        assert not close_thread.is_alive()
        assert not accept_thread.is_alive()
        assert close_errors == []
        if accepted_streams:
            assert accept_errors == []
            stream = accepted_streams[0]
            assert not stream.handle.closed
            io.mark_listener_closed(accepted_handles=(stream.handle,))
            stream.close()
        else:
            assert len(accept_errors) == 1
            assert isinstance(accept_errors[0], OSError)
            assert accept_errors[0].errno == errno.EBADF
            io.mark_listener_closed()
        assert io.violations == []
        assert all(event.close_count == 1 for event in io.events)
        assert all(handle.close_count == 1 for handle in io.handles)
    finally:
        release_get_result.set()
        close_thread.join(_CLOSE_BOUND_SECONDS)
        accept_thread.join(_CLOSE_BOUND_SECONDS)


def test_close_after_accept_keeps_accepted_stream_open_and_closes_next_pipe(
    fake_pipe_module, tmp_path: Path
) -> None:
    module, io = fake_pipe_module
    listener = module.PipeListener(tmp_path / "daemon.sock")
    io.complete_connect(io.operations[0])
    stream, addr = listener.accept()
    assert addr is None
    assert len(io.handles) == 2
    assert io.events[0].close_count == 1
    assert not io.handles[0].closed
    listener.close()
    io.mark_listener_closed(accepted_handles=(stream.handle,))
    assert not stream.handle.closed
    assert io.handles[1].close_count == 1
    assert io.events[1].close_count == 1
    stream.close()
    assert io.handles[0].close_count == 1
    assert io.violations == []


def test_accept_after_close_raises_oserror_and_close_is_idempotent(
    fake_pipe_module, tmp_path: Path
) -> None:
    module, io = fake_pipe_module
    listener = module.PipeListener(tmp_path / "daemon.sock")
    listener.close()
    listener.close()
    io.mark_listener_closed()
    with pytest.raises(OSError):
        listener.accept()
    assert io.violations == []
    assert len(io.handles) == 1 and io.handles[0].close_count == 1
    assert len(io.events) == 1 and io.events[0].close_count == 1


def test_accept_timeout_reuses_pending_pipe_and_race_with_close_disposes_once(
    fake_pipe_module, tmp_path: Path
) -> None:
    module, io = fake_pipe_module
    listener = module.PipeListener(tmp_path / "daemon.sock")
    listener.settimeout(0.01)
    with pytest.raises(TimeoutError):
        listener.accept()
    assert len(io.handles) == 1 and io.handles[0].close_count == 0
    assert len(io.events) == 1 and io.events[0].close_count == 0

    wait_timed_out = threading.Event()
    cancel_called = threading.Event()
    accept_errors: list[BaseException] = []

    def on_wait_exit(_event: _FakeEvent, _state: int) -> int:
        wait_timed_out.set()
        assert cancel_called.wait(_CLOSE_BOUND_SECONDS)
        return _WAIT_TIMEOUT

    io.on_wait_exit = on_wait_exit
    io.on_cancel_exit = lambda _handle: cancel_called.set()

    def run_accept() -> None:
        try:
            listener.accept()
        except BaseException as error:
            accept_errors.append(error)

    worker = threading.Thread(target=run_accept)
    worker.start()
    try:
        assert wait_timed_out.wait(_CLOSE_BOUND_SECONDS)
        started = time.monotonic()
        listener.close()
        elapsed = time.monotonic() - started
        io.mark_listener_closed()
        worker.join(_CLOSE_BOUND_SECONDS)
        assert not worker.is_alive()
        assert elapsed < _CLOSE_BOUND_SECONDS
        assert len(accept_errors) == 1
        assert isinstance(accept_errors[0], OSError)
        assert accept_errors[0].errno == errno.EBADF
        assert not isinstance(accept_errors[0], TimeoutError)
        assert io.violations == []
        assert io.handles[0].close_count == 1
        assert io.events[0].close_count == 1
    finally:
        cancel_called.set()
        worker.join(_CLOSE_BOUND_SECONDS)


def test_init_endpoint_write_failure_closes_prepared_pipe_once(
    fake_pipe_module, tmp_path: Path
) -> None:
    module, io = fake_pipe_module
    with pytest.raises(OSError):
        module.PipeListener(tmp_path)
    assert io.violations == []
    assert len(io.handles) == 1 and io.handles[0].close_count == 1
    assert len(io.events) == 1 and io.events[0].close_count == 1

