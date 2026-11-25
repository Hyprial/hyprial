"""The expected side of the bootstrap peer check.

Decision: daemon-first-z1-peer-uid-20261115.  The daemon socket sat in a
``0700`` state directory with mode ``0600``, so only the owning user could
reach it; the peer uid check was defense in depth, and this module read the
operating-system peer credential of an accepted connection.

Decision: ipc-bootstrap-2026-11-19.  The credential reader moved into the
kernel local IPC seam (``kernel/impl/platform/local_ipc/posix.py``): the
listener's ``accept`` hands the wire host a :class:`LocalIpcPeer` that already
carries the uid, the unreadable-credential record and the transport's
same-user guarantee, so nothing here reads socket options anymore.  What
stays is ``own_uid`` -- the uid a peer must have, the expected side of the
wire host's comparison.
"""

from __future__ import annotations

import os


def own_uid() -> int | None:
    """The uid a peer must have, or ``None`` where the platform has no uids."""

    geteuid = getattr(os, "geteuid", None)
    return None if geteuid is None else geteuid()
