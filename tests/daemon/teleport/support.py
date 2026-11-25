"""Shared helpers for the agent-teleport slice 2 tests.

Decisions recorded here (see docs/design/agent-teleport-acceptance.md, slice 2):

- Every test imports the product surface lazily through ``teleport()`` so a
  missing package fails each test on its own with ``ModuleNotFoundError``
  instead of failing collection for the whole directory.
- The public surface consumed by tests is the package
  ``hyprial.daemon.impl.teleport`` itself; the implementer may split it into
  private submodules freely as long as the package re-exports these names.
- ``ScriptedSession`` is a ``TransportSession``-shaped fake whose ``query``
  replies are produced by a scripted responder, so sender behaviour (waiting,
  retry, protocol errors) is pinned without Zenoh timing.
- ``FROZEN_ERROR_CODES`` holds the 20 codes frozen after the 2026-11-12
  ruling in docs/design/agent-teleport-protocol.md §8, which added
  ``LANDING_FAILED`` and ``SOURCE_NOT_QUIESCED`` to the original 18.
"""

from __future__ import annotations

import hashlib
import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from types import ModuleType

from hyprial.daemon.impl.transport import TransportSample

PACKAGE = "hyprial.daemon.impl.teleport"
CHUNK = 768_000

FROZEN_ERROR_CODES = frozenset(
    "TELEPORT_" + name
    for name in (
        "BAD_FRAME",
        "WRONG_TARGET",
        "UNKNOWN_OPERATION",
        "OPERATION_CONFLICT",
        "OFFSET_GAP",
        "CHUNK_TOO_LARGE",
        "CHUNK_DIGEST_MISMATCH",
        "STREAM_MISMATCH",
        "INCOMPLETE",
        "DIGEST_MISMATCH",
        "AGENT_EXISTS",
        "ROUTINE_CONFLICT",
        "RUNTIME_UNSUPPORTED",
        "OWNER_MISMATCH",
        "STRICT_RESUME_FAILED",
        "IN_FLIGHT_TIMEOUT",
        "SOURCE_CLEANUP_FAILED",
        "UNRESOLVED",
        "LANDING_FAILED",
        "SOURCE_NOT_QUIESCED",
    )
)


def teleport() -> ModuleType:
    """Import the slice-2 library package on demand."""

    return importlib.import_module(PACKAGE)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stream_bytes(size: int, *, seed: int = 7) -> bytes:
    """A deterministic, non-repeating-looking byte stream of ``size`` bytes."""

    out = bytearray()
    counter = 0
    while len(out) < size:
        out.extend(hashlib.sha256(f"{seed}:{counter}".encode()).digest())
        counter += 1
    return bytes(out[:size])


def chunk_requests(
    data: bytes,
    operation_id: str,
    *,
    chunk_bytes: int,
    stream_sha256: str | None = None,
    total: int | None = None,
) -> list:
    """Split ``data`` into ``ChunkRequest`` objects exactly as the wire expects."""

    module = teleport()
    declared = sha256_hex(data) if stream_sha256 is None else stream_sha256
    declared_total = len(data) if total is None else total
    requests = []
    for offset in range(0, len(data), chunk_bytes):
        piece = data[offset : offset + chunk_bytes]
        requests.append(
            module.ChunkRequest(
                operation_id=operation_id,
                offset=offset,
                total=declared_total,
                size=len(piece),
                stream_sha256=declared,
                chunk_sha256=sha256_hex(piece),
                data=piece,
            )
        )
    return requests


@dataclass
class QueryCall:
    key: str
    payload: bytes
    timeout: float


Responder = Callable[[QueryCall], list[TransportSample]]


@dataclass
class ScriptedSession:
    """Fake ``TransportSession`` whose ``query`` is answered by ``responder``."""

    responder: Responder
    calls: list[QueryCall] = field(default_factory=list)

    def query(
        self,
        key_expr: str,
        payload: bytes,
        *,
        timeout: float = 3.0,
        errors: list[str] | None = None,
        all_replies: bool = False,
    ) -> list[TransportSample]:
        call = QueryCall(key=key_expr, payload=payload, timeout=timeout)
        self.calls.append(call)
        return self.responder(call)


def ack_reply(call: QueryCall, *, persisted: int | None = None) -> list[TransportSample]:
    """Answer a chunk query with ``ok:true`` and the given (or natural) ``persisted``."""

    module = teleport()
    request = module.decode_chunk_request(call.payload)
    value = request.offset + request.size if persisted is None else persisted
    body = module.encode_reply(
        "chunk", request.operation_id, {"persisted": value}
    )
    return [TransportSample(key=call.key, payload=body)]


def auto_ack(call: QueryCall) -> list[TransportSample]:
    return ack_reply(call)
