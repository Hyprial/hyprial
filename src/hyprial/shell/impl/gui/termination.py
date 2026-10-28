"""Foreground termination handling for the source-mode GUI command."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import signal
import threading

#: Conventional shell status for a process that handled SIGTERM and exited.
SIGTERM_EXIT_CODE = 128 + int(signal.SIGTERM)


class GuiTerminationRequested(BaseException):
    """Unwinds a foreground GUI command so its ``finally`` can reclaim children."""

    __slots__ = ()


@contextmanager
def termination_guard() -> Iterator[None]:
    """Turn the first SIGTERM into cleanup control flow, then ignore repeats.

    Python installs signal handlers only on the main thread, so callers on a
    worker thread keep the process's existing SIGTERM disposition.  The second
    signal during cleanup is ignored: it must not interrupt the process-group
    termination that the first one requested.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)
    triggered = False

    def request_termination(_signum: int, _frame: object) -> None:
        nonlocal triggered
        if triggered:
            return
        triggered = True
        raise GuiTerminationRequested()

    signal.signal(signal.SIGTERM, request_termination)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
