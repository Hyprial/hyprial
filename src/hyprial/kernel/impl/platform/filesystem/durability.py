"""Directory-entry durability after ``os.replace`` -- decided by capability, not platform.

A rename is durable on POSIX only once the parent directory is flushed, which
is done by opening the directory read-only and ``fsync``-ing that descriptor.
Not every OS can open a directory as a file at all: the Windows CRT refuses it
with ``PermissionError`` (EACCES), and NTFS journals the rename's metadata on
its own.  No maintained cross-platform library offers this (``atomicwrites`` is
archived), so this is the single seam every caller goes through instead of an
``os.open(dir, O_RDONLY)`` + ``os.fsync`` pair of its own
(``scripts/ci/check_windows_portability.py`` rule W007 keeps new pairs out).

The capability test is the open itself: when the directory cannot be opened
for reading, there is nothing to flush and the call is a no-op.  A failure of
the flush itself is a real I/O error and propagates; so does a missing
directory.

Known trade-off: on POSIX a directory the caller may write but not read (mode
``0o300``) also raises ``PermissionError`` here, so its entries are not flushed
and the call returns False instead of failing.  Callers that must know whether
the rename reached the disk check the return value.
"""

from __future__ import annotations

import os
from pathlib import Path


def fsync_directory(path: str | os.PathLike[str]) -> bool:
    """Flush ``path``'s directory entries where the OS supports it.

    Returns True when a flush was performed and False when the platform cannot
    open a directory for reading (the capability is absent).  Never branches on
    the platform name.
    """

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(Path(path), flags)
    except PermissionError:
        return False
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True
