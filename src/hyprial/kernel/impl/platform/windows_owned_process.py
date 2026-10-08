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
from dataclasses import dataclass, field

import win32api
import win32con
import win32event
import win32job

from hyprial.kernel.impl.processes.process_facts import ProcessLiveness, ProcessLivenessState
from hyprial.kernel.impl.processes.owned_process import (
    PROCESS_FORCE_KILL_SECONDS,
    PROCESS_FORCE_TERM_SECONDS,
    OwnedProcessGroup,
)
from hyprial.kernel.impl.platform.windows_process import process_identity

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


@dataclass(slots=True)
class _SpawnCustody:
    """Every native resource acquired while preparing one Job generation."""

    job: object | None = None
    inherited_job: object | None = None
    ready: object | None = None
    inherited_ready: object | None = None
    bootstrap: object | None = None
    failed: bool = False
    cleanup_errors: dict[str, str] = field(default_factory=dict)


class WindowsOwnedProcessGroup(OwnedProcessGroup):
    """Ownership follows the kernel Job, never a potentially reused PID."""

    def __init__(self, *, label: str) -> None:
        self._label = label
        self._lock = threading.RLock()
        self._custody: _SpawnCustody | None = None
        self._pid = None
        self._identity = None
        self._observed = False
        self._stop_requested = False
        self._spawning = False

    @property
    def _job(self):
        custody = self._custody
        return None if custody is None else custody.job

    async def spawn(self, command, **kwargs):
        await self._settle_pending_cleanup()
        custody = _SpawnCustody()
        process = None
        failed = True
        with self._lock:
            if self._stop_requested:
                raise ConnectionError(f"{self._label} is stopping")
            if self._custody is not None:
                raise RuntimeError("process generation is already owned")
            self._custody = custody
            self._spawning = True
            self._observed = True
        try:
            with self._lock:
                custody.job = win32job.CreateJobObject(None, "")
                limits = win32job.QueryInformationJobObject(
                    custody.job, win32job.JobObjectExtendedLimitInformation
                )
                limits["BasicLimitInformation"]["LimitFlags"] |= (
                    win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                )
                win32job.SetInformationJobObject(
                    custody.job,
                    win32job.JobObjectExtendedLimitInformation,
                    limits,
                )
                current = win32api.GetCurrentProcess()
                custody.inherited_job = win32api.DuplicateHandle(
                    current,
                    custody.job,
                    current,
                    0,
                    True,
                    win32con.DUPLICATE_SAME_ACCESS,
                )
                custody.ready = win32event.CreateEvent(None, True, False, None)
                custody.inherited_ready = win32api.DuplicateHandle(
                    current,
                    custody.ready,
                    current,
                    0,
                    True,
                    win32con.DUPLICATE_SAME_ACCESS,
                )
            startup = subprocess.STARTUPINFO()
            startup.lpAttributeList = {
                "handle_list": [
                    int(custody.inherited_job),
                    int(custody.inherited_ready),
                ]
            }
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-S",
                "-c",
                _BOOTSTRAP,
                str(int(custody.inherited_job)),
                str(int(custody.inherited_ready)),
                *command,
                startupinfo=startup,
                close_fds=True,
                **kwargs,
            )
            with self._lock:
                custody.bootstrap = process
                self._pid = process.pid
                self._identity = process_identity(process.pid)
            state = await asyncio.to_thread(
                win32event.WaitForSingleObject, custody.ready, 5000
            )
            if state != win32event.WAIT_OBJECT_0:
                raise ConnectionError("Windows worker did not confirm Job ownership")
            with self._lock:
                if self._stop_requested:
                    raise ConnectionError(f"{self._label} is stopping")
                custody.bootstrap = None
                failed = False
            return process
        except BaseException:
            with self._lock:
                custody.failed = True
                cleanup_requested = self._terminate_job_locked(custody)
            if process is not None:
                # This exact unregistered bootstrap can still be before Assign.
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    except BaseException as error:
                        with self._lock:
                            self._record_cleanup_error_locked(
                                custody, "bootstrap-kill", error
                            )
                    else:
                        cleanup_requested = True
                else:
                    cleanup_requested = True
                if cleanup_requested:
                    try:
                        await process.wait()
                    except BaseException as error:
                        with self._lock:
                            self._record_cleanup_error_locked(
                                custody, "bootstrap-wait", error
                            )
                    else:
                        with self._lock:
                            if custody.bootstrap is process:
                                custody.bootstrap = None
                                custody.cleanup_errors.pop("bootstrap-kill", None)
                                custody.cleanup_errors.pop("bootstrap-wait", None)
            raise
        finally:
            with self._lock:
                self._spawning = False
                if self._stop_requested:
                    self._terminate_job_locked(custody)
                if failed:
                    custody.failed = True
                    self._release_if_gone_locked(self._pid)
                else:
                    self._close_prepared_locked(custody)

    async def _settle_pending_cleanup(self) -> None:
        with self._lock:
            custody = self._custody
            if custody is None or not (custody.failed or custody.cleanup_errors):
                return
            self._close_prepared_locked(custody)
            had_job = custody.job is not None
            cleanup_requested = (
                self._terminate_job_locked(custody) if custody.failed else False
            )
            termination_deadline = (
                time.monotonic()
                + PROCESS_FORCE_TERM_SECONDS
                + PROCESS_FORCE_KILL_SECONDS
                if had_job and cleanup_requested
                else None
            )
            bootstrap = custody.bootstrap
            if bootstrap is not None and getattr(bootstrap, "returncode", None) is None:
                try:
                    bootstrap.kill()
                except ProcessLookupError:
                    pass
                except BaseException as error:
                    self._record_cleanup_error_locked(
                        custody, "bootstrap-kill", error
                    )
                else:
                    cleanup_requested = True
            elif bootstrap is not None:
                cleanup_requested = True
        if bootstrap is not None and cleanup_requested:
            try:
                await bootstrap.wait()
            except BaseException as error:
                with self._lock:
                    self._record_cleanup_error_locked(custody, "bootstrap-wait", error)
                if isinstance(error, asyncio.CancelledError):
                    raise
            else:
                with self._lock:
                    if custody.bootstrap is bootstrap:
                        custody.bootstrap = None
                        custody.cleanup_errors.pop("bootstrap-kill", None)
                        custody.cleanup_errors.pop("bootstrap-wait", None)
        if termination_deadline is not None:
            await self._wait_for_job_inactive(custody, termination_deadline)
        with self._lock:
            self._release_if_gone_locked(self._pid)
            custody = self._custody
            if custody is not None and (custody.failed or custody.cleanup_errors):
                raise RuntimeError(
                    "previous Windows process generation cleanup is unresolved"
                )

    async def _wait_for_job_inactive(
        self, custody: _SpawnCustody, deadline: float
    ) -> bool:
        while True:
            with self._lock:
                if self._custody is not custody:
                    return True
                active = self._active_locked(custody, retain_error=True)
                if active is False:
                    return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.01, remaining))

    @staticmethod
    def _record_cleanup_error_locked(
        custody: _SpawnCustody, operation: str, error: BaseException
    ) -> None:
        custody.cleanup_errors[operation] = (
            f"{type(error).__name__}: {str(error)[:300]}"
        )

    def _close_handle_locked(self, custody: _SpawnCustody, attribute: str) -> None:
        handle = getattr(custody, attribute)
        if handle is None:
            return
        try:
            handle.Close()
        except BaseException as error:
            self._record_cleanup_error_locked(custody, f"close:{attribute}", error)
        else:
            setattr(custody, attribute, None)
            custody.cleanup_errors.pop(f"close:{attribute}", None)

    def _close_prepared_locked(self, custody: _SpawnCustody) -> None:
        for attribute in ("inherited_ready", "ready", "inherited_job"):
            self._close_handle_locked(custody, attribute)

    def _terminate_job_locked(self, custody: _SpawnCustody) -> bool:
        if custody.job is None:
            return True
        try:
            win32job.TerminateJobObject(custody.job, 1)
        except BaseException as error:
            self._record_cleanup_error_locked(custody, "terminate:job", error)
            return False
        custody.cleanup_errors.pop("terminate:job", None)
        return True

    def _active_locked(
        self, custody: _SpawnCustody, *, retain_error: bool = False
    ) -> bool | None:
        if custody.job is None:
            return False
        try:
            active = (
                win32job.QueryInformationJobObject(
                    custody.job, win32job.JobObjectBasicAccountingInformation
                )["ActiveProcesses"]
                > 0
            )
        except BaseException as error:
            if not retain_error:
                raise
            self._record_cleanup_error_locked(custody, "query:active", error)
            return None
        custody.cleanup_errors.pop("query:active", None)
        return active

    def _release_if_gone_locked(self, pid: int | None) -> None:
        custody = self._custody
        if custody is None or self._pid != pid or self._spawning:
            return
        self._close_prepared_locked(custody)
        bootstrap = custody.bootstrap
        if bootstrap is not None:
            if getattr(bootstrap, "returncode", None) is None:
                return
            custody.bootstrap = None
            custody.cleanup_errors.pop("bootstrap-kill", None)
            custody.cleanup_errors.pop("bootstrap-wait", None)
        active = self._active_locked(custody, retain_error=True)
        if active is not False:
            return
        self._close_handle_locked(custody, "job")
        if any(
            getattr(custody, attribute) is not None
            for attribute in ("job", "inherited_job", "ready", "inherited_ready")
        ) or custody.bootstrap is not None:
            return
        self._custody = None
        self._pid = None
        self._identity = None

    def register(self, pid: int) -> None:
        with self._lock:
            if self._pid != pid:
                raise ConnectionError(
                    "Windows workers must be spawned through their Job owner"
                )
            if self._stop_requested or self._identity is None:
                custody = self._custody
                if custody is not None:
                    self._terminate_job_locked(custody)
                raise ConnectionError("Windows worker ownership cannot be published")

    def _active(self) -> bool:
        custody = self._custody
        return False if custody is None else bool(self._active_locked(custody))

    def exists(self, pid: int) -> bool:
        with self._lock:
            return self._pid == pid and self._active()

    def signal(self, pid: int, signum) -> None:
        # Cooperative cancellation belongs to worker RPC. OS force-stop must
        # kill the full Job; Windows terminate() is not a POSIX SIGTERM.
        with self._lock:
            custody = self._custody
            if self._pid == pid and custody is not None and custody.job is not None:
                self._terminate_job_locked(custody)

    def release_if_gone(self, pid: int | None) -> None:
        with self._lock:
            self._release_if_gone_locked(pid)

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
            custody = self._custody
            if custody is not None and (custody.failed or custody.cleanup_errors):
                active = self._active_locked(custody, retain_error=True)
                return ProcessLiveness(
                    (
                        ProcessLivenessState.ALIVE
                        if active is True
                        else ProcessLivenessState.UNKNOWN
                    ),
                    observed=True,
                    pid=self._pid,
                    marker=self._identity,
                    detail="Windows Job cleanup is pending",
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
            if self._spawning:
                return False
            # Deliberately double as the synchronous release-retry seam: a
            # successful stop observation also settles handles retained by an
            # earlier close or asynchronous Job-termination delay.
            self._release_if_gone_locked(self._pid)
            return self._custody is None

    def force_close(self) -> None:
        with self._lock:
            self._stop_requested = True
            custody = self._custody
            if custody is not None:
                self._terminate_job_locked(custody)
        deadline = (
            time.monotonic()
            + PROCESS_FORCE_TERM_SECONDS
            + PROCESS_FORCE_KILL_SECONDS
        )
        while time.monotonic() < deadline:
            if self.stopped():
                return
            with self._lock:
                custody = self._custody
                if custody is not None and not self._spawning:
                    self._terminate_job_locked(custody)
            time.sleep(0.01)
        if self._pid is not None:
            self.release_if_gone(self._pid)

    def _close_unregistered_group(self, pid: int) -> None:
        self.signal(pid, 9)

    def __del__(self):
        custody = getattr(self, "_custody", None)
        if custody is None:
            return
        for attribute in ("inherited_ready", "ready", "inherited_job", "job"):
            handle = getattr(custody, attribute, None)
            if handle is not None:
                try:
                    handle.Close()
                except BaseException:
                    pass
