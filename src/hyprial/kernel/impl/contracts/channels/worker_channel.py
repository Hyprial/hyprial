"""Narrow IPC surface for one managed worker; no host administration paths."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from hyprial.kernel.impl.contracts.daemon import ipc_errors
from hyprial.kernel.impl.primitives.uri import parse_agent_uri

GUEST_WORKER_STATE = "/run/hyprial-worker"
GUEST_WORKER_SOCKET = GUEST_WORKER_STATE + "/daemon.sock"
MAX_FRAME_BYTES = 8 * 1024 * 1024

# Only the actual managed MCP/Pi tools, not the interactive registration loop.
# targets retains today's visibility; AT09 owns filtering, not this boundary.
METHOD_PARAMS = {
    "identity.whoami": frozenset(),
    "targets": frozenset({"kind"}),
    "message.pending.list": frozenset({"fetched"}),
    "progress.list": frozenset({"deliveryId", "sinceSeq"}),
    "message.send": frozenset({"to", "message"}),
    "message.reply": frozenset({"messageId", "message"}),
    "message.ack": frozenset({"messageId"}),
    "workflow.complete": frozenset(
        {"graphId", "nodeId", "requestId", "reasonRef", "outputText"}
    ),
    "workflow.fail": frozenset(
        {"graphId", "nodeId", "requestId", "reasonRef", "outputText"}
    ),
}


class WorkerChannelError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class WorkerBinding:
    actor: str
    session_ref: str
    daemon_epoch: str

    def __post_init__(self) -> None:
        if not isinstance(self.actor, str) or parse_agent_uri(self.actor) is None:
            raise WorkerChannelError(
                ipc_errors.INVALID_ARGUMENT, "A canonical agent actor is required"
            )
        if any(
            not isinstance(value, str)
            or not value
            or len(value) > 512
            or any(ord(c) < 32 for c in value)
            for value in (self.actor, self.session_ref, self.daemon_epoch)
        ):
            raise WorkerChannelError(
                ipc_errors.INVALID_ARGUMENT, "Invalid worker binding"
            )

    def envelope(self, request: dict[str, Any]) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "sessionRef": self.session_ref,
            "daemonEpoch": self.daemon_epoch,
            "request": request,
        }


def bound_request(
    request: object, binding: WorkerBinding
) -> tuple[str, dict[str, Any]]:
    """Validate on BOTH sides of the relay, then stamp the fixed identity.

    Never forward unknown fields: e.g. resourcePaths on message.send can read
    a host file even though the method itself is appropriate for a worker.
    """
    if (
        not isinstance(request, dict)
        or set(request) - {"version", "id", "method", "params"}
        or type(request.get("version")) is not int
        or request["version"] != 1
    ):
        raise WorkerChannelError(ipc_errors.INVALID_REQUEST, "Invalid worker IPC frame")
    method = request.get("method")
    if not isinstance(method, str) or method not in METHOD_PARAMS:
        raise WorkerChannelError(
            ipc_errors.METHOD_NOT_FOUND, "Method unavailable on the worker channel"
        )
    request_id = request.get("id")
    if request_id is not None and (
        not isinstance(request_id, str) or not 0 < len(request_id) <= 256
    ):
        raise WorkerChannelError(ipc_errors.INVALID_REQUEST, "Invalid request id")
    params = request.get("params", {})
    if not isinstance(params, dict):
        raise WorkerChannelError(
            ipc_errors.INVALID_ARGUMENT, "Invalid worker parameters"
        )
    for key, expected in (
        ("actor", binding.actor),
        ("sessionRef", binding.session_ref),
    ):
        if key in params and params[key] != expected:
            raise WorkerChannelError(
                ipc_errors.CALLER_NOT_AUTHORIZED,
                "Worker identity does not match channel",
            )
    if set(params) - METHOD_PARAMS[method] - {"actor", "sessionRef"}:
        raise WorkerChannelError(
            ipc_errors.INVALID_ARGUMENT, "Parameter unavailable on the worker channel"
        )
    forwarded = {**params, "actor": binding.actor, "sessionRef": binding.session_ref}
    if method == "message.send":
        if request_id is None:
            raise WorkerChannelError(
                ipc_errors.INVALID_REQUEST, "message.send requires a stable request id"
            )
        # JSON tuple avoids delimiter collisions between user-controlled IDs.
        key = json.dumps(
            [binding.actor, binding.session_ref, binding.daemon_epoch, request_id],
            separators=(",", ":"),
        )
        forwarded["idempotencyKey"] = str(uuid5(NAMESPACE_URL, "worker-channel:" + key))
    return method, forwarded
