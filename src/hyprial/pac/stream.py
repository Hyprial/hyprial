"""Lifetime of a public CLI follow stream, independent of parent signal policy.

No stdin is consumed: a subscriber launched with /dev/null stdin is valid. A
closed OUTPUT pipe/socket is cancellation, including while the journal is idle.
"""

from __future__ import annotations

import io
import os
import select
import signal
import stat
import sys
from contextlib import contextmanager


class OutputWatch:
    def __init__(self, stream):
        self.poller = None
        self.queue = None
        try:
            fd = stream.fileno()
            mode = os.fstat(fd).st_mode
        except (AttributeError, io.UnsupportedOperation, OSError):
            return  # an in-process text capture is not a pipe
        if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
            return  # a terminal or regular log file has no downstream pipe EOF
        if hasattr(select, "poll"):
            self.poller = select.poll()
            self.poller.register(fd, select.POLLERR | select.POLLHUP)
        else:  # macOS/BSD: kqueue exposes write-side EOF without emitting bytes
            self.queue = select.kqueue()
            try:
                self.queue.control([select.kevent(fd, filter=select.KQ_FILTER_WRITE,
                                                  flags=select.KQ_EV_ADD)], 0, 0)
            except BaseException:
                self.queue.close()
                raise

    def closed(self) -> bool:
        if self.poller is not None:
            return any(mask & (select.POLLERR | select.POLLHUP) for _, mask in self.poller.poll(0))
        if self.queue is not None:
            return any(event.flags & (select.KQ_EV_EOF | select.KQ_EV_ERROR)
                       for event in self.queue.control(None, 1, 0))
        return False

    def close(self) -> None:
        if self.queue is not None:
            self.queue.close()


def _interrupt(_signal, _frame):
    raise KeyboardInterrupt


@contextmanager
def follow_lifetime(enabled: bool):
    previous = {}
    watcher = None
    try:
        if enabled:
            # exec preserves SIG_IGN. Install our own policy even under nohup,
            # daemon/CI launchers; SIGTERM is as valid as interactive Ctrl-C.
            for kind in (signal.SIGINT, signal.SIGTERM):
                previous[kind] = signal.signal(kind, _interrupt)
            watcher = OutputWatch(sys.stdout)
        yield watcher
    finally:
        try:
            if watcher is not None:
                watcher.close()
        finally:
            for kind, handler in previous.items():
                signal.signal(kind, handler)


def silence_broken_pipe() -> None:
    """Avoid a second EPIPE from interpreter-finalization stdout flush."""
    try:
        fd = sys.stdout.fileno()
    except (AttributeError, io.UnsupportedOperation, OSError):
        return
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null, fd)
    finally:
        os.close(null)
