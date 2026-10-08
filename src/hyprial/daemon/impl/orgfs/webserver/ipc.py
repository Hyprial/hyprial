from __future__ import annotations
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4
import json
import logging
import os
import socket
from hyprial.kernel import ipc_errors


#: Daemon IPC round-trip budget per orgfs call.
DAEMON_IPC_TIMEOUT_SECONDS = 10.0

_DAEMON_IPC_MAX_RESPONSE = 8 * 1024 * 1024

_LOG = logging.getLogger('hyprial.daemon.impl.orgfs.webserver')

#: orgfs typed codes with a dedicated HTTP mapping; ``content-pending`` is
#: 503 + Retry-After and every other code is a 502 carrying the typed code
#: (brief §2).
_ORGFS_ERROR_STATUS = {
    "unknown-doc": 404,
    ipc_errors.ORGFS_CROSS_SPACE_URI: 400,
    ipc_errors.ORGFS_INVALID_URI: 400,
}

_CONTENT_PENDING = ipc_errors.ORGFS_CONTENT_PENDING


# ---------------------------------------------------------------------------
# Daemon IPC (the only orgfs read path)
# ---------------------------------------------------------------------------


class OrgfsIpcError(Exception):
    """One daemon IPC failure: a typed orgfs/IPC code, never a stack trace."""

    def __init__(self, code: str, message: str = "", data: Any = None) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.data = data


def default_daemon_socket() -> Path:
    """The daemon socket for this environment's isolation boundary.

    Same precedence as the CLI's: an explicit state/home root wins over
    ``HARNESS_SOCKET_PATH`` so a test-owned layout can never escape onto the
    production daemon's socket.
    """

    from hyprial.kernel import configured_hyprial_home

    # Same precedence as the CLI's _socket_path: an explicit state/home root is
    # an isolation boundary, so HARNESS_SOCKET_PATH never overrides it.  The
    # home itself comes from the one authority (hyprial.home).
    state_dir = os.environ.get("HARNESS_STATE_DIR")
    if state_dir or "HYPRIAL_HOME" in os.environ:
        root = (
            Path(state_dir).expanduser().resolve()
            if state_dir
            else configured_hyprial_home()[0] / "state"
        )
        return root / "daemon.sock"
    configured = os.environ.get("HARNESS_SOCKET_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    return configured_hyprial_home()[0] / "state" / "daemon.sock"


def daemon_request(
    socket_path: Path,
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    timeout: float = DAEMON_IPC_TIMEOUT_SECONDS,
) -> Any:
    """One version-1 newline-delimited JSON call to the local daemon.

    The same protocol the CLI and MCP use; failures surface as
    :class:`OrgfsIpcError` with the typed code (orgfs codes pass through
    verbatim, transport failures get their own codes).
    """

    request_id = str(uuid4())
    frame: dict[str, Any] = {"version": 1, "id": request_id, "method": method}
    if params is not None:
        frame["params"] = dict(params)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        try:
            client.connect(str(socket_path))
        except OSError as error:
            raise OrgfsIpcError(
                "daemon-unavailable", f"cannot connect to {socket_path}: {error}"
            ) from error
        try:
            client.sendall(json.dumps(frame, separators=(",", ":")).encode() + b"\n")
        except OSError as error:
            raise OrgfsIpcError(
                "daemon-unavailable", f"IPC write failed: {error}"
            ) from error
        buffer = bytearray()
        while len(buffer) <= _DAEMON_IPC_MAX_RESPONSE:
            try:
                chunk = client.recv(64 * 1024)
            except socket.timeout as error:
                raise OrgfsIpcError(
                    "ipc-timeout", f"no {method} answer within {timeout:g}s"
                ) from error
            if not chunk:
                raise OrgfsIpcError("daemon-unavailable", "daemon closed the connection")
            buffer.extend(chunk)
            while b"\n" in buffer:
                line, _, remainder = buffer.partition(b"\n")
                buffer = bytearray(remainder)
                if not line.strip():
                    continue
                try:
                    response = json.loads(line)
                except ValueError as error:
                    raise OrgfsIpcError(
                        "invalid-response", f"invalid daemon IPC response: {error}"
                    ) from error
                if not isinstance(response, dict) or response.get("version") != 1:
                    raise OrgfsIpcError("invalid-response", "unsupported IPC version")
                if response.get("id") != request_id:
                    continue
                failure = response.get("error")
                if isinstance(failure, dict):
                    raise OrgfsIpcError(
                        str(failure.get("code", "daemon-error")),
                        str(failure.get("message", "daemon request failed")),
                        failure.get("data"),
                    )
                if "result" not in response:
                    raise OrgfsIpcError("invalid-response", "missing result")
                return response["result"]
        raise OrgfsIpcError("invalid-response", "daemon IPC response exceeded 8 MiB")
    finally:
        client.close()


class DirectoryNodeKeyResolver:
    """nodekey → owner through the daemon's org directory (``org.network``).

    The standalone web process holds no directory of its own — the daemon
    is the authority — so the PROXY v2 TLV ``0xE0`` nodekey is mapped to an
    owner by asking the daemon for the orgs' device directories and matching
    the recorded public keys.  Fail closed on every failure: a daemon that
    cannot answer yields no identities, never open access.
    """

    def __init__(
        self,
        socket_path: Path | None = None,
        *,
        timeout: float = DAEMON_IPC_TIMEOUT_SECONDS,
    ) -> None:
        self._socket_path = socket_path
        self._timeout = timeout

    def __call__(self, nodekey: str) -> str | None:
        try:
            result = daemon_request(
                self._socket_path or default_daemon_socket(),
                "org.network",
                {},
                timeout=self._timeout,
            )
        except OrgfsIpcError:
            return None
        if not isinstance(result, dict):
            return None
        directory = result.get("directory")
        if not isinstance(directory, dict):
            return None
        for devices in directory.values():
            if not isinstance(devices, list):
                continue
            for device in devices:
                if not isinstance(device, dict):
                    continue
                if nodekey in (device.get("serverPublic"), device.get("clientPublic")):
                    owner = device.get("owner")
                    if isinstance(owner, str) and owner.strip():
                        return owner.strip()
        return None
