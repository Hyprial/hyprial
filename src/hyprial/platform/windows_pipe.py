"""Local, current-user Windows named pipes with bounded overlapped I/O.

Only the small stream interface used by daemon IPC is implemented. This is
not a general socket replacement. No TCP listener or bearer credential.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import re
import time
import threading
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
        if error != 1168:  # no operation remains pending
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


class PipeStream:
    def __init__(self, handle):
        self.handle = handle
        self.timeout = None
        self._io_lock = threading.Lock()

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv(self, size):
        with self._io_lock:
            if self.handle is None:
                raise OSError("named pipe is closed")
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
        with self._io_lock:
            if self.handle is None:
                raise OSError("named pipe is closed")
            self._sendall(data)

    def _sendall(self, data):
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        while data:
            operation = _overlapped()
            try:
                win32file.WriteFile(self.handle, data, operation)
                remaining = None if deadline is None else max(0, deadline - time.monotonic())
                count = _complete(self.handle, operation, remaining)
                if count <= 0:
                    raise BrokenPipeError("named pipe wrote zero bytes")
                data = data[count:]
            except pywintypes.error as error:
                raise OSError(error.winerror, "named pipe write failed") from error
            finally:
                operation.hEvent.Close()

    def shutdown(self, _how):
        if self.handle is not None:
            try:
                _cancel(self.handle)
            except OSError as error:
                if error.winerror not in {1168, 6}:
                    raise OSError(error.winerror, "named pipe cancellation failed") from error

    def close(self):
        with self._io_lock:
            if self.handle is not None:
                handle, self.handle = self.handle, None
                handle.Close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class PipeListener:
    def __init__(self, endpoint: Path, *, gui_write_sid: str | None = None):
        self.name = rf"\\.\pipe\hyprial-{uuid4().hex}"
        self.timeout = 0.25
        self.handle = None
        self.operation = None
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
        finally:
            token.Close()
        dacl = win32security.ACL()
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.GENERIC_ALL, sid)
        if gui_write_sid is not None:
            # Explicit desktop-launcher capability, derived by the pinned DSH
            # runtime from the private GUI state directory. Restricted tokens
            # must pass BOTH the normal current-user and restricting-SID check.
            # Never grant Everyone/Users or change any machine-wide ACL.
            if not re.fullmatch(r"S-1-4-[1-9][0-9]{0,9}-[1-9][0-9]{0,9}", gui_write_sid):
                raise ValueError("invalid DSH GUI workspace capability SID")
            if any(int(part) > 2**30 - 1 for part in gui_write_sid.split("-")[-2:]):
                raise ValueError("invalid DSH GUI workspace capability SID")
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

    def _prepare(self, *, first=False):
        self.handle = win32pipe.CreateNamedPipe(
            self.name,
            win32pipe.PIPE_ACCESS_DUPLEX | win32file.FILE_FLAG_OVERLAPPED
            | (0x00080000 if first else 0),
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE
            | win32pipe.PIPE_WAIT | 8,  # PIPE_REJECT_REMOTE_CLIENTS
            255, 65536, 65536, 0, self.security,
        )
        self.operation = _overlapped()
        try:
            result = win32pipe.ConnectNamedPipe(self.handle, self.operation)
            self._connected_immediately = result == 535
            if self._connected_immediately:
                win32event.SetEvent(self.operation.hEvent)
        except pywintypes.error as error:
            if error.winerror != 535:  # client connected before ConnectNamedPipe
                raise
            self._connected_immediately = True
            win32event.SetEvent(self.operation.hEvent)

    def settimeout(self, timeout):
        self.timeout = timeout

    def accept(self):
        state = win32event.WaitForSingleObject(self.operation.hEvent, int(self.timeout * 1000))
        if state == win32event.WAIT_TIMEOUT:
            raise TimeoutError("named pipe accept timed out")
        try:
            if not self._connected_immediately:
                win32file.GetOverlappedResult(self.handle, self.operation, True)
        except pywintypes.error as error:
            if error.winerror != 535:
                raise OSError(error.winerror, "named pipe accept failed") from error
        accepted = PipeStream(self.handle)
        self.handle = None
        self.operation.hEvent.Close()
        self.operation = None
        try:
            self._prepare()
        except BaseException:
            accepted.close()
            raise
        return accepted, None

    def close(self):
        if self.handle is not None:
            if self.operation is not None:
                try:
                    _cancel(self.handle)
                except OSError:
                    pass
                try:
                    win32file.GetOverlappedResult(self.handle, self.operation, True)
                except pywintypes.error:
                    pass
                self.operation.hEvent.Close()
                self.operation = None
            self.handle.Close()
            self.handle = None


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
                win32file.FILE_FLAG_OVERLAPPED | 0x100000, None,  # SECURITY_SQOS_PRESENT: anonymous
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
