"""Query-handler seam between the transport and ``StagingReceiver``.

Decisions:

- ``handle_teleport_query`` is pure with respect to the transport: it returns
  ``(key, payload)`` replies and never raises for business failures, which are
  ``ok:false`` frames keyed by the concrete queried key (Zenoh only delivers a
  reply keyed by that key).
- Header ``op`` and ``operationId`` must equal the key's; a frame that cannot
  be trusted is answered using the key's own ``op``/``operationId``.
- Only ``chunk`` and ``abort`` are served here.  prepare, receive and commit
  payloads are defined by later slices; until then they are answered with
  ``TELEPORT_BAD_FRAME`` rather than silently ignored.
- A key that is not a teleport key has no op or operationId to echo, so it gets
  no reply.
"""

from __future__ import annotations

from hyprial.daemon.impl.teleport.errors import TeleportError
from hyprial.daemon.impl.teleport.frames import (
    decode_chunk_request,
    decode_frame,
    encode_error_reply,
    encode_reply,
)
from hyprial.daemon.impl.teleport.keys import (
    TeleportKey,
    parse_teleport_key,
    strip_selector,
)
from hyprial.daemon.impl.teleport.staging import StagingReceiver
from hyprial.daemon.impl.transport import KeySpace, Registration, TransportSession


def handle_teleport_query(
    receiver: StagingReceiver,
    selector: str,
    payload: bytes | None,
    *,
    local_node: str,
    key_space: KeySpace | None = None,
) -> list[tuple[str, bytes]]:
    concrete = strip_selector(selector)
    try:
        key = parse_teleport_key(concrete)
    except ValueError:
        return []
    try:
        body = _serve(receiver, key, payload, local_node)
    except TeleportError as error:
        body = encode_error_reply(key.op, key.operation_id, error)
    return [(concrete, body)]


def _serve(
    receiver: StagingReceiver, key: TeleportKey, payload: bytes | None, local_node: str
) -> bytes:
    if key.target_node != local_node:
        raise TeleportError(
            "TELEPORT_WRONG_TARGET", f"node {key.target_node!r} is not {local_node!r}"
        )
    if key.op not in ("chunk", "abort"):
        raise TeleportError("TELEPORT_BAD_FRAME", f"op {key.op!r} is not served yet")
    if not payload:
        raise TeleportError("TELEPORT_BAD_FRAME", "empty payload")
    header = decode_frame(payload).header
    if header["op"] != key.op or header["operationId"] != key.operation_id:
        raise TeleportError("TELEPORT_BAD_FRAME", "header does not match key")
    if key.op == "abort":
        receiver.abort(key.operation_id)
        return encode_reply("abort", key.operation_id)
    persisted = receiver.accept_chunk(decode_chunk_request(payload))
    return encode_reply("chunk", key.operation_id, {"persisted": persisted})


def bind_staging_receiver(
    session: TransportSession,
    receiver: StagingReceiver,
    *,
    local_node: str,
    key_space: KeySpace | None = None,
) -> Registration:
    keys = key_space or KeySpace()

    def handler(selector: str, payload: bytes | None) -> list[tuple[str, bytes]]:
        return handle_teleport_query(
            receiver, selector, payload, local_node=local_node, key_space=keys
        )

    return session.declare_query_handler(keys.agent_teleport_all(local_node), handler)
