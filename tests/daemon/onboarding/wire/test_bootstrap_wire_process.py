"""The bootstrap wire host as a real process: reachable, stoppable, lock-releasing.

Decision: daemon-first-z1-process-tests-20261115.  The process is launched by the
test through ``wire_process_entry.py`` in an isolated root (own ``HOME``, no
owner override, loopback-only network) and never touches an ambient home,
service unit or production daemon.
"""

from __future__ import annotations

import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hyprial.daemon import BootstrapWireHost
from hyprial.kernel import daemon_request_once, ipc_errors

from tests.support.runtime.environment import isolated_environment
from wire_support import BUSY, OwnerlessHome

ENTRY = Path(__file__).with_name("wire_process_entry.py")


def _launch(root: OwnerlessHome) -> subprocess.Popen:
    environment = isolated_environment(root.root)
    environment.pop("HYPRIAL_OWNER", None)
    environment["HYPRIAL_HOME"] = str(root.home)
    environment["HARNESS_STATE_DIR"] = str(root.state)
    process = subprocess.Popen(
        [sys.executable, str(ENTRY), str(root.home), str(root.state)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    ready, _, _ = select.select([process.stdout], [], [], 60)
    line = process.stdout.readline() if ready else ""
    if line.strip() != "READY":
        process.kill()
        _, error = process.communicate(timeout=10)
        pytest.fail(f"entry did not become ready: {line!r} {error}")
    return process


def _finish(process: subprocess.Popen) -> tuple[int, str]:
    try:
        output, _ = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    return process.returncode, output


@pytest.fixture
def root(monkeypatch):
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    home = OwnerlessHome()
    try:
        yield home
    finally:
        home.cleanup()


def test_the_process_answers_over_its_socket_and_stops_on_shutdown(root):
    process = _launch(root)
    try:
        ping = daemon_request_once("ping", socket_path=root.socket, timeout=10)
        assert ping["phase"] == "bootstrap"
        assert ping["pid"] == process.pid
        with pytest.raises(Exception) as refused:
            daemon_request_once("agent.list", {}, socket_path=root.socket, timeout=10)
        assert getattr(refused.value, "code", None) == "ACCOUNT_REQUIRED"
        with pytest.raises(Exception) as unknown:
            daemon_request_once("no.such.method", {}, socket_path=root.socket, timeout=10)
        assert getattr(unknown.value, "code", None) == ipc_errors.METHOD_NOT_FOUND
        stopped = daemon_request_once("shutdown", socket_path=root.socket, timeout=10)
        assert stopped["pid"] == process.pid
    finally:
        code, output = _finish(process)
    assert code == 0
    assert "OUTCOME stop" in output
    assert not root.socket.exists()
    assert not (root.state / "bootstrap.json").exists()
    reopened = BootstrapWireHost.open(root.home, state_dir=root.state)
    reopened.close()


def test_sigterm_stops_the_process_and_frees_the_home(root):
    process = _launch(root)
    try:
        with pytest.raises(BUSY):
            BootstrapWireHost.open(root.home, state_dir=root.state)
        process.send_signal(signal.SIGTERM)
    finally:
        code, output = _finish(process)
    assert code == 0
    assert "OUTCOME stop" in output
    deadline = time.monotonic() + 10
    while root.socket.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not root.socket.exists()
    again = BootstrapWireHost.open(root.home, state_dir=root.state)
    again.close()
