"""The remote-authority guard reaches the daemon through the IPC seam.

``remote_current`` must answer its liveness question over whatever backend
the seam selects; the memory backend's marker endpoint proves no AF_UNIX
socket is opened (the Windows named-pipe shape).
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from tests.kernel.ipc.local_ipc.memory_backend import MemoryPipeBackend

from hyprial.daemon.impl.pac.contracts import bindings
from hyprial.kernel import listen_local_ipc
from hyprial.kernel.impl.manysys.ipc import local_ipc_backend


class _AuthorityDaemon:
    """One fake authority behind the seam answering workflow.remote.current."""

    def __init__(self, endpoint: Path) -> None:
        self.frames: list[dict] = []
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
                reply = {
                    "version": 1,
                    "id": frame["id"],
                    "result": {"current": True},
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


def test_remote_current_round_trips_over_injected_backend(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    with local_ipc_backend(MemoryPipeBackend()):
        daemon = _AuthorityDaemon(state_dir / "daemon.sock")
        try:
            bound = {
                "message_id": "workflow-1",
                "deadline_ms": (time.time_ns() // 1_000_000) + 60_000,
            }
            assert bindings.remote_current(state_dir, bound) is True
        finally:
            daemon.close()
    [frame] = daemon.frames
    assert frame["method"] == "workflow.remote.current"
    assert frame["params"] == {"messageId": "workflow-1"}
