"""Query-handler seam between the transport and ``TeleportTargetService``.

Decisions:

- ``handle_query`` never raises for business failures: they are ``ok:false``
  frames keyed by the concrete queried key (Zenoh only delivers a reply keyed
  by that key).  A key that is not a teleport key has nothing to echo and gets
  no reply.
- The key's node must be this node (``WRONG_TARGET``); the header ``op`` and
  ``operationId`` must equal the key's (``BAD_FRAME``).
- receive and commit run on a daemon worker thread, never the query thread.
  The handler waits at most ``strictTimeoutSeconds + reply_margin_seconds``
  (receive) or ``commit_timeout_seconds`` (commit); past that it returns no
  reply while the work finishes and its outcome is kept in the service, so a
  repeated query returns the stored reply.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from typing import TYPE_CHECKING, Any

from hyprial.daemon.impl.teleport.errors import TeleportError
from hyprial.daemon.impl.teleport.frames import (
    decode_chunk_request,
    decode_frame,
    encode_error_reply,
    encode_reply,
)
from hyprial.daemon.impl.teleport.keys import TeleportKey, parse_teleport_key, strip_selector
from hyprial.daemon.impl.teleport.protocol import (
    decode_abort_request,
    decode_commit_request,
    decode_prepare_request,
    decode_receive_request,
    encode_abort_reply,
    encode_commit_reply,
    encode_prepare_reply,
    encode_receive_reply,
)
from hyprial.daemon.impl.transport import KeySpace, Registration, TransportSession

if TYPE_CHECKING:
    from hyprial.daemon.impl.teleport.target.service import TeleportTargetService


class _NoReply(Exception):
    pass


def _in_worker(work: Callable[[], Any], timeout: float) -> Any:
    future: Future[Any] = Future()

    def run() -> None:
        try:
            future.set_result(work())
        except BaseException as error:
            future.set_exception(error)

    threading.Thread(target=run, name="teleport-target-worker", daemon=True).start()
    try:
        return future.result(timeout)
    except FutureTimeout:
        raise _NoReply from None


def handle_query(
    service: "TeleportTargetService", selector: str, payload: bytes | None
) -> list[tuple[str, bytes]]:
    concrete = strip_selector(selector)
    try:
        key = parse_teleport_key(concrete)
    except ValueError:
        return []
    try:
        body = _serve(service, key, payload)
    except _NoReply:
        return []
    except TeleportError as error:
        body = encode_error_reply(key.op, key.operation_id, error)
    return [(concrete, body)]


def _check_id(key: TeleportKey, operation_id: str) -> None:
    if operation_id != key.operation_id:
        raise TeleportError("TELEPORT_BAD_FRAME", "header does not match key")


def _serve(target: "TeleportTargetService", key: TeleportKey, payload: bytes | None) -> bytes:
    if key.target_node != target.node_id:
        raise TeleportError(
            "TELEPORT_WRONG_TARGET", f"node {key.target_node!r} is not {target.node_id!r}"
        )
    if not payload:
        raise TeleportError("TELEPORT_BAD_FRAME", "empty payload")
    if decode_frame(payload).header["op"] != key.op:
        raise TeleportError("TELEPORT_BAD_FRAME", "header does not match key")
    if key.op == "prepare":
        request = decode_prepare_request(payload)
        _check_id(key, request.operation_id)
        return encode_prepare_reply(target.prepare(request))
    if key.op == "chunk":
        chunk = decode_chunk_request(payload)
        _check_id(key, chunk.operation_id)
        return encode_reply("chunk", key.operation_id, {"persisted": target.chunk(chunk)})
    if key.op == "receive":
        receive = decode_receive_request(payload)
        _check_id(key, receive.operation_id)
        timeout = receive.strict_timeout_seconds + target.reply_margin_seconds
        return encode_receive_reply(_in_worker(lambda: target.receive(receive), timeout))
    if key.op == "commit":
        commit = decode_commit_request(payload)
        _check_id(key, commit.operation_id)
        timeout = target.commit_timeout_seconds
        return encode_commit_reply(_in_worker(lambda: target.commit(commit), timeout))
    abort = decode_abort_request(payload)
    _check_id(key, abort.operation_id)
    return encode_abort_reply(target.abort(abort))


def bind_target_service(
    session: TransportSession,
    service: "TeleportTargetService",
    *,
    key_space: KeySpace | None = None,
) -> Registration:
    keys = key_space or service.key_space
    return session.declare_query_handler(
        keys.agent_teleport_all(service.node_id),
        lambda selector, payload: handle_query(service, selector, payload),
    )
