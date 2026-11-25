"""``daemon_socket_absent`` connects without sending a frame."""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

from hyprial.kernel import daemon_socket_absent
from tests.support.runtime._tmp import hyprial_test_tmp_root

pytestmark = pytest.mark.skipif(os.name == "nt", reason="AF_UNIX listener fixtures are POSIX only")


@pytest.fixture
def path():
    root = Path(tempfile.mkdtemp(prefix="sa", dir=hyprial_test_tmp_root()))
    try:
        yield root / "s.sock"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _bind(path: Path) -> socket.socket:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(4)
    return server


def test_a_missing_socket_file_is_absent(path):
    assert daemon_socket_absent(path) is True


def test_a_stale_socket_file_is_absent(path):
    _bind(path).close()
    assert path.exists()
    assert daemon_socket_absent(path) is True


def test_a_listening_socket_is_present_and_no_bytes_are_sent(path):
    server = _bind(path)
    try:
        assert daemon_socket_absent(path) is False
        connection, _ = server.accept()
        connection.settimeout(1.0)
        assert connection.recv(16) == b""
        connection.close()
    finally:
        server.close()


def test_an_unconnectable_path_is_not_reported_absent(path):
    path.parent.joinpath("file").write_text("x")
    assert daemon_socket_absent(path.parent / "file" / "s.sock") is False
