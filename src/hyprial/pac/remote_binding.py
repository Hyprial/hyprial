"""Read-only remote request guard shared by daemon and native workers."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from time import time_ns
from uuid import uuid4


class RemoteWorkflowUnavailable(RuntimeError):
    """The authority could not be reached; hold work, never infer withdrawal."""


def remote_binding(store, message_id):
    return store._db.execute(
        "SELECT * FROM remote_workflow_requests WHERE message_id=?", (message_id,)
    ).fetchone()


def remote_current(state_dir: Path, bound, *, now_ms=None):
    at = time_ns() // 1_000_000 if now_ms is None else now_ms
    if at > bound["deadline_ms"]:
        return False
    frame = {
        "version": 1,
        "id": uuid4().hex,
        "method": "workflow.remote.current",
        "params": {"messageId": bound["message_id"]},
    }
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(6)
            client.connect(str(state_dir / "daemon.sock"))
            client.sendall(json.dumps(frame).encode() + b"\n")
            buffer = bytearray()
            while b"\n" not in buffer and len(buffer) < 65536:
                chunk = client.recv(65536)
                if not chunk:
                    break
                buffer.extend(chunk)
        response = json.loads(buffer.partition(b"\n")[0])
        if response.get("id") != frame["id"] or "error" in response:
            raise ValueError("authority unavailable")
        current = response["result"]["current"]
        if not isinstance(current, bool):
            raise ValueError("invalid authority response")
        return current
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RemoteWorkflowUnavailable(
            "remote workflow authority unavailable"
        ) from error
