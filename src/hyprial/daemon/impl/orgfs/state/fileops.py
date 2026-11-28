# Decision record (C1 state store, 2026-11-25):
# - Every durable step the store takes (write, fsync, directory fsync, rename,
#   unlink, truncate) goes through one ``FileOps`` instance and carries a step
#   name such as ``delta.write`` or ``pending.rename``.  ``before(step)`` runs
#   first; tests override it to raise an I/O error or to simulate a crash at
#   exactly that step, and the C1 crash tests enumerate every step of every
#   operation this way (design section 7, row C1).
# - Writes go straight to the descriptor with ``os.write`` (no Python buffer), so
#   a process that dies after a step returned has handed every byte of it to the
#   kernel; a write is looped until complete.
# - Directory flushes use ``hyprial.kernel.fsync_directory`` (the repository's
#   single seam for it); atomic replacement follows the temp-file, fsync, rename,
#   fsync-directory sequence of ``network/sync.py::_save_sync_cursor``.
"""Injectable filesystem primitives of the file state store."""

from __future__ import annotations

import os
from pathlib import Path

from hyprial.kernel import fsync_directory


class FileOps:
    """Filesystem steps of ``FileStateStore``; subclass ``before`` to inject faults."""

    def before(self, step: str) -> None:
        """Hook called before each named step; the default does nothing."""

    def write(self, fd: int, data: bytes, step: str) -> None:
        self.before(step)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]

    def fsync(self, fd: int, step: str) -> None:
        self.before(step)
        os.fsync(fd)

    def fsync_dir(self, path: Path, step: str) -> None:
        self.before(step)
        fsync_directory(path)

    def rename(self, source: Path, target: Path, step: str) -> None:
        self.before(step)
        os.replace(source, target)

    def unlink(self, path: Path, step: str) -> bool:
        self.before(step)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    def truncate(self, fd: int, size: int, step: str) -> None:
        self.before(step)
        os.ftruncate(fd, size)
