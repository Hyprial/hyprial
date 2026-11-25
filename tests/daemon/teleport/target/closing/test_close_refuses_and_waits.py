"""A closing target refuses new work and waits (bounded) for an in-flight receive."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from hyprial.daemon.impl.teleport.frames import decode_frame

from ..support import (
    FakeHost,
    chunks,
    error_code,
    key,
    make_service,
    prepare_request,
    protocol,
    receive_request,
    staged,
)


def _commit(operation_id: str):
    return protocol().CommitRequest(operation_id=operation_id)


def test_closed_service_refuses_new_work_with_a_verdict(tmp_path: Path) -> None:
    service, host, receiver = make_service(tmp_path)
    packed = staged(service, tmp_path, "op-1")
    service.close()
    assert error_code(service.prepare, prepare_request("op-2")) == "TELEPORT_OPERATION_CONFLICT"
    assert error_code(service.chunk, chunks("op-1", packed)[0]) == "TELEPORT_OPERATION_CONFLICT"
    assert (
        error_code(service.receive, receive_request("op-1", packed))
        == "TELEPORT_OPERATION_CONFLICT"
    )
    assert error_code(service.commit, _commit("op-1")) == "TELEPORT_OPERATION_CONFLICT"
    assert host.calls == []
    assert service.state("op-2") is None


def test_closed_service_answers_queries_with_an_error_not_silence(tmp_path: Path) -> None:
    service, host, _receiver = make_service(tmp_path)
    packed = staged(service, tmp_path, "op-1")
    service.close()
    payload = protocol().encode_receive_request(receive_request("op-1", packed))
    replies = service.handle_query(key("receive", "op-1"), payload)
    assert len(replies) == 1
    header = decode_frame(replies[0][1]).header
    assert header["error"]["code"] == "TELEPORT_OPERATION_CONFLICT"
    assert host.calls == []


def test_close_waits_for_the_receive_in_flight(tmp_path: Path) -> None:
    gate = threading.Event()
    host = FakeHost(start_gate=gate)
    service, _host, _receiver = make_service(tmp_path, host)
    packed = staged(service, tmp_path, "op-1")
    request = receive_request("op-1", packed)
    outcome: dict[str, object] = {}
    worker = threading.Thread(target=lambda: outcome.update(reply=service.receive(request)))
    worker.start()
    assert host.start_entered.wait(10.0)
    closer = threading.Thread(target=service.close)
    closer.start()
    closer.join(0.3)
    assert closer.is_alive(), "close returned while a receive was still running"
    assert service.closed is True
    assert error_code(service.prepare, prepare_request("op-2")) == "TELEPORT_OPERATION_CONFLICT"
    gate.set()
    closer.join(10.0)
    worker.join(10.0)
    assert not closer.is_alive()
    assert service.state("op-1") == "received"
    assert "reply" in outcome


def test_close_wait_is_bounded(tmp_path: Path) -> None:
    gate = threading.Event()
    host = FakeHost(start_gate=gate)
    service, _host, _receiver = make_service(tmp_path, host, close_wait_seconds=0.3)
    packed = staged(service, tmp_path, "op-1")
    worker = threading.Thread(
        target=lambda: service.receive(receive_request("op-1", packed))
    )
    worker.start()
    try:
        assert host.start_entered.wait(10.0)
        began = time.monotonic()
        service.close()
        assert time.monotonic() - began < 5.0
        assert service.closed is True
    finally:
        gate.set()
        worker.join(10.0)
