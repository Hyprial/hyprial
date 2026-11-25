"""Contract of the socketserver base the seam lends POSIX-only servers.

The orgfs web server terminates TLS on a unix-socket listener built from
``socketserver``; that base class is a raw local-IPC capability, so it must
come from the seam. POSIX gets a working stream-server base, Windows a clear
``OSError``. The seam export lands with the implementation commit, so each
test imports it at call time: before that commit these fail, not skip.
"""

from __future__ import annotations

import os
import shutil
import socket
import socketserver
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.support.runtime._tmp import hyprial_test_tmp_root

from hyprial.kernel import connect_local_ipc


@pytest.fixture
def tmp_root() -> Iterator[Path]:
    directory = Path(
        tempfile.mkdtemp(prefix="hyprial-lipcsv-", dir=hyprial_test_tmp_root())
    )
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _base() -> type[socketserver.BaseServer]:
    from hyprial.kernel import local_ipc_stream_server_base

    return local_ipc_stream_server_base()


@pytest.mark.skipif(os.name == "nt", reason="POSIX unix stream server")
def test_the_seam_base_serves_socketserver_stream_requests(
    tmp_root: Path,
) -> None:
    base = _base()
    assert issubclass(base, socketserver.BaseServer)

    class _Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.settimeout(5.0)
            received = bytearray()
            while b"\n" not in received:
                chunk = self.request.recv(64)
                if not chunk:
                    break
                received.extend(chunk)
            assert bytes(received) == b"ping\n"
            self.request.sendall(b"pong\n")

    class _Server(socketserver.ThreadingMixIn, base):
        daemon_threads = True

    endpoint = tmp_root / "stream-server.sock"
    server = _Server(str(endpoint), _Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        with connect_local_ipc(endpoint, 5.0) as client:
            client.sendall(b"ping\n")
            assert client.recv(64) == b"pong\n"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX unix stream server")
def test_the_seam_base_is_the_unix_stream_family() -> None:
    assert _base().address_family == socket.AF_UNIX


def test_a_windows_call_gets_a_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(OSError, match="not supported"):
        _base()
