"""Slice 2 over two real ``ZenohTransport`` instances on loopback.

A fake session would accept replies keyed by anything; Zenoh only delivers a
reply keyed by the concrete queried key, so the wire is proven here once.
Kept to a single module-scoped mesh and a small chunk size so the whole file
stays fast; every query carries a bounded timeout.

Decisions pinned here:

- The target binds with ``bind_staging_receiver(transport, receiver,
  local_node=...)``; ``prepare`` is called in-process because the prepare op
  is not served by the slice-2 binding.
- Ports are allocated dynamically and peer discovery is disabled, so the test
  never reaches a daily daemon.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from hyprial.daemon.impl.transport import KeySpace, ZenohConfig, ZenohTransport

from .support import chunk_requests, sha256_hex, stream_bytes, teleport

TARGET = "node-target"
SMALL = 4096
QUERY_TIMEOUT = 5.0


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def mesh(tmp_path_factory) -> Iterator[tuple[ZenohTransport, ZenohTransport]]:
    patcher = pytest.MonkeyPatch()
    base = tmp_path_factory.mktemp("teleport-mesh")
    patcher.setenv("HYPRIAL_HOME", str(base / "home"))
    patcher.setenv("HARNESS_STATE_DIR", str(base / "state"))
    patcher.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    endpoint = f"tcp/127.0.0.1:{_port()}"
    target = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    source = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    try:
        time.sleep(0.3)
        yield source, target
    finally:
        source.close()
        target.close()
        patcher.undo()


@pytest.fixture
def bound(mesh, tmp_path: Path):
    module = teleport()
    source, target = mesh
    receiver = module.StagingReceiver(module.staging_root(tmp_path / "target-home"))
    receiver.purge()
    registration = module.bind_staging_receiver(target, receiver, local_node=TARGET)
    try:
        _wait_until_served(source)
        yield source, receiver
    finally:
        registration.close()


def _wait_until_served(source: ZenohTransport) -> None:
    module = teleport()
    key = KeySpace().agent_teleport(TARGET, "abort", "warmup")
    frame = module.encode_frame({"v": 1, "op": "abort", "operationId": "warmup"})
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if source.query(key, frame, timeout=1.0):
            return
        time.sleep(0.1)
    raise AssertionError("target query handler never became reachable")


def _reply(source: ZenohTransport, key: str, payload: bytes) -> dict:
    samples = source.query(key, payload, timeout=QUERY_TIMEOUT)
    assert len(samples) == 1
    assert samples[0].key == key
    return teleport().decode_reply(samples[0].payload).header


def test_multi_chunk_stream_arrives_byte_identical(bound, tmp_path: Path) -> None:
    module = teleport()
    source, receiver = bound
    data = stream_bytes(SMALL * 3 + 123)
    export = tmp_path / "bundle.tar"
    export.write_bytes(data)
    receiver.prepare("op-multi")
    result = module.send_stream(
        source,
        target_node=TARGET,
        operation_id="op-multi",
        source=export,
        chunk_bytes=SMALL,
        timeout=QUERY_TIMEOUT,
    )
    assert result.persisted == len(data)
    assert result.total == len(data)
    path = receiver.finalize("op-multi", stream_sha256=sha256_hex(data))
    assert path.read_bytes() == data


def test_query_for_other_target_gets_no_reply(bound) -> None:
    module = teleport()
    source, receiver = bound
    receiver.prepare("op-elsewhere")
    request = chunk_requests(stream_bytes(SMALL), "op-elsewhere", chunk_bytes=SMALL)[0]
    key = KeySpace().agent_teleport("node-other", "chunk", "op-elsewhere")
    assert source.query(key, module.encode_chunk_request(request), timeout=1.0) == []
    assert receiver.persisted("op-elsewhere") == 0


def test_duplicate_chunk_over_wire_is_idempotent(bound) -> None:
    module = teleport()
    source, receiver = bound
    receiver.prepare("op-dup")
    data = stream_bytes(SMALL * 2)
    first, second = chunk_requests(data, "op-dup", chunk_bytes=SMALL)
    key = KeySpace().agent_teleport(TARGET, "chunk", "op-dup")
    assert _reply(source, key, module.encode_chunk_request(first))["persisted"] == SMALL
    assert _reply(source, key, module.encode_chunk_request(first))["persisted"] == SMALL
    assert _reply(source, key, module.encode_chunk_request(second))["persisted"] == SMALL * 2
    assert receiver.finalize("op-dup", stream_sha256=sha256_hex(data)).read_bytes() == data


def test_interrupted_stream_then_abort_removes_staging(bound) -> None:
    module = teleport()
    source, receiver = bound
    directory = receiver.prepare("op-abort")
    data = stream_bytes(SMALL * 3)
    requests = chunk_requests(data, "op-abort", chunk_bytes=SMALL)
    chunk_key = KeySpace().agent_teleport(TARGET, "chunk", "op-abort")
    assert _reply(source, chunk_key, module.encode_chunk_request(requests[0]))["ok"] is True
    abort_key = KeySpace().agent_teleport(TARGET, "abort", "op-abort")
    abort_frame = module.encode_frame({"v": 1, "op": "abort", "operationId": "op-abort"})
    assert _reply(source, abort_key, abort_frame)["ok"] is True
    assert not directory.exists()
    assert _reply(source, abort_key, abort_frame)["ok"] is True
    late = _reply(source, chunk_key, module.encode_chunk_request(requests[1]))
    assert late["ok"] is False
    assert late["error"]["code"] == "TELEPORT_UNKNOWN_OPERATION"
