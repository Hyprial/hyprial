"""Read the daemon ``ps`` snapshot over IPC.

R7 deleted the top-level ``ps`` CLI command.  Runners that still need the
full snapshot (``zenoh``, ``adapters``, ``pendingMessages``, ...) read it
through the same public IPC seam ``daemon status`` uses; this keeps them
independent of a CLI-only command without adding one back.
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4


def _request(socket_path: Path, method: str, params: dict[str, Any]) -> dict[str, Any]:
    request_id = uuid4().hex
    frame = {"version": 1, "id": request_id, "method": method, "params": params}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(15.0)
        client.connect(str(socket_path))
        client.sendall(json.dumps(frame, separators=(",", ":")).encode() + b"\n")
        buffer = bytearray()
        while b"\n" not in buffer:
            chunk = client.recv(64 * 1024)
            if not chunk:
                raise AssertionError("daemon disconnected before IPC response")
            buffer.extend(chunk)
    response = json.loads(buffer.partition(b"\n")[0])
    if not isinstance(response, dict) or response.get("id") != request_id:
        raise AssertionError(f"daemon returned an invalid IPC response: {response}")
    failure = response.get("error")
    if isinstance(failure, dict):
        raise AssertionError(f"daemon IPC {method} failed: {failure}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise AssertionError(f"daemon IPC {method} returned no object result")
    return result


def ps_from_socket(socket_path: Path, *, transient_timeout: float = 0.0) -> dict[str, Any]:
    """Return ``{"ok": True, **ps}`` with the retired CLI's fallback shape.

    ``ping`` never rides the restore gate, so it is the readiness seam while
    ``ps`` itself may still refuse; the restore and unavailable fallbacks keep
    the same fields the removed CLI emitted.
    """

    deadline = time.monotonic() + transient_timeout
    while True:
        try:
            probe = _request(socket_path, "ping", {})
        except (AssertionError, OSError):
            if time.monotonic() < deadline:
                time.sleep(0.2)
                continue
            return {"ok": True, "daemon": {"running": False}, "connectors": []}
        if probe.get("restorePending") is not True or time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    if probe.get("restorePending") is True:
        daemon = {
            key: probe[key]
            for key in ("running", "pid", "epoch", "nodeId", "owner", "socket", "phase")
            if key in probe
        }
        return {
            "ok": True,
            "restoring": True,
            "daemon": daemon,
            "restorePending": True,
            "connectors": [],
        }
    try:
        result = _request(socket_path, "ps", {})
    except (AssertionError, OSError):
        return {"ok": True, "daemon": {"running": False}, "connectors": []}
    return {"ok": True, **result}


def ps_from_env(env: Mapping[str, str], *, timeout: float = 30.0) -> dict[str, Any]:
    """Return ``{"ok": True, **ps}`` for the daemon owning ``env``'s state dir."""

    del timeout  # the socket client sets its own bound
    configured = env.get("HARNESS_STATE_DIR")
    if configured:
        state_dir = Path(configured)
    else:
        state_dir = Path(env["HYPRIAL_HOME"]) / "state"
    return ps_from_socket(state_dir / "daemon.sock")
