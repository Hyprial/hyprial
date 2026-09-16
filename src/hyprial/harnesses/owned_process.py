"""PID-group ownership fenced by stable process birth identity."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from hyprial.daemon.api import (
    ProcessLiveness,
    ProcessLivenessProbeError,
    ProcessLivenessState,
)

PROCESS_FORCE_TERM_SECONDS = 0.25
PROCESS_FORCE_KILL_SECONDS = 0.25
DEAD_PROCESS_STATES = frozenset({"X", "Z"})


def parse_linux_process_stat(stat: str) -> tuple[str, int, str] | None:
    """Return state, process group, and birth marker from /proc/PID/stat."""

    closing = stat.rfind(")")
    fields = stat[closing + 2 :].split() if closing >= 0 else []
    if len(fields) <= 19:
        return None
    try:
        process_group_id = int(fields[2])
    except ValueError:
        return None
    return fields[0], process_group_id, fields[19]


def read_linux_process_stat(pid: int) -> tuple[str, int, str] | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    return parse_linux_process_stat(stat)


def linux_group_has_live_members(process_group_id: int) -> bool | None:
    """Distinguish live group members from PID-1-owned zombie remnants.

    ``killpg(pgid, 0)`` still succeeds for a zombie-only group.  An orphaned
    grandchild is reparented outside this process, so only PID 1 can reap it;
    job containers without an init may retain that harmless group shell for
    their lifetime.  We still fail safe on every runnable or unknown member.
    """

    saw_member = False
    try:
        entries = Path("/proc").iterdir()
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
        except FileNotFoundError:
            # The process exited between listing /proc and reading its stat.
            continue
        except (OSError, UnicodeError):
            # If any extant entry is unreadable, group membership cannot be
            # proven.  Keep ownership fail-safe instead of reporting clean.
            return None
        observed = parse_linux_process_stat(stat)
        if observed is None:
            return None
        if observed[1] != process_group_id:
            continue
        saw_member = True
        if observed[0] not in DEAD_PROCESS_STATES:
            return True
    return False if saw_member else None


def darwin_group_has_live_members(process_group_id: int) -> bool | None:
    """Return Darwin group liveness, or ``None`` when it cannot be proven."""

    try:
        result = subprocess.run(
            ["ps", "-axo", "state=,pgid="],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None

    saw_member = False
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            return None
        state, raw_group_id = fields
        try:
            observed_group_id = int(raw_group_id)
        except ValueError:
            return None
        if observed_group_id != process_group_id:
            continue
        saw_member = True
        if not state or state[0] not in DEAD_PROCESS_STATES:
            return True
    return False if saw_member else None


def process_birth_identity(pid: int) -> str | None:
    """Return a stable birth marker used to fence PID/PGID reuse."""

    if sys.platform.startswith("linux"):
        observed = read_linux_process_stat(pid)
        return f"proc:{observed[2]}" if observed is not None else None
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    identity = result.stdout.strip() if result.returncode == 0 else ""
    return f"ps:{identity}" if identity else None


class OwnedProcessGroup:
    """Thread-safe OS-only cleanup handle for one subprocess generation."""

    def __init__(self, *, label: str) -> None:
        self._label = label
        self._lock = threading.Lock()
        self._pid: int | None = None
        self._identity: str | None = None
        self._stop_requested = False
        self._registrations = 0
        self._observed = False

    @staticmethod
    def _process_birth_identity(pid: int) -> str | None:
        return process_birth_identity(pid)

    @staticmethod
    def _linux_group_has_live_members(process_group_id: int) -> bool | None:
        return linux_group_has_live_members(process_group_id)

    @staticmethod
    def _darwin_group_has_live_members(process_group_id: int) -> bool | None:
        return darwin_group_has_live_members(process_group_id)

    def register(self, pid: int) -> None:
        with self._lock:
            self._registrations += 1
            self._observed = True
        try:
            identity = self._process_birth_identity(pid)
            with self._lock:
                stopping = self._stop_requested
                if identity is not None and not stopping:
                    self._pid = pid
                    self._identity = identity
                    return
                # Retain the rejected generation until its whole PGID is
                # proven gone.  In particular, an unreadable identity or a
                # signal permission error must not make stopped() report a
                # false success.
                self._pid = pid
                self._identity = identity
            # The subprocess was already placed in its own process group by
            # the spawn call.  If ownership cannot be published, drain that
            # entire fresh group rather than killing only its leader.
            self._close_unregistered_group(pid)
            self.release_if_gone(pid)
            if identity is None:
                raise ConnectionError(
                    f"cannot establish {self._label} process identity"
                )
            raise ConnectionError(f"{self._label} process is stopping")
        finally:
            with self._lock:
                self._registrations -= 1

    def exists(self, pid: int) -> bool:
        with self._lock:
            return self._pid == pid and self._is_current_locked()

    def signal(self, pid: int, signum: signal.Signals) -> None:
        with self._lock:
            if self._pid != pid or not self._is_current_locked():
                return
            try:
                os.killpg(pid, signum)
            except (ProcessLookupError, PermissionError):
                pass

    def release_if_gone(self, pid: int) -> None:
        with self._lock:
            if self._pid == pid and not self._group_has_live_members(pid):
                self._pid = None
                self._identity = None

    def liveness(self) -> ProcessLiveness:
        with self._lock:
            observed = self._observed
            pid = self._pid
            marker = self._identity
        if not observed:
            return ProcessLiveness(
                ProcessLivenessState.UNKNOWN,
                observed=False,
                detail="process group has never been observed",
            )
        if pid is None:
            return ProcessLiveness(ProcessLivenessState.DEAD, observed=True)
        if marker is None:
            return ProcessLiveness(
                ProcessLivenessState.UNKNOWN,
                observed=True,
                pid=pid,
                detail="process identity criterion is unavailable",
            )
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return ProcessLiveness(
                ProcessLivenessState.DEAD,
                observed=True,
                pid=pid,
                marker=marker,
            )
        except OSError as error:
            raise ProcessLivenessProbeError(
                f"cannot probe process group {pid}: {error}"
            ) from error
        observed_marker = self._process_birth_identity(pid)
        if observed_marker is None:
            return ProcessLiveness(
                ProcessLivenessState.UNKNOWN,
                observed=True,
                pid=pid,
                marker=marker,
                detail="live process identity cannot be read",
            )
        if observed_marker != marker:
            raise ProcessLivenessProbeError(
                f"process identity changed for group {pid}"
            )
        return ProcessLiveness(
            ProcessLivenessState.ALIVE,
            observed=True,
            pid=pid,
            marker=marker,
        )

    def stopped(self) -> bool:
        with self._lock:
            if self._registrations:
                return False
            pid = self._pid
            if pid is None:
                return True
            if self._is_current_locked():
                return False
            # A live group with an unreadable live leader is ambiguous and
            # must fail safe instead of being declared cleaned.
            if self._group_has_live_members(pid):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return False
                except OSError:
                    return False
                observed = self._process_birth_identity(pid)
                if observed is None:
                    return False
            self._pid = None
            self._identity = None
            return True

    def force_close(self) -> None:
        with self._lock:
            # This intent is deliberately sticky.  A generation whose spawn
            # or identity lookup races with stop must never publish ownership
            # after forced cleanup has observed an empty slot.
            self._stop_requested = True
            pid = self._pid
        if pid is None:
            return
        self.signal(pid, signal.SIGTERM)
        if self._wait_gone(pid, PROCESS_FORCE_TERM_SECONDS):
            return
        self.signal(pid, signal.SIGKILL)
        self._wait_gone(pid, PROCESS_FORCE_KILL_SECONDS)

    @classmethod
    def _close_unregistered_group(cls, pid: int) -> None:
        cls._signal_unregistered_group(pid, signal.SIGTERM)
        if cls._wait_unregistered_group_gone(pid, PROCESS_FORCE_TERM_SECONDS):
            return
        cls._signal_unregistered_group(pid, signal.SIGKILL)
        cls._wait_unregistered_group_gone(pid, PROCESS_FORCE_KILL_SECONDS)

    @staticmethod
    def _signal_unregistered_group(pid: int, signum: signal.Signals) -> None:
        try:
            os.killpg(pid, signum)
        except (ProcessLookupError, PermissionError):
            pass

    @classmethod
    def _wait_unregistered_group_gone(cls, pid: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while cls._group_has_live_members(pid):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def _wait_gone(self, pid: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self.exists(pid):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        self.release_if_gone(pid)
        return True

    def _is_current_locked(self) -> bool:
        pid = self._pid
        identity = self._identity
        if pid is None or identity is None or not self._group_has_live_members(pid):
            return False
        observed = self._process_birth_identity(pid)
        if observed is not None:
            return observed == identity
        # If the original group leader is gone but descendants retain its
        # PGID, that numeric group cannot be reused until those descendants
        # exit.  An existing but unreadable leader is fail-safe: do not signal.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        return False

    @classmethod
    def _group_has_live_members(cls, pid: int) -> bool:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        if sys.platform.startswith("linux"):
            live = cls._linux_group_has_live_members(pid)
            if live is not None:
                return live
        elif sys.platform == "darwin":
            live = cls._darwin_group_has_live_members(pid)
            if live is not None:
                return live
        return True
