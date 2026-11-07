"""Tests for the local adapter host behind the desktop Core bridge.

The host is the composition root's serving face: a real AF_UNIX socket,
newline-delimited JSON in and out, every answer produced by the adapter.
These tests drive it end to end with a fake daemon request port.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import socket
import struct
import tempfile
import threading
from pathlib import Path

import pytest

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.gui.adapter.envelope import (
    ENVELOPE_MISSING,
    ENVELOPE_VERSION,
    REASON_DEVICE_AUTHENTICATION_REQUIRED,
    AuthGate,
    DeviceRow,
    mac_text,
    request_digest,
)
from hyprial.shell.impl.gui.adapter.host import (
    HOST_MODULE,
    HOST_SOCKET_ENV,
    HOST_STOP_SIGNAL,
    MAX_REQUEST_LINE_BYTES,
    HostLaunch,
    adapter_from_config,
    handle_request,
    host_socket_path,
    serve,
    serve_host,
    serve_tcp,
)

SENDER = "agent:chloe.martin@example.com:dev000000001.local:squire"


class FakeDaemon:
    def __init__(self, *answers: object) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._answers = list(answers)

    def __call__(self, method: str, params: dict) -> object:
        self.calls.append((method, dict(params)))
        answer = self._answers.pop(0) if self._answers else {}
        if isinstance(answer, Exception):
            raise answer
        return answer


CONFIG = {
    "sender": SENDER,
    "sessions": {
        "s-1": {
            "targets": ["agent:chloe.martin@example.com:dev000000001.local:mini-gui-worker"],
            "conversationId": "conv-1",
        }
    },
}


@pytest.fixture()
def host_socket():
    """A short AF_UNIX socket path (macOS sun_path is finite) and its server."""

    directory = tempfile.mkdtemp(prefix="apc-", dir="/tmp")
    socket_path = Path(directory) / "s"
    yield directory, socket_path
    shutil.rmtree(directory, ignore_errors=True)


def start_host(host_socket, *answers: object) -> FakeDaemon:
    directory, socket_path = host_socket
    daemon = FakeDaemon(*answers)
    adapter = adapter_from_config(CONFIG, daemon)
    ready = threading.Event()
    stop = threading.Event()
    thread = threading.Thread(
        target=serve, args=(socket_path, adapter), kwargs={"ready": ready, "stop": stop}
    )
    thread.start()
    assert ready.wait(timeout=5.0)
    host_socket_sessions.append((stop, thread))
    return daemon


host_socket_sessions: list[tuple[threading.Event, threading.Thread]] = []


@pytest.fixture(autouse=True)
def stop_hosts():
    """Join every host thread so no socket fd survives into the fd guard."""

    yield
    for stop, thread in host_socket_sessions:
        stop.set()
    for _, thread in host_socket_sessions:
        thread.join(timeout=3.0)
    host_socket_sessions.clear()


def frame(socket_path: Path, request: object) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        payload = (
            request if isinstance(request, str) else json.dumps(request)
        )
        client.sendall(payload.encode() + b"\n")
        return json.loads(client.makefile("rb").readline())


def test_host_serves_list_and_capabilities_over_the_socket(host_socket) -> None:
    directory, socket_path = host_socket
    start_host(host_socket)

    assert frame(socket_path, {"operation": "sessions.list", "params": {}}) == {
        "ok": True,
        "result": [{"sessionId": "s-1"}],
    }
    answer = frame(socket_path, {"operation": "capabilities.get", "params": {}})
    assert answer["ok"] is True
    assert answer["result"]["operations"]["sessions.list"] == {"supported": True}
    assert answer["result"]["operations"]["targets.list"] == {"supported": True}
    assert answer["result"]["operations"]["sessions.create"] == {"supported": True}


def test_host_relays_events_and_send_through_the_adapter(host_socket) -> None:
    directory, socket_path = host_socket
    daemon = start_host(
        host_socket,
        {
            "ok": True,
            "events": [
                {
                    "messageId": "e-1",
                    "conversationId": "conv-1",
                    "phase": "running",
                    "seq": 0,
                    "emittedAtMs": 10,
                    "summary": "working",
                }
            ],
            "daemonEpoch": "epoch-1",
        },
        {
            "ok": True,
            "operationId": "op-1",
            "conversationId": "conv-1",
            "deliveries": [{"target": "agent:t", "accepted": True, "queued": False}],
        },
    )

    events = frame(
        socket_path, {"operation": "sessions.events", "params": {"sessionId": "s-1", "since": 0}}
    )
    assert events == {
        "ok": True,
        "result": {
            "entries": [
                {"id": "e-1", "seq": 0, "state": "running", "at": 10, "summary": "working"}
            ],
            "terminal": None,
        },
    }
    assert daemon.calls[0] == ("progress.list", {"from": SENDER})

    ack = frame(
        socket_path,
        {
            "operation": "sessions.send",
            "params": {"sessionId": "s-1", "request": {"text": "hi", "idempotencyKey": "k-1"}},
        },
    )
    assert ack["ok"] is True
    assert ack["result"]["status"] == "accepted"
    assert daemon.calls[1][0] == "message.send"


def test_host_relays_history_through_the_adapter(host_socket) -> None:
    directory, socket_path = host_socket
    daemon = start_host(
        host_socket,
        {
            "ok": True,
            "conversationId": "conv-1",
            "entries": [],
            "nextCursor": None,
        },
    )

    answer = frame(
        socket_path,
        {
            "operation": "sessions.history",
            "params": {"sessionId": "s-1", "args": {"limit": 5}},
        },
    )

    assert answer == {"ok": True, "result": {"events": [], "nextCursor": None}}
    assert daemon.calls == [
        (
            "message.history",
            {"from": SENDER, "conversationId": "conv-1", "limit": 5},
        )
    ]


@pytest.mark.parametrize(
    "args",
    [
        {"from": "agent:someone-else"},
        {"actor": "agent:someone-else"},
        {"sender": "agent:someone-else"},
        {"onBehalfOf": "agent:someone-else"},
        {"sessionRef": "another-session"},
        {"token": "secret"},
        {"credential": "secret"},
        {"principal": "agent:someone-else"},
        {"conversationId": "conv-other"},
        {"cursor": "c", "unknown": 1},
        [],
    ],
)
def test_host_refuses_history_arguments_the_browser_may_not_choose(
    host_socket, args: object
) -> None:
    directory, socket_path = host_socket
    daemon = start_host(host_socket)

    answer = frame(
        socket_path,
        {
            "operation": "sessions.history",
            "params": {"sessionId": "s-1", "args": args},
        },
    )

    assert answer["ok"] is False
    assert answer["error"]["code"] in {"identity-rejected", "invalid-argument"}
    assert answer["error"]["definitive"] is True
    assert daemon.calls == []


@pytest.mark.parametrize(
    "params",
    [
        {"sessionId": "s-1", "args": {}, "from": "agent:someone-else"},
        {"sessionId": "s-1", "args": {}, "conversationId": "conv-other"},
        {"sessionId": "s-1", "args": {}, "unknown": 1},
    ],
)
def test_host_refuses_unknown_history_envelope_fields(
    host_socket, params: dict
) -> None:
    directory, socket_path = host_socket
    daemon = start_host(host_socket)

    answer = frame(
        socket_path, {"operation": "sessions.history", "params": params}
    )

    assert answer["ok"] is False
    assert answer["error"]["code"] in {"identity-rejected", "invalid-argument"}
    assert answer["error"]["definitive"] is True
    assert daemon.calls == []


def test_host_reports_definitive_adapter_rejections(host_socket) -> None:
    directory, socket_path = host_socket
    start_host(host_socket)

    answer = frame(
        socket_path,
        {"operation": "sessions.send", "params": {"sessionId": "s-x", "request": {}}},
    )

    assert answer["ok"] is False
    assert answer["error"]["code"] == "unknown-session"
    assert answer["error"]["definitive"] is True


def test_host_reports_daemon_unavailability_as_not_definitive(host_socket) -> None:
    directory, socket_path = host_socket
    start_host(host_socket, ipc_errors.DaemonUnavailableError("daemon is starting"))

    answer = frame(
        socket_path,
        {"operation": "sessions.events", "params": {"sessionId": "s-1"}},
    )

    assert answer["ok"] is False
    assert answer["error"]["definitive"] is False


def test_host_refuses_garbage_lines_and_unknown_operations(host_socket) -> None:
    directory, socket_path = host_socket
    start_host(host_socket)

    garbage = frame(socket_path, "not json at all\n")
    assert garbage["ok"] is False
    assert garbage["error"]["code"] == "invalid-argument"

    unknown = frame(socket_path, {"operation": "sessions.delete", "params": {}})
    assert unknown["ok"] is False
    assert unknown["error"]["code"] == "unsupported-operation"
    assert unknown["error"]["definitive"] is True


def test_host_keeps_serving_after_a_client_resets_the_connection(host_socket) -> None:
    directory, socket_path = host_socket
    start_host(host_socket)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.sendall(b'{"operation": "sessions.list", "params": {}}\n')
    # SO_LINGER(0) makes close send RST: the host reads ECONNRESET instead of
    # a clean EOF. One aborting client must not take the host down.
    client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    client.close()

    answer = frame(socket_path, {"operation": "sessions.list", "params": {}})
    assert answer["ok"] is True
    assert answer["result"] == [{"sessionId": "s-1"}]


def test_host_answers_are_frames_even_for_direct_calls() -> None:
    adapter = adapter_from_config(CONFIG, FakeDaemon())

    assert handle_request(adapter, "not-an-object")["ok"] is False
    assert handle_request(adapter, {"operation": "sessions.list"})["ok"] is True


def test_adapter_from_config_validates_the_composition_root_contract() -> None:
    with pytest.raises(ValueError):
        adapter_from_config({"sessions": {}}, FakeDaemon())
    with pytest.raises(ValueError):
        adapter_from_config({"sender": SENDER}, FakeDaemon())
    with pytest.raises(ValueError):
        adapter_from_config(
            {"sender": SENDER, "sessions": {"s-1": {"targets": [], "conversationId": "c"}}},
            FakeDaemon(),
        )
    with pytest.raises(ValueError):
        adapter_from_config(
            {"sender": SENDER, "sessions": {"s-1": {"targets": ["t"]}}},
            FakeDaemon(),
        )


# --- loopback TCP facade ----------------------------------------------------


def start_tcp_host(*answers: object, idle_timeout: float = 30.0):
    """A real ``serve_tcp`` listener on an ephemeral loopback port."""

    daemon = FakeDaemon(*answers)
    adapter = adapter_from_config(CONFIG, daemon)
    bound: list[tuple[str, int]] = []
    ready = threading.Event()
    stop = threading.Event()
    thread = threading.Thread(
        target=serve_tcp,
        args=(0, adapter),
        kwargs={
            "ready": ready,
            "stop": stop,
            "on_bound": lambda host, port: bound.append((host, port)),
            "idle_timeout": idle_timeout,
        },
    )
    thread.start()
    assert ready.wait(timeout=5.0)
    # The same autouse fixture drains both listener kinds; each entry is a
    # stop event plus the thread that must join before the fd guard runs.
    host_socket_sessions.append((stop, thread))
    address, port = bound[0]
    return daemon, address, port


class TcpFrameClient:
    """One facade connection: bounded reads, exact bytes out."""

    def __init__(self, address: str, port: int) -> None:
        self.socket = socket.create_connection((address, port), timeout=5.0)
        self.reader = self.socket.makefile("rb")

    def send(self, request: object) -> None:
        payload = request if isinstance(request, str) else json.dumps(request)
        self.socket.sendall(payload.encode() + b"\n")

    def send_raw(self, payload: bytes) -> None:
        self.socket.sendall(payload)

    def read(self) -> dict:
        return json.loads(self.reader.readline())

    def read_eof(self) -> bool:
        return self.reader.readline() == b""

    def close(self) -> None:
        # The makefile owns an io ref, so closing the socket alone defers the
        # real close until GC; the peer would never see the FIN.
        self.reader.close()
        self.socket.close()

    def __enter__(self) -> "TcpFrameClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def test_an_occupied_tcp_port_is_a_named_refusal_that_leaves_the_occupant_alone() -> None:
    """A bind conflict fails closed: no kill, no silent port change."""

    occupant = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupant.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    occupant.bind(("127.0.0.1", 0))
    occupant.listen(1)
    port = occupant.getsockname()[1]
    adapter = adapter_from_config(CONFIG, FakeDaemon())
    try:
        with pytest.raises(RuntimeError, match="address already in use"):
            serve_tcp(port, adapter)
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.settimeout(2.0)
            assert probe.connect_ex(("127.0.0.1", port)) == 0, (
                "the occupant must keep its listener"
            )
        finally:
            probe.close()
    finally:
        occupant.close()


def test_tcp_facade_binds_loopback_and_serves_the_five_operations() -> None:
    daemon, address, port = start_tcp_host(
        {
            "ok": True,
            "conversationId": "conv-1",
            "entries": [],
            "nextCursor": None,
        },
        {
            "ok": True,
            "operationId": "op-1",
            "conversationId": "conv-1",
            "deliveries": [
                {"target": "agent:t", "messageId": "m-1", "accepted": True, "queued": False}
            ],
            "messageId": "m-1",
        },
        {"ok": True, "events": [], "daemonEpoch": "e"},
    )
    assert address == "127.0.0.1", "the facade must bind loopback only"

    with TcpFrameClient(address, port) as client:
        client.send({"operation": "capabilities.get", "params": {}})
        capabilities = client.read()
        assert capabilities["ok"] is True
        assert capabilities["result"]["operations"]["sessions.history"] == {"supported": True}

        client.send({"operation": "sessions.list", "params": {}})
        assert client.read() == {"ok": True, "result": [{"sessionId": "s-1"}]}

        client.send(
            {
                "operation": "sessions.history",
                "params": {"sessionId": "s-1", "args": {"limit": 5}},
            }
        )
        assert client.read() == {"ok": True, "result": {"events": [], "nextCursor": None}}

        client.send(
            {
                "operation": "sessions.send",
                "params": {"sessionId": "s-1", "request": {"text": "hi", "idempotencyKey": "k-1"}},
            }
        )
        assert client.read()["result"]["status"] == "accepted"

        client.send(
            {"operation": "sessions.events", "params": {"sessionId": "s-1", "since": 0}}
        )
        assert client.read() == {"ok": True, "result": {"entries": [], "terminal": None}}

    # The projection touched the daemon through the same callable the desktop
    # path uses; nothing on this transport bypasses the adapter.
    assert daemon.calls[0] == (
        "message.history",
        {"from": SENDER, "conversationId": "conv-1", "limit": 5},
    )


def test_tcp_facade_maps_errors_and_survives_malformed_lines() -> None:
    _daemon, address, port = start_tcp_host(
        ipc_errors.DaemonUnavailableError("daemon is starting")
    )

    with TcpFrameClient(address, port) as client:
        client.send("not json at all")
        malformed = client.read()
        assert malformed["error"]["code"] == "invalid-argument"
        assert malformed["error"]["definitive"] is True

        client.send(
            {
                "operation": "sessions.send",
                "params": {"sessionId": "s-1", "request": {"text": "hi", "idempotencyKey": "k-1"}},
            }
        )
        unavailable = client.read()
        assert unavailable["error"]["code"] == "unavailable"
        assert unavailable["error"]["definitive"] is False

        client.send(
            {"operation": "sessions.send", "params": {"sessionId": "s-x", "request": {}}}
        )
        rejected = client.read()
        assert rejected["error"]["code"] == "unknown-session"
        assert rejected["error"]["definitive"] is True

        client.send({"operation": "sessions.delete", "params": {}})
        unknown = client.read()
        assert unknown["error"]["code"] == "unsupported-operation"
        assert unknown["error"]["definitive"] is True

        # The same connection kept its alignment across every failure class.
        client.send({"operation": "sessions.list", "params": {}})
        assert client.read()["ok"] is True


def test_tcp_facade_refuses_an_oversize_line_and_closes_the_connection() -> None:
    _daemon, address, port = start_tcp_host()

    with TcpFrameClient(address, port) as client:
        client.send_raw(b"x" * (MAX_REQUEST_LINE_BYTES + 1))
        refusal = client.read()
        assert refusal["error"]["code"] == "invalid-argument"
        assert refusal["error"]["definitive"] is True
        assert client.read_eof(), "an oversize stream must be closed, not re-aligned"


def test_tcp_facade_closes_an_idle_connection_after_the_timeout() -> None:
    _daemon, address, port = start_tcp_host(idle_timeout=0.4)

    with TcpFrameClient(address, port) as client:
        assert client.read_eof(), "a silent connection must not hold the slot forever"


def test_tcp_facade_keeps_serving_after_a_mid_line_disconnect() -> None:
    # A short idle timeout bounds how long the half-line connection may hold
    # the listener, exactly the frozen semantic under test.
    _daemon, address, port = start_tcp_host(idle_timeout=0.5)

    broken = TcpFrameClient(address, port)
    broken.send_raw(b'{"operation": "sess')
    broken.socket.shutdown(socket.SHUT_WR)
    broken.close()

    with TcpFrameClient(address, port) as client:
        client.send({"operation": "capabilities.get", "params": {}})
        assert client.read()["ok"] is True


@pytest.mark.parametrize("port", [-1, 65536, 1.5, True])
def test_tcp_facade_refuses_an_invalid_port(port: object) -> None:
    adapter = adapter_from_config(CONFIG, FakeDaemon())

    with pytest.raises(ValueError):
        serve_tcp(port, adapter)


def test_tcp_facade_refuses_a_non_positive_idle_timeout() -> None:
    adapter = adapter_from_config(CONFIG, FakeDaemon())

    with pytest.raises(ValueError):
        serve_tcp(0, adapter, idle_timeout=0)


def test_tcp_facade_cli_requires_exactly_one_listener() -> None:
    from hyprial.shell.impl.gui.adapter.host import main as host_main

    with pytest.raises(SystemExit):
        host_main(["--config", "unused.json"])
    with pytest.raises(SystemExit):
        host_main(["--socket", "/tmp/x.sock", "--tcp-port", "0", "--config", "unused.json"])


# --- composition-root conventions (state dir, single instance, cleanup) -----


def test_a_live_host_is_never_stolen_by_a_second_start(host_socket) -> None:
    directory, socket_path = host_socket
    start_host(host_socket)

    with pytest.raises(RuntimeError, match="already serving"):
        serve(socket_path, adapter_from_config(CONFIG, FakeDaemon()))

    # The live host still answers: its socket was neither unlinked nor rebound.
    assert frame(socket_path, {"operation": "sessions.list", "params": {}}) == {
        "ok": True,
        "result": [{"sessionId": "s-1"}],
    }


def test_a_stale_socket_file_is_replaced(host_socket) -> None:
    directory, socket_path = host_socket
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_path))
    stale.close()  # the file remains; nothing answers on it

    start_host(host_socket)

    assert frame(socket_path, {"operation": "capabilities.get", "params": {}})[
        "ok"
    ] is True


def test_a_clean_stop_removes_the_socket(host_socket) -> None:
    directory, socket_path = host_socket
    start_host(host_socket)
    stop, thread = host_socket_sessions[-1]
    stop.set()
    thread.join(timeout=3.0)

    assert not socket_path.exists(), "a clean stop must leave no socket behind"


def test_state_dir_convention_and_cli_validation(tmp_path) -> None:
    from hyprial.shell.impl.gui.adapter.host import main as host_main

    state_dir = tmp_path / "state"
    assert host_socket_path(state_dir) == state_dir / "app-core-host.sock"

    for argv in (
        ["--socket", "/tmp/a.sock", "--state-dir", str(tmp_path), "--config", "x.json"],
        ["--state-dir", str(tmp_path), "--tcp-port", "0", "--config", "x.json"],
        ["--socket", "/tmp/a.sock", "--state-dir", str(tmp_path), "--tcp-port", "0", "--config", "x.json"],
        ["--config", "x.json"],
    ):
        with pytest.raises(SystemExit):
            host_main(argv)


# --- ownership-neutral launch seam ------------------------------------------
#
# The seam describes what a host needs and where a consumer connects; it
# deliberately does not spawn anything or decide who owns the process.  A
# future launcher (desktop shell, ops script, e2e harness) supplies the
# interpreter prefix and decides when to start/stop; this value object only
# pins the command, environment, ports and lifecycle facts the host already
# serves.


def test_host_launch_builds_the_exact_cli_for_each_listener(tmp_path) -> None:
    config = tmp_path / "config.json"
    socket_path = tmp_path / "host.sock"
    state_dir = tmp_path / "state"

    desktop = HostLaunch(config_path=config, socket_path=socket_path)
    assert desktop.argv(python="/opt/python") == [
        "/opt/python",
        "-m",
        HOST_MODULE,
        "--socket",
        str(socket_path),
        "--config",
        str(config),
    ]
    assert desktop.resolved_socket_path == socket_path
    assert desktop.consumer_environment() == {HOST_SOCKET_ENV: str(socket_path)}
    assert desktop.readiness_target == socket_path
    assert not desktop.remote

    stateful = HostLaunch(config_path=config, state_dir=state_dir)
    assert stateful.argv(python="py") == [
        "py",
        "-m",
        HOST_MODULE,
        "--state-dir",
        str(state_dir),
        "--config",
        str(config),
    ]
    assert stateful.resolved_socket_path == state_dir / "app-core-host.sock"
    assert stateful.consumer_environment() == {
        HOST_SOCKET_ENV: str(state_dir / "app-core-host.sock")
    }

    remote = HostLaunch(config_path=config, tcp_port=0, expose_port=7443)
    assert remote.argv(python="py") == [
        "py",
        "-m",
        HOST_MODULE,
        "--tcp-port",
        "0",
        "--expose-port",
        "7443",
        "--config",
        str(config),
    ]
    # The remote consumer's address/port source is decision D9-②; the seam
    # deliberately does not invent one.
    assert remote.consumer_environment() == {}
    assert remote.readiness_target == ("127.0.0.1", 0)
    assert remote.remote
    assert not remote.local_tcp

    # The Windows local arm: loopback TCP only, no exposure argument at all.
    local = HostLaunch(config_path=config, tcp_port=0)
    assert local.argv(python="py") == [
        "py",
        "-m",
        HOST_MODULE,
        "--tcp-port",
        "0",
        "--config",
        str(config),
    ]
    assert local.consumer_environment() == {}
    assert local.readiness_target == ("127.0.0.1", 0)
    assert local.local_tcp
    assert not local.remote


def test_host_launch_hands_the_child_environment_through_unchanged(tmp_path) -> None:
    launch = HostLaunch(
        config_path=tmp_path / "c.json",
        socket_path=tmp_path / "s",
        environment={"HYPRIAL_HOME": "/isolated"},
    )

    assert launch.child_environment() == {"HYPRIAL_HOME": "/isolated"}
    assert launch.child_environment({"PATH": "/bin", "HYPRIAL_HOME": "/wrong"}) == {
        "PATH": "/bin",
        "HYPRIAL_HOME": "/isolated",
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {},  # no listener at all
        {"socket_path": Path("/tmp/a.sock"), "state_dir": Path("/tmp/state")},
        {"socket_path": Path("/tmp/a.sock"), "tcp_port": 0},
        {"state_dir": Path("/tmp/state"), "tcp_port": 0},
        {"expose_port": 7443},  # exposure without a remote listener
        {"tcp_port": 0, "expose_port": 0},
        {"tcp_port": 0, "expose_port": 65536},
        {"tcp_port": 65536, "expose_port": 7443},
        {"tcp_port": True, "expose_port": 7443},
    ],
)
def test_host_launch_refuses_an_incomplete_listener_contract(tmp_path, kwargs) -> None:
    with pytest.raises(ValueError):
        HostLaunch(config_path=tmp_path / "c.json", **kwargs)


def test_host_lifecycle_is_stop_by_sigterm_and_connect_readiness(tmp_path) -> None:
    # The lifecycle facts a launcher can rely on today: SIGTERM is the stop
    # request the host adopts on the remote arm; readiness is a connect probe
    # at the target above (socket for the desktop arm, loopback for remote).
    assert HOST_STOP_SIGNAL == "SIGTERM"

    launch = HostLaunch(config_path=tmp_path / "c.json", socket_path=tmp_path / "s")
    assert launch.readiness_target == launch.resolved_socket_path


# --- the N-A2 gate is an explicit, default-off composition seam -------------

GATE_OWNER = "agent:chloe.martin@example.com:dev000000001.local:mini-gui-worker"
GATE_PEER = "fixture-client-public"
GATE_KEY = b"\x05" * 32
GATE_NOW = 1_000_000
GATE_NODE = "node:dev000000001.local"


def make_test_gate() -> AuthGate:
    return AuthGate(
        derive=lambda _role, _peer: GATE_KEY,
        device_row={"dev-1": DeviceRow(owner=GATE_OWNER, client_public=GATE_PEER)}.get,
        target_node_id=GATE_NODE,
        clock=lambda: float(GATE_NOW),
    )


def signed_test_frame(
    *,
    nonce: str,
    operation: str = "capabilities.get",
    params: dict | None = None,
) -> dict:
    body: dict = {"operation": operation, "params": dict(params or {})}
    session_id = body["params"].get("sessionId")
    space = session_id if isinstance(session_id, str) and session_id else ""
    digest = request_digest(body)
    mac = hmac.new(
        GATE_KEY,
        mac_text(
            ENVELOPE_VERSION,
            GATE_NODE,
            1,
            GATE_NOW,
            nonce,
            space,
            operation,
            digest,
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    body["auth"] = {
        "version": ENVELOPE_VERSION,
        "deviceId": "dev-1",
        "deviceGeneration": 1,
        "targetNodeId": GATE_NODE,
        "ts": GATE_NOW,
        "nonce": nonce,
        "mac": mac,
    }
    return body


class ExposeRecorder:
    """The daemon-request port of a composed remote facade, recording calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, method: str, params: dict) -> object:
        self.calls.append((method, dict(params)))
        if method == "network.expose":
            return {"exposure": dict(params)}
        return {"port": params.get("port"), "removed": True}


def start_launched_host(launch: HostLaunch, *, auth_gate: AuthGate | None = None):
    """One real ``serve_host`` on an ephemeral port, with a recording port."""

    expose = ExposeRecorder()
    adapter = adapter_from_config(CONFIG, FakeDaemon())
    ready = threading.Event()
    stop = threading.Event()
    failures: list[BaseException] = []

    def run() -> None:
        try:
            serve_host(
                launch,
                adapter=adapter,
                request=expose,
                ready=ready,
                stop=stop,
                auth_gate=auth_gate,
            )
        except BaseException as error:  # noqa: BLE001 - surfaced to the test
            failures.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    host_socket_sessions.append((stop, thread))
    assert ready.wait(timeout=5.0), failures
    method, params = expose.calls[0]
    assert method == "network.expose"
    host, _, port = params["target"].removeprefix("tcp:").rpartition(":")
    return host, int(port)


def test_serve_host_refuses_the_remote_arm_without_a_gate(tmp_path) -> None:
    launch = HostLaunch(config_path=tmp_path / "c.json", tcp_port=0, expose_port=7443)
    recorder = ExposeRecorder()

    with pytest.raises(ValueError, match="auth gate"):
        serve_host(
            launch,
            adapter=adapter_from_config(CONFIG, FakeDaemon()),
            request=recorder,
        )

    assert recorder.calls == [], "the C1 entrance never exposes unauthenticated"


def test_serve_host_applies_an_explicitly_injected_gate(tmp_path) -> None:
    launch = HostLaunch(config_path=tmp_path / "c.json", tcp_port=0, expose_port=7443)
    address, port = start_launched_host(launch, auth_gate=make_test_gate())

    with TcpFrameClient(address, port) as client:
        client.send({"operation": "capabilities.get", "params": {}})
        unsigned = client.read()
        assert unsigned["ok"] is False
        assert unsigned["error"]["code"] == ENVELOPE_MISSING
        assert (
            unsigned["error"]["details"]["reason"]
            == REASON_DEVICE_AUTHENTICATION_REQUIRED
        )

        client.send(signed_test_frame(nonce="launch-gate-1"))
        assert client.read()["ok"] is True


def test_serve_host_refuses_a_gate_on_the_desktop_unix_path(tmp_path) -> None:
    launch = HostLaunch(config_path=tmp_path / "c.json", socket_path=tmp_path / "s")

    with pytest.raises(ValueError, match="AF_UNIX"):
        serve_host(
            launch,
            adapter=adapter_from_config(CONFIG, FakeDaemon()),
            request=FakeDaemon(),
            auth_gate=make_test_gate(),
        )
    assert not launch.socket_path.exists(), "a refused composition binds nothing"


def test_main_composes_without_a_gate_by_default(tmp_path, monkeypatch) -> None:
    from hyprial.shell.impl.gui.adapter import host as host_module

    observed: dict[str, object] = {}

    def composing_stub(launch, **kwargs) -> None:
        observed["gate"] = kwargs.get("auth_gate")

    monkeypatch.setattr(host_module, "serve_host", composing_stub)
    monkeypatch.setattr(host_module, "daemon_request_callable", lambda: FakeDaemon())
    config = tmp_path / "config.json"
    config.write_text(json.dumps(CONFIG), encoding="utf-8")

    code = host_module.main(
        ["--socket", str(tmp_path / "host.sock"), "--config", str(config)]
    )

    assert code == 0
    assert observed["gate"] is None, (
        "the CLI composition must never enable the gate implicitly; there is "
        "no user-visible gate switch"
    )
