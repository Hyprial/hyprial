"""Real Unix clients, daemon dispatch and SQLite inbox behind a scoped relay.

Supervisor projection and mesh transport are synthetic; no worker/model runs.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import socketserver
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path

import anyio
import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from hyprial.kernel import WorkerBinding
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.worker_relay import BoundWorkerRelay
from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.memory import MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.service import InboxService
from hyprial.daemon.impl.mcp import create_mcp_server
from tests.support.links.gate import PermissiveLinkGate
from hyprial.daemon.impl.mcp.proxy import StatelessDaemonProxy
from hyprial.daemon.impl.mcp.unix import UnixDaemonConnectionFactory
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

ACTOR = "agent:test:isolated:alice"
OTHER = "agent:test:isolated:bob"


class SupervisorProjection:
    def __init__(self):
        self.refs = {
            ("codex", "alice"): "alice-session",
            ("codex", "bob"): "bob-session",
        }

    def projected_worker_session_refs(self):
        return self.refs.copy()

    def status(self):
        return ()


class Presence:
    def list(self):
        return ()

    def actor_online(self, actor):
        return False


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch):
    root = Path(
        tempfile.mkdtemp(
            prefix="wrelay-",
            dir="/private/tmp" if os.uname().sysname == "Darwin" else "/tmp",
        )
    ).resolve()
    user_home = root / "user-home"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("HYPRIAL_HOME", str(root / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(root / "harness-state"))
    monkeypatch.setenv("HARNESS_SOCKET_PATH", str(root / "isolated.sock"))
    monkeypatch.setenv("HYPRIAL_NETWORK_ISOLATED", "1")
    app = DaemonApplication(
        state_dir=root / "state",
        hyprial_home=root / "home",
        socket_path=root / "daemon.sock",
        node_id="isolated",
        owner="test",
    **_deep_module_daemon_dependencies())
    # The batch read runs link admission (design §3.3); this fixture's sender
    # and recipient are both canonical agent URIs, so bind the permissive gate
    # the durable batch fixtures use instead of the fail-closed default.
    app.link_admission.bind(PermissiveLinkGate())
    app._inbox = InboxService(
        root / "inbox.sqlite",
        MemoryDeliveryTransport(),
        link_admission=app.link_admission,
    )
    app._harnesses = SupervisorProjection()
    app._presence = Presence()
    for name in ("alice", "bob"):
        app.agents.create(name)
        app.desired_state.upsert_harness(
            HarnessLaunchSpec(
                harness="codex",
                name=name,
                headless=True,
                session_ref=name + "-session",
            )
        )

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(2)
            app._serve_connection(self.request)

    server = socketserver.ThreadingUnixStreamServer(str(app.socket_path), Handler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    binding = WorkerBinding(ACTOR, "alice-session", app.epoch)
    try:
        with BoundWorkerRelay(
            directory=root / "relay",
            daemon_socket=app.socket_path,
            binding=binding,
            timeout=0.5,
        ) as relay:
            yield root, app, relay, binding
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        app._inbox.close()
        app._agent_session_domains.close()
        app.agents.close()
        shutil.rmtree(root)


def frame(method="identity.whoami", params=None, request_id="test-id"):
    return {"version": 1, "id": request_id, "method": method, "params": params or {}}


def request(path, document):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(str(path))
        client.sendall(json.dumps(document).encode() + b"\n")
        with client.makefile("rb") as stream:
            return json.loads(stream.readline())


def message(recipient, message_id):
    return InboxMessage(
        message_id=message_id,
        conversation_id="fixture",
        sender=OTHER,
        recipient=recipient,
        payload=b'{"message":"private"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=int(time.time() * 1000),
    )


def test_identity_and_inbox_remain_bound(isolated):
    _, app, relay, _ = isolated
    app._inbox.receive(message(ACTOR, "a"))
    app._inbox.receive(message(OTHER, "b"))
    identity = request(relay.socket_path, frame())["result"]
    assert identity["actor"] == ACTOR
    assert identity["sessionRegistered"] is True
    result = request(
        relay.socket_path, frame("message.pending.list", {"fetched": True})
    )
    assert [m["messageId"] for m in result["result"]["messages"]] == ["a"]
    assert app._inbox.consumption_state("b").value == "pending"


@pytest.mark.parametrize(
    "method,params,code",
    [
        ("identity.whoami", {"actor": OTHER}, "CALLER_NOT_AUTHORIZED"),
        ("identity.whoami", {"sessionRef": "bob-session"}, "CALLER_NOT_AUTHORIZED"),
        ("message.pending.list", {"recipient": OTHER}, "INVALID_ARGUMENT"),
        (
            "message.send",
            {"to": [OTHER], "message": "x", "from": OTHER},
            "INVALID_ARGUMENT",
        ),
        (
            "message.send",
            {"to": [OTHER], "message": "x", "providerMetadata": {"senderId": "fake"}},
            "INVALID_ARGUMENT",
        ),
        (
            "message.send",
            {"to": [OTHER], "message": "x", "resourcePaths": ["/host/secret"]},
            "INVALID_ARGUMENT",
        ),
        (
            "message.send",
            {"to": [OTHER], "message": "x", "idempotencyKey": "forged"},
            "INVALID_ARGUMENT",
        ),
        ("orgfs.read", {"node": "/host/secret"}, "METHOD_NOT_FOUND"),
        ("secrets.list", {}, "METHOD_NOT_FOUND"),
        ("session.register", {}, "METHOD_NOT_FOUND"),
        ("lifecycle.start", {}, "METHOD_NOT_FOUND"),
        ("message.delegate", {}, "METHOD_NOT_FOUND"),
        ("worker.channel.request", {}, "METHOD_NOT_FOUND"),
    ],
)
def test_rejects_before_business_operation(isolated, method, params, code):
    _, app, relay, binding = isolated
    before = app._inbox.outbox_count()
    result = request(relay.socket_path, frame(method, params))
    assert result["error"]["code"] == code
    # The daemon repeats the same checks even if a defective relay omitted them.
    direct = request(
        app.socket_path,
        frame("worker.channel.request", binding.envelope(frame(method, params))),
    )
    assert direct["error"]["code"] == code
    assert app._inbox.outbox_count() == before
    assert "/host/secret" not in json.dumps(result)


@pytest.mark.parametrize("change", ["epoch", "session", "removed", "interactive_only"])
def test_expired_binding_never_renews(isolated, change):
    _, app, relay, binding = isolated
    if change == "epoch":
        app.epoch = "replacement-daemon"
    elif change == "session":
        app._harnesses.refs[("codex", "alice")] = "new-session"
    else:
        app._harnesses.refs.pop(("codex", "alice"))
        if change == "interactive_only":
            app.handle(
                "session.register",
                {
                    "actor": ACTOR,
                    "sessionRef": binding.session_ref,
                    "cwd": str(app.hyprial_home),
                    "command": ["codex"],
                    "source": "codex-app-server",
                },
            )
    result = request(relay.socket_path, frame())
    assert result["error"]["code"] == "STALE_SESSION"
    sent = request(
        relay.socket_path, frame("message.send", {"to": [OTHER], "message": "x"})
    )
    assert sent["error"]["code"] == "STALE_SESSION"
    assert app._inbox.outbox_count() == 0


def test_send_retry_and_cross_worker_id_namespace(isolated):
    root, app, relay, binding = isolated
    sent = frame("message.send", {"to": [OTHER], "summary": "hello", "message": "hello"})
    reply = request(relay.socket_path, sent)
    assert "result" in reply, request(
        app.socket_path, frame("worker.channel.request", binding.envelope(sent))
    )
    first = reply["result"]
    second = request(relay.socket_path, sent)["result"]
    assert first["messageId"] == second["messageId"]
    assert app._inbox.outbox_count() == 1
    row = app._inbox.outbox_item(first["messageId"])
    assert row.message.sender == ACTOR
    with BoundWorkerRelay(
        directory=root / "other",
        daemon_socket=app.socket_path,
        binding=replace(binding, actor=OTHER, session_ref="bob-session"),
    ) as other:
        third = request(other.socket_path, sent)["result"]
    assert third["messageId"] != first["messageId"]
    assert app._inbox.outbox_count() == 2
    assert (
        request(
            relay.socket_path,
            frame("message.send", {"to": [OTHER], "summary": "x", "message": "x"}, None),
        )["error"]["code"]
        == "INVALID_REQUEST"
    )



@pytest.fixture
def enforced_isolated(monkeypatch, request):
    monkeypatch.setenv("HYPRIAL_SENDER_AUTH_MODE", "enforce")
    return request.getfixturevalue("isolated")


def test_enforced_sender_auth_preserves_scoped_relay_identity_and_retry(enforced_isolated):
    """The smolvm relay's fenced sender must survive the sender-auth rollout."""
    _, app, relay, binding = enforced_isolated
    assert app._sender_auth_enforced is True
    sent = frame("message.send", {"to": [OTHER], "summary": "relay-auth", "message": "relay-auth"})
    first = request(relay.socket_path, sent)["result"]
    assert request(relay.socket_path, sent)["result"]["messageId"] == first["messageId"]
    assert app._inbox.outbox_count() == 1
    row = app._inbox.outbox_item(first["messageId"])
    assert row.message.sender == ACTOR
    assert json.loads(row.message.payload)["origin"] == {"via": "session"}
    assert request(relay.socket_path, frame("message.send", {
        "to": [OTHER], "summary": "spoof", "message": "spoof", "onBehalfOf": OTHER,
    }))["error"]["code"] == "INVALID_ARGUMENT"
    app._harnesses.refs[("codex", "alice")] = "replacement-session"
    assert request(relay.socket_path, sent)["error"]["code"] == "STALE_SESSION"
    assert app._inbox.outbox_count() == 1

def test_python_and_typescript_clients_use_same_socket(isolated, monkeypatch):
    root, _, relay, binding = isolated
    monkeypatch.setenv("HARNESS_STATE_DIR", str(relay.directory))
    monkeypatch.setenv("HYPRIAL_HOME", str(root / "wrong-home"))
    monkeypatch.setenv("HARNESS_SOCKET_PATH", str(root / "wrong.sock"))
    assert _cli_get_services()._socket_path() == relay.socket_path

    async def call():
        return await StatelessDaemonProxy(
            UnixDaemonConnectionFactory(_cli_get_services()._socket_path())
        ).call(
            actor=binding.actor,
            session_ref=binding.session_ref,
            method="identity.whoami",
            params={},
            mutation=False,
        )

    assert anyio.run(call)["actor"] == binding.actor
    if shutil.which("node") is None:
        pytest.skip("Node unavailable: actual TS carrier NOT_RUN")
    version = subprocess.check_output(["node", "--version"], text=True).strip()
    major, minor = [int(part) for part in version.lstrip("v").split(".")[:2]]
    if (major, minor) < (22, 6):
        pytest.skip("Node lacks native TypeScript stripping: actual TS carrier NOT_RUN")
    source = (
        Path(__file__).resolve().parents[3] / 'src/hyprial/daemon/impl/harnesses/pi/resources/pi_daemon_ipc.ts'
    )
    script = f"import {{daemonSocketPath,callDaemon}} from {json.dumps(source.as_uri())}; console.log(JSON.stringify({{socket:daemonSocketPath(), result:await callDaemon('identity.whoami',{{actor:{json.dumps(binding.actor)},sessionRef:{json.dumps(binding.session_ref)}}},false)}}));"
    result = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["socket"] == str(relay.socket_path)
    assert parsed["result"]["actor"] == binding.actor


def test_half_frame_deadline_and_close_revoke_connections(isolated):
    _, _, relay, _ = isolated
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2)
    client.connect(str(relay.socket_path))
    client.sendall(b'{"version":')
    assert json.loads(client.recv(4096))["error"]["code"] == "DAEMON_UNAVAILABLE"
    client.close()
    assert request(relay.socket_path, frame())["result"]["actor"] == ACTOR
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as old:
        old.connect(str(relay.socket_path))
        old.sendall(b"{")
        relay.close()
    assert not relay.socket_path.exists()
    assert not relay._threads
    with pytest.raises(RuntimeError):
        relay.start()


def test_limit_and_unknown_payloads_leave_channel_usable(isolated, monkeypatch):
    _, _, relay, _ = isolated
    import hyprial.daemon.impl.harnesses.turn_delivery.channel.worker_relay as module

    monkeypatch.setattr(module, "MAX_FRAME_BYTES", 4096)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.connect(str(relay.socket_path))
        peer.sendall(b"x" * 4097)
        assert json.loads(peer.recv(4096))["error"]["code"] == "PAYLOAD_TOO_LARGE"
    for value in (
        [],
        None,
        {**frame(), "version": True},
        {**frame(), "id": []},
        {**frame(), "params": []},
        {**frame(), "extra": 1},
    ):
        assert "error" in request(relay.socket_path, value)
    assert request(relay.socket_path, frame())["result"]["actor"] == ACTOR


def test_worker_mcp_read_crosses_bound_relay_and_returns_daemon_batch(isolated):
    _, app, relay, binding = isolated
    app._inbox.receive(message(ACTOR, "batch-private"))
    server = create_mcp_server(
        StatelessDaemonProxy(UnixDaemonConnectionFactory(relay.socket_path)),
        actor=binding.actor,
        session_ref=binding.session_ref,
    )

    async def call() -> dict[str, object]:
        async with (
            InMemoryTransport(server) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as client,
        ):
            await client.initialize()
            result = await client.call_tool("harness_read", {})
        assert result.is_error is not True
        assert isinstance(result.structured_content, dict)
        return result.structured_content

    payload = anyio.run(call)

    assert payload["format"] == "inbox-batch-v1"
    assert payload["itemCount"] == 1
    assert payload["items"][0]["messageId"] == "batch-private"  # type: ignore[index]
    delivery = payload["delivery"]
    assert isinstance(delivery, dict)
    assert delivery["text"].startswith("[Harness Network inbox batch")
    assert "private" in delivery["text"]


def test_existing_paths_permissions_and_no_unrelated_cleanup(isolated):
    root, _, relay, binding = isolated
    assert stat.S_IMODE(relay.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(relay.socket_path.stat().st_mode) == 0o600
    existing = root / "existing"
    existing.mkdir()
    for path in (existing, root / "link"):
        if path.name == "link":
            path.symlink_to(existing, target_is_directory=True)
        with pytest.raises(FileExistsError):
            BoundWorkerRelay(
                directory=path, daemon_socket=relay.daemon_socket, binding=binding
            ).start()
        assert existing.is_dir()
    preserved = relay.directory / "unrelated"
    preserved.write_text("keep")
    relay.close()
    assert preserved.read_text() == "keep"


def test_daemon_errors_do_not_disclose_paths(isolated):
    _, app, relay, _ = isolated
    # A valid method with invalid business data reaches the real daemon; its
    # diagnostic may contain this string, but the guest only receives a code.
    result = request(
        relay.socket_path,
        frame("message.reply", {"messageId": "/private/host/secret", "message": "x"}),
    )
    assert "error" in result
    assert "/private/host/secret" not in json.dumps(result)


def test_cannot_ack_or_reply_to_other_workers_message(isolated):
    _, app, relay, _ = isolated
    app._inbox.receive(message(OTHER, "other-private"))
    for method, params in (
        ("message.ack", {"messageId": "other-private"}),
        ("message.reply", {"messageId": "other-private", "message": "forged"}),
    ):
        result = request(relay.socket_path, frame(method, params))
        assert "error" in result or result["result"].get("ok") is False
    assert app._inbox.consumption_state("other-private").value == "pending"
    assert app._inbox.outbox_count() == 0


def test_unreachable_daemon_has_no_ambient_fallback(isolated, monkeypatch):
    root, app, relay, _ = isolated
    monkeypatch.setenv("HARNESS_SOCKET_PATH", str(app.socket_path))
    relay.daemon_socket = root / "missing.sock"
    result = request(relay.socket_path, frame())
    assert result["error"]["code"] == "DAEMON_UNAVAILABLE"
    assert str(root) not in json.dumps(result)


def test_busy_relay_bounds_admission_and_recovers(isolated):
    root, app, _, binding = isolated
    with BoundWorkerRelay(
        directory=root / "small",
        daemon_socket=app.socket_path,
        binding=binding,
        max_clients=1,
        timeout=0.3,
    ) as relay:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as first:
            first.settimeout(2)
            first.connect(str(relay.socket_path))
            first.sendall(b"{")
            deadline = time.monotonic() + 1
            while not relay._threads and time.monotonic() < deadline:
                time.sleep(0.005)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as excess:
                excess.settimeout(2)
                excess.connect(str(relay.socket_path))
                assert excess.recv(1) == b""
            assert "error" in json.loads(first.recv(4096))
        assert request(relay.socket_path, frame())["result"]["actor"] == ACTOR


def test_darwin_real_user_temp_canary_ignores_environment_override(
    tmp_path, monkeypatch
):
    import hyprial.daemon.impl.transfer.execution.smolvm as module

    actual, overridden = tmp_path / "actual", tmp_path / "overridden"
    actual.mkdir()
    overridden.mkdir()
    canary = actual / "smolvm-hidden"
    canary.write_text("before")
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module.os, "confstr", lambda _: str(actual))
    monkeypatch.setenv("TMPDIR", str(overridden))
    first = module._host_canaries()
    canary.write_text("after")
    second = module._host_canaries()
    assert str(canary) in first
    assert first != second
