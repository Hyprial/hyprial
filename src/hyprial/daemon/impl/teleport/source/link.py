# Decision record (file header; no inline comments by repository rule):
# - One ``TransportSession.query`` per attempt, keyed by
#   ``KeySpace.agent_teleport(target, op, operationId)``.  Budgets are the §2
#   table: prepare 10 s x1, chunk 15 s x3 (delegated to ``send_stream``),
#   receive ``strictTimeoutSeconds + 60`` s x1, commit 15 s x2, abort 10 s x1.
# - A query that raises a transport error (timeout, closed session) is the
#   same as silence: the link never learns more than "no answer".
# - A reply with ``ok:false`` is a verdict and surfaces as ``TeleportError``
#   carrying the remote code; an unknown remote code reads as ``BAD_FRAME``.
#   A reply that cannot be decoded is ``BAD_FRAME`` for prepare and commit, and
#   ``UNRESOLVED`` for receive, where the target may already have landed the
#   agent and nothing may be assumed.
# - Silence on prepare is ``WRONG_TARGET`` (no node answers), on receive
#   ``UNRESOLVED`` and on commit ``UNRESOLVED`` after the second attempt; the
#   service decides what each means for the source.  Abort never raises.
"""The source side's five-request client for the teleport protocol."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any, TypeVar

from hyprial.daemon.impl.teleport.errors import (
    TELEPORT_CHUNK_BYTES,
    TELEPORT_ERROR_CODES,
    TeleportError,
)
from hyprial.daemon.impl.teleport.frames import decode_reply
from hyprial.daemon.impl.teleport.protocol import (
    AbortRequest,
    CommitReply,
    CommitRequest,
    PrepareReply,
    PrepareRequest,
    ReceiveReply,
    ReceiveRequest,
    decode_abort_reply,
    decode_commit_reply,
    decode_prepare_reply,
    decode_receive_reply,
    encode_abort_request,
    encode_commit_request,
    encode_prepare_request,
    encode_receive_request,
)
from hyprial.daemon.impl.teleport.sender import SendResult, send_stream
from hyprial.kernel import TELEPORT_BUDGET
from hyprial.daemon.impl.transport import KeySpace, TransportSession


PREPARE_TIMEOUT = TELEPORT_BUDGET.prepare_timeout
CHUNK_TIMEOUT = TELEPORT_BUDGET.chunk_timeout
CHUNK_ATTEMPTS = TELEPORT_BUDGET.chunk_attempts
RECEIVE_GRACE = TELEPORT_BUDGET.receive_grace
COMMIT_TIMEOUT = TELEPORT_BUDGET.commit_timeout
COMMIT_ATTEMPTS = TELEPORT_BUDGET.commit_attempts
ABORT_TIMEOUT = TELEPORT_BUDGET.abort_timeout
STREAM_SECONDS = TELEPORT_BUDGET.stream_seconds

_TRANSPORT_FAILURES = (RuntimeError, TimeoutError, OSError)
Reply = TypeVar("Reply")


class TeleportLink:
    """prepare / send / receive / commit / abort against one target node."""

    def __init__(
        self,
        session: TransportSession,
        *,
        key_space: KeySpace | None = None,
        chunk_bytes: int = TELEPORT_CHUNK_BYTES,
    ) -> None:
        self._session = session
        self._keys = key_space or KeySpace()
        self._chunk_bytes = chunk_bytes

    def prepare(self, target_node: str, request: PrepareRequest) -> PrepareReply:
        payload = self._ask(
            target_node,
            "prepare",
            request.operation_id,
            encode_prepare_request(request),
            PREPARE_TIMEOUT,
            1,
        )
        if payload is None:
            raise TeleportError(
                "TELEPORT_WRONG_TARGET",
                f"node {target_node!r} did not answer the prepare request",
            )
        return _decode("prepare", payload, decode_prepare_reply, "TELEPORT_BAD_FRAME")

    def send(
        self, target_node: str, operation_id: str, path: str | os.PathLike[str]
    ) -> SendResult:
        return send_stream(
            self._session,
            target_node=target_node,
            operation_id=operation_id,
            source=path,
            chunk_bytes=self._chunk_bytes,
            timeout=CHUNK_TIMEOUT,
            max_attempts=CHUNK_ATTEMPTS,
            key_space=self._keys,
            deadline=time.monotonic() + STREAM_SECONDS,
        )

    def receive(self, target_node: str, request: ReceiveRequest) -> ReceiveReply:
        payload = self._ask(
            target_node,
            "receive",
            request.operation_id,
            encode_receive_request(request),
            request.strict_timeout_seconds + RECEIVE_GRACE,
            1,
        )
        if payload is None:
            raise TeleportError(
                "TELEPORT_UNRESOLVED",
                f"node {target_node!r} gave no answer to the receive request",
            )
        return _decode("receive", payload, decode_receive_reply, "TELEPORT_UNRESOLVED")

    def commit(self, target_node: str, request: CommitRequest) -> CommitReply:
        payload = self._ask(
            target_node,
            "commit",
            request.operation_id,
            encode_commit_request(request),
            COMMIT_TIMEOUT,
            COMMIT_ATTEMPTS,
        )
        if payload is None:
            raise TeleportError(
                "TELEPORT_UNRESOLVED",
                f"node {target_node!r} gave no answer to the commit request",
            )
        return _decode("commit", payload, decode_commit_reply, "TELEPORT_BAD_FRAME")

    def abort(self, target_node: str, request: AbortRequest) -> bool:
        payload = self._ask(
            target_node,
            "abort",
            request.operation_id,
            encode_abort_request(request),
            ABORT_TIMEOUT,
            1,
        )
        if payload is None:
            return False
        try:
            _decode("abort", payload, decode_abort_reply, "TELEPORT_BAD_FRAME")
        except TeleportError:
            return False
        return True

    def _ask(
        self,
        target_node: str,
        op: str,
        operation_id: str,
        payload: bytes,
        timeout: float,
        attempts: int,
    ) -> bytes | None:
        key = self._keys.agent_teleport(target_node, op, operation_id)
        for _ in range(attempts):
            try:
                samples = self._session.query(key, payload, timeout=timeout)
            except _TRANSPORT_FAILURES:
                continue
            if samples:
                return samples[0].payload
        return None


def _decode(
    op: str,
    payload: bytes,
    decoder: Callable[[bytes], Reply],
    unreadable: str,
) -> Reply:
    """Return the decoded reply, raise the remote verdict, or ``unreadable``."""

    try:
        frame = decode_reply(payload)
        if frame.header.get("op") != op:
            raise TeleportError("TELEPORT_BAD_FRAME", f"not a {op} reply")
    except Exception as error:
        raise TeleportError(unreadable, f"unreadable {op} reply: {error}") from error
    if frame.header["ok"] is False:
        remote: dict[str, Any] = frame.header["error"]
        code = remote["code"] if remote["code"] in TELEPORT_ERROR_CODES else "TELEPORT_BAD_FRAME"
        raise TeleportError(code, remote["message"])
    try:
        return decoder(payload)
    except Exception as error:
        raise TeleportError(unreadable, f"unreadable {op} reply: {error}") from error

