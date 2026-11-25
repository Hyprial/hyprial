"""The Lark worker's daemon link reaches the daemon through the IPC seam.

The endpoint is a memory-backend marker, so a passing round trip proves the
worker opened no AF_UNIX socket at all -- the transport-blind contract W020
demands (the same shape as a Windows named-pipe platform).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from tests.kernel.ipc.local_ipc.memory_backend import MemoryPipeBackend

from hyprial.daemon.impl.adapters.lark.worker import daemon_link as worker
from hyprial.kernel import listen_local_ipc
from hyprial.kernel.impl.manysys.ipc import local_ipc_backend


class _SeamDaemon:
    """One fake daemon behind the seam: answer each frame by its method."""

    def __init__(self, endpoint: Path, results: dict[str, Any]) -> None:
        self.frames: list[dict[str, Any]] = []
        self._results = results
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
                    "result": self._results[frame["method"]],
                }
                connection.sendall(json.dumps(reply).encode() + b"\n")
            except (OSError, KeyError):
                pass
            finally:
                connection.close()

    def close(self) -> None:
        self._stop.set()
        self._listener.close()
        self._thread.join(timeout=5)


def test_daemon_link_round_trips_over_injected_backend(tmp_path: Path) -> None:
    endpoint = tmp_path / "state" / "daemon.sock"
    with local_ipc_backend(MemoryPipeBackend()):
        daemon = _SeamDaemon(
            endpoint, {"adapter.list": {"adapters": [{"name": "gateway", "agent": "agent:probe:local:target"}]}}
        )
        try:
            routes = worker._Routes(
                gateway_name="gateway",
                socket_path=endpoint,
                report=lambda _event: None,
            )
            target = routes.routed_actor("oc_chat")
        finally:
            daemon.close()
    assert target is not None
    assert target.actor_id == "agent:probe:local:target"
    [frame] = daemon.frames
    assert frame["version"] == 1
    assert frame["method"] == "adapter.list"
    assert frame["params"] == {}
