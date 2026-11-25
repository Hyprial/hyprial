"""Foreground termination handling for the GUI command.

Decision record (``--stdin-stop``, 2026-11-16): EOF or a ``shutdown`` line on
stdin is a stop request that needs no signal from the caller, so a supervising
shell that crashes (its pipe closes) leaves no orphan.  A watcher thread
watches file descriptor 0 and signals the main thread with SIGTERM, so the one
guard below and the caller's ``finally`` reclaim children exactly as for a real
SIGTERM; the only difference is ``GuiTerminationRequested.clean``, which the
command maps to exit 0.  The guard yields a handle whose ``seal`` the command
calls first in its ``finally``: from then on no stop request (stdin or SIGTERM)
is acted on, so the first outcome's exit status and failure line win and
cleanup always runs to completion.  Without the flag stdin is never touched.

Decision stdin-stop-manysys-20261121 (W1C-GUI, owner 2026-11-21): descriptor
readability polling (interruptible wake pipe / non-blocking fallback on POSIX,
``PeekNamedPipe`` slices on Windows) and cross-platform main-thread ``SIGTERM``
delivery (``signal.pthread_kill`` where present, ``_thread.interrupt_main``
otherwise) are owned by ``hyprial.kernel.manysys`` (``StdinStopWatcher`` and
``deliver_main_termination``).  This module keeps only the GUI command's
``TerminationGuard`` state machine and ``GuiTerminationRequested`` exception,
with zero platform or low-level descriptor/signal branches.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import signal
import sys
import threading

from hyprial.kernel import ipc_errors, manysys

from .errors import GuiError

SIGTERM_EXIT_CODE = 128 + int(signal.SIGTERM)
STDIN_SHUTDOWN_LINE = manysys.STDIN_SHUTDOWN_LINE


class GuiTerminationRequested(BaseException):
    """Unwinds a foreground GUI command so its ``finally`` can reclaim children."""

    __slots__ = ("clean",)

    def __init__(self, clean: bool = False) -> None:
        super().__init__()
        self.clean = clean


def _stdin_descriptor() -> int:
    try:
        return int(sys.stdin.fileno())
    except (AttributeError, OSError, ValueError) as error:
        raise GuiError(
            ipc_errors.INVALID_ARGUMENT,
            "--stdin-stop needs a standard input that is a real file descriptor",
        ) from error


class TerminationGuard:
    """Handle yielded by :func:`termination_guard`; ``seal`` ends stop handling."""

    def __init__(self, watcher: manysys.StdinStopWatcher | None = None) -> None:
        self._watcher = watcher
        self._delivery_lock = threading.Lock()
        self.sealed = False

    def seal(self) -> None:
        self.sealed = True
        if self._watcher is not None:
            self._watcher.close()
        with self._delivery_lock:
            pass
        if self._watcher is not None:
            join = getattr(self._watcher, "join", None)
            if callable(join):
                join(timeout=1.0)


@contextmanager
def termination_guard(*, stdin_stop: bool = False) -> Iterator[TerminationGuard]:
    """Turn the first SIGTERM into cleanup control flow, then ignore repeats.

    Python installs signal handlers only on the main thread, so callers on a
    worker thread keep the process's existing SIGTERM disposition.  The second
    signal during cleanup is ignored: it must not interrupt the process-group
    termination that the first one requested.
    """
    if threading.current_thread() is not threading.main_thread():
        yield TerminationGuard()
        return
    descriptor = _stdin_descriptor() if stdin_stop else None
    handle = TerminationGuard()
    active = True

    def deliver_stop() -> None:
        with handle._delivery_lock:
            if active and not handle.sealed:
                manysys.deliver_main_termination(int(signal.SIGTERM))

    watcher = (
        manysys.StdinStopWatcher(descriptor, on_stop=deliver_stop)
        if descriptor is not None
        else None
    )
    handle._watcher = watcher
    previous = signal.getsignal(signal.SIGTERM)
    triggered = False

    def request_termination(_signum: int, _frame: object) -> None:
        nonlocal triggered
        if triggered or handle.sealed or not active:
            return
        triggered = True
        raise GuiTerminationRequested(
            clean=watcher is not None and watcher.pending.is_set()
        )

    try:
        signal.signal(signal.SIGTERM, request_termination)
    except BaseException:
        if watcher is not None:
            watcher.close()
        raise
    try:
        if watcher is not None:
            watcher.start()
        yield handle
    finally:
        active = False
        handle.seal()
        signal.signal(signal.SIGTERM, previous)
