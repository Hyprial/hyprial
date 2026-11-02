"""Kernel-owned daemon IPC client contracts."""

from hyprial.kernel.impl.ipc.client import (
    daemon_request,
    daemon_request_once,
)
from hyprial.kernel.impl.ipc.endpoint import daemon_socket_path

__all__ = ["daemon_request", "daemon_request_once", "daemon_socket_path"]
