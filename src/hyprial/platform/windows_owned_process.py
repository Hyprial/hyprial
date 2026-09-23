"""Job-owned async workers; the worker starts only after its bootstrap joins.

The bootstrap inherits exactly a Job and a readiness-event handle, runs with
stdlib-only Python, joins the Job, and drops its Job handle before spawning the
worker. The daemon remains the only long-lived Job owner. If it crashes, the
kernel terminates the worker and descendants, including during startup.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time

import win32api
import win32con
import win32event
import win32job

from hyprial.daemon.api import ProcessLiveness, ProcessLivenessState
from hyprial.harnesses.owned_process import OwnedProcessGroup
from hyprial.platform.windows_process import process_identity

_BOOTSTRAP = r"""
import ctypes,sys,subprocess
from ctypes import wintypes as w
k=ctypes.WinDLL('kernel32',use_last_error=True)
k.GetCurrentProcess.restype=w.HANDLE
k.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
k.AssignProcessToJobObject.restype=w.BOOL
k.SetEvent.argtypes=[w.HANDLE];k.SetEvent.restype=w.BOOL
k.CloseHandle.argtypes=[w.HANDLE];k.CloseHandle.restype=w.BOOL
job,event=map(int,sys.argv[1:3])
if not k.AssignProcessToJobObject(job,k.GetCurrentProcess()):raise ctypes.WinError(ctypes.get_last_error())
if not k.SetEvent(event):raise ctypes.WinError(ctypes.get_last_error())
k.CloseHandle(event)
k.CloseHandle(job)
child=subprocess.Popen(sys.argv[3:],close_fds=True)
raise SystemExit(child.wait())
"""


class WindowsOwnedProcessGroup(OwnedProcessGroup):
    """Ownership follows the kernel Job, never a potentially reused PID."""

    def __init__(self, *, label: str) -> None:
        self._label = label
        self._lock = threading.RLock()
        self._job = None
        self._pid = None
        self._identity = None
        self._observed = False
        self._stop_requested = False
        self._spawning = False

    async def spawn(self, command, **kwargs):
        with self._lock:
            if self._stop_requested:
                raise ConnectionError(f"{self._label} is stopping")
            if self._job is not None:
                raise RuntimeError("process generation is already owned")
            self._job = win32job.CreateJobObject(None, "")
            limits = win32job.QueryInformationJobObject(
                self._job, win32job.JobObjectExtendedLimitInformation
            )
            limits["BasicLimitInformation"]["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(
                self._job, win32job.JobObjectExtendedLimitInformation, limits
            )
            current = win32api.GetCurrentProcess()
            inherited_job = win32api.DuplicateHandle(
                current, self._job, current, 0, True, win32con.DUPLICATE_SAME_ACCESS
            )
            ready = win32event.CreateEvent(None, True, False, None)
            inherited_ready = win32api.DuplicateHandle(
                current, ready, current, 0, True, win32con.DUPLICATE_SAME_ACCESS
            )
            self._spawning = True
            self._observed = True
        process = None
        failed = True
        try:
            startup = subprocess.STARTUPINFO()
            startup.lpAttributeList = {
                "handle_list": [int(inherited_job), int(inherited_ready)]
            }
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-S",
                "-c",
                _BOOTSTRAP,
                str(int(inherited_job)),
                str(int(inherited_ready)),
                *command,
                startupinfo=startup,
                close_fds=True,
                **kwargs,
            )
            with self._lock:
                self._pid = process.pid
                self._identity = process_identity(process.pid)
            state = await asyncio.to_thread(win32event.WaitForSingleObject, ready, 5000)
            if state != win32event.WAIT_OBJECT_0:
                raise ConnectionError("Windows worker did not confirm Job ownership")
            with self._lock:
                if self._stop_requested:
                    raise ConnectionError(f"{self._label} is stopping")
            failed = False
            return process
        except BaseException:
            with self._lock:
                if self._job is not None:
                    win32job.TerminateJobObject(self._job, 1)
            if process is not None:
                # This exact unregistered bootstrap can still be before Assign.
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.wait()
            raise
        finally:
            inherited_job.Close()
            inherited_ready.Close()
            ready.Close()
            with self._lock:
                self._spawning = False
                if self._stop_requested and self._job is not None:
                    win32job.TerminateJobObject(self._job, 1)
                if failed:
                    self.release_if_gone(self._pid)

    def register(self, pid: int) -> None:
        with self._lock:
            if self._pid != pid:
                raise ConnectionError(
                    "Windows workers must be spawned through their Job owner"
                )
            if self._stop_requested or self._identity is None:
                if self._job is not None:
                    win32job.TerminateJobObject(self._job, 1)
                raise ConnectionError("Windows worker ownership cannot be published")

    def _active(self) -> bool:
        return (
            self._job is not None
            and win32job.QueryInformationJobObject(
                self._job, win32job.JobObjectBasicAccountingInformation
            )["ActiveProcesses"]
            > 0
        )

    def exists(self, pid: int) -> bool:
        with self._lock:
            return self._pid == pid and self._active()

    def signal(self, pid: int, signum) -> None:
        # Cooperative cancellation belongs to worker RPC. OS force-stop must
        # kill the full Job; Windows terminate() is not a POSIX SIGTERM.
        with self._lock:
            if self._pid == pid and self._job is not None:
                win32job.TerminateJobObject(self._job, 1)

    def release_if_gone(self, pid: int) -> None:
        with self._lock:
            if self._pid == pid and not self._spawning and not self._active():
                if self._job is not None:
                    self._job.Close()
                    self._job = None
                self._pid = None
                self._identity = None

    def liveness(self) -> ProcessLiveness:
        with self._lock:
            if not self._observed:
                return ProcessLiveness(ProcessLivenessState.UNKNOWN, observed=False)
            if self._spawning:
                return ProcessLiveness(
                    ProcessLivenessState.UNKNOWN,
                    observed=True,
                    detail="Windows Job registration is pending",
                )
            return ProcessLiveness(
                ProcessLivenessState.ALIVE
                if self._active()
                else ProcessLivenessState.DEAD,
                observed=True,
                pid=self._pid,
                marker=self._identity,
            )

    def stopped(self) -> bool:
        with self._lock:
            return not self._spawning and not self._active()

    def force_close(self) -> None:
        with self._lock:
            self._stop_requested = True
            if self._job is not None:
                win32job.TerminateJobObject(self._job, 1)
        deadline = time.monotonic() + 0.5
        while not self.stopped() and time.monotonic() < deadline:
            time.sleep(0.01)
        if self._pid is not None:
            self.release_if_gone(self._pid)

    def _close_unregistered_group(self, pid: int) -> None:
        self.signal(pid, 9)

    def __del__(self):
        job = getattr(self, "_job", None)
        if job is not None:
            job.Close()
