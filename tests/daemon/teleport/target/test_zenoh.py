"""The target service over two real ``ZenohTransport`` instances on loopback.

prepare, receive, commit and abort travel as raw protocol-encoded queries; the
stream is sent with the slice 2 ``send_stream``.  The target binds with
``bind_target_service``.  Ports are dynamic and peer discovery is disabled, so
the test never reaches a daily daemon; every query carries a bounded timeout.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from hyprial.daemon.impl.transport import ZenohConfig, ZenohTransport

from ..support import teleport
from .support import (
    AGENT,
    CHUNK,
    NODE,
    PUBLIC_KEY,
    SESSION,
    TARGET_URI,
    key,
    make_service,
    packed_bundle,
    prepare_request,
    protocol,
    receive_request,
    target,
)

QUERY_TIMEOUT = 10.0


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def mesh(tmp_path_factory) -> Iterator[tuple[ZenohTransport, ZenohTransport]]:
    patcher = pytest.MonkeyPatch()
    base = tmp_path_factory.mktemp("teleport-target-mesh")
    patcher.setenv("HYPRIAL_HOME", str(base / "home"))
    patcher.setenv("HARNESS_STATE_DIR", str(base / "state"))
    patcher.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    endpoint = f"tcp/127.0.0.1:{_port()}"
    target_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    source_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    try:
        time.sleep(0.3)
        yield source_transport, target_transport
    finally:
        source_transport.close()
        target_transport.close()
        patcher.undo()


@pytest.fixture
def bound(mesh, tmp_path: Path):
    source, target_transport = mesh
    service, host, receiver = make_service(tmp_path)
    registration = target().bind_target_service(target_transport, service)
    try:
        _wait_until_served(source)
        yield source, service, host, receiver
    finally:
        registration.close()
        service.close()


def _wait_until_served(source: ZenohTransport) -> None:
    p = protocol()
    payload = p.encode_abort_request(p.AbortRequest(operation_id="warmup"))
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if source.query(key("abort", "warmup"), payload, timeout=1.0):
            return
        time.sleep(0.1)
    raise AssertionError("target query handler never became reachable")


def _ask(source: ZenohTransport, op: str, operation_id: str, payload: bytes) -> bytes:
    k = key(op, operation_id)
    samples = source.query(k, payload, timeout=QUERY_TIMEOUT)
    assert len(samples) == 1
    assert samples[0].key == k
    return samples[0].payload


def test_prepare_chunks_receive_commit_over_zenoh(bound, tmp_path: Path) -> None:
    p = protocol()
    source, service, host, receiver = bound
    packed = packed_bundle(tmp_path)
    assert packed.total > CHUNK

    prepared = p.decode_prepare_reply(
        _ask(source, "prepare", "op-z", p.encode_prepare_request(prepare_request("op-z")))
    )
    assert prepared.target_node == NODE
    assert prepared.recipient_public_key == PUBLIC_KEY

    sent = teleport().send_stream(
        source,
        target_node=NODE,
        operation_id="op-z",
        source=packed.path,
        chunk_bytes=CHUNK,
        timeout=QUERY_TIMEOUT,
    )
    assert sent.persisted == packed.total
    assert sent.stream_sha256 == packed.sha256
    assert service.state("op-z") == "staging"

    received = p.decode_receive_reply(
        _ask(
            source,
            "receive",
            "op-z",
            p.encode_receive_request(receive_request("op-z", packed)),
        )
    )
    assert received.agent_uri == TARGET_URI
    assert received.session_ref == SESSION
    assert host.calls == ["land", "activate", "start"]
    assert host.running == {AGENT}

    committed = p.decode_commit_reply(
        _ask(source, "commit", "op-z", p.encode_commit_request(p.CommitRequest(operation_id="op-z")))
    )
    assert committed.routines == ()
    assert service.state("op-z") == "committed"
    assert not (Path(receiver.root) / "op-z").exists()


def test_refusal_travels_as_error_reply_over_zenoh(bound) -> None:
    p = protocol()
    source, service, host, _receiver = bound
    host.existing.add(AGENT)
    payload = _ask(source, "prepare", "op-e", p.encode_prepare_request(prepare_request("op-e")))
    with pytest.raises(teleport().TeleportError) as refused:
        p.decode_prepare_reply(payload)
    assert refused.value.code == "TELEPORT_AGENT_EXISTS"
    assert service.state("op-e") is None
