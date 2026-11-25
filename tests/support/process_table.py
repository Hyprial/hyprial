"""One portable process ledger for tests that start real processes.

Decision: portable-process-cleanup-20261119.  Tests that start launchers, hosts
and daemons used to read ``/proc`` (child listing, zombie state, "is it gone"),
which does not exist on macOS: ``not Path("/proc/<pid>").exists()`` is always
true there, so a "the process is gone" assertion proved nothing.  Everything here
goes through ``ps -A -o pid=,ppid=,stat=,command=`` (same flags on Linux and
macOS), ``os.kill(pid, 0)`` and ``hyprial.kernel.process_birth_identity`` (which
already picks ``/proc`` on Linux, libproc on macOS and ``ps lstart`` elsewhere),
so there is one implementation and no platform branch.
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
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from hyprial.kernel import process_birth_identity

DEAD_STATES = frozenset({"Z", "X"})
LISTING = ["ps", "-A", "-ww", "-o", "pid=,ppid=,stat=,command="]
POLL_SECONDS = 0.05


@dataclass(frozen=True)
class Row:
    pid: int
    ppid: int
    stat: str
    command: str

    @property
    def dead(self) -> bool:
        return self.stat[:1] in DEAD_STATES


def table() -> dict[int, Row]:
    listing = subprocess.run(LISTING, capture_output=True, text=True, check=True, timeout=30)
    rows: dict[int, Row] = {}
    for line in listing.stdout.splitlines():
        fields = line.split(None, 3)
        if len(fields) >= 3 and fields[0].isdigit() and fields[1].isdigit():
            rows[int(fields[0])] = Row(int(fields[0]), int(fields[1]), fields[2], fields[3] if len(fields) > 3 else "")
    return rows


def children_of(parent: int) -> list[int]:
    return sorted(row.pid for row in table().values() if row.ppid == parent)


def descendants_of(parent: int) -> list[int]:
    rows = table()
    found: list[int] = []
    pending = [parent]
    while pending:
        current = pending.pop()
        for row in rows.values():
            if row.ppid == current and row.pid not in found:
                found.append(row.pid)
                pending.append(row.pid)
    return sorted(found)


def state(pid: int) -> str:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return ""
    except PermissionError:
        pass
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
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return False
        return True

    def stop(self, *, term: float = 0.0, kill: float = 20.0, exclude: Iterable[int] = ()) -> list[int]:
        """SIGTERM (when ``term`` > 0), then SIGKILL, every recorded live pid but ``exclude``; return the survivors."""

        if term > 0:
            for pid in self.verified_live(exclude):
                self.signal(pid, signal.SIGTERM)
            wait_until(lambda: not self.verified_live(exclude), term)
        for pid in self.verified_live(exclude):
            self.signal(pid, signal.SIGKILL)
        wait_until(lambda: not self.verified_live(exclude), kill)
        return self.live(exclude)
