"""Source-side chunk sender: one ordered stream, one acknowledged chunk at a time.

Decisions:

- The whole stream digest is computed first so every chunk header can carry it.
- Silence (empty reply list: timeout, no queryable, transport reply error) is
  retried at the same offset up to ``max_attempts`` queries per chunk; an
  ``ok:false`` reply is a verdict and is raised without retry.
- The acknowledged ``persisted`` must equal the end of the chunk just sent.  A
  larger value means the receiver claims bytes the sender never sent; a smaller
  one means it lost bytes it acknowledged.  Both stop the transfer.
- An optional monotonic ``deadline`` is checked before each chunk; past it the
  send stops with ``TELEPORT_INCOMPLETE`` so a long stream cannot outlive the
  caller's budget.
- An empty stream is sent as one zero-length chunk so the receiver can bind
  ``total`` and ``streamSha256``.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path

from hyprial.daemon.impl.teleport.errors import (
    TELEPORT_CHUNK_BYTES,
    TELEPORT_ERROR_CODES,
    TeleportError,
)
from hyprial.daemon.impl.teleport.frames import (
    ChunkRequest,
    decode_reply,
    encode_chunk_request,
)
from hyprial.daemon.impl.transport import KeySpace, TransportSession

DEFAULT_QUERY_TIMEOUT = 10.0
_READ_BLOCK = 1 << 20


@dataclass(frozen=True, slots=True)
class SendResult:
    operation_id: str
    total: int
    stream_sha256: str
    persisted: int


def _stream_digest(source: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    with source.open("rb") as handle:
        while block := handle.read(_READ_BLOCK):
            digest.update(block)
            total += len(block)
    return total, digest.hexdigest()


def send_stream(
    session: TransportSession,
    *,
    target_node: str,
    operation_id: str,
    source: str | os.PathLike[str],
    chunk_bytes: int = TELEPORT_CHUNK_BYTES,
    timeout: float = DEFAULT_QUERY_TIMEOUT,
    max_attempts: int = 3,
    key_space: KeySpace | None = None,
    deadline: float | None = None,
) -> SendResult:
    if chunk_bytes < 1 or max_attempts < 1:
        raise ValueError("chunk_bytes and max_attempts must be positive")
    path = Path(source)
    total, stream_sha256 = _stream_digest(path)
    key = (key_space or KeySpace()).agent_teleport(target_node, "chunk", operation_id)
    persisted = 0
    with path.open("rb") as handle:
        while True:
            if deadline is not None and time.monotonic() > deadline:
                raise TeleportError(
                    "TELEPORT_INCOMPLETE",
                    f"the stream was not fully sent within its time window "
                    f"({persisted} of {total} bytes)",
                )
            data = handle.read(chunk_bytes)
            if not data and total > 0:
                break
            request = ChunkRequest(
                operation_id=operation_id,
                offset=persisted,
                total=total,
                size=len(data),
                stream_sha256=stream_sha256,
                chunk_sha256=hashlib.sha256(data).hexdigest(),
                data=data,
            )
            persisted = _send_chunk(session, key, request, timeout, max_attempts)
            if not data or persisted >= total:
                break
    return SendResult(
        operation_id=operation_id,
        total=total,
        stream_sha256=stream_sha256,
        persisted=persisted,
    )


def _send_chunk(
    session: TransportSession,
    key: str,
    request: ChunkRequest,
    timeout: float,
    max_attempts: int,
) -> int:
    payload = encode_chunk_request(request)
    end = request.offset + request.size
    for _ in range(max_attempts):
        samples = session.query(key, payload, timeout=timeout)
        if not samples:
            continue
        frame = decode_reply(samples[0].payload)
        if not frame.header["ok"]:
            error = frame.header["error"]
            code = error["code"]
            if code not in TELEPORT_ERROR_CODES:
                code = "TELEPORT_BAD_FRAME"
            raise TeleportError(code, error["message"])
        persisted = frame.header.get("persisted")
        if type(persisted) is not int:
            raise TeleportError("TELEPORT_BAD_FRAME", "ok chunk reply lacks persisted")
        if persisted != end:
            raise TeleportError(
                "TELEPORT_STREAM_MISMATCH",
                f"receiver acknowledged {persisted} but chunk ends at {end}",
            )
        return persisted
    raise TeleportError(
        "TELEPORT_INCOMPLETE",
        f"no acknowledgement for offset {request.offset} after {max_attempts} attempts",
    )
