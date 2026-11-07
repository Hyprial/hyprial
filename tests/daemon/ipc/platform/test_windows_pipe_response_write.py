"""Fake Win32 overlapped I/O controls; runnable without a Windows host."""
from __future__ import annotations

from collections import deque
import importlib.util
from pathlib import Path
import sys
import threading
import types

import pytest

from tests.daemon.ipc.response.test_ipc_response_write import SyntheticClientOwner
from hyprial.daemon.impl.ipc import server as application


class WinError(Exception):
    def __init__(self, code):
        self.winerror = code
        super().__init__(code)


class WinIo:
    def __init__(self):
        self.now = 0.0
        self.waits = deque()
        self.results = deque()
        self.writes = []
        self.operations = []
        self.cancels = 0
        self.cancel_error = None
        self.handle_closed = 0
        self.sleeps = []
        self.calls = []
        model = self
        class Handle:
            def Close(self):
                assert not any(op.pending for op in model.operations)
                model.handle_closed += 1
                model.calls.append("handle.close")
        self.handle = Handle()

    def operation(self):
        model = self
        operation = types.SimpleNamespace(pending=False, buffer=None, closed=False)
        class Event:
            def Close(self):
                assert not operation.pending, "freed an unsettled OVERLAPPED event"
                operation.closed = True
                model.calls.append("event.close")
        operation.hEvent = Event()
        self.operations.append(operation)
        return operation

    def write(self, handle, data, operation):
        assert handle is self.handle
        assert not any(op.pending for op in self.operations if op is not operation)
        operation.pending = True
        operation.buffer = data
        self.writes.append((data, operation))
        return 997, None

    def read(self, handle, size, operation):
        operation.pending = True
        operation.buffer = b"read"
        return 997, operation.buffer

    def wait(self, event, milliseconds):
        state = self.waits.popleft() if self.waits else 0
        if isinstance(state, BaseException):
            raise state
        if state == 258:
            self.now += milliseconds / 1000
        return state

    def result(self, handle, operation, wait):
        assert wait is True
        result = self.results.popleft() if self.results else len(operation.buffer)
        if callable(result):
            result = result(operation)
        if isinstance(result, WinError):
            if result.winerror != 996:
                operation.pending = False
                self.calls.append("settled-error")
            raise result
        operation.pending = False
        self.calls.append("settled-count")
        return result

    def cancel(self, handle):
        assert handle is self.handle
        self.cancels += 1
        if self.cancel_error:
            raise self.cancel_error

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def windows(monkeypatch):
    io = WinIo()
    for name in ("pywintypes", "win32api", "win32con", "win32event", "win32file", "win32pipe", "win32security"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules["pywintypes"].error = WinError
    events = sys.modules["win32event"]
    events.INFINITE = 0xFFFFFFFF
    events.WAIT_TIMEOUT = 258
    events.WAIT_OBJECT_0 = 0
    events.WaitForSingleObject = io.wait
    files = sys.modules["win32file"]
    files.WriteFile = io.write
    files.ReadFile = io.read
    files.GetOverlappedResult = io.result
    path = Path(__file__).resolve().parents[4] / 'src/hyprial/kernel/impl/platform/windows_pipe.py'
    spec = importlib.util.spec_from_file_location("_isolated_windows_pipe_response", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_overlapped", io.operation)
    monkeypatch.setattr(module, "_cancel", io.cancel)
    monkeypatch.setattr(module, "time", types.SimpleNamespace(monotonic=lambda: io.now, sleep=io.sleep))
    return module, io, module.PipeStream(io.handle)


def send(stream, data=b"abcdefgh", **kwargs):
    return stream.send_response(data, timeout=kwargs.pop("timeout", 1), poll_interval=0.1, should_stop=kwargs.pop("should_stop", lambda: False), **kwargs)


def test_response_polls_same_operation_and_advances_only_confirmed_bytes(windows):
    _module, io, stream = windows
    io.waits.extend([258, 258, 0, 0])
    io.results.extend([3, 5])
    assert application._write_ipc_response(
        stream, b"abcdefgh", timeout=1, poll_interval=0.1, should_stop=lambda: False,
    ) is True
    assert [data for data, _ in io.writes] == [b"abcdefgh", b"defgh"]
    assert len(io.operations) == 2 and all(op.closed for op in io.operations)
    assert io.cancels == 0
    stream.close()


@pytest.mark.parametrize("terminal", ["full", "partial", "aborted"])
def test_deadline_cancel_settles_late_success_or_abort_without_reissue(windows, terminal):
    _module, io, stream = windows
    io.waits.extend([258, 258])
    io.results.append({"full": 8, "partial": 3, "aborted": WinError(995)}[terminal])
    if terminal == "full":
        assert send(stream, timeout=0.2) is True
    else:
        with pytest.raises(TimeoutError) as caught:
            send(stream, timeout=0.2)
        assert caught.value.response_bytes_written == (3 if terminal == "partial" else 0)
        assert caught.value.response_complete is False
    assert len(io.writes) == 1 and io.cancels == 1
    assert io.operations[0].closed
    stream.close()


@pytest.mark.parametrize("terminal", ["full", "partial", "aborted"])
def test_stop_cancel_preserves_confirmed_count_without_more_writes(windows, terminal):
    _module, io, stream = windows
    io.waits.append(258)
    io.results.append({"full": 8, "partial": 3, "aborted": WinError(995)}[terminal])
    assert send(stream, should_stop=lambda: io.now >= 0.1) is (terminal == "full")
    assert len(io.writes) == 1 and io.cancels == 1
    stream.close()


@pytest.mark.parametrize("state", [0xFFFFFFFF, 128, WinError(6)])
def test_failed_or_abandoned_wait_is_not_false_success(windows, state):
    _module, io, stream = windows
    io.waits.append(state)
    io.results.append(8)
    with pytest.raises((OSError, WinError)) as caught:
        send(stream)
    assert caught.value.response_bytes_written == 8
    assert caught.value.response_complete is True
    assert io.cancels == 1 and len(io.writes) == 1
    assert io.operations[0].closed
    stream.close()


def test_cancel_failure_is_reported_after_confirmed_completion(windows):
    _module, io, stream = windows
    io.waits.extend([258, 258])
    io.cancel_error = OSError("synthetic cancellation failure")
    io.results.append(8)
    with pytest.raises(OSError, match="cancellation failure") as caught:
        send(stream, timeout=0.2)
    assert caught.value.response_bytes_written == 8
    assert caught.value.response_complete is True
    assert io.operations[0].closed
    stream.close()


def test_busy_daemon_close_retains_unknown_io_until_its_holder_settles(windows, monkeypatch):
    _module, io, stream = windows
    entered, release = threading.Event(), threading.Event()
    io.waits.extend([258, 258])
    io.results.append(WinError(996))
    def pending(operation):
        entered.set()
        assert release.wait(3)
        return len(operation.buffer)
    io.results.append(pending)
    outcomes, errors = [], []
    def write():
        try:
            outcomes.append(send(stream, timeout=0.2))
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=write)
    owner = SyntheticClientOwner()
    owner._ipc_clients.add(stream)
    owner._ipc_client_threads.add(worker)
    monkeypatch.setattr(application, "_IPC_CLIENT_SHUTDOWN_GRACE", 0)
    assert application._IPC_CLIENT_SHUTDOWN_TIMEOUT == 2.0
    monkeypatch.setattr(application, "_IPC_CLIENT_SHUTDOWN_TIMEOUT", 0.05)
    worker.start()
    try:
        assert entered.wait(1)
        with pytest.raises(RuntimeError, match="did not stop"):
            owner._close_ipc_clients()
        assert worker in owner._ipc_client_threads and stream in owner._ipc_clients
        assert stream.handle is io.handle and io.handle_closed == 0
        assert io.operations[0].pending and not io.operations[0].closed
        assert io.operations[0].buffer == b"abcdefgh" and len(io.writes) == 1
        release.set()
        worker.join(1)
        assert not worker.is_alive() and outcomes == [True] and not errors
        assert io.operations[0].closed and io.handle_closed == 1
        assert stream.handle is None
        owner._close_ipc_clients()
        stream.close()  # repeated close does not release twice
        assert io.handle_closed == 1
    finally:
        release.set()
        worker.join(1)


@pytest.mark.parametrize("operation", ["recv", "sendall", "response"])
def test_non_daemon_io_holder_finishes_recorded_close_without_context(windows, operation):
    _module, io, stream = windows
    entered, release = threading.Event(), threading.Event()
    def pending(op):
        entered.set()
        assert release.wait(3)
        return len(op.buffer)
    io.results.append(pending)
    errors = []
    def perform():
        try:
            if operation == "recv":
                assert stream.recv(4) == b"read"
            elif operation == "sendall":
                stream.sendall(b"abcdefgh")
            else:
                assert send(stream) is True
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=perform)
    worker.start()
    try:
        assert entered.wait(1)
        with pytest.raises(BlockingIOError):
            stream.close()
        assert stream.handle is io.handle and io.handle_closed == 0
        release.set()
        worker.join(1)
        assert not worker.is_alive() and not errors
        assert stream.handle is None and io.handle_closed == 1
        assert io.operations[0].closed
    finally:
        release.set()
        worker.join(1)


def test_idle_close_is_idempotent_and_later_io_is_refused(windows):
    _module, io, stream = windows
    stream.close()
    stream.close()
    assert io.handle_closed == 1
    with pytest.raises(OSError, match="closed"):
        send(stream)
    assert not io.writes


def test_cancel_failure_keeps_pending_operation_until_late_full_completion(windows):
    _module, io, stream = windows
    entered, release = threading.Event(), threading.Event()
    io.waits.extend([258, 258])
    io.cancel_error = OSError("synthetic cancel failure with pending I/O")
    io.cancel_error.winerror = 5  # Native ctypes.WinError carries this field.
    def pending(operation):
        entered.set()
        assert release.wait(3)
        return len(operation.buffer)
    io.results.append(pending)
    errors = []
    def write():
        try:
            send(stream, timeout=0.2)
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=write)
    worker.start()
    try:
        assert entered.wait(1)
        with pytest.raises(OSError, match="cancellation failed"):
            stream.close()
        assert not errors and io.handle_closed == 0
        assert stream.handle is io.handle
        assert io.operations[0].pending and not io.operations[0].closed
        assert len(io.writes) == 1 and io.operations[0].buffer == b"abcdefgh"
        release.set()
        worker.join(1)
        assert not worker.is_alive() and len(errors) == 1
        assert errors[0].response_bytes_written == 8
        assert errors[0].response_complete is True
        assert io.operations[0].closed and io.handle_closed == 1
        assert stream.handle is None
    finally:
        release.set()
        worker.join(1)
