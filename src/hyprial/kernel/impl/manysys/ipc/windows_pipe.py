# Decision record (manysys/ipc/windows_pipe, W1-MANYSYS 2026-11-21):
# - Migrated from kernel/impl/platform/windows_pipe.py into manysys/ipc; module
#   basename stays windows_pipe.py so check_windows_portability W020 continues
#   to reject any direct import outside PLATFORM_SEAMS.
# - CancelIoEx error 1168 means no pending operation remains and is ignored.
# - Cancellation is not completion: _settle_response_write retains the
#   OVERLAPPED/event/buffer until GetOverlappedResult settles (winerror 996
#   ERROR_IO_INCOMPLETE polls until terminal status). Cancellation can lose a
#   race with successful I/O; _complete_response_write returns the confirmed
#   byte count without issuing another chunk past the deadline, and timed-out
#   writes record response_bytes_written / response_complete on the raised
#   exception as confirmed progress only.
# - PipeStream._owned_io checks _close_requested AFTER releasing _io_lock so a
#   racing close() that observed a busy lock hands settlement to the active I/O
#   holder without a check/release gap.
# - PipeListener restricts its DACL to the current user TokenUser SID plus,
#   when supplied, a validated S-1-4-*-* desktop-launcher capability SID;
#   PIPE_REJECT_REMOTE_CLIENTS (0x8) is always set, ConnectNamedPipe winerror
#   535 (ERROR_PIPE_CONNECTED) marks immediate connection, and client CreateFile
#   passes SECURITY_SQOS_PRESENT (0x100000) for anonymous impersonation level.
# - PipeListener accept/close concurrency (2026-11-26): a shared
#   threading.Condition (_cond) guards handle, operation, _connected_immediately,
#   _closed, _accepting, _active_handle, _cancel_sent, _cancelling, and
#   _closing_handles. accept() checks out (handle, operation) and clears the
#   instance fields under _cond before waiting/settling unlocked, so close()
#   never races on GetOverlappedResult or closes live handles. To interrupt an
#   in-flight accept(), close() marks _closed, pins _active_handle via
#   _cancelling while calling _cancel() once, and waits on _cond until
#   _accepting and _closing_handles drain to zero. accept() waits for
#   _cancelling == 0 before disposing or handing off the handle, ensuring every
#   handle and hEvent closes exactly once after settlement and no thread uses
#   them after close() returns. Calling accept() on a closed listener raises
#   OSError(errno.EBADF, ...). close() is blocking (it waits for any in-flight
#   accept() to finish overlapped settlement before returning, and neither
#   handle, operation, nor hEvent is released before then), must not be called
#   from the thread currently executing accept(), and is idempotent.
"""Local, current-user Windows named pipes with bounded overlapped I/O.

Only the small stream interface used by daemon IPC is implemented. This is
not a general socket replacement. No TCP listener or bearer credential.
"""

from __future__ import annotations

import ctypes
from contextlib import contextmanager
from ctypes import wintypes
import errno
import json
import re
import threading
import time
from pathlib import Path
from uuid import uuid4

import pywintypes
import win32api
import win32con
import win32event
import win32file
import win32pipe
import win32security


def _overlapped():
    operation = pywintypes.OVERLAPPED()
    operation.hEvent = win32event.CreateEvent(None, True, False, None)
    return operation


def _cancel(handle):
    cancel = ctypes.WinDLL("kernel32", use_last_error=True).CancelIoEx
    cancel.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    cancel.restype = wintypes.BOOL
    if not cancel(int(handle), None):
        error = ctypes.get_last_error()
        if error != 1168:
            raise ctypes.WinError(error)


def _complete(handle, operation, timeout):
    milliseconds = win32event.INFINITE if timeout is None else max(0, int(timeout * 1000))
    state = win32event.WaitForSingleObject(operation.hEvent, milliseconds)
    if state == win32event.WAIT_TIMEOUT:
        _cancel(handle)
        try:
            return win32file.GetOverlappedResult(handle, operation, True)
        except pywintypes.error as error:
            if error.winerror == 995:
                raise TimeoutError("named pipe I/O timed out") from error
            raise
    return win32file.GetOverlappedResult(handle, operation, True)


def _settle_response_write(handle, operation, poll_interval):
    while True:
        try:
            return win32file.GetOverlappedResult(handle, operation, True)
        except pywintypes.error as error:
            if error.winerror != 996:
                raise
            time.sleep(poll_interval)


def _complete_response_write(handle, operation, deadline, poll_interval, should_stop):
    reason = None
    while True:
        try:
            if should_stop():
                reason = "stop"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = "deadline"
                break
            state = win32event.WaitForSingleObject(
                operation.hEvent, max(1, int(min(poll_interval, remaining) * 1000)),
            )
        except BaseException as error:
            reason = error
            break
        if state == win32event.WAIT_OBJECT_0:
            try:
                return _settle_response_write(handle, operation, poll_interval), None
            except pywintypes.error as error:
                if error.winerror == 995 and should_stop():
                    return None, None
                raise
        if state != win32event.WAIT_TIMEOUT:
            reason = OSError("named pipe response wait failed")
            break

    cancel_error = None
    try:
        _cancel(handle)
    except BaseException as error:
        cancel_error = error
    try:
        count = _settle_response_write(handle, operation, poll_interval)
    except pywintypes.error as error:
        if cancel_error is not None:
            raise cancel_error from error
        if isinstance(reason, BaseException):
            raise reason from error
        if error.winerror == 995:
            if reason == "stop":
                return None, None
            raise TimeoutError("named pipe response write deadline expired") from error
        raise
    failure = cancel_error or (reason if isinstance(reason, BaseException) else None)
    return count, failure


class PipeStream:
    def __init__(self, handle):
        self.handle = handle
        self.timeout = None
        self._io_lock = threading.Lock()
        self._close_requested = False

    def settimeout(self, timeout):
        self.timeout = timeout

    @contextmanager
    def _owned_io(self):
        self._io_lock.acquire()
        try:
            if self.handle is None or self._close_requested:
                raise OSError("named pipe is closed or closing")
            yield
        finally:
            self._io_lock.release()
            if self._close_requested:
                self._finish_requested_close()

    def recv(self, size):
        with self._owned_io():
            return self._recv(size)

    def _recv(self, size):
        operation = _overlapped()
        try:
            _, buffer = win32file.ReadFile(self.handle, size, operation)
            count = _complete(self.handle, operation, self.timeout)
            return bytes(buffer[:count])
        except pywintypes.error as error:
            if error.winerror in {109, 232, 233}:
                return b""
            raise OSError(error.winerror, "named pipe read failed") from error
        finally:
            operation.hEvent.Close()

    def sendall(self, data):
        with self._owned_io():
            self._sendall(data)

    def send_response(self, data, *, timeout, poll_interval, should_stop):
        with self._owned_io():
            return self._sendall(
                data, response_timeout=timeout, poll_interval=poll_interval,
                should_stop=lambda: self._close_requested or should_stop(),
            )

    def _sendall(self, data, *, response_timeout=None, poll_interval=None, should_stop=None):
        response = should_stop is not None
        timeout = response_timeout if response else self.timeout
        deadline = None if timeout is None else time.monotonic() + timeout
        size = len(data)
        try:
            while data:
                if response:
                    if should_stop():
                        return False
                    if time.monotonic() >= deadline:
                        raise TimeoutError("named pipe response write deadline expired")
                operation = _overlapped()
                try:
                    try:
                        win32file.WriteFile(self.handle, data, operation)
                    except pywintypes.error as error:
                        if not response or error.winerror != 997:
                            raise
                    if response:
                        count, failure = _complete_response_write(
                            self.handle, operation, deadline, poll_interval, should_stop,
                        )
                        if count is None:
                            return False
                    else:
                        remaining = None if deadline is None else max(0, deadline - time.monotonic())
                        count = _complete(self.handle, operation, remaining)
                        failure = None
                    if count <= 0:
                        raise BrokenPipeError("named pipe wrote zero bytes")
                    if count > len(data):
                        raise OSError("named pipe returned an invalid write count")
                    data = data[count:]
                    if failure is not None:
                        raise failure
                except pywintypes.error as error:
                    raise OSError(error.winerror, "named pipe write failed") from error
                finally:
                    operation.hEvent.Close()
            return True
        except BaseException as error:
            if response:
                error.response_bytes_written = size - len(data)
                error.response_complete = not data
            raise

    def shutdown(self, _how):
        if self.handle is not None:
            try:
                _cancel(self.handle)
            except OSError as error:
                if error.winerror not in {1168, 6}:
                    raise OSError(error.winerror, "named pipe cancellation failed") from error

    def _finish_requested_close(self):
        if not self._io_lock.acquire(blocking=False):
            return False
        try:
            if self.handle is not None:
                self.handle.Close()
                self.handle = None
            return True
        finally:
            self._io_lock.release()

    def close(self):
        self._close_requested = True
        if not self._finish_requested_close():
            self.shutdown(2)
            raise BlockingIOError("named pipe close awaits I/O settlement")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def _settle_overlapped(handle, operation):
    while True:
        try:
            win32file.GetOverlappedResult(handle, operation, True)
            return
        except pywintypes.error as error:
            if error.winerror != 996:
                raise
            time.sleep(0.001)


def _dispose_pending(handle, operation, settled):
    try:
        if not settled:
            try:
                _cancel(handle)
            except OSError:
                pass
            try:
                _settle_overlapped(handle, operation)
            except pywintypes.error:
                pass
    finally:
        try:
            operation.hEvent.Close()
        finally:
            handle.Close()


class PipeListener:
    def __init__(self, endpoint: Path, *, gui_write_sid: str | None = None):
        self.name = rf"\\.\pipe\hyprial-{uuid4().hex}"
        self.timeout = 0.25
        self._cond = threading.Condition()
        self._closed = False
        self._accepting = False
        self._active_handle = None
        self._cancel_sent = False
        self._cancelling = 0
        self._closing_handles = 0
        self.handle = None
        self.operation = None
        self._connected_immediately = False
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
        finally:
            token.Close()
        dacl = win32security.ACL()
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.GENERIC_ALL, sid)
        if gui_write_sid is not None:
            if not re.fullmatch(r"S-1-4-[1-9][0-9]{0,9}-[1-9][0-9]{0,9}", gui_write_sid):
                raise ValueError("invalid GUI workspace capability SID")
            if any(int(part) > 2**30 - 1 for part in gui_write_sid.split("-")[-2:]):
                raise ValueError("invalid GUI workspace capability SID")
            capability = win32security.ConvertStringSidToSid(gui_write_sid)
            dacl.AddAccessAllowedAce(
                win32security.ACL_REVISION,
                win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                capability,
            )
        descriptor = win32security.SECURITY_DESCRIPTOR()
        descriptor.SetSecurityDescriptorDacl(True, dacl, False)
        self.security = pywintypes.SECURITY_ATTRIBUTES()
        self.security.SECURITY_DESCRIPTOR = descriptor
        try:
            self._prepare(first=True)
            endpoint.write_text(json.dumps({"transport": "windows-pipe-v1", "pipe": self.name}), encoding="utf-8")
        except BaseException:
            self.close()
            raise

    def _create_pending(self, *, first=False):
        handle = win32pipe.CreateNamedPipe(
            self.name,
            win32pipe.PIPE_ACCESS_DUPLEX | win32file.FILE_FLAG_OVERLAPPED
            | (0x00080000 if first else 0),
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE
            | win32pipe.PIPE_WAIT | 8,
            255, 65536, 65536, 0, self.security,
        )
        operation = None
        try:
            operation = _overlapped()
            try:
                result = win32pipe.ConnectNamedPipe(handle, operation)
                connected_immediately = result == 535
                if connected_immediately:
                    win32event.SetEvent(operation.hEvent)
            except pywintypes.error as error:
                if error.winerror != 535:
                    raise
                connected_immediately = True
                win32event.SetEvent(operation.hEvent)
            return handle, operation, connected_immediately
        except BaseException:
            try:
                if operation is not None:
                    operation.hEvent.Close()
            finally:
                handle.Close()
            raise

    def _prepare(self, *, first=False):
        handle, operation, connected_immediately = self._create_pending(first=first)
        with self._cond:
            if not self._closed:
                self.handle = handle
                self.operation = operation
                self._connected_immediately = connected_immediately
                return
        _dispose_pending(handle, operation, connected_immediately)

    def _release_active_handle(self):
        with self._cond:
            self._active_handle = None
            while self._cancelling > 0:
                self._cond.wait()
            return self._closed

    def _finish_accept(self):
        with self._cond:
            self._active_handle = None
            self._accepting = False
            self._cond.notify_all()

    def _rearm_after_error(self):
        try:
            next_handle, next_operation, next_connected = self._create_pending()
        except BaseException:
            return
        with self._cond:
            if not self._closed:
                self.handle = next_handle
                self.operation = next_operation
                self._connected_immediately = next_connected
                return
        _dispose_pending(next_handle, next_operation, next_connected)

    def settimeout(self, timeout):
        with self._cond:
            self.timeout = timeout

    def accept(self):
        with self._cond:
            while self._accepting and not self._closed:
                self._cond.wait()
            if self._closed or self.handle is None or self.operation is None:
                raise OSError(errno.EBADF, "named pipe listener is closed")
            handle = self.handle
            operation = self.operation
            connected_immediately = self._connected_immediately
            timeout = self.timeout
            self.handle = None
            self.operation = None
            self._connected_immediately = False
            self._accepting = True
            self._active_handle = None if connected_immediately else handle
            self._cancel_sent = False

        settled = connected_immediately
        accepting_owned = True
        wait_ms = win32event.INFINITE if timeout is None else max(0, int(timeout * 1000))
        try:
            state = win32event.WaitForSingleObject(operation.hEvent, wait_ms)
            if state == win32event.WAIT_TIMEOUT:
                with self._cond:
                    self._active_handle = None
                    while self._cancelling > 0:
                        self._cond.wait()
                    if not self._closed:
                        self.handle = handle
                        self.operation = operation
                        self._connected_immediately = connected_immediately
                        self._accepting = False
                        self._cond.notify_all()
                        accepting_owned = False
                        handle = None
                        operation = None
                        raise TimeoutError("named pipe accept timed out")
                _dispose_pending(handle, operation, settled)
                handle = None
                operation = None
                raise OSError(errno.EBADF, "named pipe listener is closed")

            if state != win32event.WAIT_OBJECT_0:
                raise OSError("named pipe accept wait failed")

            if not settled:
                try:
                    _settle_overlapped(handle, operation)
                except pywintypes.error as error:
                    if error.winerror != 535:
                        settled = True
                        closed = self._release_active_handle()
                        _dispose_pending(handle, operation, settled)
                        handle = None
                        operation = None
                        if closed:
                            raise OSError(
                                errno.EBADF, "named pipe listener is closed"
                            ) from error
                        self._rearm_after_error()
                        raise OSError(
                            error.winerror, "named pipe accept failed"
                        ) from error
                settled = True

            if self._release_active_handle():
                _dispose_pending(handle, operation, settled)
                handle = None
                operation = None
                raise OSError(errno.EBADF, "named pipe listener is closed")

            event = operation.hEvent
            operation = None
            event.Close()
            accepted = PipeStream(handle)
            handle = None
            try:
                next_handle, next_operation, next_connected = self._create_pending()
            except BaseException:
                accepted.close()
                raise
            with self._cond:
                if not self._closed:
                    self.handle = next_handle
                    self.operation = next_operation
                    self._connected_immediately = next_connected
                    self._accepting = False
                    self._cond.notify_all()
                    accepting_owned = False
                    return accepted, None
            _dispose_pending(next_handle, next_operation, next_connected)
            return accepted, None
        except BaseException:
            if handle is not None or operation is not None:
                self._release_active_handle()
                if handle is not None and operation is not None:
                    _dispose_pending(handle, operation, settled)
                elif operation is not None:
                    operation.hEvent.Close()
                elif handle is not None:
                    handle.Close()
            raise
        finally:
            if accepting_owned:
                self._finish_accept()

    def close(self):
        while True:
            idle_handle = None
            idle_operation = None
            idle_connected = False
            cancel_handle = None
            with self._cond:
                self._closed = True
                if self.handle is not None:
                    idle_handle = self.handle
                    idle_operation = self.operation
                    idle_connected = self._connected_immediately
                    self.handle = None
                    self.operation = None
                    self._connected_immediately = False
                    self._closing_handles += 1
                elif self._active_handle is not None and not self._cancel_sent:
                    cancel_handle = self._active_handle
                    self._cancel_sent = True
                    self._cancelling += 1
                elif self._accepting or self._closing_handles > 0:
                    self._cond.wait()
                    continue
                else:
                    self._cond.notify_all()
                    return

            if idle_handle is not None:
                try:
                    if idle_operation is not None:
                        _dispose_pending(idle_handle, idle_operation, idle_connected)
                    else:
                        idle_handle.Close()
                finally:
                    with self._cond:
                        self._closing_handles -= 1
                        self._cond.notify_all()
                continue

            if cancel_handle is not None:
                try:
                    _cancel(cancel_handle)
                except OSError:
                    pass
                finally:
                    with self._cond:
                        self._cancelling -= 1
                        self._cond.notify_all()


def connect(endpoint: Path, timeout: float) -> PipeStream:
    try:
        with endpoint.open("r", encoding="utf-8") as stream:
            text = stream.read(4097)
        document = json.loads(text) if len(text) <= 4096 else None
    except (ValueError, UnicodeError) as error:
        raise OSError("invalid local Windows IPC endpoint") from error
    if not isinstance(document, dict):
        raise OSError("invalid local Windows IPC endpoint")
    name = document.get("pipe", "")
    prefix = "\\\\.\\pipe\\hyprial-"
    suffix = name.removeprefix(prefix) if isinstance(name, str) else ""
    if (document.get("transport") != "windows-pipe-v1" or not isinstance(name, str) or not name.startswith(prefix)
            or len(suffix) != 32 or any(c not in "0123456789abcdef" for c in suffix)):
        raise OSError("invalid local Windows IPC endpoint")
    deadline = time.monotonic() + timeout
    while True:
        try:
            handle = win32file.CreateFile(
                name, win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                0, None, win32con.OPEN_EXISTING,
                win32file.FILE_FLAG_OVERLAPPED | 0x100000, None,
            )
            stream = PipeStream(handle)
            stream.settimeout(max(0, deadline - time.monotonic()))
            return stream
        except pywintypes.error as error:
            if error.winerror != 231:
                raise OSError(error.winerror, "named pipe connect failed") from error
            if time.monotonic() >= deadline:
                raise TimeoutError("named pipe connect timed out") from error
            time.sleep(0.01)
