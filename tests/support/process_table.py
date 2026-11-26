"""One portable process ledger for tests that start real processes.

Decision: portable-process-cleanup-20261119.  Tests that start launchers, hosts
and daemons used to read ``/proc`` (child listing, zombie state, "is it gone"),
which does not exist on macOS: ``not Path("/proc/<pid>").exists()`` is always
true there, so a "the process is gone" assertion proved nothing.  Everything here
goes through ``ps -A -o pid=,ppid=,stat=,command=`` (same flags on Linux and
macOS), the existence probe ``pid_alive`` (``hyprial.kernel.probe_process``, a
signal-zero probe on POSIX) and ``hyprial.kernel.process_birth_identity`` (which
already picks ``/proc`` on Linux, libproc on macOS and ``ps lstart`` elsewhere),
so there is one POSIX implementation and no Linux/macOS branch.
A pid is never signalled by number alone: it is recorded together with its birth
identity, and ``Ledger.signal`` refuses (returns False) when the live process
at that pid has another birth, so a reused pid is left alone.  A recorded pid
whose birth could not be read is never signalled, but it is not silently dropped
either: a pid that is alive while its birth cannot be verified (unreadable, or
different from a birth another process supplied, which is how a differently
formatted birth string would show up) is reported by ``live`` and ``stop`` as a
survivor, "alive, birth unverifiable", and left unsignalled.  ``record`` without
a birth never adds a second, live-read birth to a pid that already has one, so a
reused pid cannot pass the fence.  "Gone" means no live process
with the recorded birth: a zombie (state ``Z``/``X``) is dead for this purpose,
a process that exists with the same birth and another state is not.  Every wait
is a bounded loop on a condition and reports the survivors at its deadline.

Decision (2026-11-26): Windows support.  On Windows (``WINDOWS``) there is no
``ps``: ``table`` reads a Toolhelp32 process snapshot (pid, parent pid and image
name, so ``Row.command`` is only the image name there and ``Row.stat`` is always
``"R"``), and ``state`` is ``"R"`` while ``pid_alive`` sees the process and
``""`` otherwise, because Windows has no zombie state.  A Windows parent pid can
name a process that exited long ago and whose pid was reused, so a row is a
child of ``parent`` there only when its creation time is not earlier than the
parent's; a row whose creation time cannot be read is never a child.  Windows
has no signals to send by number: ``Ledger.signal`` calls
``hyprial.kernel.terminate_fenced_process`` with the recorded birth, which
checks that birth on an open process handle before ``TerminateProcess``, so the
fence is the same and every signal number ends the process.  ``KILL`` is
``SIGKILL`` where it exists and ``SIGTERM`` otherwise; ``stop`` and
``Ledger.kill`` use it, and nothing here names ``signal.SIGKILL`` directly.  The
POSIX paths are unchanged.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import time
from collections.abc import Callable, Iterable
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

from hyprial.kernel import process_birth_identity, terminate_fenced_process
from tests.support.windows_suite.host import pid_alive

DEAD_STATES = frozenset({"Z", "X"})
LISTING = ["ps", "-A", "-ww", "-o", "pid=,ppid=,stat=,command="]
POLL_SECONDS = 0.05
WINDOWS = os.name == "nt"
KILL = getattr(signal, "SIGKILL", signal.SIGTERM)
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class ProcessEntry32W(ctypes.Structure):
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


@dataclass(frozen=True)
class Row:
    pid: int
    ppid: int
    stat: str
    command: str

    @property
    def dead(self) -> bool:
        return self.stat[:1] in DEAD_STATES


def _kernel32() -> Any:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        function = getattr(kernel, name)
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
        function.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def toolhelp_rows(kernel: Any) -> dict[int, Row]:
    """Every process in one Toolhelp32 snapshot taken through ``kernel`` (a bound ``kernel32``)."""

    snapshot = kernel.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        raise OSError("CreateToolhelp32Snapshot returned no process snapshot")
    rows: dict[int, Row] = {}
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(ProcessEntry32W)
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            pid = int(entry.th32ProcessID)
            if pid > 0:
                rows[pid] = Row(pid, int(entry.th32ParentProcessID), "R", entry.szExeFile)
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return rows


def table() -> dict[int, Row]:
    if WINDOWS:
        return toolhelp_rows(_kernel32())
    listing = subprocess.run(LISTING, capture_output=True, text=True, check=True, timeout=30)
    rows: dict[int, Row] = {}
    for line in listing.stdout.splitlines():
        fields = line.split(None, 3)
        if len(fields) >= 3 and fields[0].isdigit() and fields[1].isdigit():
            rows[int(fields[0])] = Row(int(fields[0]), int(fields[1]), fields[2], fields[3] if len(fields) > 3 else "")
    return rows


def _creation_stamp(pid: int) -> int | None:
    marker = process_birth_identity(pid)
    if marker is None:
        return None
    scheme, _, value = marker.partition(":")
    return int(value) if scheme == "win-filetime" and value.isdigit() else None


def _child(row: Row, parent: int) -> bool:
    if row.ppid != parent:
        return False
    if not WINDOWS:
        return True
    born, parent_born = _creation_stamp(row.pid), _creation_stamp(parent)
    return born is not None and parent_born is not None and parent_born <= born


def children_of(parent: int) -> list[int]:
    return sorted(row.pid for row in table().values() if _child(row, parent))


def descendants_of(parent: int) -> list[int]:
    rows = table()
    found: list[int] = []
    pending = [parent]
    while pending:
        current = pending.pop()
        for row in rows.values():
            if row.pid != parent and row.pid not in found and _child(row, current):
                found.append(row.pid)
                pending.append(row.pid)
    return sorted(found)


def state(pid: int) -> str:
    try:
        present = pid_alive(pid)
    except PermissionError:
        present = True
    if not present:
        return ""
    if WINDOWS:
        return "R"
    probe = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False, timeout=30)
    return probe.stdout.strip()


def exists(pid: int) -> bool:
    return bool(state(pid))


def alive(pid: int) -> bool:
    return exists(pid) and state(pid)[:1] not in DEAD_STATES


def wait_until(condition: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if condition():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)


class Ledger:
    """Pids a test started, each with the birth identity it had when first seen."""

    def __init__(self, spare: Callable[[], Iterable[int]] = lambda: ()) -> None:
        self.births: dict[int, set[str]] = {}
        self.supplied: set[int] = set()
        self._spare = spare

    def record(self, pid: int, birth: str | None = None) -> bool:
        if birth is None and pid in self.births:
            return True
        observed = birth if birth is not None else process_birth_identity(pid)
        if observed is None:
            return False
        self.births.setdefault(pid, set()).add(observed)
        if birth is not None:
            self.supplied.add(pid)
        return True

    def adopt_descendants(self, root: int) -> list[int]:
        added = []
        spare = set(self._spare())
        for pid in descendants_of(root):
            if pid not in self.births and pid not in spare and self.record(pid):
                added.append(pid)
        return added

    def same(self, pid: int) -> bool:
        if pid not in self.births or not alive(pid):
            return False
        current = process_birth_identity(pid)
        return current is not None and current in self.births[pid]

    def exists(self, pid: int) -> bool:
        """A process with this pid and a recorded birth exists, zombies included; an unreadable birth counts as existing."""

        if not exists(pid):
            return False
        current = process_birth_identity(pid)
        return current is None or pid not in self.births or current in self.births[pid]

    def unverifiable(self, pid: int) -> bool:
        """The pid is alive and recorded, but its birth cannot be checked against the record."""

        if pid not in self.births or not alive(pid):
            return False
        current = process_birth_identity(pid)
        if current is None:
            return True
        return pid in self.supplied and current not in self.births[pid]

    def verified_live(self, exclude: Iterable[int] = ()) -> list[int]:
        skip = set(exclude) | set(self._spare())
        return sorted(pid for pid in self.births if pid not in skip and self.same(pid))

    def live(self, exclude: Iterable[int] = ()) -> list[int]:
        skip = set(exclude) | set(self._spare())
        return sorted(pid for pid in self.births if pid not in skip and (self.same(pid) or self.unverifiable(pid)))

    def describe(self, pids: Iterable[int]) -> str:
        rows = table()
        parts = []
        for pid in pids:
            row = rows.get(pid)
            text = f"pid {pid} ({row.command if row else 'command unknown'})"
            parts.append(text + (" alive, birth unverifiable" if self.unverifiable(pid) else ""))
        return "; ".join(parts)

    def signal(self, pid: int, sig: int) -> bool:
        if not self.same(pid):
            return False
        if WINDOWS:
            return any(terminate_fenced_process(pid, birth_marker=birth) for birth in sorted(self.births[pid]))
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return False
        return True

    def kill(self, pid: int) -> bool:
        """Hard-stop one recorded pid (``SIGKILL`` on POSIX, a fenced ``TerminateProcess`` on Windows)."""

        return self.signal(pid, KILL)

    def stop(self, *, term: float = 0.0, kill: float = 20.0, exclude: Iterable[int] = ()) -> list[int]:
        """SIGTERM (when ``term`` > 0), then SIGKILL, every recorded live pid but ``exclude``; return the survivors."""

        if term > 0:
            for pid in self.verified_live(exclude):
                self.signal(pid, signal.SIGTERM)
            wait_until(lambda: not self.verified_live(exclude), term)
        for pid in self.verified_live(exclude):
            self.kill(pid)
        wait_until(lambda: not self.verified_live(exclude), kill)
        return self.live(exclude)
