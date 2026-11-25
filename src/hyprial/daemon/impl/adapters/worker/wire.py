"""Authenticated, bounded IPC frames for supervised adapter workers."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import selectors
import socket
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from hyprial.daemon.impl.adapters.contracts import (
    FrozenJsonMap,
    canonical_json_v1,
    freeze_json_v1,
    frozen_json_digest_v1,
)


MAX_WORKER_FRAME_BYTES = 1024 * 1024
WORKER_TOKEN_BYTES = 32
READY_SCHEMA = "adapter.worker.ready/v1"
CONTROL_SCHEMA = "adapter.worker.control/v1"
_MAC_DOMAIN = b"hyprial-adapter-worker-frame-v1\0"


class WorkerWireError(ValueError):
    """Typed refusal for malformed, unauthenticated, or stale worker IPC."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True, slots=True)
class WorkerIdentity:
    instance_id: str
    kind: str
    generation: int

    def __post_init__(self) -> None:
        for label, value in (("instance_id", self.instance_id), ("kind", self.kind)):
            if not isinstance(value, str) or not value or len(value.encode()) > 4096:
                raise ValueError(f"{label} must be a non-empty bounded string")
        if self.generation < 1:
            raise ValueError("generation must be positive")


@dataclass(frozen=True, slots=True)
class DecodedWorkerFrame:
    """A defensive byte copy plus its immutable FrozenJson body."""

    body: FrozenJsonMap
    raw: bytes

    def as_dict(self) -> dict[str, Any]:
        return json.loads(canonical_json_v1(self.body))


def _token_bytes(token: bytes | bytearray | memoryview) -> bytes:
    copied = bytes(token)
    if len(copied) != WORKER_TOKEN_BYTES:
        raise WorkerWireError(
            "ADAPTER_WORKER_TOKEN_INVALID",
            f"worker token must be exactly {WORKER_TOKEN_BYTES} bytes",
        )
    return copied


def _body_map(value: object) -> FrozenJsonMap:
    try:
        frozen = freeze_json_v1(value)
    except (TypeError, ValueError) as error:
        raise WorkerWireError("ADAPTER_WORKER_FRAME_INVALID", str(error)) from error
    if not isinstance(frozen, FrozenJsonMap):
        raise WorkerWireError("ADAPTER_WORKER_FRAME_INVALID", "frame body must be an object")
    return frozen


def _mac(body: FrozenJsonMap, token: bytes) -> str:
    return hmac.new(token, _MAC_DOMAIN + canonical_json_v1(body), hashlib.sha256).hexdigest()


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise WorkerWireError(
                "ADAPTER_WORKER_FRAME_INVALID", f"duplicate JSON key: {key}"
            )
        result[key] = value
    return result


def _loads(encoded: bytes) -> object:
    try:
        return json.loads(encoded, object_pairs_hook=_pairs)
    except WorkerWireError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise WorkerWireError("ADAPTER_WORKER_FRAME_INVALID", "invalid JSON frame") from error


def build_ready_frame(
    identity: WorkerIdentity,
    *,
    protocol: str,
    build: str,
    capability_digest: str,
) -> dict[str, object]:
    return {
        "schema": READY_SCHEMA,
        "instanceId": identity.instance_id,
        "generation": identity.generation,
        "kind": identity.kind,
        "protocol": protocol,
        "build": build,
        "capabilityDigest": capability_digest,
    }


def build_control_frame(
    identity: WorkerIdentity,
    operation_id: str,
    control_type: str,
    payload: object,
) -> dict[str, object]:
    frozen_payload = freeze_json_v1(payload)
    return {
        "schema": CONTROL_SCHEMA,
        "operationId": operation_id,
        "instanceId": identity.instance_id,
        "generation": identity.generation,
        "kind": identity.kind,
        "type": control_type,
        "payloadDigest": frozen_json_digest_v1(frozen_payload),
        "payload": frozen_payload,
    }


def encode_worker_frame(
    body: Mapping[str, object] | FrozenJsonMap,
    token: bytes | bytearray | memoryview,
) -> bytes:
    """Seal one FrozenJson body and return a length-prefixed byte copy."""

    token_copy = _token_bytes(token)
    frozen = _body_map(body)
    envelope = _body_map({"body": frozen, "mac": _mac(frozen, token_copy)})
    encoded = bytes(canonical_json_v1(envelope))
    if len(encoded) > MAX_WORKER_FRAME_BYTES:
        raise WorkerWireError(
            "ADAPTER_WORKER_PAYLOAD_TOO_LARGE",
            f"worker frame exceeds {MAX_WORKER_FRAME_BYTES} bytes",
        )
    return len(encoded).to_bytes(4, "big") + encoded


def decode_worker_frame(
    frame: bytes | bytearray | memoryview,
    token: bytes | bytearray | memoryview,
) -> DecodedWorkerFrame:
    """Authenticate a complete length-prefixed frame before exposing its body."""

    copied = bytes(frame)
    token_copy = _token_bytes(token)
    if len(copied) < 4:
        raise WorkerWireError("ADAPTER_WORKER_FRAME_INVALID", "truncated frame prefix")
    declared = int.from_bytes(copied[:4], "big")
    if declared > MAX_WORKER_FRAME_BYTES:
        raise WorkerWireError(
            "ADAPTER_WORKER_PAYLOAD_TOO_LARGE",
            f"worker frame exceeds {MAX_WORKER_FRAME_BYTES} bytes",
        )
    if declared != len(copied) - 4:
        raise WorkerWireError("ADAPTER_WORKER_FRAME_INVALID", "frame length mismatch")
    envelope = _loads(copied[4:])
    if not isinstance(envelope, dict) or set(envelope) != {"body", "mac"}:
        raise WorkerWireError("ADAPTER_WORKER_FRAME_INVALID", "invalid frame envelope")
    received_mac = envelope["mac"]
    if not isinstance(received_mac, str) or len(received_mac) != 64:
        raise WorkerWireError("ADAPTER_WORKER_MAC_INVALID", "invalid worker frame MAC")
    body = _body_map(envelope["body"])
    if not hmac.compare_digest(received_mac, _mac(body, token_copy)):
        raise WorkerWireError("ADAPTER_WORKER_MAC_INVALID", "worker frame MAC mismatch")
    return DecodedWorkerFrame(body=body, raw=copied)


def require_worker_identity(
    frame: DecodedWorkerFrame, expected: WorkerIdentity
) -> None:
    fields = frame.as_dict()
    generation = fields.get("generation")
    if generation != expected.generation:
        raise WorkerWireError(
            "ADAPTER_WORKER_GENERATION_STALE",
            f"expected generation {expected.generation}, got {generation}",
        )
    if fields.get("instanceId") != expected.instance_id or fields.get("kind") != expected.kind:
        raise WorkerWireError(
            "ADAPTER_WORKER_INSTANCE_MISMATCH", "worker instance identity mismatch"
        )


def _read_some(endpoint: int | socket.socket, size: int) -> bytes:
    if isinstance(endpoint, socket.socket):
        return endpoint.recv(size)
    return os.read(endpoint, size)


def _wait_readable(endpoint: int | socket.socket, timeout: float | None) -> bool:
    descriptor = endpoint.fileno() if isinstance(endpoint, socket.socket) else endpoint
    with selectors.DefaultSelector() as selector:
        selector.register(descriptor, selectors.EVENT_READ)
        return bool(selector.select(timeout))


def _read_exact(
    endpoint: int | socket.socket, size: int, deadline: float | None
) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        if deadline is not None and (remaining == 0 or not _wait_readable(endpoint, remaining)):
            raise WorkerWireError("ADAPTER_WORKER_TIMEOUT", "worker frame read timed out")
        try:
            chunk = _read_some(endpoint, size - len(chunks))
        except OSError as error:
            raise WorkerWireError("ADAPTER_WORKER_DISCONNECTED", str(error)) from error
        if not chunk:
            raise WorkerWireError("ADAPTER_WORKER_DISCONNECTED", "worker IPC closed")
        chunks.extend(chunk)
    return bytes(chunks)


def recv_worker_frame(
    endpoint: int | socket.socket,
    token: bytes | bytearray | memoryview,
    *,
    timeout: float | None = None,
) -> DecodedWorkerFrame:
    deadline = None if timeout is None else time.monotonic() + timeout
    prefix = _read_exact(endpoint, 4, deadline)
    declared = int.from_bytes(prefix, "big")
    if declared > MAX_WORKER_FRAME_BYTES:
        raise WorkerWireError(
            "ADAPTER_WORKER_PAYLOAD_TOO_LARGE",
            f"worker frame exceeds {MAX_WORKER_FRAME_BYTES} bytes",
        )
    return decode_worker_frame(prefix + _read_exact(endpoint, declared, deadline), token)


def send_worker_frame(
    endpoint: int | socket.socket,
    body: Mapping[str, object] | FrozenJsonMap,
    token: bytes | bytearray | memoryview,
) -> None:
    encoded = encode_worker_frame(body, token)
    if isinstance(endpoint, socket.socket):
        endpoint.sendall(encoded)
        return
    view = memoryview(encoded)
    while view:
        written = os.write(endpoint, view)
        view = view[written:]


def decode_worker_wire(value: Mapping[str, object]) -> dict[str, object]:
    """C0 behavioral seam; real transport uses authenticated byte frames."""

    payload_bytes = value.get("payloadBytes")
    if not isinstance(payload_bytes, int) or payload_bytes > MAX_WORKER_FRAME_BYTES:
        return {
            "ok": False,
            "code": "ADAPTER_WORKER_PAYLOAD_TOO_LARGE",
            "ready": False,
        }
    ready = value.get("type") == "ready" and isinstance(value.get("generation"), int)
    return {"ok": ready, "code": None if ready else "ADAPTER_WORKER_FRAME_INVALID", "ready": ready}


def admit_worker_message(value: Mapping[str, object]) -> dict[str, object]:
    expected = value.get("expectedGeneration")
    actual = value.get("generation")
    if expected != actual:
        return {
            "ok": False,
            "code": "ADAPTER_WORKER_GENERATION_STALE",
            "admissions": 0,
        }
    payload_bytes = value.get("payloadBytes")
    if not isinstance(payload_bytes, int) or payload_bytes > MAX_WORKER_FRAME_BYTES:
        return {
            "ok": False,
            "code": "ADAPTER_WORKER_PAYLOAD_TOO_LARGE",
            "admissions": 0,
        }
    return {"ok": True, "code": None, "admissions": 1}


__all__ = [
    "CONTROL_SCHEMA",
    "DecodedWorkerFrame",
    "MAX_WORKER_FRAME_BYTES",
    "READY_SCHEMA",
    "WORKER_TOKEN_BYTES",
    "WorkerIdentity",
    "WorkerWireError",
    "admit_worker_message",
    "build_control_frame",
    "build_ready_frame",
    "decode_worker_frame",
    "decode_worker_wire",
    "encode_worker_frame",
    "recv_worker_frame",
    "require_worker_identity",
    "send_worker_frame",
]
