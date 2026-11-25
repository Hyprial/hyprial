"""Slice 2 source-side chunk sender against a scripted ``TransportSession``.

Decisions pinned here:

- ``send_stream(session, *, target_node, operation_id, source, chunk_bytes=
  TELEPORT_CHUNK_BYTES, timeout=..., max_attempts=3, key_space=None)`` reads
  the exported stream from the file ``source`` and returns ``SendResult``
  (``operation_id``, ``total``, ``stream_sha256``, ``persisted``).
- Every chunk is one ``session.query`` on ``KeySpace.agent_teleport(target,
  "chunk", operationId)`` carrying ``encode_chunk_request`` bytes and the
  configured ``timeout``; the next chunk is sent only after an ``ok`` reply.
- An empty reply list (timeout, no queryable, transport reply error) is retried
  at the same offset; ``max_attempts`` counts every query for that chunk.
  Exhaustion raises ``TeleportError`` with a frozen ``TELEPORT_*`` code.
- An ``ok:false`` reply is not retried; its code is raised as is.
- ``persisted`` greater than the end of the chunk just sent is a protocol
  error (``TeleportError``) and stops the transfer immediately.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.transport import KeySpace, TransportSample

from .support import (
    FROZEN_ERROR_CODES,
    QueryCall,
    ScriptedSession,
    ack_reply,
    auto_ack,
    sha256_hex,
    stream_bytes,
    teleport,
)

SMALL = 1000


def _source(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "export" / "bundle.tar"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _send(session, source: Path, **kwargs):
    options = {
        "target_node": "node-b",
        "operation_id": "op-1",
        "source": source,
        "chunk_bytes": SMALL,
        "timeout": 0.5,
    }
    options.update(kwargs)
    return teleport().send_stream(session, **options)


def _decoded(session: ScriptedSession) -> list:
    module = teleport()
    return [module.decode_chunk_request(call.payload) for call in session.calls]


def test_sends_every_chunk_in_order_and_returns_summary(tmp_path: Path) -> None:
    data = stream_bytes(SMALL * 3 + 250)
    session = ScriptedSession(auto_ack)
    result = _send(session, _source(tmp_path, data))
    requests = _decoded(session)
    assert [r.offset for r in requests] == [0, SMALL, SMALL * 2, SMALL * 3]
    assert [r.size for r in requests] == [SMALL, SMALL, SMALL, 250]
    assert b"".join(r.data for r in requests) == data
    for request in requests:
        assert request.operation_id == "op-1"
        assert request.total == len(data)
        assert request.stream_sha256 == sha256_hex(data)
        assert request.chunk_sha256 == sha256_hex(request.data)
    expected_key = KeySpace().agent_teleport("node-b", "chunk", "op-1")
    assert {call.key for call in session.calls} == {expected_key}
    assert {call.timeout for call in session.calls} == {0.5}
    assert result.operation_id == "op-1"
    assert result.total == len(data)
    assert result.stream_sha256 == sha256_hex(data)
    assert result.persisted == len(data)


def test_default_chunk_size_is_wire_constant(tmp_path: Path) -> None:
    data = stream_bytes(768_000 + 10)
    session = ScriptedSession(auto_ack)
    teleport().send_stream(
        session,
        target_node="node-b",
        operation_id="op-1",
        source=_source(tmp_path, data),
        timeout=0.5,
    )
    assert [r.size for r in _decoded(session)] == [768_000, 10]


def test_waits_for_ack_before_next_chunk(tmp_path: Path) -> None:
    data = stream_bytes(SMALL * 3)
    seen_offsets: list[int] = []

    def responder(call: QueryCall) -> list[TransportSample]:
        request = teleport().decode_chunk_request(call.payload)
        assert request.offset == (seen_offsets[-1] + SMALL if seen_offsets else 0)
        seen_offsets.append(request.offset)
        return ack_reply(call)

    _send(ScriptedSession(responder), _source(tmp_path, data))
    assert seen_offsets == [0, SMALL, SMALL * 2]


def test_retries_same_offset_after_timeout(tmp_path: Path) -> None:
    data = stream_bytes(SMALL * 2)
    silent = {"remaining": 2}

    def responder(call: QueryCall) -> list[TransportSample]:
        request = teleport().decode_chunk_request(call.payload)
        if request.offset == SMALL and silent["remaining"]:
            silent["remaining"] -= 1
            return []
        return ack_reply(call)

    session = ScriptedSession(responder)
    result = _send(session, _source(tmp_path, data), max_attempts=3)
    assert [r.offset for r in _decoded(session)] == [0, SMALL, SMALL, SMALL]
    assert result.persisted == len(data)


def test_duplicate_ack_after_lost_reply_is_accepted(tmp_path: Path) -> None:
    data = stream_bytes(SMALL * 2)
    lost = {"done": False}

    def responder(call: QueryCall) -> list[TransportSample]:
        request = teleport().decode_chunk_request(call.payload)
        if request.offset == 0 and not lost["done"]:
            lost["done"] = True
            return []
        return ack_reply(call)

    session = ScriptedSession(responder)
    assert _send(session, _source(tmp_path, data)).persisted == len(data)


def test_gives_up_after_bounded_attempts(tmp_path: Path) -> None:
    module = teleport()
    data = stream_bytes(SMALL * 2)

    def responder(call: QueryCall) -> list[TransportSample]:
        request = module.decode_chunk_request(call.payload)
        return ack_reply(call) if request.offset == 0 else []

    session = ScriptedSession(responder)
    with pytest.raises(module.TeleportError) as caught:
        _send(session, _source(tmp_path, data), max_attempts=4)
    assert caught.value.code in FROZEN_ERROR_CODES
    assert [r.offset for r in _decoded(session)] == [0, SMALL, SMALL, SMALL, SMALL]


def test_remote_error_reply_is_raised_without_retry(tmp_path: Path) -> None:
    module = teleport()
    data = stream_bytes(SMALL * 2)

    def responder(call: QueryCall) -> list[TransportSample]:
        error = module.TeleportError("TELEPORT_UNKNOWN_OPERATION", "no such op")
        body = module.encode_error_reply("chunk", "op-1", error)
        return [TransportSample(key=call.key, payload=body)]

    session = ScriptedSession(responder)
    with pytest.raises(module.TeleportError) as caught:
        _send(session, _source(tmp_path, data))
    assert caught.value.code == "TELEPORT_UNKNOWN_OPERATION"
    assert len(session.calls) == 1


def test_persisted_running_ahead_is_protocol_error(tmp_path: Path) -> None:
    module = teleport()
    data = stream_bytes(SMALL * 3)

    def responder(call: QueryCall) -> list[TransportSample]:
        request = module.decode_chunk_request(call.payload)
        return ack_reply(call, persisted=request.offset + request.size + 1)

    session = ScriptedSession(responder)
    with pytest.raises(module.TeleportError) as caught:
        _send(session, _source(tmp_path, data))
    assert caught.value.code in FROZEN_ERROR_CODES
    assert len(session.calls) == 1


def test_malformed_reply_is_bad_frame(tmp_path: Path) -> None:
    module = teleport()
    data = stream_bytes(SMALL)

    def responder(call: QueryCall) -> list[TransportSample]:
        return [TransportSample(key=call.key, payload=b"\x00\x00")]

    with pytest.raises(module.TeleportError) as caught:
        _send(ScriptedSession(responder), _source(tmp_path, data))
    assert caught.value.code == "TELEPORT_BAD_FRAME"


def test_expired_deadline_stops_before_the_next_chunk(tmp_path: Path) -> None:
    session = ScriptedSession(auto_ack)
    with pytest.raises(teleport().TeleportError) as caught:
        _send(session, _source(tmp_path, stream_bytes(SMALL * 3)), deadline=0.0)
    assert caught.value.code == "TELEPORT_INCOMPLETE"
    assert session.calls == []
