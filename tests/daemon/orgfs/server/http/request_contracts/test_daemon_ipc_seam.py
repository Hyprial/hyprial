"""orgfs daemon calls reach the daemon through the IPC seam.

``daemon_request`` and the nodekey resolver must complete a round trip over
whatever backend the seam selects; the memory backend's marker endpoint
proves no AF_UNIX socket is opened (the Windows named-pipe shape).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from tests.kernel.ipc.local_ipc.memory_backend import MemoryPipeBackend

from hyprial.daemon.impl.orgfs.webserver.ipc import (
    DirectoryNodeKeyResolver,
    daemon_request,
)
from hyprial.kernel import listen_local_ipc
from hyprial.kernel.impl.manysys.ipc import local_ipc_backend

OWNER = "alice@org-a.example"


class _SeamDaemon:
    """One fake daemon behind the seam answering org.network by peer key."""

    def __init__(self, endpoint: Path) -> None:
        self.frames: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._listener = listen_local_ipc(endpoint)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._listener.settimeout(0.1)
        while not self._stop.is_set():
            try:
                connection, _peer = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                connection.settimeout(5.0)
                buffer = bytearray()
                while b"\n" not in buffer:
                    chunk = connection.recv(64 * 1024)
                    if not chunk:
                        break
                    buffer.extend(chunk)
                if b"\n" not in buffer:
                    continue
                frame = json.loads(buffer.partition(b"\n")[0])
                self.frames.append(frame)
                peer_key = frame.get("params", {}).get("peerKey")
                owner = OWNER if peer_key == "nodekey:1" else None
                reply = {
                    "version": 1,
                    "id": frame["id"],
                    "result": {"owner": owner},
                }
                connection.sendall(json.dumps(reply).encode() + b"\n")
            except OSError:
                pass
            finally:
                connection.close()

    def close(self) -> None:
        self._stop.set()
        self._listener.close()
        self._thread.join(timeout=5)


def test_daemon_request_round_trips_over_injected_backend(tmp_path: Path) -> None:
    endpoint = tmp_path / "state" / "daemon.sock"
    with local_ipc_backend(MemoryPipeBackend()):
        daemon = _SeamDaemon(endpoint)
        try:
            result = daemon_request(endpoint, "org.network", {"peerKey": "nodekey:1"})
        finally:
            daemon.close()
    assert result == {"owner": OWNER}
    [frame] = daemon.frames
    assert frame["version"] == 1
    assert frame["method"] == "org.network"
    assert frame["params"] == {"peerKey": "nodekey:1"}


def test_nodekey_resolver_round_trips_over_injected_backend(tmp_path: Path) -> None:
    endpoint = tmp_path / "state" / "daemon.sock"
    with local_ipc_backend(MemoryPipeBackend()):
        daemon = _SeamDaemon(endpoint)
        try:
            resolver = DirectoryNodeKeyResolver(endpoint)
            assert resolver("nodekey:1") == OWNER
            assert resolver("nodekey:missing") is None
        finally:
            daemon.close()
    assert [frame["params"]["peerKey"] for frame in daemon.frames] == [
        "nodekey:1",
        "nodekey:missing",
    ]
