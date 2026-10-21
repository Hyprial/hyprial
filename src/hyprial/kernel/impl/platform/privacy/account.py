"""Current OS account facts where the platform exposes POSIX process ids."""

from __future__ import annotations

import os

_POSIX_METADATA_ENFORCED = os.name != "nt"


def current_uid() -> int:
    """Return this process's uid, or 0 where the platform has no ``os.getuid``.

    Windows has no POSIX uids: ``os.getuid`` does not exist and ``stat``
    reports uid 0 for every path.  Callers keep their ownership fences on
    POSIX; this function only removes the attribute-lookup failure.
    """
    getuid = getattr(os, "getuid", None)
    return 0 if getuid is None else getuid()


def posix_metadata_enforced() -> bool:
    """Whether ``stat`` uid and permission bits describe access control here.

    Windows synthesises ``st_mode`` bits (0o777 directories, 0o666 files) and
    ignores ``chmod``, so equality checks against POSIX modes are meaningless
    there; the account ACLs enforce access instead.  A platform without
    ``os.getuid`` cannot compare ownership either, so it is never enforced.
    """
    return _POSIX_METADATA_ENFORCED and getattr(os, "getuid", None) is not None
