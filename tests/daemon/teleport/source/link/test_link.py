"""``TeleportLink``: keys, payloads, per-op timeouts and §2 retry rules.

``TeleportLink(session, *, key_space=None, chunk_bytes=TELEPORT_CHUNK_BYTES)``
wraps a ``TransportSession``; every request names the target node.  Timeouts
and attempts per op (§2): prepare 10 s x1, chunk 15 s x3 at the same offset,
receive ``strictTimeoutSeconds + 60`` s x1 (never retried), commit 15 s x2,
abort 10 s x1 (best effort, never raises).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.teleport import (
    TeleportError,
    decode_chunk_request,
    encode_error_reply,
    encode_frame,
)
from hyprial.daemon.impl.teleport.protocol import (
    AbortReply,
    AbortRequest,
    CommitReply,
    CommitRequest,
    PrepareReply,
    PrepareRequest,
    ReceiveReply,
    ReceiveRequest,
    RoutineOutcome,
    decode_abort_request,
    decode_commit_request,
    decode_prepare_request,
    decode_receive_request,
    encode_abort_reply,
    encode_commit_reply,
    encode_prepare_reply,
    encode_receive_reply,
)
from hyprial.daemon.impl.transport import KeySpace, TransportSample

from ...support import QueryCall, ScriptedSession, auto_ack, sha256_hex, stream_bytes
from ..fakes import source

NODE = "node-b"
OP = "op-link"
KEY = bytes(range(32))


def _key(op: str) -> str:
    return KeySpace().agent_teleport(NODE, op, OP)


def _link(session: ScriptedSession, **kwargs):
    return source().TeleportLink(session, **kwargs)


def _answer(payload: bytes):
    def responder(call: QueryCall) -> list[TransportSample]:
        return [TransportSample(key=call.key, payload=payload)]

    return responder


def _silent(call: QueryCall) -> list[TransportSample]:
    return []


def _refuse(op: str, code: str):
    return _answer(encode_error_reply(op, OP, TeleportError(code, "refused")))


def _prepare_request() -> PrepareRequest:
    return PrepareRequest(
        operation_id=OP,
        source_node="node-a",
        target_node=NODE,
        owner="owner-1",
        agent="w1",
        source_agent_uri="agent://owner-1/node-a/w1",
        harness="claude",
        routines=(),
        dry_run=False,
    )


def _receive_request(strict: float = 60.0) -> ReceiveRequest:
    return ReceiveRequest(
        operation_id=OP,
        manifest_digest="ab" * 32,
        stream_sha256="cd" * 32,
        harness="claude",
        session_ref="sess-1",
        routines=(),
        args=("--x",),
        strict_timeout_seconds=strict,
    )


PREPARE_REPLY = PrepareReply(
    operation_id=OP, target_node=NODE, recipient_public_key=KEY, dry_run=False
)
RECEIVE_REPLY = ReceiveReply(
    operation_id=OP, agent_uri="agent://owner-1/node-b/w1", session_ref="sess-1", routines=()
)
COMMIT_REPLY = CommitReply(
    operation_id=OP, routines=(RoutineOutcome(name="r", enabled=True, resumed=True),)
)


def test_prepare_query_shape_and_reply() -> None:
    session = ScriptedSession(_answer(encode_prepare_reply(PREPARE_REPLY)))
    reply = _link(session).prepare(NODE, _prepare_request())
    assert reply == PREPARE_REPLY
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call.key == _key("prepare")
    assert decode_prepare_request(call.payload) == _prepare_request()
    assert call.timeout == 10.0


def test_prepare_without_reply_is_wrong_target_after_one_attempt() -> None:
    session = ScriptedSession(_silent)
    with pytest.raises(TeleportError) as caught:
        _link(session).prepare(NODE, _prepare_request())
    assert caught.value.code == "TELEPORT_WRONG_TARGET"
    assert len(session.calls) == 1


def test_prepare_refusal_surfaces_the_remote_code() -> None:
    session = ScriptedSession(_refuse("prepare", "TELEPORT_OWNER_MISMATCH"))
    with pytest.raises(TeleportError) as caught:
        _link(session).prepare(NODE, _prepare_request())
    assert caught.value.code == "TELEPORT_OWNER_MISMATCH"
    assert len(session.calls) == 1


def _stream(tmp_path: Path, size: int) -> tuple[Path, bytes]:
    data = stream_bytes(size)
    path = tmp_path / "stream.tar"
    path.write_bytes(data)
    return path, data


def test_send_chunks_with_fifteen_second_queries(tmp_path: Path) -> None:
    path, data = _stream(tmp_path, 700)
    session = ScriptedSession(auto_ack)
    result = _link(session, chunk_bytes=256).send(NODE, OP, path)
    requests = [decode_chunk_request(call.payload) for call in session.calls]
    assert [r.offset for r in requests] == [0, 256, 512]
    assert {call.key for call in session.calls} == {_key("chunk")}
    assert {call.timeout for call in session.calls} == {15.0}
    assert result.total == len(data)
    assert result.stream_sha256 == sha256_hex(data)


def test_send_retries_the_same_offset_three_times_then_incomplete(tmp_path: Path) -> None:
    path, _ = _stream(tmp_path, 100)
    session = ScriptedSession(_silent)
    with pytest.raises(TeleportError) as caught:
        _link(session).send(NODE, OP, path)
    assert caught.value.code == "TELEPORT_INCOMPLETE"
    offsets = [decode_chunk_request(call.payload).offset for call in session.calls]
    assert offsets == [0, 0, 0]


def test_send_recovers_after_a_silent_attempt(tmp_path: Path) -> None:
    path, data = _stream(tmp_path, 100)
    state = {"silent": 2}

    def responder(call: QueryCall) -> list[TransportSample]:
        if state["silent"]:
            state["silent"] -= 1
            return []
        return auto_ack(call)

    session = ScriptedSession(responder)
    assert _link(session).send(NODE, OP, path).persisted == len(data)
    assert len(session.calls) == 3


@pytest.mark.parametrize(("strict", "timeout"), [(60.0, 120.0), (30.0, 90.0)])
def test_receive_waits_strict_timeout_plus_sixty(strict: float, timeout: float) -> None:
    session = ScriptedSession(_answer(encode_receive_reply(RECEIVE_REPLY)))
    reply = _link(session).receive(NODE, _receive_request(strict))
    assert reply == RECEIVE_REPLY
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call.key == _key("receive")
    assert decode_receive_request(call.payload) == _receive_request(strict)
    assert call.timeout == timeout


@pytest.mark.parametrize(
    "payload",
    [
        None,
        b"\x00\x00\x00\x02{}",
        b"not a frame",
        encode_frame({"v": 1, "op": "receive", "operationId": OP, "ok": True}),
        encode_commit_reply(CommitReply(operation_id=OP, routines=())),
    ],
    ids=["no-reply", "bad-header", "garbage", "missing-fields", "wrong-op"],
)
def test_receive_without_a_readable_reply_is_unresolved_and_not_retried(payload) -> None:
    session = ScriptedSession(_silent if payload is None else _answer(payload))
    with pytest.raises(TeleportError) as caught:
        _link(session).receive(NODE, _receive_request())
    assert caught.value.code == "TELEPORT_UNRESOLVED"
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "code",
    ["TELEPORT_STRICT_RESUME_FAILED", "TELEPORT_LANDING_FAILED", "TELEPORT_BAD_FRAME"],
)
def test_receive_refusal_is_a_verdict_with_the_remote_code(code: str) -> None:
    session = ScriptedSession(_refuse("receive", code))
    with pytest.raises(TeleportError) as caught:
        _link(session).receive(NODE, _receive_request())
    assert caught.value.code == code
    assert len(session.calls) == 1


def test_commit_query_shape_and_reply() -> None:
    session = ScriptedSession(_answer(encode_commit_reply(COMMIT_REPLY)))
    assert _link(session).commit(NODE, CommitRequest(OP)) == COMMIT_REPLY
    call = session.calls[0]
    assert call.key == _key("commit")
    assert decode_commit_request(call.payload) == CommitRequest(OP)
    assert call.timeout == 15.0
    assert len(session.calls) == 1


def test_commit_retries_once_after_silence() -> None:
    state = {"silent": 1}

    def responder(call: QueryCall) -> list[TransportSample]:
        if state["silent"]:
            state["silent"] -= 1
            return []
        return [TransportSample(key=call.key, payload=encode_commit_reply(COMMIT_REPLY))]

    session = ScriptedSession(responder)
    assert _link(session).commit(NODE, CommitRequest(OP)) == COMMIT_REPLY
    assert len(session.calls) == 2


def test_commit_gives_up_after_two_silent_attempts() -> None:
    session = ScriptedSession(_silent)
    with pytest.raises(TeleportError):
        _link(session).commit(NODE, CommitRequest(OP))
    assert len(session.calls) == 2


def test_commit_refusal_is_not_retried() -> None:
    session = ScriptedSession(_refuse("commit", "TELEPORT_UNKNOWN_OPERATION"))
    with pytest.raises(TeleportError) as caught:
        _link(session).commit(NODE, CommitRequest(OP))
    assert caught.value.code == "TELEPORT_UNKNOWN_OPERATION"
    assert len(session.calls) == 1


def test_abort_is_one_ten_second_query_carrying_the_reason() -> None:
    session = ScriptedSession(_answer(encode_abort_reply(AbortReply(operation_id=OP))))
    request = AbortRequest(operation_id=OP, reason="send failed")
    assert _link(session).abort(NODE, request) is True
    call = session.calls[0]
    assert call.key == _key("abort")
    assert decode_abort_request(call.payload) == request
    assert call.timeout == 10.0
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "responder",
    [_silent, _refuse("abort", "TELEPORT_OPERATION_CONFLICT"), _answer(b"junk")],
    ids=["silent", "refused", "garbage"],
)
def test_abort_is_best_effort_and_never_raises(responder) -> None:
    session = ScriptedSession(responder)
    assert _link(session).abort(NODE, AbortRequest(operation_id=OP)) is False
    assert len(session.calls) == 1
