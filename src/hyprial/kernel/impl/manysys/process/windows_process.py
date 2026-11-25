# Decision record (manysys/process/windows_process, W1-MANYSYS & W2B-1 2026-11-21):
# - Migrated from kernel/impl/platform/windows_process.py into manysys/process.
# - OpenProcess error 87 (ERROR_INVALID_PARAMETER) indicates an absent PID and
#   maps to ProcessLookupError; WaitForSingleObject status 258 (WAIT_TIMEOUT)
#   indicates a still-running process, status 0 indicates an exited process.
# - windows_process_parent_pid queries NtQueryInformationProcess
#   (ProcessBasicInformation=0) on an open, verified-live child handle (with
#   CreateToolhelp32Snapshot fallback) and verifies parent_create <= child_create
#   via GetProcessTimes on an open, verified-live parent handle so rapid Windows
#   PID reuse cannot forge parent-child ancestry.
# - windows_process_cpu_sample reads creation FILETIME and (kernel + user) / 1e7
#   CPU seconds from GetProcessTimes on a single live handle.
# - Windows birth marker matching delegates scheme parsing to owned_process.
# - windows_terminate_fenced_process verifies GetProcessTimes creation FILETIME
#   on the same open PROCESS_TERMINATE handle before calling TerminateProcess
#   (matching both canonical `win-filetime:<u64>` and legacy `win32:<u64>`
#   markers while rejecting contradictions), never calling unfenced os.kill.
"""Windows process identity, parent PID, CPU sample, and handle-fenced termination."""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from hyprial.kernel.impl.manysys.process import owned_process

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_PROCESS_TERMINATE = 0x0001
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_ERROR_INVALID_PARAMETER = 87
_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _ProcessBasicInformation(ctypes.Structure):
    _fields_ = [
        ("ExitStatus", ctypes.c_long),
        ("PebBaseAddress", ctypes.c_void_p),
        ("AffinityMask", ctypes.c_size_t),
        ("BasePriority", ctypes.c_long),
        ("UniqueProcessId", ctypes.c_size_t),
        ("InheritedFromUniqueProcessId", ctypes.c_size_t),
    ]


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _filetime_u64(value: wintypes.FILETIME) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def _bind_kernel32() -> ctypes.WinDLL:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
        ctypes.POINTER(wintypes.FILETIME)
    ] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    return kernel


def _read_live_times(
    kernel: ctypes.WinDLL, handle: int
) -> tuple[int, int, int]:
    created, exited, kernel_time, user_time = (
        wintypes.FILETIME() for _ in range(4)
    )
    if not kernel.GetProcessTimes(
        handle,
        ctypes.byref(created),
        ctypes.byref(exited),
        ctypes.byref(kernel_time),
        ctypes.byref(user_time),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return (
        _filetime_u64(created),
        _filetime_u64(kernel_time),
        _filetime_u64(user_time),
    )


def process_identity(pid: int) -> str:
    """Return creation FILETIME for a live process, retaining a handle while reading.

    A missing/exited PID raises ProcessLookupError. Inaccessible state raises
    OSError (unknown, never evidence permitting cleanup).
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ProcessLookupError(pid)
    kernel = _bind_kernel32()
    handle = kernel.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid
    )
    if not handle:
        error = ctypes.get_last_error()
        if error == _ERROR_INVALID_PARAMETER:
            raise ProcessLookupError(pid)
        raise ctypes.WinError(error)
    try:
        state = kernel.WaitForSingleObject(handle, 0)
        if state == _WAIT_OBJECT_0:
            raise ProcessLookupError(pid)
        if state != _WAIT_TIMEOUT:
            raise ctypes.WinError(ctypes.get_last_error())
        stamp, _kernel_ticks, _user_ticks = _read_live_times(kernel, handle)
        return f"win-filetime:{stamp}"
    finally:
        kernel.CloseHandle(handle)


def windows_process_cpu_sample(pid: int) -> tuple[str, float] | None:
    """Return ``(birth_identity, cpu_seconds)`` for a live Windows process."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        kernel = _bind_kernel32()
    except OSError:
        return None
    handle = kernel.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid
    )
    if not handle:
        return None
    try:
        if kernel.WaitForSingleObject(handle, 0) != _WAIT_TIMEOUT:
            return None
        stamp, kernel_ticks, user_ticks = _read_live_times(kernel, handle)
        return f"win-filetime:{stamp}", (kernel_ticks + user_ticks) / 1e7
    except OSError:
        return None
    finally:
        kernel.CloseHandle(handle)


def _ntquery_parent_pid(handle: int) -> int | None:
    try:
        ntdll = ctypes.WinDLL("ntdll")
        query = ntdll.NtQueryInformationProcess
        query.argtypes = [
            wintypes.HANDLE,
            ctypes.c_uint,
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.POINTER(wintypes.ULONG),
        ]
        query.restype = ctypes.c_long
        info = _ProcessBasicInformation()
        returned = wintypes.ULONG(0)
        status = query(
            handle,
            0,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(returned),
        )
        if status == 0 and info.InheritedFromUniqueProcessId > 0:
            return int(info.InheritedFromUniqueProcessId)
    except (AttributeError, OSError, ValueError):
        return None
    return None


def _toolhelp_parent_pid(kernel: ctypes.WinDLL, pid: int) -> int | None:
    try:
        create_snap = kernel.CreateToolhelp32Snapshot
        create_snap.argtypes = [wintypes.DWORD, wintypes.DWORD]
        create_snap.restype = wintypes.HANDLE
        first = kernel.Process32FirstW
        first.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
        first.restype = wintypes.BOOL
        next_proc = kernel.Process32NextW
        next_proc.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
        next_proc.restype = wintypes.BOOL
    except AttributeError:
        return None
    snapshot = create_snap(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
        return None
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
        ok = first(snapshot, ctypes.byref(entry))
        while ok:
            if int(entry.th32ProcessID) == pid:
                ppid = int(entry.th32ParentProcessID)
                return ppid if ppid > 0 else None
            ok = next_proc(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return None


def windows_process_parent_pid(pid: int) -> int | None:
    """Return verified live parent PID on Windows, rejecting reused parent PIDs."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        kernel = _bind_kernel32()
    except OSError:
        return None
    child_handle = kernel.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid
    )
    if not child_handle:
        return None
    try:
        if kernel.WaitForSingleObject(child_handle, 0) != _WAIT_TIMEOUT:
            return None
        child_created, _ck, _cu = _read_live_times(kernel, child_handle)
        ppid = _ntquery_parent_pid(child_handle)
        if ppid is None:
            ppid = _toolhelp_parent_pid(kernel, pid)
    except OSError:
        return None
    finally:
        kernel.CloseHandle(child_handle)
    if ppid is None or ppid <= 0 or ppid == pid:
        return None
    parent_handle = kernel.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, ppid
    )
    if not parent_handle:
        return None
    try:
        if kernel.WaitForSingleObject(parent_handle, 0) != _WAIT_TIMEOUT:
            return None
        parent_created, _pk, _pu = _read_live_times(kernel, parent_handle)
        return ppid if parent_created <= child_created else None
    except OSError:
        return None
    finally:
        kernel.CloseHandle(parent_handle)


def _windows_marker_matches(expected: str, stamp: int) -> bool:
    pieces = owned_process.split_process_birth_marker_pieces(expected)
    if pieces is None:
        return False
    target = str(stamp)
    matched = False
    for scheme, value in pieces:
        if not value:
            return False
        if scheme in {"win-filetime", "win32"}:
            if not value.isdigit() or value != target:
                return False
            matched = True
    return matched


def windows_terminate_fenced_process(
    pid: int, birth_marker: str | None
) -> bool:
    """Verify ``birth_marker`` on an open process handle before ``TerminateProcess``."""
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or not isinstance(birth_marker, str)
        or not birth_marker
    ):
        return False
    kernel = _bind_kernel32()
    terminate = kernel.TerminateProcess
    terminate.argtypes = [wintypes.HANDLE, wintypes.UINT]
    terminate.restype = wintypes.BOOL
    handle = kernel.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE | _PROCESS_TERMINATE,
        False,
        pid,
    )
    if not handle:
        error = ctypes.get_last_error()
        if error == _ERROR_INVALID_PARAMETER:
            return False
        raise ctypes.WinError(error)
    try:
        state = kernel.WaitForSingleObject(handle, 0)
        if state == _WAIT_OBJECT_0:
            return False
        if state != _WAIT_TIMEOUT:
            raise ctypes.WinError(ctypes.get_last_error())
        stamp, _kernel_ticks, _user_ticks = _read_live_times(kernel, handle)
        if not _windows_marker_matches(birth_marker, stamp):
            return False
        if not terminate(handle, 1):
            error = ctypes.get_last_error()
            if kernel.WaitForSingleObject(handle, 0) == _WAIT_OBJECT_0:
                return False
            raise ctypes.WinError(error)
        return True
    finally:
        kernel.CloseHandle(handle)
