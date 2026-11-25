"""The v6 fault intermediary keeps a private stream alive across upstream loss."""

from pathlib import Path
import runpy
import socket
import tempfile
import time

import pytest


ROOT = Path(__file__).resolve().parents[3]
FORWARDER = runpy.run_path(str(ROOT / "contract/e2e/_peer_restart_stream.py"))["RespliceForwarder"]


def wait_for(probe):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("private stream fixture did not reach its milestone")


@pytest.mark.parametrize("mode", ["swallow-close", "blackhole"])
def test_upstream_close_does_not_close_the_downstream_stream(mode):
    with tempfile.TemporaryDirectory(prefix="hr-stream-", dir="/tmp") as raw:
        directory = Path(raw)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(directory / "upstream.sock"))
        server.listen()
        server.settimeout(5)
        forwarder = FORWARDER(f"unixsock-stream/{directory}/upstream.sock",
                              directory=directory, mode=mode)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        accepted = None
        replacement = None
        try:
            forwarder.start()
            client.connect(str(directory / "front.sock"))
            accepted, _ = server.accept()
            accepted.settimeout(5)
            client.sendall(b"before-loss")
            assert accepted.recv(1024) == b"before-loss"
            accepted.close()
            accepted = None
            state = "BLACKHOLE" if mode == "blackhole" else "RESPLICE_SCHEDULED"
            wait_for(lambda: any(row.get("state") == state for row in forwarder.snapshot()))
            client.settimeout(0.1)
            with pytest.raises(TimeoutError):
                client.recv(1024)
            if mode == "swallow-close":
                replacement, _ = server.accept()
                replacement.settimeout(5)
                client.sendall(b"after-resplice")
                assert replacement.recv(1024) == b"after-resplice"
            else:
                client.sendall(b"discarded")
                wait_for(lambda: any(row.get("state") == "ESTABLISHED_DISCARDING"
                                     for row in forwarder.snapshot()))
        finally:
            client.close()
            if accepted is not None:
                accepted.close()
            if replacement is not None:
                replacement.close()
            forwarder.stop()
            server.close()
        assert not (directory / "front.sock").exists()
        assert not (directory / "reverse.sock").exists()


@pytest.mark.parametrize("endpoint", ["tcp/127.0.0.1:7447", "unixsock-stream//tmp/outside.sock"])
def test_forwarder_refuses_non_private_or_out_of_root_upstreams(endpoint):
    with tempfile.TemporaryDirectory(prefix="hr-stream-", dir="/tmp") as raw:
        with pytest.raises(ValueError):
            FORWARDER(endpoint, directory=Path(raw), mode="blackhole")
        assert list(Path(raw).iterdir()) == []


def test_forwarder_refuses_an_accessible_socket_parent():
    with tempfile.TemporaryDirectory(prefix="hr-stream-", dir="/tmp") as raw:
        directory = Path(raw)
        directory.chmod(0o755)
        with pytest.raises(ValueError, match="private owned socket parent"):
            FORWARDER(f"unixsock-stream/{directory}/upstream.sock",
                      directory=directory, mode="blackhole")
        assert list(directory.iterdir()) == []


def test_forwarder_refuses_a_listener_outside_its_owned_root():
    with tempfile.TemporaryDirectory(prefix="hr-stream-", dir="/tmp") as raw:
        directory = Path(raw)
        with pytest.raises(ValueError, match="listener escaped"):
            FORWARDER(f"unixsock-stream/{directory}/upstream.sock", directory=directory,
                      listener_directory=directory.parent, mode="blackhole")
        assert list(directory.iterdir()) == []


def test_second_listener_failure_closes_only_the_first_owned_socket():
    with tempfile.TemporaryDirectory(prefix="hr-stream-", dir="/tmp") as raw:
        directory = Path(raw)
        (directory / "reverse.sock").write_text("already owned elsewhere")
        with pytest.raises(OSError):
            FORWARDER(f"unixsock-stream/{directory}/upstream.sock", directory=directory, mode="blackhole")
        assert not (directory / "front.sock").exists()
        assert (directory / "reverse.sock").read_text() == "already owned elsewhere"


def test_cleanup_can_run_before_worker_start():
    with tempfile.TemporaryDirectory(prefix="hr-stream-", dir="/tmp") as raw:
        directory = Path(raw)
        forwarder = FORWARDER(f"unixsock-stream/{directory}/upstream.sock", directory=directory, mode="blackhole")
        forwarder.stop()
        assert list(directory.iterdir()) == []
