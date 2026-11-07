from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root
import errno
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable
import pytest
import hyprial.kernel.impl.ipc.client as ipc_client

from ..support import (
    _BrokenClient,
    _IdleRuntime,
    _OversizedClient,
    _OwnedDaemonProcess,
    _REAL_POPEN,
    _app,
    _capture_owned_daemon,
    _close_application_domains as _close_application_domains,
    _connect_with_backlog_retry,
    _request,
    _response,
    short_state_root as short_state_root,
)
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_partial_client_does_not_block_a_healthy_request(
    monkeypatch: pytest.MonkeyPatch, short_state_root: Path
) -> None:
    app = _app(short_state_root)
    app._runtime = _IdleRuntime()  # type: ignore[assignment]
    monkeypatch.setattr(app, "_response", _response)
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    partial = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        partial.connect(str(app.socket_path))
        partial.sendall(b'{"version":1,"id":"partial"')

        started = time.monotonic()
        response = _request(app.socket_path, "healthy")

        assert response["id"] == "healthy"
        assert time.monotonic() - started < 0.75
        assert dispatcher.is_alive()
    finally:
        partial.close()
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(timeout=2.0)
        app._close()


def test_connect_daemon_socket_retries_full_backlog_eagain_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linux full-backlog EAGAIN must be waited out, not surfaced.

    This is the boundary behind the DinD flake: a timeout-socket connect()
    to a full AF_UNIX accept queue raises BlockingIOError *instantly* on
    Linux instead of waiting for a slot, so a single bare connect() fails
    healthy requests whenever the dispatcher is briefly slower than its
    clients.
    """

    attempts = 0

    class FullThenFreeSocket:
        def settimeout(self, _timeout: float) -> None:
            pass

        def connect(self, _path: str) -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise BlockingIOError(
                    errno.EAGAIN, "Resource temporarily unavailable"
                )

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        ipc_client.socket, "socket", lambda *_args: FullThenFreeSocket()
    )
    client = ipc_client._connect_daemon_socket(Path("/tmp/unused.sock"), 5.0)
    assert attempts == 3
    client.close()


def test_connect_daemon_socket_still_fails_after_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persistently full backlog still fails, bounded by the timeout."""

    class AlwaysFullSocket:
        def settimeout(self, _timeout: float) -> None:
            pass

        def connect(self, _path: str) -> None:
            raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")

        def close(self) -> None:
            pass

    monkeypatch.setattr(ipc_client.socket, "socket", lambda *_args: AlwaysFullSocket())
    started = time.monotonic()
    with pytest.raises(BlockingIOError):
        ipc_client._connect_daemon_socket(Path("/tmp/unused.sock"), 0.2)
    assert time.monotonic() - started >= 0.2


def test_connect_daemon_socket_does_not_retry_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ECONNREFUSED (stale socket file) must keep failing fast."""

    attempts = 0

    class RefusedSocket:
        def settimeout(self, _timeout: float) -> None:
            pass

        def connect(self, _path: str) -> None:
            nonlocal attempts
            attempts += 1
            raise ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")

        def close(self) -> None:
            pass

    monkeypatch.setattr(ipc_client.socket, "socket", lambda *_args: RefusedSocket())
    with pytest.raises(ConnectionRefusedError):
        ipc_client._connect_daemon_socket(Path("/tmp/unused.sock"), 5.0)
    assert attempts == 1


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux spells full-backlog connect as EAGAIN; macOS uses "
    "ECONNREFUSED, which _connect_daemon_socket deliberately does not retry",
)
def test_connect_daemon_socket_survives_real_full_kernel_backlog(
    short_state_root: Path,
) -> None:
    """Real kernel evidence: EAGAIN retries land once the listener drains."""

    socket_path = short_state_root / "full-backlog.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    fillers: list[socket.socket] = []
    try:
        for _ in range(8):
            filler = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            filler.settimeout(2.0)
            try:
                filler.connect(str(socket_path))
            except OSError:
                filler.close()
                break
            fillers.append(filler)
        assert fillers, "accept queue never filled; backlog assumption broken"

        # Take the clock BEFORE the drain thread exists: the thread's 0.2 s
        # sleep must not start earlier than `started`, or a main thread
        # descheduled between start() and the clock read shrinks the measured
        # wait (CI unit 33500 measured 0.11 s on a loaded runner).
        started = time.monotonic()

        def drain_one() -> None:
            remaining = 0.2 - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
            connection, _ = listener.accept()
            connection.close()

        threading.Thread(target=drain_one, daemon=True).start()

        client = ipc_client._connect_daemon_socket(socket_path, 5.0)
        client.close()
        assert time.monotonic() - started >= 0.2
    finally:
        for filler in fillers:
            filler.close()
        listener.close()


def test_slow_clients_beyond_listen_backlog_do_not_starve_healthy_request(
    monkeypatch: pytest.MonkeyPatch, short_state_root: Path
) -> None:
    app = _app(short_state_root)
    app._runtime = _IdleRuntime()  # type: ignore[assignment]
    monkeypatch.setattr(app, "_response", _response)
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    partials: list[socket.socket] = []
    try:
        # The listener backlog is 32. Crossing it reproduces the old
        # dispatcher stall while remaining below the explicit worker cap.
        connect_deadline = time.monotonic() + 5.0
        for index in range(40):
            client = _connect_with_backlog_retry(
                app.socket_path, deadline=connect_deadline
            )
            client.sendall(f'{{"id":"partial-{index}"'.encode())
            partials.append(client)

        assert _request(app.socket_path, "past-backlog")["id"] == "past-backlog"
        with app._ipc_clients_lock:
            assert len(app._ipc_client_threads) <= 64
        assert dispatcher.is_alive()
    finally:
        for client in partials:
            client.close()
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(timeout=2.0)
        app._close_ipc_clients()
        with app._ipc_clients_lock:
            assert app._ipc_clients == set()
            assert app._ipc_client_threads == set()
        app._close()


@pytest.mark.parametrize(
    ("error_number", "error_code"),
    [(errno.EMFILE, "EMFILE"), (errno.ENFILE, "ENFILE")],
)
def test_accept_resource_exhaustion_retries_with_bounded_backoff(
    monkeypatch: pytest.MonkeyPatch,
    short_state_root: Path,
    error_number: int,
    error_code: str,
) -> None:
    app = _app(short_state_root)
    app._runtime = _IdleRuntime()  # type: ignore[assignment]
    waits: list[float] = []
    logs: list[tuple[str, dict[str, Any]]] = []

    class ExhaustedServer:
        attempts = 0

        def accept(self) -> tuple[socket.socket, object]:
            self.attempts += 1
            if self.attempts <= 4:
                raise OSError(error_number, "sensitive sentinel")
            app.stop_event.set()
            raise OSError(errno.EBADF, "stopped")

    server = ExhaustedServer()
    app._server = server  # type: ignore[assignment]
    monkeypatch.setattr(
        app.stop_event,
        "wait",
        lambda delay: waits.append(delay) or False,
    )
    monkeypatch.setattr(
        app,
        "_log",
        lambda _level, _component, event, **fields: logs.append((event, fields)),
    )

    app._serve()

    assert server.attempts == 5
    assert waits == [0.05, 0.1, 0.2, 0.4]
    assert logs == [
        (
            "daemon.ipc.accept_resource_exhausted",
            {"errorType": "OSError", "errorCode": error_code},
        )
    ] * 4
    assert "sensitive sentinel" not in repr(logs)


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="Unix RLIMIT")
def test_real_low_fd_pressure_recovers_without_restarting_daemon(
    request: pytest.FixtureRequest,
) -> None:
    # macOS sockaddr_un.sun_path is much shorter than Linux's. Keep this real
    # subprocess socket under a deliberately short /tmp mkdtemp path rather
    # than pytest's descriptive (and deeply nested) tmp_path.
    short_root = Path(tempfile.mkdtemp(prefix="hyprialfd-", dir=hyprial_test_tmp_root()))
    request.addfinalizer(lambda: shutil.rmtree(short_root, ignore_errors=True))
    socket_path = short_root / "d.sock"
    state_dir = short_root / "s"
    code = r'''
import faulthandler
import json
import os
import resource
import sys
from pathlib import Path

faulthandler.dump_traceback_later(12, file=sys.stderr)
print("LOW_FD_PHASE importing", file=sys.stderr, flush=True)
from hyprial.daemon.impl.application import DaemonApplication


from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections

print("LOW_FD_PHASE constructing", file=sys.stderr, flush=True)
socket_path = Path(os.environ["HYPRIAL_TEST_SOCKET"])
state_dir = Path(os.environ["HYPRIAL_TEST_STATE"])
app = DaemonApplication(
    state_dir=state_dir,
    socket_path=socket_path,
    node_id="low-fd-test",
    hyprial_home=state_dir.parent / "home",
        **_app_injections(),
)

class Runtime:
    def poll(self):
        raise AssertionError("low-fd test exceeded its time bound")
    def stop(self):
        pass

app._runtime = Runtime()

def response(line):
    request = json.loads(line)
    app.stop_event.set()
    return {"version": 1, "id": request["id"], "result": {"ok": True}}

app._response = response
app._start_server()
print("LOW_FD_PHASE server-bound", file=sys.stderr, flush=True)
faulthandler.cancel_dump_traceback_later()
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
target = 64 if hard == resource.RLIM_INFINITY else min(hard, 64)
resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
fillers = []
released = False

def observe_exhaustion(_level, _component, event, **fields):
    global released
    if event == "daemon.ipc.accept_resource_exhausted" and not released:
        assert fields["errorCode"] == "EMFILE"
        # Release only after the real accept path observed exhaustion. A timer
        # could free the slot before accept ever encountered the condition.
        os.close(fillers.pop())
        released = True
        print("RECOVERED_FD", flush=True)

app._log = observe_exhaustion
try:
    while True:
        fillers.append(os.open(os.devnull, os.O_RDONLY))
except OSError as error:
    if error.errno != 24:
        raise

print("READY", flush=True)
try:
    app._serve()
finally:
    app.stop_event.set()
    app._close()
    for descriptor in fillers:
        try:
            os.close(descriptor)
        except OSError:
            pass
'''
    from tests.support.runtime.environment import isolated_environment

    environment = isolated_environment(state_dir.parent)
    environment["HYPRIAL_TEST_SOCKET"] = str(socket_path)
    environment["HYPRIAL_TEST_STATE"] = str(state_dir)
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        readable, _, _ = select.select([process.stdout], [], [], 15.0)
        assert readable, (
            "low-fd child did not become ready; "
            f"exit={process.poll()} stderr={process.stderr.read() if process.poll() is not None else ''}"
        )
        assert process.stdout.readline().strip() == "READY"

        response: dict[str, Any] | None = None
        deadline = time.monotonic() + 4.0
        attempt = 0
        while time.monotonic() < deadline and response is None:
            attempt += 1
            try:
                response = _request(socket_path, f"low-fd-{attempt}")
            except (ConnectionError, OSError, json.JSONDecodeError):
                time.sleep(0.05)

        assert response is not None
        assert response["result"] == {"ok": True}
        assert process.wait(timeout=5.0) == 0, process.stderr.read()
        assert "RECOVERED_FD" in process.stdout.read().splitlines()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5.0)
        # Retain startup phase/stack diagnostics on the original failure, and
        # close both pipes even when readiness or a later assertion failed.
        try:
            diagnostic = process.stderr.read()
            if diagnostic:
                print(diagnostic, file=sys.stderr)
        finally:
            process.stdout.close()
            process.stderr.close()


@pytest.mark.parametrize(
    # Factories, not instances: an exception object raised during the test
    # retains its __traceback__, and pytest keeps parametrize values on the
    # item's callspec for the whole session -- an instance here would pin
    # every frame (and every fd) the raise touched (card #77).
    "client_factory",
    [
        lambda: _BrokenClient(TimeoutError("idle client")),
        lambda: _BrokenClient(ConnectionResetError("reset client")),
        _OversizedClient,
    ],
    ids=["idle", "reset", "oversized"],
)
def test_bad_client_is_confined_to_its_connection(
    monkeypatch: pytest.MonkeyPatch,
    short_state_root: Path,
    client_factory: Callable[[], _BrokenClient],
) -> None:
    client = client_factory()
    app = _app(short_state_root)
    monkeypatch.setattr(app, "_log", lambda *_args, **_kwargs: None)
    if isinstance(client.failure, TimeoutError):
        monkeypatch.setattr(
            "hyprial.daemon.impl.ipc.server._IPC_CLIENT_IDLE_TIMEOUT", 0.0
        )

    app._serve_client(client)  # type: ignore[arg-type]

    server, healthy = socket.socketpair()
    try:
        healthy.sendall(
            b'{"version":1,"id":"healthy","method":"ps","params":{}}\n'
        )
        monkeypatch.setattr(app, "_response", _response)
        app._serve_client(server)
        assert json.loads(healthy.makefile("rb").readline())["id"] == "healthy"
    finally:
        server.close()
        healthy.close()


def test_slow_restore_publishes_worker_socket_without_queueing_cli_probes(
    monkeypatch: pytest.MonkeyPatch, short_state_root: Path
) -> None:
    """Probes are answered during restore, not parked in the backlog.

    The marker moved with the serving boundary: daemon.json now lands before
    restore starts (it means "ping answers", not "connectors are back"), and
    `_wait_for_daemon` probes with ping -- so the wait returns while restore
    is held by an event, independent of how quickly the CI runner schedules
    startup or the probe. This is an ordering contract, not startup throughput.
    """

    app = _app(short_state_root)
    (short_state_root / "home").mkdir(mode=0o700, exist_ok=True)
    monkeypatch.setenv("HYPRIAL_HOME", str(short_state_root / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(app.state_dir))
    restore_started = threading.Event()
    restore_finished = threading.Event()
    release_restore = threading.Event()
    request_count = 0

    class LiveProcess:
        pid = os.getpid()
        returncode: int | None = None

        @staticmethod
        def poll() -> None:
            return None

    def start_runtime() -> None:
        app._runtime = _IdleRuntime()  # type: ignore[assignment]

    def slow_restore() -> None:
        restore_started.set()
        # Lark workers need the socket during startup for inbound delivery and
        # the reconnect history sweep, so delaying bind until after restore is
        # not a safe fix.
        assert app.socket_path.exists()
        assert release_restore.wait(30), "test did not release the restore barrier"
        restore_finished.set()

    def response(line: bytes) -> dict[str, Any]:
        nonlocal request_count
        request_count += 1
        request = json.loads(line)
        if request.get("method") == "ping":
            return {
                "version": 1,
                "id": request["id"],
                "result": {"running": True, "pid": os.getpid()},
            }
        return _response(line)

    monkeypatch.setattr(app, "_start_runtime", start_runtime)
    monkeypatch.setattr(app, "_restore_adapters", slow_restore)
    monkeypatch.setattr(app, "_response", response)
    daemon = threading.Thread(target=app.run, daemon=True)
    daemon.start()
    try:
        assert restore_started.wait(15.0), "daemon did not reach the restore boundary"
        assert not restore_finished.is_set()
        assert app.socket_path.exists()
        # "Serving", not "restored": the marker is already down, and it must
        # name this process or the wait below would never accept it.
        marker = json.loads((app.state_dir / "daemon.json").read_text(encoding="utf-8"))
        assert marker["pid"] == os.getpid()

        status = _cli_get_services()._wait_for_daemon(  # type: ignore[arg-type]
            timeout=2.0, process=LiveProcess()
        )
        assert status["running"] is True
        # The wait returned before restore finished, on a single probe that
        # was answered rather than queued.
        assert not restore_finished.is_set()
        assert request_count == 1
        assert daemon.is_alive()
    finally:
        app.stop_event.set()
        release_restore.set()
        if app._server is not None:
            app._server.close()
        daemon.join(timeout=15.0)
        assert not daemon.is_alive(), "daemon teardown still running before fixture removal"
        app.socket_path.unlink(missing_ok=True)


def test_owned_daemon_cleanup_terminates_a_captured_child_without_a_marker() -> None:
    owner = _OwnedDaemonProcess()
    owner.process = _REAL_POPEN(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    owner.cleanup()

    assert owner.process.poll() is not None


def test_owned_daemon_cleanup_preserves_original_and_cleanup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process: subprocess.Popen[bytes] | None = None

    with pytest.raises(BaseExceptionGroup) as caught:
        with _capture_owned_daemon(monkeypatch) as owner:
            process = _REAL_POPEN(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            owner.process = process
            real_cleanup = owner.cleanup

            def fail_cleanup() -> None:
                real_cleanup()
                raise RuntimeError("cleanup sentinel")

            monkeypatch.setattr(owner, "cleanup", fail_cleanup)
            raise ValueError("original sentinel")

    assert process is not None and process.poll() is not None
    assert [type(error) for error in caught.value.exceptions] == [
        ValueError,
        RuntimeError,
    ]
    assert [str(error) for error in caught.value.exceptions] == [
        "original sentinel",
        "cleanup sentinel",
    ]
