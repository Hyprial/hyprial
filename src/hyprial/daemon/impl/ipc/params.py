"""Shared IPC request parameter decoding helpers."""

from __future__ import annotations

import socket
import struct
import sys
from typing import Any
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonRequestError

JsonObject = dict[str, Any]


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, f"{label} must be a non-empty string"
        )
    return value

def _optional_string_param(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, f"{label} must be a string when present"
        )
    return value or None

def _optional_positive_integer(value: object, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, f"{label} must be a positive integer"
        )
    return value

def _optional_boolean(value: object, label: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, f"{label} must be a boolean")
    return value

def _actor(params: JsonObject) -> str:
    """Accept the MCP identity field while retaining the existing CLI wire."""

    return _required_string(params.get("actor") or params.get("from"), "actor")

def _peer_process_id(connection: socket.socket) -> int | None:
    """Return a Unix peer PID when the platform exposes one, else ``None``."""

    peer_credential = getattr(socket, "SO_PEERCRED", None)
    if peer_credential is not None:
        try:
            raw = connection.getsockopt(socket.SOL_SOCKET, peer_credential, 12)
            pid, _uid, _gid = struct.unpack("3i", raw)
        except (OSError, struct.error):
            return None
        return pid if pid > 0 else None
    if sys.platform == "darwin":
        # Darwin exposes LOCAL_PEERPID in <sys/un.h>, but Python's socket
        # module does not publish the constants.
        try:
            raw = connection.getsockopt(0, 0x002, 4)
            (pid,) = struct.unpack("i", raw)
        except (OSError, struct.error):
            return None
        return pid if pid > 0 else None
    return None
