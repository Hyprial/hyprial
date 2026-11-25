# Decision record (manysys/signals/stdin_stop, W1-MANYSYS 2026-11-21):
# - Extracted and generalized from shell/impl/gui/termination.py so --stdin-stop
#   and any supervised foreground process share one cross-platform watcher in
#   manysys with zero platform checks in shell/daemon callers.
# - Lock-free callback execution: self._lock guards only the atomic state check
#   and transition (_closed, _triggered, pending.set(), private wake-pipe fds);
#   on_stop() or deliver_main_termination() executes strictly AFTER releasing
#   self._lock so a slow callback never blocks concurrent close(), reentrant
#   close() cannot deadlock, and _run() never calls _request() once
#   _closed_event is set.
# - POSIX interruptible wait: allocates a private non-blocking wake pipe
#   (os.pipe()) so select.select([descriptor, wake_read], ...) wakes immediately
#   when close() writes one byte to wake_write, ensuring blocking descriptors
#   never trap the reader thread in an uninterruptible os.read() and never
#   closing the caller-owned descriptor; if select.select raises (OSError,
#   ValueError) (for example fd >= FD_SETSIZE), falls back to select.poll(0) or
#   a temporary non-blocking read probe interleaved with _closed_event.wait().
# - Windows PeekNamedPipe probe: resolves CRT descriptors with
#   msvcrt.get_osfhandle and kernel32.GetFileType; on pipes (FILE_TYPE_PIPE ==
#   0x0003), polls PeekNamedPipe in bounded slices so broken/disconnected pipes
#   (109/232/233/6) trigger immediate EOF stop, avail > 0 reads bounded bytes
#   without blocking, and avail == 0 waits on _closed_event for instant close().
"""Cross-platform descriptor watcher for EOF or ``shutdown`` stop requests."""

from __future__ import annotations

from collections.abc import Callable
import ctypes
import os
import select
import threading
from typing import Any

from hyprial.kernel.impl.manysys.signals.termination import deliver_main_termination

try:
    import msvcrt as _msvcrt
except ImportError:
    _msvcrt = None  # type: ignore[assignment]

STDIN_SHUTDOWN_LINE = b"shutdown"
_STDIN_LINE_LIMIT = 4096
_STDIN_WAIT_SLICE_SECONDS = 0.2
_STDIN_FALLBACK_POLL_SECONDS = 0.05

_WIN_FILE_TYPE_DISK = 0x0001
_WIN_FILE_TYPE_CHAR = 0x0002
_WIN_FILE_TYPE_PIPE = 0x0003
_WIN_ERROR_INVALID_HANDLE = 6
_WIN_ERROR_BROKEN_PIPE = 109
_WIN_ERROR_NO_DATA = 232
_WIN_ERROR_PIPE_NOT_CONNECTED = 233
_WIN_ERROR_MORE_DATA = 234


def _load_windows_kernel32() -> Any:
    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is not None:
        kernel = win_dll("kernel32", use_last_error=True)
        get_file_type = getattr(kernel, "GetFileType", None)
        if get_file_type is not None:
            get_file_type.argtypes = [ctypes.c_void_p]
            get_file_type.restype = ctypes.c_uint32
        peek_pipe = getattr(kernel, "PeekNamedPipe", None)
        if peek_pipe is not None:
            peek_pipe.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.POINTER(ctypes.c_uint32),
            ]
            peek_pipe.restype = ctypes.c_int
        get_console_mode = getattr(kernel, "GetConsoleMode", None)
        if get_console_mode is not None:
            get_console_mode.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint32),
            ]
            get_console_mode.restype = ctypes.c_int
        return kernel
    windll = getattr(ctypes, "windll", None)
    return getattr(windll, "kernel32", None)


def _windows_last_error(kernel32: Any) -> int:
    ctypes_last = getattr(ctypes, "get_last_error", None)
    if ctypes_last is not None:
        err = int(ctypes_last())
        if err:
            return err
    get_last_error = getattr(kernel32, "GetLastError", None)
    return int(get_last_error()) if get_last_error is not None else 0


class StdinStopWatcher:
    """Watch ``descriptor`` for EOF or a ``shutdown`` line and request stop."""

    def __init__(
        self,
        descriptor: int = 0,
        *,
        on_stop: Callable[[], None] | None = None,
        signum: int | None = None,
        thread_name: str = "gui-stdin-stop",
    ) -> None:
        self.pending = threading.Event()
        self._descriptor = int(descriptor)
        self._on_stop = on_stop
        self._signum = signum
        self._thread_name = thread_name
        self._lock = threading.Lock()
        self._closed = False
        self._closed_event = threading.Event()
        self._triggered = False
        self._thread: threading.Thread | None = None
        self._wake_read: int | None = None
        self._wake_write: int | None = None
        if os.name != "nt" and hasattr(os, "pipe"):
            wake_read: int | None = None
            wake_write: int | None = None
            try:
                wake_read, wake_write = os.pipe()
                if hasattr(os, "set_blocking"):
                    os.set_blocking(wake_read, False)
                    os.set_blocking(wake_write, False)
                self._wake_read = wake_read
                self._wake_write = wake_write
            except OSError:
                for fd in (wake_read, wake_write):
                    if fd is not None:
                        try:
                            os.close(fd)
                        except OSError:
                            pass

    def start(self) -> None:
        """Start the background daemon reader thread once."""

        with self._lock:
            if self._closed or self._thread is not None:
                return
            thread = threading.Thread(
                target=self._run, name=self._thread_name, daemon=True
            )
            self._thread = thread
        try:
            thread.start()
        except BaseException:
            self._release_wake_pipe()
            raise

    def close(self) -> None:
        """Seal the watcher and immediately wake any pending wait."""

        unstarted_fds: tuple[int | None, int | None] = (None, None)
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._closed_event.set()
            if self._thread is None:
                unstarted_fds = (self._wake_read, self._wake_write)
                self._wake_read = None
                self._wake_write = None
            elif self._wake_write is not None:
                try:
                    os.write(self._wake_write, b"\x00")
                except OSError:
                    pass
        for fd in unstarted_fds:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def join(self, timeout: float | None = None) -> None:
        """Wait up to ``timeout`` seconds for the watcher thread to finish."""

        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def _release_wake_pipe(self) -> None:
        with self._lock:
            wake_read, self._wake_read = self._wake_read, None
            wake_write, self._wake_write = self._wake_write, None
        for fd in (wake_read, wake_write):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _run(self) -> None:
        try:
            buffer = b""
            while not self._closed_event.is_set():
                chunk = self._read_next_chunk()
                if chunk is None:
                    return
                if not chunk:
                    break
                buffer += chunk
                *lines, buffer = buffer.split(b"\n")
                if any(line.strip() == STDIN_SHUTDOWN_LINE for line in lines):
                    break
                buffer = buffer[-_STDIN_LINE_LIMIT:]
            if self._closed_event.is_set():
                return
            self._request()
        finally:
            self._release_wake_pipe()

    def _read_next_chunk(self) -> bytes | None:
        if os.name == "nt":
            return self._read_windows_chunk()
        return self._read_posix_chunk()

    def _read_posix_chunk(self) -> bytes | None:
        while not self._closed_event.is_set():
            state = self._block_until_input()
            if state == "closed":
                return None
            if state == "fallback_nonblocking":
                got, chunk = self._nonblocking_read_probe()
                if got:
                    return chunk
                continue
            try:
                return os.read(self._descriptor, _STDIN_LINE_LIMIT)
            except BlockingIOError:
                continue
            except OSError:
                return b""
        return None

    def _block_until_input(self) -> str:
        while not self._closed_event.is_set():
            wake_read = self._wake_read
            watch_fds = (
                [self._descriptor, wake_read]
                if wake_read is not None
                else [self._descriptor]
            )
            try:
                readable, _, _ = select.select(
                    watch_fds, [], [], _STDIN_WAIT_SLICE_SECONDS
                )
            except (OSError, ValueError):
                if self._closed_event.wait(_STDIN_FALLBACK_POLL_SECONDS):
                    return "closed"
                poll_factory = getattr(select, "poll", None)
                if poll_factory is not None:
                    try:
                        poller = poll_factory()
                        poller.register(
                            self._descriptor, getattr(select, "POLLIN", 1)
                        )
                        if not poller.poll(0):
                            continue
                        return "ready"
                    except (OSError, ValueError):
                        pass
                return "fallback_nonblocking"
            if wake_read is not None and wake_read in readable:
                try:
                    os.read(wake_read, 64)
                except OSError:
                    pass
            if self._closed_event.is_set():
                return "closed"
            if self._descriptor in readable:
                return "ready"
        return "closed"

    def _nonblocking_read_probe(self) -> tuple[bool, bytes]:
        get_blocking = getattr(os, "get_blocking", None)
        set_blocking = getattr(os, "set_blocking", None)
        restore_blocking = False
        if get_blocking is not None and set_blocking is not None:
            try:
                if get_blocking(self._descriptor):
                    set_blocking(self._descriptor, False)
                    restore_blocking = True
            except OSError:
                return True, b""
        try:
            return True, os.read(self._descriptor, _STDIN_LINE_LIMIT)
        except BlockingIOError:
            return False, b""
        except OSError:
            return True, b""
        finally:
            if restore_blocking and set_blocking is not None:
                try:
                    set_blocking(self._descriptor, True)
                except OSError:
                    pass

    def _read_windows_chunk(self) -> bytes | None:
        if _msvcrt is None:
            if self._closed_event.wait(_STDIN_FALLBACK_POLL_SECONDS):
                return None
            return b""
        try:
            handle = int(_msvcrt.get_osfhandle(self._descriptor))
        except (OSError, ValueError):
            return b""
        if handle in (-1, 0):
            return b""
        kernel32 = _load_windows_kernel32()
        if kernel32 is None:
            if self._closed_event.wait(_STDIN_FALLBACK_POLL_SECONDS):
                return None
            return b""
        file_type = int(kernel32.GetFileType(handle))
        if file_type == _WIN_FILE_TYPE_PIPE:
            while not self._closed_event.is_set():
                avail = ctypes.c_uint32(0)
                ok = bool(
                    kernel32.PeekNamedPipe(
                        handle, None, 0, None, ctypes.byref(avail), None
                    )
                )
                if not ok:
                    err = _windows_last_error(kernel32)
                    if err == _WIN_ERROR_MORE_DATA:
                        try:
                            return os.read(
                                self._descriptor,
                                min(_STDIN_LINE_LIMIT, max(1, int(avail.value))),
                            )
                        except BlockingIOError:
                            continue
                        except OSError:
                            return b""
                    if err in {
                        _WIN_ERROR_BROKEN_PIPE,
                        _WIN_ERROR_NO_DATA,
                        _WIN_ERROR_PIPE_NOT_CONNECTED,
                        _WIN_ERROR_INVALID_HANDLE,
                    }:
                        return b""
                    break
                if avail.value > 0:
                    try:
                        return os.read(
                            self._descriptor,
                            min(_STDIN_LINE_LIMIT, int(avail.value)),
                        )
                    except BlockingIOError:
                        continue
                    except OSError:
                        return b""
                if self._closed_event.wait(_STDIN_FALLBACK_POLL_SECONDS):
                    return None
            if self._closed_event.is_set():
                return None
        if file_type == _WIN_FILE_TYPE_DISK:
            try:
                return os.read(self._descriptor, _STDIN_LINE_LIMIT)
            except OSError:
                return b""
        if file_type == _WIN_FILE_TYPE_CHAR:
            get_console_mode = getattr(kernel32, "GetConsoleMode", None)
            mode = ctypes.c_uint32(0)
            is_console = bool(
                get_console_mode is not None
                and get_console_mode(handle, ctypes.byref(mode))
            )
            if not is_console:
                try:
                    return os.read(self._descriptor, _STDIN_LINE_LIMIT)
                except OSError:
                    return b""
            kbhit = getattr(_msvcrt, "kbhit", None)
            while not self._closed_event.is_set():
                if kbhit is not None and kbhit():
                    try:
                        return os.read(self._descriptor, _STDIN_LINE_LIMIT)
                    except OSError:
                        return b""
                if self._closed_event.wait(_STDIN_FALLBACK_POLL_SECONDS):
                    return None
            return None
        while not self._closed_event.is_set():
            try:
                ready, _, _ = select.select(
                    [self._descriptor], [], [], _STDIN_WAIT_SLICE_SECONDS
                )
            except (OSError, ValueError):
                if self._closed_event.wait(_STDIN_FALLBACK_POLL_SECONDS):
                    return None
                return b""
            if ready:
                try:
                    return os.read(self._descriptor, _STDIN_LINE_LIMIT)
                except OSError:
                    return b""
        return None

    def _request(self) -> None:
        with self._lock:
            if self._closed or self._triggered:
                return
            self._triggered = True
            self.pending.set()
            on_stop = self._on_stop
            signum = self._signum
        if on_stop is not None:
            on_stop()
        else:
            deliver_main_termination(signum)

    def __del__(self) -> None:
        if getattr(self, "_thread", None) is None:
            try:
                self._release_wake_pipe()
            except BaseException:
                pass
