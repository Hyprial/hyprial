"""The real Windows named-pipe backend behind the local IPC seam.

These cases need a real Windows machine: the default backend there is the
current-user named pipe (DACL-restricted, no OS peer credentials), and the
endpoint file keeps its ``windows-pipe-v1`` document format.
"""

from __future__ import annotations

import json
import sys

import pytest

from hyprial.kernel import (
    LocalIpcPeer,
    listen_local_ipc,
    local_ipc_path_limit,
)

from .test_contract import close_pair, connect_pair, read_frame

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="the real named-pipe backend is Windows only"
)


def test_one_frame_round_trips_over_the_named_pipe(tmp_path) -> None:
    pair = connect_pair(tmp_path / "daemon.sock")
    try:
        pair.client.sendall(b'{"version":1,"id":"w1","method":"ping"}\n')
        assert read_frame(pair.stream)["id"] == "w1"
        reply = b'{"version":1,"id":"w1","result":{"ok":true}}\n'
        assert pair.stream.send_response(
            reply, timeout=2.0, poll_interval=0.05, should_stop=lambda: False
        ) is True
        assert read_frame(pair.client)["result"] == {"ok": True}
    finally:
        close_pair(pair)


def test_the_endpoint_file_keeps_the_pipe_document_format(tmp_path) -> None:
    listener = listen_local_ipc(tmp_path / "daemon.sock")
    try:
        document = json.loads((tmp_path / "daemon.sock").read_text(encoding="utf-8"))
        assert document["transport"] == "windows-pipe-v1"
        assert isinstance(document["pipe"], str)
    finally:
        listener.close()


def test_the_pipe_peer_has_no_credentials_but_enforces_same_user(tmp_path) -> None:
    pair = connect_pair(tmp_path / "daemon.sock")
    try:
        assert pair.peer == LocalIpcPeer(pid=None, uid=None, same_user_enforced=True)
    finally:
        close_pair(pair)


def test_the_pipe_backend_has_no_path_limit() -> None:
    assert local_ipc_path_limit() is None
