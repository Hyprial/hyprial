"""chunk, commit and abort on the target, one receive at a time, worker threads."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from ..support import teleport
from .support import (
    AGENT,
    FakeHost,
    chunks,
    error_code,
    key,
    make_service,
    packed_bundle,
    prepare_request,
    protocol,
    receive_request,
    staged,
    staging_entries,
    target,
)


def _commit(operation_id: str):
    return protocol().CommitRequest(operation_id=operation_id)


def _abort(operation_id: str, reason: str | None = None):
    return protocol().AbortRequest(operation_id=operation_id, reason=reason)


def _received(tmp_path: Path, host: FakeHost | None = None, operation_id: str = "op-1"):
    service, host, receiver = make_service(tmp_path, host)
    packed = staged(service, tmp_path, operation_id)
    service.receive(receive_request(operation_id, packed))
    return service, host, receiver, packed


def test_chunks_are_delegated_to_staging(tmp_path: Path) -> None:
    service, host, receiver = make_service(tmp_path)
    packed = packed_bundle(tmp_path)
    service.prepare(prepare_request("op-1"))
    requests = chunks("op-1", packed)
    assert len(requests) > 1
    assert service.state("op-1") == "prepared"
    assert service.chunk(requests[0]) == requests[0].size
    assert service.state("op-1") == "staging"
    assert service.chunk(requests[0]) == requests[0].size
    assert receiver.persisted("op-1") == requests[0].size
    assert error_code(service.chunk, requests[2]) == "TELEPORT_OFFSET_GAP"
    for request in requests[1:]:
        service.chunk(request)
    assert receiver.persisted("op-1") == packed.total
    assert host.calls == []


def test_chunk_for_unknown_operation(tmp_path: Path) -> None:
    service, _host, _receiver = make_service(tmp_path)
    first = chunks("op-x", packed_bundle(tmp_path))[0]
    assert error_code(service.chunk, first) == "TELEPORT_UNKNOWN_OPERATION"


def test_chunk_after_receive_is_conflict(tmp_path: Path) -> None:
    service, _host, _receiver, packed = _received(tmp_path)
    first = chunks("op-1", packed)[0]
    assert error_code(service.chunk, first) == "TELEPORT_OPERATION_CONFLICT"
    assert service.state("op-1") == "received"


def test_commit_moves_to_committed_and_removes_staging(tmp_path: Path) -> None:
    service, host, receiver, _packed = _received(tmp_path)
    calls = list(host.calls)
    reply = service.commit(_commit("op-1"))
    assert reply.operation_id == "op-1"
    assert reply.routines == ()
    assert service.state("op-1") == "committed"
    assert staging_entries(receiver) == []
    assert host.calls == calls
    assert host.running == {AGENT}
    assert service.commit(_commit("op-1")) == reply
    assert service.state("op-1") == "committed"


def test_commit_unknown_operation(tmp_path: Path) -> None:
    service, _host, _receiver = make_service(tmp_path)
    assert error_code(service.commit, _commit("op-x")) == "TELEPORT_UNKNOWN_OPERATION"


def test_commit_before_receive_is_conflict(tmp_path: Path) -> None:
    service, _host, receiver = make_service(tmp_path)
    service.prepare(prepare_request("op-1"))
    assert error_code(service.commit, _commit("op-1")) == "TELEPORT_OPERATION_CONFLICT"
    assert service.state("op-1") == "prepared"
    staged(service, tmp_path, "op-2")
    assert error_code(service.commit, _commit("op-2")) == "TELEPORT_OPERATION_CONFLICT"
    assert service.state("op-2") == "staging"
    assert staging_entries(receiver) == ["op-1", "op-2"]


def test_abort_forgets_prepared_and_staging_operations(tmp_path: Path) -> None:
    service, host, receiver = make_service(tmp_path)
    service.prepare(prepare_request("op-1"))
    staged(service, tmp_path, "op-2")
    assert service.abort(_abort("op-1", "source not quiesced")).operation_id == "op-1"
    assert service.abort(_abort("op-2")).operation_id == "op-2"
    assert service.state("op-1") is None
    assert service.state("op-2") is None
    assert staging_entries(receiver) == []
    assert host.calls == []
    service.prepare(prepare_request("op-1"))
    assert service.state("op-1") == "prepared"


def test_abort_of_unknown_operation_is_idempotent(tmp_path: Path) -> None:
    service, _host, _receiver = make_service(tmp_path)
    assert service.abort(_abort("op-x")).operation_id == "op-x"
    assert service.abort(_abort("op-x")).operation_id == "op-x"


def test_abort_after_receive_is_refused_and_keeps_the_agent(tmp_path: Path) -> None:
    service, host, _receiver, _packed = _received(tmp_path)
    calls = list(host.calls)
    assert error_code(service.abort, _abort("op-1")) == "TELEPORT_OPERATION_CONFLICT"
    assert service.state("op-1") == "received"
    assert host.running == {AGENT}
    assert host.calls == calls
    service.commit(_commit("op-1"))
    assert error_code(service.abort, _abort("op-1")) == "TELEPORT_OPERATION_CONFLICT"
    assert service.state("op-1") == "committed"
    assert host.calls == calls


def test_prepare_of_a_received_operation_is_conflict(tmp_path: Path) -> None:
    service, _host, _receiver, _packed = _received(tmp_path)
    assert error_code(service.prepare, prepare_request("op-1")) == "TELEPORT_OPERATION_CONFLICT"
    assert service.state("op-1") == "received"


def test_restarted_service_knows_no_operation(tmp_path: Path) -> None:
    service, _host, receiver, packed = _received(tmp_path)
    receiver.purge()
    fresh_receiver = teleport().StagingReceiver(receiver.root)
    fresh = target().TeleportTargetService(host=FakeHost(), receiver=fresh_receiver)
    assert error_code(fresh.commit, _commit("op-1")) == "TELEPORT_UNKNOWN_OPERATION"
    first = chunks("op-1", packed)[0]
    assert error_code(fresh.chunk, first) == "TELEPORT_UNKNOWN_OPERATION"


def test_only_one_receive_at_a_time(tmp_path: Path) -> None:
    gate = threading.Event()
    host = FakeHost(start_gate=gate)
    service, _host, _receiver = make_service(tmp_path, host)
    first = staged(service, tmp_path, "op-1")
    second = staged(service, tmp_path, "op-2", agent="worker-2")
    outcome: list[object] = []
    worker = threading.Thread(
        target=lambda: outcome.append(service.receive(receive_request("op-1", first))),
        daemon=True,
    )
    worker.start()
    try:
        assert host.start_entered.wait(10.0)
        began = time.monotonic()
        code = error_code(service.receive, receive_request("op-2", second))
        assert code == "TELEPORT_OPERATION_CONFLICT"
        assert time.monotonic() - began < 5.0
        assert service.state("op-2") == "staging"
        assert host.calls.count("land") == 1
    finally:
        gate.set()
        worker.join(10.0)
    assert service.state("op-1") == "received"
    reply = service.receive(receive_request("op-2", second))
    assert reply.operation_id == "op-2"
    assert service.state("op-2") == "received"


def _query_receive(service, operation_id: str, request) -> list:
    payload = protocol().encode_receive_request(request)
    return service.handle_query(key("receive", operation_id), payload)


def test_query_receive_runs_ports_off_the_caller_thread(tmp_path: Path) -> None:
    service, host, _receiver = make_service(tmp_path)
    packed = staged(service, tmp_path)
    replies = _query_receive(service, "op-1", receive_request("op-1", packed))
    assert len(replies) == 1
    reply = protocol().decode_receive_reply(replies[0][1])
    assert reply.operation_id == "op-1"
    caller = threading.get_ident()
    assert {name: ident != caller for name, ident in host.threads.items()} == {
        "land": True,
        "activate": True,
        "start": True,
    }
    service.close()


def test_slow_receive_query_gives_no_reply_then_keeps_the_outcome(tmp_path: Path) -> None:
    gate = threading.Event()
    host = FakeHost(start_gate=gate)
    service, _host, _receiver = make_service(
        tmp_path, host, reply_margin_seconds=0.2, commit_timeout_seconds=0.5
    )
    packed = staged(service, tmp_path)
    request = receive_request("op-1", packed, strict_timeout_seconds=0.3)
    began = time.monotonic()
    try:
        assert _query_receive(service, "op-1", request) == []
        assert time.monotonic() - began < 5.0
    finally:
        gate.set()
    deadline = time.monotonic() + 10.0
    while service.state("op-1") != "received":
        assert time.monotonic() < deadline, "receive never completed on the worker"
        time.sleep(0.02)
    replies = _query_receive(service, "op-1", request)
    assert len(replies) == 1
    assert protocol().decode_receive_reply(replies[0][1]).operation_id == "op-1"
    assert host.calls.count("start") == 1
    service.close()
