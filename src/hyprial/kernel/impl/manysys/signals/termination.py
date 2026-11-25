# Decision record (manysys/signals/termination, W1-MANYSYS 2026-11-21):
# - Centralizes cross-platform main-thread termination delivery for foreground
#   commands (such as GUI --stdin-stop) inside manysys.
# - POSIX uses signal.pthread_kill against threading.main_thread().ident so a
#   watcher thread delivers the exact signal asynchronously to the main thread.
# - Platforms without signal.pthread_kill (Windows) call
#   _thread.interrupt_main(signum), which since Python 3.10 accepts SIGTERM and
#   invokes the main thread's registered Python signal handler.
"""Cross-platform main-thread termination signal delivery."""

from __future__ import annotations

import _thread
import signal
import threading


def deliver_main_termination(signum: int | None = None) -> None:
    """Deliver ``signum`` (default ``SIGTERM``) to the Python main thread."""

    resolved = int(signal.SIGTERM if signum is None else signum)
    pthread_kill = getattr(signal, "pthread_kill", None)
    if pthread_kill is not None:
        ident = threading.main_thread().ident
        if ident is not None:
            pthread_kill(ident, resolved)
            return
    _thread.interrupt_main(resolved)
