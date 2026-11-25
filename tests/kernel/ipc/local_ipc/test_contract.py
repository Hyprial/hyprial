"""The local IPC seam contract, run against two backends.

The same cases run over the real AF_UNIX backend and over the injected
in-memory pipe backend, so the wrapper's rules -- endpoint lifecycle, frame
round trip, timeouts, the bounded response write, peer identity, path
limits -- hold for a transport that has no AF_UNIX at all.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.support.runtime._tmp import hyprial_test_tmp_root

from hyprial.kernel import (
    LocalIpcListener,
    LocalIpcPeer,
    LocalIpcStream,
    connect_local_ipc,
    listen_local_ipc,
    local_ipc_path_limit,
)
from hyprial.kernel.impl.manysys.ipc import local_ipc_backend

from .memory_backend import MemoryPipeBackend

_UNIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="the AF_UNIX backend is POSIX only"
)
_UNIX_PARAM = pytest.param("unix", marks=[_UNIX_ONLY])


@pytest.fixture(params=[_UNIX_PARAM, "memory-pipe"])
def local_backend(request: pytest.FixtureRequest) -> Iterator[str]:
    if request.param == "memory-pipe":
        with local_ipc_backend(MemoryPipeBackend()):
            yield request.param
    else:
        yield request.param


@pytest.fixture
def ipc_root() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="hyprial-lipc-", dir=hyprial_test_tmp_root()))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@dataclass
class _Pair:
    listener: LocalIpcListener
    stream: LocalIpcStream
    peer: LocalIpcPeer
    client: LocalIpcStream


def connect_pair(endpoint: Path) -> _Pair:
    """One listener, one accepted stream and the client that connected."""
    listener = listen_local_ipc(endpoint)
    box: dict[str, object] = {}
    ready = threading.Event()

    def serve() -> None:
        listener.settimeout(5.0)
        stream, peer = listener.accept()
        box["stream"] = stream
        box["peer"] = peer
        ready.set()

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        client = connect_local_ipc(endpoint, 5.0)
        assert ready.wait(5.0), "accept did not settle after connect"
    finally:
        worker.join(5.0)
    assert not worker.is_alive()
    return _Pair(
        listener=listener,
        stream=box["stream"],  # type: ignore[arg-type]
        peer=box["peer"],  # type: ignore[arg-type]
        client=client,
    )


def read_frame(stream: LocalIpcStream) -> dict:
    buffer = bytearray()
    while b"\n" not in buffer:
        chunk = stream.recv(65536)
        if not chunk:
            raise AssertionError("the peer closed before a full frame")
        buffer.extend(chunk)
    return json.loads(buffer.partition(b"\n")[0])  # type: ignore[no-any-return]


def close_pair(pair: _Pair) -> None:
    pair.stream.close()
    pair.client.close()
    pair.listener.close()


def test_one_frame_round_trips_in_both_directions(local_backend: str, ipc_root: Path) -> None:
    pair = connect_pair(ipc_root / "daemon.sock")
    try:
        with pair.stream, pair.client:
            pair.client.sendall(b'{"version":1,"id":"r1","method":"ping"}\n')
            assert read_frame(pair.stream) == {
                "version": 1,
                "id": "r1",
                "method": "ping",
            }
            reply = b'{"version":1,"id":"r1","result":{"pong":true}}\n'
            assert pair.stream.send_response(
                reply, timeout=2.0, poll_interval=0.05, should_stop=lambda: False
            ) is True
            assert read_frame(pair.client)["result"] == {"pong": True}
    finally:
        close_pair(pair)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are the contract")
def test_listener_hardens_directory_and_endpoint(local_backend: str, ipc_root: Path) -> None:
    endpoint = ipc_root / "state" / "inner" / "daemon.sock"
    listener = listen_local_ipc(endpoint)
    try:
        assert stat.S_IMODE(endpoint.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(endpoint.stat().st_mode) == 0o600
    finally:
        listener.close()


def test_a_stale_endpoint_file_is_replaced(local_backend: str, ipc_root: Path) -> None:
    endpoint = ipc_root / "daemon.sock"
    endpoint.write_text("stale endpoint from a dead daemon", encoding="utf-8")
    pair = connect_pair(endpoint)
    try:
        pair.client.sendall(b'{"version":1,"id":"fresh"}\n')
        assert read_frame(pair.stream)["id"] == "fresh"
    finally:
        close_pair(pair)


def test_accept_times_out(local_backend: str, ipc_root: Path) -> None:
    listener = listen_local_ipc(ipc_root / "daemon.sock")
    try:
        listener.settimeout(0.05)
        with pytest.raises(TimeoutError):
            listener.accept()
    finally:
        listener.close()


def test_read_times_out(local_backend: str, ipc_root: Path) -> None:
    pair = connect_pair(ipc_root / "daemon.sock")
    try:
        pair.stream.settimeout(0.05)
        with pytest.raises(TimeoutError):
            pair.stream.recv(4096)
    finally:
        close_pair(pair)


def test_response_write_deadline_expires_without_a_reader(local_backend: str, ipc_root: Path) -> None:
    pair = connect_pair(ipc_root / "daemon.sock")
    try:
        frame = b"x" * (4 * 1024 * 1024)
        started = time.monotonic()
        with pytest.raises(
            TimeoutError, match="daemon IPC response write deadline expired"
        ):
            pair.stream.send_response(
                frame, timeout=0.25, poll_interval=0.05, should_stop=lambda: False
            )
        elapsed = time.monotonic() - started
        assert 0.2 <= elapsed < 3.0
    finally:
        close_pair(pair)


def test_a_zero_byte_write_is_a_broken_pipe_with_the_original_text() -> None:
    class ZeroSendTransport:
        def settimeout(self, _timeout: float | None) -> None:
            pass

        def send(self, _data: bytes) -> int:
            return 0

    with pytest.raises(
        BrokenPipeError, match="daemon IPC response wrote zero bytes"
    ):
        LocalIpcStream(ZeroSendTransport()).send_response(
            b"x\n", timeout=1.0, poll_interval=0.05, should_stop=lambda: False
        )


def test_response_write_stops_before_any_byte(local_backend: str, ipc_root: Path) -> None:
    pair = connect_pair(ipc_root / "daemon.sock")
    try:
        assert pair.stream.send_response(
            b"x" * 1024, timeout=2.0, poll_interval=0.05, should_stop=lambda: True
        ) is False
        pair.client.settimeout(0.1)
        with pytest.raises(TimeoutError):
            pair.client.recv(64)
    finally:
        close_pair(pair)


def test_peer_disconnected_tracks_the_live_peer_then_the_hangup(
    local_backend: str, ipc_root: Path
) -> None:
    """The wrapper's hangup probe on a real accepted stream.

    The daemon's existing hangup tests feed raw sockets to the server's
    fallback; this exercises LocalIpcStream.peer_disconnected itself: a live
    peer reports connected, a closed client reports hung up on the socket
    backend, and a pipe-like transport without an OS-level peek stays False
    (the Windows named-pipe contract).
    """
    pair = connect_pair(ipc_root / "daemon.sock")
    try:
        assert pair.stream.peer_disconnected() is False
        pair.client.close()
        if local_backend == "unix":
            deadline = time.monotonic() + 2.0
            while not pair.stream.peer_disconnected():
                assert time.monotonic() < deadline, "hangup never became visible"
                time.sleep(0.01)
        else:
            assert pair.stream.peer_disconnected() is False
    finally:
        close_pair(pair)


def test_peer_identity_fields(local_backend: str, ipc_root: Path) -> None:
    pair = connect_pair(ipc_root / "daemon.sock")
    try:
        if local_backend == "unix":
            assert pair.peer.pid == os.getpid()
            assert pair.peer.uid == os.geteuid()
            assert pair.peer.uid_unreadable is False
            assert pair.peer.same_user_enforced is False
        else:
            assert pair.peer == LocalIpcPeer(pid=None, uid=None, same_user_enforced=True)
    finally:
        close_pair(pair)


def test_connect_to_an_absent_endpoint_fails(local_backend: str, ipc_root: Path) -> None:
    with pytest.raises(OSError):
        connect_local_ipc(ipc_root / "missing.sock", 0.5)


def test_connect_to_an_invalid_endpoint_file_fails(local_backend: str, ipc_root: Path) -> None:
    endpoint = ipc_root / "junk.sock"
    endpoint.write_text("not an endpoint document", encoding="utf-8")
    with pytest.raises(OSError):
        connect_local_ipc(endpoint, 0.5)


def test_endpoint_paths_beyond_the_transport_limit(local_backend: str, ipc_root: Path) -> None:
    limit = local_ipc_path_limit()
    long_name = "n" * 200
    if local_backend == "unix":
        assert isinstance(limit, int)
        assert limit >= 100
        assert len(os.fsencode(str(ipc_root))) + 1 + len(long_name.encode()) > limit
        with pytest.raises(OSError):
            listen_local_ipc(ipc_root / long_name)
    else:
        assert limit is None
        listener = listen_local_ipc(ipc_root / long_name)
        listener.close()
