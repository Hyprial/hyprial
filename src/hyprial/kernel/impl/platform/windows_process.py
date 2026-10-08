"""Read-only Windows process identity; never use os.kill(pid, 0) on Windows."""

from __future__ import annotations

import ctypes
from ctypes import wintypes


def process_identity(pid: int) -> str:
    """Return creation FILETIME for a live process, retaining a handle while reading.

    A missing/exited PID raises ProcessLookupError. Inaccessible state raises
    OSError (unknown, never evidence permitting cleanup).
    """
    if pid <= 0:
        raise ProcessLookupError(pid)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    wait = kernel.WaitForSingleObject
    wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait.restype = wintypes.DWORD
    times = kernel.GetProcessTimes
    times.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    times.restype = wintypes.BOOL
    handle = open_process(0x1000 | 0x100000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: PID not present
            raise ProcessLookupError(pid)
        raise ctypes.WinError(error)
    try:
        state = wait(handle, 0)
        if state == 0:
            raise ProcessLookupError(pid)
        if state != 258:  # WAIT_TIMEOUT = still running
            raise ctypes.WinError(ctypes.get_last_error())
        created, exited, kernel_time, user_time = (
            wintypes.FILETIME() for _ in range(4)
        )
        if not times(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        stamp = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return f"win-filetime:{stamp}"
    finally:
        close(handle)
