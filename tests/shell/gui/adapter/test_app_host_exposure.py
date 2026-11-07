"""Tests for the composed tailnet exposure behind the remote TCP facade.

The facade is reachable only through the daemon's own ``network.expose``
route: these tests drive the composition with a recording daemon-request
port and a real loopback listener, and pin the order (bind, expose with the
effective port, serve, unexpose) plus every named refusal.  Nothing here
touches a real daemon, sidecar or tailnet.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from hyprial.kernel import DaemonRequestError
from hyprial.shell.impl.gui.adapter.host import (
    EXPOSURE_PROXY_PROTOCOL,
    MAX_REQUEST_LINE_BYTES,
    HostExposureError,
    HostLaunch,
    adapter_from_config,
    run_host,
    serve_host,
    serve_tcp,
    serve_tcp_exposed,
)
from hyprial.shell.impl.gui.adapter.lifecycle import start_readiness_reporter
from tests.shell.gui.adapter.test_app_adapter_host import (
    CONFIG,
    GATE_NODE,
    FakeDaemon,
    TcpFrameClient,
    make_test_gate,
    signed_test_frame,
)

EXPOSE_PORT = 7443


def remote_authority_flags(tmp_path: Path, monkeypatch) -> list[str]:
    """CLI flags for the fail-closed remote arm, with a seam-only gate.

    The composition root is the real one; only the gate factory is replaced so
    these exposure tests do not need a real derive helper on disk.
    """

    from hyprial.shell.impl.gui.adapter import host as host_module

    monkeypatch.setattr(host_module, "build_auth_gate", lambda *_args: make_test_gate())
    return [
        "--mobile-access-db",
        str(tmp_path / "access.sqlite3"),
        "--node-id",
        GATE_NODE,
        "--derive-command",
        str(tmp_path / "derive"),
    ]


class RecordingDaemonPort:
    """A daemon-request port that records calls and answers exposure ops.

    The default expose answer echoes the request, which is the daemon's own
    contract; ``expose_error``, a canned ``expose_answer`` and
    ``unexpose_error`` model the refusal shapes.
    """

    def __init__(
        self,
        *,
        expose_error: Exception | None = None,
        expose_answer: object | None = None,
        unexpose_error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._expose_error = expose_error
        self._expose_answer = expose_answer
        self._unexpose_error = unexpose_error

    def __call__(self, method: str, params: dict) -> object:
        recorded = dict(params)
        self.calls.append((method, recorded))
        if method == "network.expose":
            if self._expose_error is not None:
                raise self._expose_error
            if self._expose_answer is not None:
                return self._expose_answer
            return {"exposure": recorded}
        if method == "network.unexpose":
            if self._unexpose_error is not None:
                raise self._unexpose_error
            return {"port": recorded["port"], "removed": True}
        return {}


facade_sessions: list[tuple[threading.Event, threading.Thread]] = []


@pytest.fixture(autouse=True)
def stop_facades():
    """Join every facade thread so no listener fd survives into the guard."""

    yield
    for stop, _ in facade_sessions:
        stop.set()
    for _, thread in facade_sessions:
        thread.join(timeout=3.0)
    facade_sessions.clear()


def start_exposed(port: RecordingDaemonPort) -> tuple[
    threading.Event, threading.Event, threading.Thread, list[BaseException]
]:
    """One real ``serve_tcp_exposed`` listener on an ephemeral loopback port."""

    adapter = adapter_from_config(CONFIG, port)
    ready = threading.Event()
    stop = threading.Event()
    failures: list[BaseException] = []

    def run() -> None:
        try:
            serve_tcp_exposed(
                0,
                adapter,
                expose_port=EXPOSE_PORT,
                request=port,
                ready=ready,
                stop=stop,
                auth_gate=make_test_gate(),
            )
        except BaseException as error:  # noqa: BLE001 - surfaced to the test
            failures.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    facade_sessions.append((stop, thread))
    return ready, stop, thread, failures


def exposed_target(port: RecordingDaemonPort) -> str:
    method, params = port.calls[0]
    assert method == "network.expose", port.calls
    return params["target"]


def test_the_facade_exposes_the_effective_port_then_unexposes_on_stop() -> None:
    port = RecordingDaemonPort()
    ready, stop, thread, failures = start_exposed(port)
    assert ready.wait(timeout=5.0), "the facade must announce readiness once exposed"

    target = exposed_target(port)
    bound_port = int(target.rsplit(":", 1)[1])
    assert port.calls[0] == (
        "network.expose",
        {
            "port": EXPOSE_PORT,
            "target": f"tcp:127.0.0.1:{bound_port}",
            "proxyProtocol": EXPOSURE_PROXY_PROTOCOL,
        },
    )

    with TcpFrameClient("127.0.0.1", bound_port) as client:
        client.send(signed_test_frame(nonce="expose-frame-1"))
        assert client.read()["ok"] is True
        # End the stream so the facade's single connection slot is free and
        # the stop below is honored promptly (the reader holds a dup).
        client.socket.shutdown(socket.SHUT_RDWR)

    stop.set()
    thread.join(timeout=3.0)
    assert [method for method, _ in port.calls] == [
        "network.expose",
        "network.unexpose",
    ]
    assert port.calls[1] == ("network.unexpose", {"port": EXPOSE_PORT})
    assert failures == []


def test_a_refused_expose_stops_the_facade_with_the_daemons_named_error() -> None:
    refusal = DaemonRequestError(
        "FORWARDING_UNAVAILABLE", "forwarding sidecar is not running"
    )
    port = RecordingDaemonPort(expose_error=refusal)
    ready, _, thread, failures = start_exposed(port)
    thread.join(timeout=5.0)

    assert not ready.is_set(), "a refused expose must not announce a serving facade"
    assert len(failures) == 1
    error = failures[0]
    assert isinstance(error, HostExposureError)
    assert (error.phase, error.code, error.message) == (
        "expose",
        "FORWARDING_UNAVAILABLE",
        "forwarding sidecar is not running",
    )
    assert [method for method, _ in port.calls] == ["network.expose"]


def test_an_unconfirmed_expose_answer_is_one_named_refusal() -> None:
    for answer in (
        {},
        {"exposure": None},
        {
            "exposure": {
                "port": EXPOSE_PORT,
                "target": "tcp:127.0.0.1:1",
                "proxyProtocol": EXPOSURE_PROXY_PROTOCOL,
            }
        },
        {
            "exposure": {
                "port": EXPOSE_PORT + 1,
                "target": "tcp:127.0.0.1:1",
                "proxyProtocol": EXPOSURE_PROXY_PROTOCOL,
            }
        },
        {
            "exposure": {
                "port": EXPOSE_PORT,
                "target": "tcp:127.0.0.1:1",
                "proxyProtocol": "v2",
            }
        },
    ):
        port = RecordingDaemonPort(expose_answer=answer)
        ready, _, thread, failures = start_exposed(port)
        thread.join(timeout=5.0)

        assert len(failures) == 1, answer
        assert isinstance(failures[0], HostExposureError), answer
        assert (failures[0].phase, failures[0].code) == (
            "expose",
            "INVALID_RESPONSE",
        ), answer
        assert not ready.is_set(), answer
        assert [method for method, _ in port.calls] == ["network.expose"], answer


def test_a_refused_expose_releases_the_bound_listener() -> None:
    port = RecordingDaemonPort(
        expose_error=DaemonRequestError("FORWARDING_UNAVAILABLE", "no sidecar")
    )
    _, _, thread, failures = start_exposed(port)
    thread.join(timeout=5.0)
    assert len(failures) == 1

    # An unclosed listener would refuse a second bind on the same port.
    host, _, raw_port = exposed_target(port).removeprefix("tcp:").rpartition(":")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, int(raw_port)))


def test_a_refused_unexpose_is_named_after_the_facade_stopped() -> None:
    refusal = DaemonRequestError(
        "FORWARDING_UNAVAILABLE", "forwarding sidecar is not running"
    )
    port = RecordingDaemonPort(unexpose_error=refusal)
    ready, stop, thread, failures = start_exposed(port)
    assert ready.wait(timeout=5.0)

    stop.set()
    thread.join(timeout=3.0)

    assert len(failures) == 1
    error = failures[0]
    assert isinstance(error, HostExposureError)
    assert (error.phase, error.code, error.message) == (
        "cleanup",
        "FORWARDING_UNAVAILABLE",
        "forwarding sidecar is not running",
    )
    assert [method for method, _ in port.calls] == [
        "network.expose",
        "network.unexpose",
    ]


@pytest.mark.parametrize("port_number", [0, -1, 65536, True, 1.5])
def test_an_invalid_expose_port_is_refused_before_any_socket_work(
    port_number: object,
) -> None:
    port = RecordingDaemonPort()
    adapter = adapter_from_config(CONFIG, FakeDaemon())

    with pytest.raises(ValueError):
        serve_tcp_exposed(
            0,
            adapter,
            expose_port=port_number,
            request=port,
            auth_gate=make_test_gate(),
        )
    assert port.calls == []


# --- the CLI composition ----------------------------------------------------


def write_config(tmp_path) -> str:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(CONFIG), encoding="utf-8")
    return str(path)


def test_cli_pairs_the_expose_port_with_the_tcp_listener() -> None:
    from hyprial.shell.impl.gui.adapter.host import main as host_main

    # ``--tcp-port`` without ``--expose-port`` is the Windows local arm and is
    # valid; the pairing refusals are the exposure-only combinations below.
    for argv in (
        ["--socket", "/tmp/x.sock", "--expose-port", "7443", "--config", "unused.json"],
        ["--state-dir", "/tmp/state", "--expose-port", "7443", "--config", "unused.json"],
        ["--tcp-port", "0", "--expose-port", "0", "--config", "unused.json"],
        ["--tcp-port", "0", "--expose-port", "65536", "--config", "unused.json"],
    ):
        with pytest.raises(SystemExit):
            host_main(argv)


def test_main_composes_expose_serve_and_unexpose(tmp_path, monkeypatch) -> None:
    from hyprial.shell.impl.gui.adapter import host as host_module

    port = RecordingDaemonPort()
    monkeypatch.setattr(host_module, "daemon_request_callable", lambda: port)
    observed: list[str] = []

    def serving_stub(loopback_port, adapter, **kwargs) -> None:
        kwargs["on_bound"]("127.0.0.1", 51234)
        observed.append("served")

    monkeypatch.setattr(host_module, "serve_tcp", serving_stub)

    code = host_module.main(
        ["--tcp-port", "0", "--expose-port", "7443", "--config", write_config(tmp_path)]
        + remote_authority_flags(tmp_path, monkeypatch)
    )

    assert code == 0
    assert observed == ["served"]
    assert port.calls == [
        (
            "network.expose",
            {
                "port": 7443,
                "target": "tcp:127.0.0.1:51234",
                "proxyProtocol": EXPOSURE_PROXY_PROTOCOL,
            },
        ),
        ("network.unexpose", {"port": 7443}),
    ]


def test_main_reports_a_refused_expose_on_stderr(tmp_path, monkeypatch, capsys) -> None:
    from hyprial.shell.impl.gui.adapter import host as host_module

    port = RecordingDaemonPort(
        expose_error=DaemonRequestError(
            "FORWARDING_UNAVAILABLE", "forwarding sidecar is not running"
        )
    )
    monkeypatch.setattr(host_module, "daemon_request_callable", lambda: port)

    code = host_module.main(
        ["--tcp-port", "0", "--expose-port", "7443", "--config", write_config(tmp_path)]
        + remote_authority_flags(tmp_path, monkeypatch)
    )

    assert code == 1
    assert [method for method, _ in port.calls] == ["network.expose"]
    stderr = capsys.readouterr().err
    assert "expose" in stderr
    assert "FORWARDING_UNAVAILABLE: forwarding sidecar is not running" in stderr


def test_main_treats_a_stop_while_serving_as_a_clean_exit(
    tmp_path, monkeypatch
) -> None:
    from hyprial.shell.impl.gui.adapter import host as host_module

    port = RecordingDaemonPort()
    monkeypatch.setattr(host_module, "daemon_request_callable", lambda: port)

    def interrupted_stub(loopback_port, adapter, **kwargs) -> None:
        kwargs["on_bound"]("127.0.0.1", 51234)
        raise KeyboardInterrupt

    monkeypatch.setattr(host_module, "serve_tcp", interrupted_stub)

    code = host_module.main(
        ["--tcp-port", "0", "--expose-port", "7443", "--config", write_config(tmp_path)]
        + remote_authority_flags(tmp_path, monkeypatch)
    )

    assert code == 0
    assert [method for method, _ in port.calls] == [
        "network.expose",
        "network.unexpose",
    ]


# --- SIGTERM while the remote facade serves ---------------------------------


def test_main_maps_sigterm_onto_the_clean_stop_path(tmp_path, monkeypatch) -> None:
    """The remote facade adopts SIGTERM as the same stop Ctrl-C already is.

    While serving, SIGTERM is answered by a handler that unwinds exactly like
    the Ctrl-C stop (so the composed cleanup still runs), SIGINT keeps its
    current disposition, and the previous SIGTERM handler returns once
    ``main`` is done.
    """

    from hyprial.shell.impl.gui.adapter import host as host_module

    port = RecordingDaemonPort()
    monkeypatch.setattr(host_module, "daemon_request_callable", lambda: port)
    before_sigterm = signal.getsignal(signal.SIGTERM)
    before_sigint = signal.getsignal(signal.SIGINT)
    observed: dict[str, object] = {}

    def sigterm_while_serving(loopback_port, adapter, **kwargs) -> None:
        kwargs["on_bound"]("127.0.0.1", 51234)
        observed["sigint"] = signal.getsignal(signal.SIGINT)
        handler = signal.getsignal(signal.SIGTERM)
        observed["handler"] = handler
        assert handler is not before_sigterm, (
            "SIGTERM must be adopted while the remote facade serves"
        )
        assert callable(handler)
        # The adopted shape is the Ctrl-C unwind: KeyboardInterrupt, which
        # serve_tcp_exposed's finally turns into the composed unexpose.
        handler(signal.SIGTERM, None)

    monkeypatch.setattr(host_module, "serve_tcp", sigterm_while_serving)

    code = host_module.main(
        ["--tcp-port", "0", "--expose-port", "7443", "--config", write_config(tmp_path)]
        + remote_authority_flags(tmp_path, monkeypatch)
    )

    assert code == 0
    assert observed["sigint"] is before_sigint, "SIGINT keeps its current disposition"
    assert signal.getsignal(signal.SIGTERM) is before_sigterm, (
        "the previous SIGTERM handler must return after main"
    )
    assert [method for method, _ in port.calls] == [
        "network.expose",
        "network.unexpose",
    ]


def test_the_desktop_socket_path_adopts_sigterm_for_its_own_cleanup(
    tmp_path, monkeypatch
) -> None:
    """The AF_UNIX desktop path owns its socket; SIGTERM must remove it.

    The remote arm needs the adopted signal for ``network.unexpose``; the
    desktop arm needs the same unwind for its own socket file, so a
    supervisor stop leaves nothing behind.  The previous handler returns
    once ``main`` is done.
    """

    from hyprial.shell.impl.gui.adapter import host as host_module

    port = RecordingDaemonPort()
    monkeypatch.setattr(host_module, "daemon_request_callable", lambda: port)
    before = signal.getsignal(signal.SIGTERM)
    observed: dict[str, object] = {}

    def socket_stub(socket_path, adapter, **kwargs) -> None:
        observed["handler"] = signal.getsignal(signal.SIGTERM)

    monkeypatch.setattr(host_module, "serve", socket_stub)

    code = host_module.main(
        ["--socket", str(tmp_path / "host.sock"), "--config", write_config(tmp_path)]
    )

    assert code == 0
    assert observed["handler"] is not before
    assert callable(observed["handler"])
    assert signal.getsignal(signal.SIGTERM) is before
    assert port.calls == []


def _started_host(argv: list[str], *, env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "hyprial.shell.impl.gui.adapter.host", *argv],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_for_socket(socket_path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if socket_path.exists():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.5)
                if probe.connect_ex(str(socket_path)) == 0:
                    return
            finally:
                probe.close()
        time.sleep(0.05)
    raise AssertionError(f"the host never served {socket_path}")


def test_a_started_host_reports_readiness_and_cleans_up_on_sigterm() -> None:
    """The real production host: readiness callback, self-probe, SIGTERM cleanup."""

    from hyprial.shell.impl.gui.adapter.lifecycle import READY_PORT_ENV, READY_TOKEN_ENV

    directory = tempfile.mkdtemp(prefix="apc-ready-", dir="/tmp")
    socket_path = Path(directory) / "host.sock"
    config = Path(write_config(Path(directory)))
    callback = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    callback.bind(("127.0.0.1", 0))
    callback.listen(1)
    callback.settimeout(10.0)
    env = dict(os.environ)
    env[READY_PORT_ENV] = str(callback.getsockname()[1])
    env[READY_TOKEN_ENV] = "readiness-token"
    host = None
    try:
        host = _started_host(
            ["--socket", str(socket_path), "--config", str(config)], env=env
        )
        connection, _ = callback.accept()
        with connection:
            message = json.loads(connection.makefile("rb").readline())
        assert message == {
            "token": "readiness-token",
            "probe": "ok",
            "socket": str(socket_path),
        }
        assert socket_path.exists()
        host.send_signal(signal.SIGTERM)
        assert host.wait(timeout=10.0) == 0, host.stderr.read().decode()
        assert not socket_path.exists(), "a clean stop removes its own socket"
    finally:
        callback.close()
        if host is not None and host.poll() is None:
            host.kill()
            host.wait(timeout=5.0)
        shutil.rmtree(directory, ignore_errors=True)


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner watchdog")
def test_the_host_stops_when_the_starting_owner_disappears() -> None:
    """Abnormal shell exit: PID watched, never a process name."""

    from hyprial.shell.impl.gui.adapter.lifecycle import OWNER_PID_ENV

    directory = tempfile.mkdtemp(prefix="apc-owner-", dir="/tmp")
    socket_path = Path(directory) / "host.sock"
    config = Path(write_config(Path(directory)))
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    env = dict(os.environ)
    env[OWNER_PID_ENV] = str(owner.pid)
    host = None
    try:
        host = _started_host(
            ["--socket", str(socket_path), "--config", str(config)], env=env
        )
        _wait_for_socket(socket_path)
        owner.kill()
        owner.wait(timeout=5.0)
        assert host.wait(timeout=15.0) == 0, host.stderr.read().decode()
        assert not socket_path.exists(), "the orphaned host cleans its socket"
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5.0)
        if host is not None and host.poll() is None:
            host.kill()
            host.wait(timeout=5.0)
        shutil.rmtree(directory, ignore_errors=True)


def test_a_real_sigterm_stops_the_served_facade_and_unexposes(
    tmp_path, monkeypatch
) -> None:
    """End to end in-process: real listener, real signal, real cleanup.

    The daemon request port is the only fake.  The SIGTERM goes to this very
    process and is answered by the handler ``main`` adopted, the frame
    round-trip proves the facade was really serving when it arrived, and the
    recorded ``network.unexpose`` proves the stop unwound through the
    composed cleanup -- the same order the stop-event path already pins.
    """

    from hyprial.shell.impl.gui.adapter import host as host_module

    port = RecordingDaemonPort()
    monkeypatch.setattr(host_module, "daemon_request_callable", lambda: port)
    before = signal.getsignal(signal.SIGTERM)
    problems: list[str] = []

    def stop_it() -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if [method for method, _ in port.calls] == ["network.expose"]:
                break
            time.sleep(0.02)
        else:
            problems.append("the facade never exposed")
            os.kill(os.getpid(), signal.SIGINT)  # unblock main; failure recorded
            return
        bound_port = int(exposed_target(port).rsplit(":", 1)[1])
        try:
            with TcpFrameClient("127.0.0.1", bound_port) as client:
                client.send(signed_test_frame(nonce="sigterm-frame-1"))
                assert client.read()["ok"] is True
                client.socket.shutdown(socket.SHUT_RDWR)
        except BaseException as error:  # noqa: BLE001 - recorded, never fatal
            problems.append(f"frame round-trip failed: {error!r}")
        handler = signal.getsignal(signal.SIGTERM)
        if handler is before or not callable(handler):
            problems.append("SIGTERM was not adopted while serving")
            # Unblock main with SIGINT instead of firing an unadopted SIGTERM,
            # which would kill this test process rather than fail this test.
            os.kill(os.getpid(), signal.SIGINT)
            return
        os.kill(os.getpid(), signal.SIGTERM)

    helper = threading.Thread(target=stop_it)
    helper.start()
    code = host_module.main(
        ["--tcp-port", "0", "--expose-port", "7443", "--config", write_config(tmp_path)]
        + remote_authority_flags(tmp_path, monkeypatch)
    )
    helper.join(timeout=5.0)

    assert problems == []
    assert code == 0, "a SIGTERM stop is the clean path, like a Ctrl-C stop"
    assert signal.getsignal(signal.SIGTERM) is before
    assert [method for method, _ in port.calls] == [
        "network.expose",
        "network.unexpose",
    ]


# --- Windows local loopback arm (no AF_UNIX, no exposure) ------------------


def start_local_tcp_host(
    launch: HostLaunch, *, request_token: str | None = None
) -> tuple[FakeDaemon, str, int]:
    """One real local ``serve_host`` on an ephemeral loopback port."""

    daemon = FakeDaemon()
    adapter = adapter_from_config(CONFIG, daemon)
    bound: list[tuple[str, int]] = []
    ready = threading.Event()
    stop = threading.Event()
    failures: list[BaseException] = []

    def run() -> None:
        try:
            serve_host(
                launch,
                adapter=adapter,
                request=daemon,
                ready=ready,
                stop=stop,
                on_bound=lambda host, port: bound.append((host, port)),
                request_token=request_token,
            )
        except BaseException as error:  # noqa: BLE001 - surfaced to the test
            failures.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    facade_sessions.append((stop, thread))
    assert ready.wait(timeout=5.0), failures
    host, port = bound[-1]
    return daemon, host, port


def test_local_tcp_serves_the_five_operations_without_an_exposure_call(tmp_path) -> None:
    daemon, host, port = start_local_tcp_host(
        HostLaunch(config_path=tmp_path / "c.json", tcp_port=0)
    )

    assert host == "127.0.0.1"
    assert port != 0, "the ephemeral port is the one readiness must report"
    with TcpFrameClient(host, port) as client:
        client.send({"operation": "capabilities.get", "params": {}})
        assert client.read()["ok"] is True
        client.send({"operation": "sessions.list", "params": {}})
        answer = client.read()
        assert answer["ok"] is True
        assert [row["sessionId"] for row in answer["result"]] == ["s-1"]

    assert daemon.calls == [], (
        "the local arm is not the remote facade: it never calls network.expose"
    )


def test_local_tcp_requires_the_per_launch_request_token(tmp_path) -> None:
    launch = HostLaunch(config_path=tmp_path / "c.json", tcp_port=0)
    _daemon, host, port = start_local_tcp_host(
        launch, request_token="local-capability"
    )

    with TcpFrameClient(host, port) as client:
        client.send({"operation": "sessions.list", "params": {}})
        missing = client.read()
        assert missing["ok"] is False
        assert missing["error"]["code"] == "unauthorized"
        assert missing["error"]["definitive"] is True

        client.send(
            {"operation": "sessions.list", "params": {}, "token": "wrong"}
        )
        assert client.read()["error"]["code"] == "unauthorized"

        client.send(
            {
                "operation": "sessions.list",
                "params": {},
                "token": "local-capability",
            }
        )
        assert client.read()["ok"] is True


def test_a_non_ascii_token_is_refused_without_killing_the_listener(tmp_path) -> None:
    launch = HostLaunch(config_path=tmp_path / "c.json", tcp_port=0)
    _daemon, host, port = start_local_tcp_host(
        launch, request_token="local-capability"
    )

    with TcpFrameClient(host, port) as client:
        # ``hmac.compare_digest`` raises TypeError on non-ASCII str operands;
        # a hostile frame must be answered, never allowed to break the loop.
        client.send(
            {"operation": "sessions.list", "params": {}, "token": "tökén"}
        )
        refused = client.read()
        assert refused["ok"] is False
        assert refused["error"]["code"] == "unauthorized"

        client.send(
            {
                "operation": "sessions.list",
                "params": {},
                "token": "local-capability",
            }
        )
        assert client.read()["ok"] is True, "the listener survived the hostile frame"


def test_the_request_token_is_refused_on_non_local_arms(tmp_path, monkeypatch) -> None:
    unix = HostLaunch(config_path=tmp_path / "c.json", socket_path=tmp_path / "s")
    with pytest.raises(ValueError, match="loopback TCP"):
        serve_host(
            unix,
            adapter=adapter_from_config(CONFIG, FakeDaemon()),
            request=FakeDaemon(),
            request_token="not-for-unix",
        )
    assert not unix.socket_path.exists()

    remote = HostLaunch(
        config_path=tmp_path / "c.json", tcp_port=0, expose_port=7443
    )
    with pytest.raises(ValueError, match="loopback TCP"):
        serve_host(
            remote,
            adapter=adapter_from_config(CONFIG, FakeDaemon()),
            request=FakeDaemon(),
            request_token="not-for-remote",
            auth_gate=make_test_gate(),
        )

    monkeypatch.setenv("HYPRIAL_APP_CORE_REQUEST_TOKEN", "not-for-unix")
    assert (
        run_host(
            unix,
            adapter=adapter_from_config(CONFIG, FakeDaemon()),
            request=FakeDaemon(),
        )
        == 1
    ), "a token on the AF_UNIX arm is a named configuration refusal"


def test_readiness_reports_the_effective_local_tcp_endpoint(tmp_path) -> None:
    launch = HostLaunch(config_path=tmp_path / "c.json", tcp_port=0)
    callback_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    callback_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    callback_listener.bind(("127.0.0.1", 0))
    callback_listener.listen(1)
    callback_listener.settimeout(5.0)
    callback_port = callback_listener.getsockname()[1]

    bound: list[tuple[str, int]] = []
    ready = threading.Event()
    stop = threading.Event()
    adapter = adapter_from_config(CONFIG, FakeDaemon())
    start_readiness_reporter(
        launch,
        ready,
        (callback_port, "readiness-token"),
        max_line_bytes=MAX_REQUEST_LINE_BYTES,
        bound=bound,
    )
    thread = threading.Thread(
        target=serve_tcp,
        args=(0, adapter),
        kwargs={
            "ready": ready,
            "stop": stop,
            "on_bound": lambda host, port: bound.append((host, port)),
        },
    )
    thread.start()
    facade_sessions.append((stop, thread))
    try:
        connection, _ = callback_listener.accept()
        with connection:
            line = connection.makefile("rb").readline()
        message = json.loads(line)
    finally:
        callback_listener.close()

    assert bound and bound[-1][1] != 0
    assert message["token"] == "readiness-token"
    assert message["probe"] == "ok", message
    assert message["socket"] == ""
    assert message["tcpHost"] == "127.0.0.1"
    assert message["tcpPort"] == bound[-1][1]
