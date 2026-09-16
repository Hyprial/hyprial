"""Async facade over the daemon's versioned Unix-socket IPC."""

from __future__ import annotations

import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio

from .api import DaemonDisconnected, DaemonRequestRejected
from hyprial.contracts import ipc_errors


class UnixDaemonConnection:
    def __init__(self, socket_path: Path, *, timeout: float = 15.0) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = timeout

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return await anyio.to_thread.run_sync(
            self._request_sync, method, params, request_id
        )

    def _request_sync(
        self, method: str, params: dict[str, Any], request_id: str | None
    ) -> dict[str, Any]:
        frame = {
            "version": 1,
            "id": request_id,
            "method": method,
            "params": params,
        }
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(self.timeout)
        try:
            client.connect(str(self.socket_path))
            client.sendall(json.dumps(frame, separators=(",", ":")).encode() + b"\n")
            response = bytearray()
            while b"\n" not in response:
                chunk = client.recv(64 * 1024)
                if not chunk:
                    raise DaemonDisconnected("daemon closed IPC before responding")
                response.extend(chunk)
        except (OSError, TimeoutError) as error:
            raise DaemonDisconnected(str(error)) from error
        finally:
            client.close()
        try:
            document = json.loads(response.partition(b"\n")[0])
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise DaemonDisconnected("daemon returned invalid JSON") from error
        if not isinstance(document, dict) or document.get("version") != 1:
            raise DaemonDisconnected("daemon returned an invalid IPC envelope")
        error = document.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or ipc_errors.DAEMON_ERROR)
            message = str(error.get("message") or "daemon request failed")
            # PR #332 F4②: transient envelope codes deserialise through the
            # shared registry into the SAME class every other client gets.
            # TransientDaemonError is RuntimeError-based, so the channel
            # poll loop's daemon-contact retry keeps surviving it; the
            # supersede verdict (not transient) stays a
            # DaemonRequestRejected for its dedicated branch.
            transient = ipc_errors.transient_error_from_code(code, message)
            if transient is not None:
                raise transient  # noqa: TRY004
            # A RuntimeError subclass: broad ``except RuntimeError`` handlers keep
            # surviving the envelope, but ``.code`` lets a caller branch on the
            # daemon's verdict (e.g. supersede) without parsing the string.
            raise DaemonRequestRejected(code, message)  # noqa: TRY004
        result = document.get("result")
        if not isinstance(result, dict):
            raise DaemonDisconnected("daemon result must be an object")
        return result


class UnixDaemonConnectionFactory:
    def __init__(self, socket_path: Path, *, timeout: float = 15.0) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = timeout

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[UnixDaemonConnection]:
        yield UnixDaemonConnection(self.socket_path, timeout=self.timeout)
