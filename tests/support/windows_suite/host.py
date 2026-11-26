"""Process helpers for the Windows suite: ``pid_alive`` and the ``os.kill`` interceptor.

Decision (2026-11-26): on Windows ``os.kill``
is not a probe.  ``signal`` 0 and 1 are ``CTRL_C_EVENT`` and
``CTRL_BREAK_EVENT``, which go to a whole console process group, and every
other number calls ``TerminateProcess`` with that number as exit code, so
``os.kill(pid, 0)`` ends the target and ``os.kill(os.getpid(), SIGTERM)`` ends
the xdist worker itself.  When the ledger is enabled on Windows,
``install_kill_interceptor`` replaces ``os.kill`` for the life of the process:
``sig in (0, 1)`` or ``pid == os.getpid()`` appends a line to
``interceptor-hits.jsonl`` and raises ``AssertionError`` in the calling test;
every other call goes to the original function.  It is never installed on
POSIX.  Code that bound the original ``os.kill`` earlier (for example as a
default argument) is not intercepted; such product paths are fixed at the
source instead.  A test that swallows the ``AssertionError`` still leaves its
hit in the file, which the summary treats as a hard failure.

Decision (same date): ``pid_alive`` wraps ``hyprial.kernel.probe_process``,
which answers without signalling on every platform.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path

from hyprial.kernel import probe_process

HITS_FILE = "interceptor-hits.jsonl"


def pid_alive(pid: int) -> bool:
    """Whether a process with this id exists; other probe errors propagate."""

    try:
        probe_process(pid)
    except ProcessLookupError:
        return False
    return True


class KillInterceptor:
    """Stand-in for ``os.kill`` that refuses probes and self-signals."""

    def __init__(
        self,
        original: Callable[[int, int], None],
        hits: Path,
        current_nodeid: Callable[[], str | None],
    ) -> None:
        self.original = original
        self.hits = hits
        self._current_nodeid = current_nodeid

    def __call__(self, pid: int, sig: int, /) -> None:
        if sig in (0, 1) or pid == os.getpid():
            self._record(pid, sig)
            raise AssertionError(
                "os.kill(pid=%d, sig=%d) is not allowed in Windows tests; use "
                "tests.support.windows_suite.host.pid_alive or Popen.poll/kill" % (pid, sig)
            )
        self.original(pid, sig)

    def _record(self, pid: int, sig: int) -> None:
        record = {
            "nodeid": self._current_nodeid(),
            "pid": int(pid),
            "sig": int(sig),
            "process": os.getpid(),
            "wall": time.time(),
        }
        with open(self.hits, "ab") as stream:
            stream.write((json.dumps(record) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())


def install_kill_interceptor(
    ledger_dir: Path, current_nodeid: Callable[[], str | None]
) -> KillInterceptor | None:
    """Replace ``os.kill`` on Windows; return the interceptor, or ``None`` elsewhere."""

    if os.name != "nt":
        return None
    if isinstance(os.kill, KillInterceptor):
        return os.kill
    interceptor = KillInterceptor(os.kill, Path(ledger_dir) / HITS_FILE, current_nodeid)
    os.kill = interceptor
    return interceptor
