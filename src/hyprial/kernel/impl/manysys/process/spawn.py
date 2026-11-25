# Decision record (manysys/process/spawn, W1-MANYSYS 2026-11-21):
# - Migrated from kernel/impl/platform/process.py into manysys/process and
#   extended with spawn_detached_daemon.
# - spawn_detached_daemon launches a long-lived background daemon process that
#   must survive closure of the invoking CLI or native desktop shell:
#   * POSIX defaults to start_new_session=True, close_fds=True;
#   * Windows inspects Job membership and breakaway permission up front via
#     _windows_current_job_allows_breakaway() (IsProcessInJob +
#     QueryInformationJobObject(None, JobObjectExtendedLimitInformation=9)):
#     - when the current process is not in a Job, or its Job forbids breakaway,
#       it spawns once with CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS |
#       CREATE_NO_WINDOW;
#     - only when the current Job explicitly sets JOB_OBJECT_LIMIT_BREAKAWAY_OK
#       or JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK does it add
#       CREATE_BREAKAWAY_FROM_JOB so a parent Job with
#       JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE does not kill the resident daemon;
#     - plain PermissionError (without winerror == 5) and target executable/cwd
#       access failures are never retried or swallowed; fallback retry is
#       restricted to explicit WinError 5 when an outer nested Job chain still
#       denies breakaway on an accessible target.
"""Read-only process existence observation, child spawn/terminate helpers, and
detached daemon launch seam.

Decision: gui-lifecycle-process-seam-20261107.  The GUI supervisor starts
short-lived children, gives each one its own process group/session, and must
stop the whole tree when it retires a generation:

* ``spawn_kwargs()`` returns the creation flags for one child: a new process
  group on Windows, a new session on POSIX;
* ``hidden_subprocess_kwargs()`` returns the creation flags for one short-lived
  probe whose console window must never appear on Windows; POSIX gets no
  additional arguments;
* ``terminate_process_tree()`` stops one already-recorded child.  On POSIX it
  signals the recorded group SIGINT -> SIGTERM -> SIGKILL, probing membership
  with ``killpg(group, 0)`` (``PermissionError`` counts as alive).  With no
  recorded group it falls back to the single process: poll, ``terminate()``,
  wait, then ``kill()`` and wait.  On Windows it asks ``taskkill /PID <pid> /T
  /F`` for the tree, waits, then falls back to ``terminate()``/``kill()``;
* ``spawn_detached_daemon()`` launches a detached long-lived daemon process
  across POSIX sessions and Windows Job breakaway/detached process groups.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from typing import Any

_GROUP_POLL_INTERVAL_SECONDS = 0.05
_WIN_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_WIN_CREATE_NO_WINDOW = 0x08000000
_WIN_CREATE_NEW_PROCESS_GROUP = 0x00000200
_WIN_DETACHED_PROCESS = 0x00000008
_WIN_ERROR_ACCESS_DENIED = 5
_WIN_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_WIN_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_WIN_JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _load_windows_kernel32() -> Any:
    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is not None:
        kernel = win_dll("kernel32", use_last_error=True)
        get_current = getattr(kernel, "GetCurrentProcess", None)
        if get_current is not None:
            get_current.argtypes = []
            get_current.restype = ctypes.c_void_p
        in_job = getattr(kernel, "IsProcessInJob", None)
        if in_job is not None:
            in_job.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_int),
            ]
            in_job.restype = ctypes.c_int
        query_job = getattr(kernel, "QueryInformationJobObject", None)
        if query_job is not None:
            query_job.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
            ]
            query_job.restype = ctypes.c_int
        return kernel
    windll = getattr(ctypes, "windll", None)
    return getattr(windll, "kernel32", None)


def _windows_current_job_allows_breakaway() -> tuple[bool, bool]:
    """Return ``(in_job, breakaway_ok)`` for the current Windows process."""

    kernel32 = _load_windows_kernel32()
    if kernel32 is None:
        return False, False
    try:
        current_process = kernel32.GetCurrentProcess()
        in_job = ctypes.c_int(0)
        if not kernel32.IsProcessInJob(current_process, None, ctypes.byref(in_job)):
            return False, False
        if not in_job.value:
            return False, False
        ext_info = _JobObjectExtendedLimitInformation()
        if not kernel32.QueryInformationJobObject(
            None,
            _WIN_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(ext_info),
            ctypes.sizeof(ext_info),
            None,
        ):
            return True, False
        limit_flags = int(ext_info.BasicLimitInformation.LimitFlags)
        allows_breakaway = bool(
            limit_flags
            & (
                _WIN_JOB_OBJECT_LIMIT_BREAKAWAY_OK
                | _WIN_JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
            )
        )
        return True, allows_breakaway
    except (AttributeError, OSError, TypeError, ValueError):
        return False, False


def _target_access_denied(args: Any, kwargs: dict[str, Any]) -> bool:
    program = kwargs.get("executable")
    if program is None:
        if isinstance(args, (str, bytes, os.PathLike)):
            program = args
        elif isinstance(args, (list, tuple)) and args:
            program = args[0]
    if isinstance(program, (str, bytes, os.PathLike)):
        try:
            raw_prog = os.fsdecode(program)
            target = Path(raw_prog)
            if not target.exists() and os.sep not in raw_prog and "/" not in raw_prog:
                env = kwargs.get("env")
                search_path = env.get("PATH") if isinstance(env, dict) else None
                located = shutil.which(raw_prog, path=search_path)
                if located is not None:
                    target = Path(located)
            if target.is_dir():
                return True
            if target.exists():
                if not os.access(target, os.R_OK | os.X_OK):
                    return True
                with target.open("rb"):
                    pass
        except OSError:
            return True
    cwd = kwargs.get("cwd")
    if isinstance(cwd, (str, bytes, os.PathLike)):
        try:
            cwd_path = Path(os.fsdecode(cwd))
            if cwd_path.exists():
                if not cwd_path.is_dir() or not os.access(cwd_path, os.R_OK | os.X_OK):
                    return True
                os.listdir(cwd_path)
        except OSError:
            return True
    return False


def probe_process(pid: int) -> None:
    if sys.platform == "win32" or os.name == "nt":
        from hyprial.kernel.impl.manysys.process.ports import windows_process_identity

        windows_process_identity(pid)
    else:
        os.kill(pid, 0)


def spawn_kwargs() -> dict[str, Any]:
    """Creation arguments for one synchronously supervised child process."""

    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def hidden_subprocess_kwargs() -> dict[str, Any]:
    """Creation arguments for a short-lived probe that must stay windowless."""

    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def spawn_detached_daemon(args: Any, **popen_kwargs: Any) -> subprocess.Popen[Any]:
    """Launch a detached background daemon process across POSIX and Windows."""

    kwargs = dict(popen_kwargs)
    kwargs.setdefault("close_fds", True)
    if sys.platform != "win32":
        kwargs.setdefault("start_new_session", True)
        return subprocess.Popen(args, **kwargs)
    kwargs.pop("start_new_session", None)
    caller_flags = int(kwargs.pop("creationflags", 0))
    breakaway = int(
        getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", _WIN_CREATE_BREAKAWAY_FROM_JOB)
    )
    no_window = int(getattr(subprocess, "CREATE_NO_WINDOW", _WIN_CREATE_NO_WINDOW))
    new_group = int(
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", _WIN_CREATE_NEW_PROCESS_GROUP)
    )
    detached = int(getattr(subprocess, "DETACHED_PROCESS", _WIN_DETACHED_PROCESS))
    detached_flags = caller_flags | new_group | detached | no_window
    _in_job, breakaway_ok = _windows_current_job_allows_breakaway()
    if not breakaway_ok:
        return subprocess.Popen(args, creationflags=detached_flags, **kwargs)
    try:
        return subprocess.Popen(
            args, creationflags=detached_flags | breakaway, **kwargs
        )
    except OSError as error:
        if getattr(
            error, "winerror", None
        ) != _WIN_ERROR_ACCESS_DENIED or _target_access_denied(args, kwargs):
            raise
        return subprocess.Popen(args, creationflags=detached_flags, **kwargs)


def _group_alive(group: int) -> bool:
    """Whether the already-recorded child process group still has members."""

    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _terminate_single(process: Any, *, kill_timeout: float) -> None:
    """Stop one process with no recorded group: terminate, wait, then kill."""

    try:
        if process.poll() is not None:
            return
        process.terminate()
        process.wait(timeout=kill_timeout)
    except (OSError, AttributeError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=kill_timeout)
        except (OSError, AttributeError, subprocess.TimeoutExpired):
            pass


def _terminate_windows_tree(
    process: Any, *, stop_timeout: float, kill_timeout: float
) -> None:
    """Stop one Windows child tree with ``taskkill``, then the handle."""

    try:
        if process.poll() is not None:
            return
    except (AttributeError, OSError):
        return
    try:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=stop_timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.wait(timeout=kill_timeout)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.terminate()
        process.wait(timeout=kill_timeout)
    except (OSError, subprocess.TimeoutExpired):
        pass


def terminate_process_tree(
    process: Any,
    group: int | None,
    *,
    stop_timeout: float,
    kill_timeout: float,
) -> None:
    """Stop one child and, when a group is recorded, its process group."""

    if sys.platform == "win32":
        _terminate_windows_tree(
            process, stop_timeout=stop_timeout, kill_timeout=kill_timeout
        )
        return
    if group is None:
        try:
            if process.poll() is not None:
                return
        except (AttributeError, OSError):
            return
        _terminate_single(process, kill_timeout=kill_timeout)
        return
    if not _group_alive(group):
        process.poll()
        return
    for signal_number, timeout in (
        (signal.SIGINT, stop_timeout),
        (signal.SIGTERM, stop_timeout),
        (signal.SIGKILL, kill_timeout),
    ):
        if not _group_alive(group):
            return
        try:
            os.killpg(group, signal_number)
        except ProcessLookupError:
            return
        except OSError:
            _terminate_single(process, kill_timeout=kill_timeout)
            return
        deadline = time.monotonic() + timeout
        while _group_alive(group) and time.monotonic() < deadline:
            process.poll()
            time.sleep(_GROUP_POLL_INTERVAL_SECONDS)
        if not _group_alive(group):
            return
    process.poll()
