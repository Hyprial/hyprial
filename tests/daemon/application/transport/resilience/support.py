from __future__ import annotations

from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections

from tests.support.runtime._tmp import hyprial_test_tmp_root

from contextlib import contextmanager
import errno
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from hyprial.daemon.impl.application import DaemonApplication
import subprocess as _cli_subprocess


ROOT = Path(__file__).resolve().parents[5]
FAKE_LARK_WORKER = ROOT / "tests" / "fixtures" / "lark" / "fake_lark_worker.py"
_REAL_POPEN = subprocess.Popen


class _IdleRuntime:
    def poll(self) -> Any:
        raise AssertionError("the test must finish before reconciliation")

    def stop(self) -> None:
        pass


def _app(state_root: Path) -> DaemonApplication:
    state = state_root / "state"
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="ipc-resilience-test",
        hyprial_home=state_root / "home",
        **_app_injections(),
    )
    _TEST_APPLICATIONS.append(app)
    return app


_TEST_APPLICATIONS: list[DaemonApplication] = []


@pytest.fixture(autouse=True)
def _close_application_domains():  # type: ignore[no-untyped-def]
    yield
    while _TEST_APPLICATIONS:
        _TEST_APPLICATIONS.pop()._agent_session_domains.close()


@pytest.fixture
def short_state_root() -> Iterator[Path]:
    """A state root short enough for AF_UNIX.

    macOS sockaddr_un.sun_path is ~104 bytes; pytest's descriptive tmp_path
    on this host overflows it, so connect() fails with "AF_UNIX path too
    long" before any daemon behavior is exercised -- the reason this whole
    file was red here and mislabeled an environmental flake.  A short /tmp
    mkdtemp keeps the tests testing what they claim to test.
    """

    root = Path(tempfile.mkdtemp(prefix="hyprial-ipc-", dir=hyprial_test_tmp_root()))
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _response(line: bytes) -> dict[str, Any]:
    request = json.loads(line)
    return {
        "version": 1,
        "id": request["id"],
        "result": {"daemon": {"running": True}},
    }


def _request(socket_path: Path, request_id: str = "probe") -> dict[str, Any]:
    # A healthy request must wait out a momentarily full listen backlog just
    # like the production client (kernel.impl.ipc.client._connect_daemon_socket)
    # does: Linux answers connect() to a full AF_UNIX accept queue with an
    # *instant* EAGAIN (macOS: ECONNREFUSED) instead of parking the connect, so a
    # single bare connect() measures the client's luck on a contended host,
    # not whether the daemon starves healthy requests.
    deadline = time.monotonic() + 5.0
    client = _connect_with_backlog_retry(socket_path, deadline=deadline)
    client.settimeout(1.0)
    try:
        client.sendall(
            json.dumps(
                {"version": 1, "id": request_id, "method": "ps", "params": {}}
            ).encode()
            + b"\n"
        )
        return json.loads(client.makefile("rb").readline())
    finally:
        client.close()


def _connect_with_backlog_retry(
    socket_path: Path, *, deadline: float
) -> socket.socket:
    """Establish one peer despite platform-specific full-backlog errors."""

    while True:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(0.25)
        try:
            client.connect(str(socket_path))
        except OSError as error:
            client.close()
            if error.errno not in {errno.EAGAIN, errno.ECONNREFUSED}:
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.005)
            continue
        client.settimeout(None)
        return client






































class _BrokenClient:
    def __init__(self, failure: OSError) -> None:
        self.failure = failure

    def __enter__(self) -> _BrokenClient:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def settimeout(self, _timeout: float) -> None:
        pass

    def recv(self, _size: int) -> bytes:
        raise self.failure


class _OversizedClient(_BrokenClient):
    def __init__(self) -> None:
        super().__init__(OSError("unused"))
        self.read = 0

    def recv(self, size: int) -> bytes:
        self.read += size
        return b"x" * size








def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


class _OwnedDaemonProcess:
    """Exact ``Popen`` birth handle for the daemon launched by this test.

    A direct child that has not been reaped retains its PID on both Linux and
    macOS.  Consequently ``poll() is None`` on this captured handle is the
    portable same-birth proof needed before signalling; no ``/proc`` or PID-only
    ownership guess is involved.
    """

    def __init__(self) -> None:
        self.process: subprocess.Popen[bytes] | None = None

    def popen(self, argv: Any, *args: Any, **kwargs: Any) -> Any:
        process = _REAL_POPEN(argv, *args, **kwargs)
        if list(argv) == [
            sys.executable, "-m", "hyprial.cli", "daemon", "run", "--json",
        ]:
            assert self.process is None, "init launched more than one daemon"
            self.process = process
        return process

    @property
    def pid(self) -> int:
        assert self.process is not None, "init never launched the daemon"
        return self.process.pid

    def cleanup(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return

        # ``process`` is the exact direct-child birth handle.  While wait() has
        # not reaped that child, its PID cannot be reused, so terminate/kill can
        # never target a later unrelated process even when daemon.json was never
        # published or startup failed midway.
        process.terminate()
        try:
            process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            assert process.poll() is None
            process.kill()
            process.wait(timeout=5.0)
        assert process.poll() is not None, f"test-owned daemon {process.pid} leaked"


@contextmanager
def _capture_owned_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_OwnedDaemonProcess]:
    owner = _OwnedDaemonProcess()
    monkeypatch.setattr(_cli_subprocess, "Popen", owner.popen)
    try:
        yield owner
    except BaseException as original:
        try:
            owner.cleanup()
        except BaseException as cleanup:
            raise BaseExceptionGroup(
                "daemon regression failed and its owned-process cleanup failed",
                [original, cleanup],
            )
        raise
    else:
        owner.cleanup()










# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    'FAKE_LARK_WORKER',
    'ROOT',
    '_BrokenClient',
    '_IdleRuntime',
    '_OversizedClient',
    '_OwnedDaemonProcess',
    '_REAL_POPEN',
    '_TEST_APPLICATIONS',
    '_app',
    '_capture_owned_daemon',
    '_close_application_domains',
    '_connect_with_backlog_retry',
    '_request',
    '_response',
    '_write_json',
    'short_state_root',
]
