"""The Codex app-server WebSocket client connects through the local IPC seam.

The peer is a third-party process (the Codex app-server), so this side only
connects. A recording backend proves the connection is made by the seam's
entry point; the memory backend proves a transport without unix sockets
surfaces as a plain ``OSError`` that callers already treat as a failed
connection, never as an ``AttributeError``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses.codex.transport import open_app_server_connection
from hyprial.kernel.impl.manysys.ipc import local_ipc_backend
from tests.kernel.ipc.local_ipc.memory_backend import MemoryPipeBackend
from tests.kernel.ipc.local_ipc.recording_backend import RecordingBackend

from .test_server_requests import _RecordingAppServer


def test_the_websocket_client_connects_through_the_seam(socket_root: Path) -> None:
    server = _RecordingAppServer(socket_root / "app.sock")
    server.start()
    recorder = RecordingBackend()
    connection = None
    try:
        with local_ipc_backend(recorder):
            connection = open_app_server_connection(server.socket_path, timeout=5.0)
        assert recorder.connects == [server.socket_path]
        assert connection.request("transport-probe", {}, 1).get("result") == {
            "pong": True
        }
    finally:
        if connection is not None:
            connection.close()
        server.stop()


def test_a_transport_without_unix_sockets_fails_with_a_plain_os_error(
    socket_root: Path,
) -> None:
    endpoint = socket_root / "junk.sock"
    endpoint.write_text("not an endpoint document", encoding="utf-8")
    with local_ipc_backend(MemoryPipeBackend()):
        with pytest.raises(OSError):
            open_app_server_connection(endpoint, timeout=1.0)
