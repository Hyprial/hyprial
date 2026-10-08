"""Shared daemon authorization for caller-named local-file reads and writes.

Every daemon surface that reads or writes a caller-named path goes through
this package, so the rule lives in one place.  Route attachments and OrgFS
import use :func:`read_authorized_file`; OrgFS export uses the descriptor-safe
write twin in :mod:`hyprial.daemon.impl.path_authz.write`.
"""

from __future__ import annotations

from hyprial.daemon.impl.path_authz.policy import (
    AuthorizedFile,
    PathPolicy,
    PathRefused,
    REASON_HYPRIAL_HOME,
    REASON_OTHER_AGENT_HOME,
    REASON_OUTSIDE_ALLOWED_ROOTS,
    REASON_CHANGED,
    REASON_EMPTY,
    REASON_NOT_FOUND,
    REASON_HARDLINK,
    REASON_NOT_REGULAR,
    REASON_SYMLINK,
    REASON_TOO_LARGE,
    REASON_UNREADABLE,
    REASON_UNVERIFIED,
    operator_policy,
    read_authorized_file,
    session_attachment_policy,
    session_policy,
)

__all__ = [
    "AuthorizedFile",
    "PathPolicy",
    "PathRefused",
    "REASON_CHANGED",
    "REASON_EMPTY",
    "REASON_HYPRIAL_HOME",
    "REASON_NOT_FOUND",
    "REASON_HARDLINK",
    "REASON_NOT_REGULAR",
    "REASON_OTHER_AGENT_HOME",
    "REASON_OUTSIDE_ALLOWED_ROOTS",
    "REASON_SYMLINK",
    "REASON_TOO_LARGE",
    "REASON_UNREADABLE",
    "REASON_UNVERIFIED",
    "operator_policy",
    "read_authorized_file",
    "session_attachment_policy",
    "session_policy",
]
