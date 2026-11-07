"""The kernel client is the only daemon IPC transport.

These tests speak the real version-1 newline-delimited protocol over an
isolated AF_UNIX socket: one connect, one frame, one response.  They pin the
wire shape, the response classification, the two places a caller may retry,
and the places it may not -- a write is never resent blindly.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import json
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from tests.support.runtime._tmp import hyprial_test_tmp_root

import hyprial.kernel.impl.ipc.client as client_module
from hyprial.kernel.impl.contracts.daemon import ipc_errors
from hyprial.kernel.impl.contracts.ports import RequestPortError


JsonObject = dict[str, Any]

_HOLD = object()


def _ok(frame: JsonObject, result: JsonObject) -> bytes:
    return (
        json.dumps(
            {"version": 1, "id": frame.get("id"), "result": result},
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def _error(frame: JsonObject, code: str, message: str, data: Any = None) -> bytes:
    failure: JsonObject = {"code": code, "message": message}
    if data is not None:
        failure["data"] = data
    return (
        json.dumps(
            {"version": 1, "id": frame.get("id"), "error": failure},
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


class _DaemonDouble:
    """One isolated AF_UNIX listener that records every connection and frame."""

    def __init__(
        self,
        socket_path: Path,
        handler: Callable[[JsonObject, int], bytes | None | object],
    ) -> None:
        self.socket_path = socket_path
        self.handler = handler
        self.accepts = 0
        self.frames: list[JsonObject] = []
        self._stop = threading.Event()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(socket_path))
        self._server.listen(8)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._server.settimeout(0.05)
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:  # pragma: no cover - listener closed under the thread
                return
            self.accepts += 1
            with connection:
                connection.settimeout(2.0)
                buffer = bytearray()
                try:
                    while b"\n" not in buffer:
                        chunk = connection.recv(64 * 1024)
                        if not chunk:
                            break
                        buffer.extend(chunk)
                except OSError:  # pragma: no cover - client abandoned the read
                    continue
                if not buffer:
                    continue
                frame = json.loads(buffer.partition(b"\n")[0])
                self.frames.append(frame)
                reply = self.handler(frame, len(self.frames))
                if reply is None:
                    continue
                if reply is _HOLD:
                    self._stop.wait(1.0)
                    continue
                try:
                    connection.sendall(reply)  # type: ignore[arg-type]
                except OSError:  # pragma: no cover - client timed out first
                    continue

    def close(self) -> None:
        self._stop.set()
        self._server.close()
        self._thread.join(timeout=2.0)


@pytest.fixture
def socket_root() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="hyprial-kipc-", dir=hyprial_test_tmp_root()))
    yield root
    shutil.rmtree(root, ignore_errors=True)


def test_request_frame_and_result_round_trip(socket_root: Path) -> None:
    """One connect sends exactly the documented v1 frame and returns result."""

    daemon = _DaemonDouble(
        socket_root / "daemon.sock",
        lambda frame, _n: _ok(frame, {"pong": frame["params"]["value"]}),
    )
    try:
        result = client_module.daemon_request_once(
            "link.stats",
            {"value": 7},
            socket_path=socket_root / "daemon.sock",
            timeout=2.0,
            uuid4=lambda: "fixed-request-id",
        )
    finally:
        daemon.close()

    assert result == {"pong": 7}
    assert daemon.accepts == 1
    assert daemon.frames == [
        {
            "version": 1,
            "id": "fixed-request-id",
            "method": "link.stats",
            "params": {"value": 7},
        }
    ]


def test_frame_omits_params_when_not_supplied(socket_root: Path) -> None:
    daemon = _DaemonDouble(
        socket_root / "daemon.sock", lambda frame, _n: _ok(frame, {"pong": True})
    )
    try:
        assert client_module.daemon_request_once(
            "ping",
            socket_path=socket_root / "daemon.sock",
            timeout=2.0,
            uuid4=lambda: "ping-id",
        ) == {"pong": True}
    finally:
        daemon.close()

    assert daemon.frames == [{"version": 1, "id": "ping-id", "method": "ping"}]


def test_response_for_another_request_id_is_skipped(socket_root: Path) -> None:
    """Event frames and other callers' answers are not this request's result."""

    def handler(frame: JsonObject, _n: int) -> bytes:
        unrelated = (
            json.dumps(
                {"version": 1, "id": "someone-else", "result": {"wrong": True}},
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        event = (
            json.dumps(
                {"version": 1, "id": "event-frame", "event": "progress"},
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        return unrelated + event + _ok(frame, {"right": True})

    daemon = _DaemonDouble(socket_root / "daemon.sock", handler)
    try:
        result = client_module.daemon_request_once(
            "link.list",
            socket_path=socket_root / "daemon.sock",
            timeout=2.0,
            uuid4=lambda: "wanted",
        )
    finally:
        daemon.close()

    assert result == {"right": True}


def test_error_envelope_becomes_a_port_error(socket_root: Path) -> None:
    """A permanent refusal keeps its code/message/data and adds no cause."""

    daemon = _DaemonDouble(
        socket_root / "daemon.sock",
        lambda frame, _n: _error(
            frame, ipc_errors.INVALID_ARGUMENT, "bad target", {"field": "to"}
        ),
    )
    try:
        with pytest.raises(RequestPortError) as caught:
            client_module.daemon_request_once(
                "message.send",
                {"to": []},
                socket_path=socket_root / "daemon.sock",
                timeout=2.0,
                uuid4=lambda: "request-1",
            )
    finally:
        daemon.close()

    assert caught.value.code == ipc_errors.INVALID_ARGUMENT
    assert str(caught.value) == "bad target"
    assert caught.value.data == {"field": "to"}
    assert caught.value.__cause__ is None


def test_restoring_refusal_is_waited_out_then_served(
    socket_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DAEMON_RESTORING is the only retried refusal, and each try is new."""

    monkeypatch.setenv("HYPRIAL_DAEMON_RESTORE_WAIT_SECONDS", "2")
    monkeypatch.setattr(client_module, "_RESTORE_WAIT_POLL_SECONDS", 0.01)
    counter = {"n": 0}

    def handler(frame: JsonObject, n: int) -> bytes:
        counter["n"] = n
        if n < 3:
            return _error(
                frame, ipc_errors.DAEMON_RESTORING, "daemon is restoring connectors"
            )
        return _ok(frame, {"served": True})

    daemon = _DaemonDouble(socket_root / "daemon.sock", handler)
    try:
        result = client_module.daemon_request(
            "message.send",
            {"from": "a", "to": ["b"]},
            socket_path=socket_root / "daemon.sock",
            timeout=2.0,
        )
    finally:
        daemon.close()

    assert result == {"served": True}
    assert daemon.accepts == 3
    # A retry is a fresh request id, never a replay of the refused frame.
    assert len({frame["id"] for frame in daemon.frames}) == 3


def test_disconnect_is_not_retried(socket_root: Path) -> None:
    """A peer that closes before answering is reported, not resent."""

    daemon = _DaemonDouble(socket_root / "daemon.sock", lambda _frame, _n: None)
    try:
        with pytest.raises(ipc_errors.DaemonDisconnectedError):
            client_module.daemon_request(
                "message.send",
                {"from": "a", "to": ["b"]},
                socket_path=socket_root / "daemon.sock",
                timeout=2.0,
            )
    finally:
        daemon.close()

    assert daemon.accepts == 1
    assert len(daemon.frames) == 1


def test_timeout_is_not_retried(socket_root: Path) -> None:
    """A deadline is a reported outcome for a write, not a resend trigger."""

    daemon = _DaemonDouble(socket_root / "daemon.sock", lambda _frame, _n: _HOLD)
    try:
        started = time.monotonic()
        with pytest.raises(ipc_errors.IpcTimeoutError) as caught:
            client_module.daemon_request(
                "message.send",
                {"from": "a", "to": ["b"]},
                socket_path=socket_root / "daemon.sock",
                timeout=0.15,
            )
        elapsed = time.monotonic() - started
    finally:
        daemon.close()

    assert caught.value.code == ipc_errors.IPC_TIMEOUT
    assert "timed out after 0.15s" in str(caught.value)
    assert "message.send" in str(caught.value)
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert elapsed < 1.0
    assert daemon.accepts == 1
    assert len(daemon.frames) == 1


def test_connect_failure_is_a_typed_unavailable_error(socket_root: Path) -> None:
    """A missing socket says what it tried, and keeps the OS cause."""

    missing = socket_root / "never-there.sock"
    with pytest.raises(ipc_errors.DaemonUnavailableError) as caught:
        client_module.daemon_request_once("ping", socket_path=missing, timeout=0.5)

    assert caught.value.code == ipc_errors.DAEMON_UNAVAILABLE
    assert str(missing) in str(caught.value)
    assert isinstance(caught.value.__cause__, OSError)


def test_explicit_endpoint_wins_over_environment(
    socket_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit socket_path is used even when the env names another root."""

    monkeypatch.setenv("HARNESS_STATE_DIR", str(socket_root / "unused-state"))
    daemon = _DaemonDouble(
        socket_root / "explicit.sock", lambda frame, _n: _ok(frame, {"explicit": True})
    )
    try:
        result = client_module.daemon_request_once(
            "ping",
            socket_path=socket_root / "explicit.sock",
            timeout=2.0,
        )
    finally:
        daemon.close()

    assert result == {"explicit": True}


class _OversizedSocket:
    def __init__(self) -> None:
        self.closed = False

    def sendall(self, _data: bytes) -> None:
        return None

    def recv(self, _size: int) -> bytes:
        return b" " * (1024 * 1024)

    def close(self) -> None:
        self.closed = True


def test_oversized_response_is_refused(
    socket_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket = _OversizedSocket()
    monkeypatch.setattr(
        client_module, "_connect_daemon_socket", lambda *_a, **_k: socket
    )

    with pytest.raises(RequestPortError) as caught:
        client_module.daemon_request_once(
            "link.list",
            socket_path=socket_root / "unused.sock",
            timeout=2.0,
        )

    assert caught.value.code == "IPC_RESPONSE_TOO_LARGE"
    assert socket.closed is True
