"""PR #332 F4①: CLI commands wait out a DAEMON_RESTORING refusal.

While the daemon's restore gate is closed, heavy IPC methods are refused
at dispatch with ``DAEMON_RESTORING`` BEFORE any side effect, so a refused
mutation never executed and waiting the refusal out is safe.  F4①'s CLI
half: ``_daemon_request`` retries that code inside a bounded budget
(``HYPRIAL_DAEMON_RESTORE_WAIT_SECONDS``, default 120s, 0 disables) instead of
handing the user an immediate ``ok:false`` hard error.

The daemon double below speaks the real version-1 newline-delimited
protocol and emits the exact envelope shapes the daemon's ``_response()``
produces (``{"version": 1, "id": ..., "error": {"code", "message"}}``),
so the stub cannot drift from the wire shape.
"""

from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root

import json
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from hyprial.kernel import ipc_errors
from hyprial.kernel.impl.contracts.daemon.ipc_errors import DaemonRestoringError
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


class RestoringDaemon:
    """One-shot daemon IPC double with a closable restore gate.

    Answers every request with the DAEMON_RESTORING envelope for the first
    ``refusals`` requests, then with a message.send result -- the dispatch
    gate's refusal followed by the served answer once restore completes.
    """

    def __init__(self, socket_path: Path, *, refusals: int, code: str | None = None):
        self.socket_path = socket_path
        self.refusals = refusals
        self.code = code or ipc_errors.DAEMON_RESTORING
        self.served = 0
        self._stop = threading.Event()
        self._stopped = threading.Event()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(socket_path))
        self._server.listen(8)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            while not self._stop.is_set():
                self._server.settimeout(0.2)
                try:
                    client, _ = self._server.accept()
                except socket.timeout:
                    continue
                with client:
                    client.settimeout(5.0)
                    buffer = bytearray()
                    while b"\n" not in buffer:
                        chunk = client.recv(64 * 1024)
                        if not chunk:
                            break
                        buffer.extend(chunk)
                    if not buffer:
                        continue
                    self.served += 1
                    frame = json.loads(buffer.partition(b"\n")[0])
                    if self.served <= self.refusals:
                        response = {
                            "version": 1,
                            "id": frame.get("id"),
                            "error": {
                                "code": self.code,
                                "message": "daemon is restoring its connectors",
                            },
                        }
                    else:
                        response = {
                            "version": 1,
                            "id": frame.get("id"),
                            "result": {"ok": True, "messageId": "m_restored"},
                        }
                    client.sendall(
                        json.dumps(response, separators=(",", ":")).encode() + b"\n"
                    )
        finally:
            self._stopped.set()

    def close(self) -> None:
        self._stop.set()
        self._stopped.wait(2.0)
        self._server.close()


@pytest.fixture
def root() -> Path:
    """A short AF_UNIX root (pytest tmp_path can exceed the path limit)."""

    path = Path(tempfile.mkdtemp(prefix="hyprial-restore-", dir=hyprial_test_tmp_root()))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _install(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    restore_wait: str | None = None,
    **kwargs,
) -> RestoringDaemon:
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HYPRIAL_HOME", str(root / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.delenv("HYPRIAL_DAEMON_RESTORE_WAIT_SECONDS", raising=False)
    if restore_wait is not None:
        monkeypatch.setenv("HYPRIAL_DAEMON_RESTORE_WAIT_SECONDS", restore_wait)
    daemon = RestoringDaemon(state / "daemon.sock", **kwargs)
    return daemon


def test_daemon_request_waits_out_the_restore_gate(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = _install(root, monkeypatch, refusals=2)
    try:
        result = _cli_support._daemon_request("message.send", {"from": "a", "to": ["b"]})
        assert result == {"ok": True, "messageId": "m_restored"}
        # Two refusals were waited out; the third attempt was served.
        assert daemon.served == 3
    finally:
        daemon.close()


def test_daemon_request_restore_wait_is_bounded(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = _install(root, monkeypatch, refusals=10**9, restore_wait="0.4")
    try:
        started = time.monotonic()
        with pytest.raises(DaemonRestoringError) as caught:
            _cli_support._daemon_request("message.send", {"from": "a", "to": ["b"]})
        elapsed = time.monotonic() - started
        # PR #332 F4②: a waited-out refusal surfaces as the REGISTERED class
        # (was a CliError before the transient hierarchy); the wire code it
        # carries is unchanged.
        assert type(caught.value) is DaemonRestoringError
        assert caught.value.code == ipc_errors.DAEMON_RESTORING
        # It retried within the budget instead of failing on the first
        # refusal, then surfaced the refusal once the budget ran out.
        assert daemon.served >= 2
        assert elapsed >= 0.4
    finally:
        daemon.close()


def test_daemon_request_restore_wait_zero_disables_waiting(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = _install(root, monkeypatch, refusals=10**9, restore_wait="0")
    try:
        with pytest.raises(DaemonRestoringError) as caught:
            _cli_support._daemon_request("message.send", {"from": "a", "to": ["b"]})
        assert caught.value.code == ipc_errors.DAEMON_RESTORING
        assert daemon.served == 1
    finally:
        daemon.close()


def test_non_restoring_errors_are_never_retried(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = _install(
        root, monkeypatch, refusals=10**9, code=ipc_errors.INVALID_ARGUMENT
    )
    try:
        with pytest.raises(_cli_get_services().CliError) as caught:
            _cli_support._daemon_request("message.send", {"from": "a", "to": ["b"]})
        assert caught.value.code == ipc_errors.INVALID_ARGUMENT
        assert daemon.served == 1
    finally:
        daemon.close()
