"""Daemon-IPC errors report facts (socket path, errno, method), never advice.

PR #332 F4②: the transport seam now raises the registered transient classes
(DaemonUnavailableError / DaemonDisconnectedError / IpcTimeoutError); these
tests drive the REAL seam (real connect failures, closes and timeouts) and
assert every wire fact unchanged -- the raised type follows the seam.

The CLI cannot tell why a connect failed (never started / mid-restart /
hijacked HYPRIAL_HOME / permissions), so the message must not guess a cause or
prescribe a remedy -- "run hyprial init" aimed at a hijacked state dir once
nearly spawned a second daemon in the production home.  Error CODES stay
machine-stable (they are the transient-classification criteria the E2E
runner relies on); only the human wording changed.
"""

from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root

import socket
import threading
from pathlib import Path

import pytest

from hyprial.kernel.impl.contracts.daemon.ipc_errors import (
    DaemonDisconnectedError,
    DaemonUnavailableError,
    IpcTimeoutError,
)
from hyprial.shell.impl.cli.commands.common import support as _cli_support


@pytest.fixture
def state(tmp_path: Path) -> Path:
    """A short state root (pytest tmp_path exceeds the AF_UNIX path limit
    on this host, which would mask the realistic connect error)."""

    import shutil
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="hyprial-unavail-", dir=hyprial_test_tmp_root()))
    path = root / "state"
    path.mkdir(parents=True)
    yield path
    shutil.rmtree(root, ignore_errors=True)


def _advice_forbidden(message: str) -> None:
    for phrase in ("hyprial init", "retry", "you should", "run '"):
        assert phrase not in message, f"message prescribes a remedy: {phrase!r}"


def test_unavailable_reports_socket_and_errno_without_advice(
    monkeypatch: pytest.MonkeyPatch, state: Path
) -> None:
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("HYPRIAL_HOME", str(state.parent / "home"))
    monkeypatch.delenv("HARNESS_SOCKET_PATH", raising=False)
    with pytest.raises(DaemonUnavailableError) as caught:
        _cli_support._daemon_request("ps", timeout=0.5)
    error = caught.value
    assert error.code == "DAEMON_UNAVAILABLE"
    message = str(error)
    # The facts are the message: which socket, which OS-level cause.
    assert str(state / "daemon.sock") in message
    assert "Errno 2" in message  # ENOENT: no such socket
    _advice_forbidden(message)


def test_unavailable_shows_a_hijacked_socket_path_as_is(
    monkeypatch: pytest.MonkeyPatch, state: Path
) -> None:
    """HARNESS_STATE_DIR pointing at another installation's state (the real
    squire-test-daemon incident): the message shows the hijacked path and
    offers no remedy -- it must never say 'hyprial init' here."""

    (state / "daemon.lock").touch()  # markers of the foreign installation
    (state / "daemon.json").write_text('{"pid": 17428}')
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("HYPRIAL_HOME", str(state.parent / "home"))
    monkeypatch.delenv("HARNESS_SOCKET_PATH", raising=False)
    with pytest.raises(DaemonUnavailableError) as caught:
        _cli_support._daemon_request("ps", timeout=0.5)
    error = caught.value
    assert error.code == "DAEMON_UNAVAILABLE"
    message = str(error)
    assert str(state / "daemon.sock") in message
    _advice_forbidden(message)


class _SocketTheatre:
    """A fake daemon socket: accepts, then either closes or stays silent."""

    def __init__(self, state: Path, *, mode: str) -> None:
        self.path = state / "daemon.sock"
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        self.server.listen(4)
        # Accepted connections are tracked explicitly: on Linux, closing the
        # listening socket does NOT wake an accept() blocked in another
        # thread, so the accept loop's frame keeps the last silent connection
        # (and its fd) alive past teardown (card #77; macOS wakes the accept
        # and never showed this).
        self._connections: list[socket.socket] = []
        self.thread = threading.Thread(target=self._run, args=(mode,), daemon=True)
        self.thread.start()

    def _run(self, mode: str) -> None:
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            self._connections.append(connection)
            if mode == "close":
                connection.close()
            elif mode == "close-after-read":
                # The recv-path close: read the request, then hang up.
                connection.settimeout(2.0)
                try:
                    connection.recv(64 * 1024)
                except OSError:
                    pass
                connection.close()
            # "silent": keep the connection open and never answer.
            # Loop back to accept the next probe.

    def close(self) -> None:
        self.server.close()
        for connection in self._connections:
            connection.close()


@pytest.mark.parametrize("mode", ["close", "close-after-read"])
def test_disconnected_reports_facts_without_advice(
    monkeypatch: pytest.MonkeyPatch, state: Path, mode: str
) -> None:
    theatre = _SocketTheatre(state, mode=mode)
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.delenv("HARNESS_SOCKET_PATH", raising=False)
    try:
        with pytest.raises(DaemonDisconnectedError) as caught:
            _cli_support._daemon_request("ps", timeout=2.0)
    finally:
        theatre.close()
    error = caught.value
    assert error.code == "DAEMON_DISCONNECTED"
    message = str(error)
    assert str(state / "daemon.sock") in message
    assert "connection closed" in message  # kept stable for e2e-010's matcher
    _advice_forbidden(message)


def test_ipc_timeout_reports_method_socket_and_budget(
    monkeypatch: pytest.MonkeyPatch, state: Path
) -> None:
    theatre = _SocketTheatre(state, mode="silent")
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.delenv("HARNESS_SOCKET_PATH", raising=False)
    try:
        with pytest.raises(IpcTimeoutError) as caught:
            _cli_support._daemon_request("ps", timeout=0.3)
    finally:
        theatre.close()
    error = caught.value
    assert error.code == "IPC_TIMEOUT"
    message = str(error)
    assert "0.3s" in message
    assert str(state / "daemon.sock") in message
    assert "'ps'" in message
    _advice_forbidden(message)
