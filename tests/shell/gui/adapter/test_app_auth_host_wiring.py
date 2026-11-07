"""Failing-first tests for the authenticated remote facade composition.

These tests close the C1 entrance: a remote ``--expose-port`` launch without
the durable access authority must refuse before the listener binds or the
exposure call happens.  They also prove the per-call admission boundary is
held across dispatch, that a revocation closes an already-authenticated
connection within five seconds, and that the CLI composes one real gate.

Every layer below the request-key derivation is the production composition
root: the SQLite authority, ``AuthGate``, ``handle_request`` and the real
loopback listener.  The derive callable is deterministic for the same reason
as the other mobile-access tests -- the sidecar derive op is an external
dependency that the baseline does not yet ship.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hyprial.shell.impl.gui.adapter.host import (
    HostLaunch,
    adapter_from_config,
    handle_request,
    serve_host,
    serve_tcp,
)
from hyprial.shell.impl.gui.adapter.mobile import MobileAccessError, MobileAccessStore
from tests.shell.gui.adapter.mobile.helpers import (
    OTHER_OWNER,
    OWNER,
    PEER_A,
    PEER_B,
    DeterministicDerive,
    make_gate,
    make_store,
    pair_device,
    signed_frame,
)
from tests.shell.gui.adapter.test_app_adapter_host import (
    CONFIG,
    FakeDaemon,
    TcpFrameClient,
)


class ExposeRecorder:
    """The daemon-request port of a composed remote facade, recording calls."""

    def __init__(self, stop: threading.Event | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._stop = stop

    def __call__(self, method: str, params: dict) -> object:
        self.calls.append((method, dict(params)))
        if self._stop is not None:
            self._stop.set()
        if method == "network.expose":
            return {"exposure": dict(params)}
        return {"port": params.get("port"), "removed": True}


def test_a_remote_facade_without_a_gate_refuses_before_binding_or_exposing(
    tmp_path: Path,
) -> None:
    launch = HostLaunch(
        config_path=tmp_path / "c.json", tcp_port=0, expose_port=7443
    )
    stop = threading.Event()
    recorder = ExposeRecorder(stop)

    with pytest.raises(ValueError, match="auth gate|access authority"):
        serve_host(
            launch,
            adapter=adapter_from_config(CONFIG, FakeDaemon()),
            request=recorder,
            stop=stop,
        )

    assert recorder.calls == [], "a refused remote composition never exposes"
    assert not stop.is_set()


def test_the_cli_refuses_expose_port_without_durable_access_flags(
    tmp_path: Path, monkeypatch
) -> None:
    from hyprial.shell.impl.gui.adapter import host as host_module

    observed: list[object] = []

    def composing_stub(*_args, **_kwargs) -> None:
        observed.append(object())

    monkeypatch.setattr(host_module, "serve_host", composing_stub)
    monkeypatch.setattr(host_module, "daemon_request_callable", lambda: FakeDaemon())
    config = tmp_path / "config.json"
    config.write_text(json.dumps(CONFIG), encoding="utf-8")

    with pytest.raises(SystemExit) as refused:
        host_module.main(
            [
                "--tcp-port",
                "0",
                "--expose-port",
                "7443",
                "--config",
                str(config),
            ]
        )

    assert refused.value.code == 2
    assert observed == [], "the refused CLI never reaches the serving composition"


class RevokingAdapter:
    """The real adapter, but its binding lookup revokes the device first.

    This places the revocation exactly between envelope verification and the
    business dispatch: the admission boundary must re-check after the binding
    lookup and refuse before the adapter operation runs.
    """

    def __init__(self, inner, store: MobileAccessStore, device_id: str) -> None:
        self.inner = inner
        self.store = store
        self.device_id = device_id

    def session_binding(self, session_id: str):
        self.store.revoke_device(
            self.device_id, operator="mate", reason="test-mid-request"
        )
        return self.inner.session_binding(session_id)

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


def test_the_admission_boundary_rechecks_after_binding_lookup(
    tmp_path: Path,
) -> None:
    store, _clock = make_store(tmp_path)
    pair_device(store, device_id="dev-a", client_public=PEER_A)
    store.grant_session(
        "dev-a", "s-1", session_owner=OWNER, operator="mate"
    )
    derive = DeterministicDerive()
    gate = make_gate(store, derive=derive)
    daemon = FakeDaemon()
    adapter = RevokingAdapter(
        adapter_from_config(CONFIG, daemon), store, "dev-a"
    )

    answer = handle_request(
        adapter,
        signed_frame(
            derive,
            operation="sessions.history",
            params={"sessionId": "s-1"},
            nonce="admission-race-1",
        ),
        gate=gate,
    )

    assert answer["ok"] is False, "the mid-request revocation must refuse"
    assert answer["error"]["code"] == "device-revoked"
    assert daemon.calls == [], "no daemon operation ran after the revocation"


def _start_gated_facade(tmp_path: Path, gate, daemon=None):
    adapter = adapter_from_config(CONFIG, daemon if daemon is not None else FakeDaemon())
    ready = threading.Event()
    stop = threading.Event()
    failures: list[BaseException] = []
    bound: list[tuple[str, int]] = []

    def run() -> None:
        try:
            serve_tcp(
                0,
                adapter,
                ready=ready,
                stop=stop,
                on_bound=lambda host, port: bound.append((host, port)),
                auth_gate=gate,
            )
        except BaseException as error:  # noqa: BLE001 - surfaced to the test
            failures.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    assert ready.wait(timeout=5.0), failures
    assert not failures
    host, port = bound[-1]
    return stop, thread, host, port


def test_revocation_closes_an_authenticated_connection_within_five_seconds(
    tmp_path: Path,
) -> None:
    store, _clock = make_store(tmp_path)
    pair_device(store, device_id="dev-a", client_public=PEER_A)
    derive = DeterministicDerive()
    gate = make_gate(store, derive=derive)
    stop, thread, host, port = _start_gated_facade(tmp_path, gate)
    try:
        with TcpFrameClient(host, port) as client:
            client.send(
                signed_frame(derive, nonce="before-revoke", params={})
            )
            assert client.read()["ok"] is True

            revoked_at = time.monotonic()
            store.revoke_device("dev-a", operator="mate", reason="test-revoke")

            client.socket.settimeout(5.5)
            assert client.read_eof(), "the revoked connection must be closed"
            assert time.monotonic() - revoked_at <= 5.0
    finally:
        stop.set()
        thread.join(timeout=3.0)
        assert not thread.is_alive()


def test_the_cli_builds_one_durable_gate_for_the_remote_facade(
    tmp_path: Path, monkeypatch
) -> None:
    from hyprial.shell.impl.gui.adapter import host as host_module

    observed: dict[str, object] = {}
    built: list[tuple[Path, str, Path]] = []

    def gate_factory(db_path: Path, node_id: str, derive_command: Path):
        built.append((db_path, node_id, derive_command))
        return make_gate(
            MobileAccessStore(db_path, node_id=node_id),
            derive=DeterministicDerive(),
            node_id=node_id,
        )

    def composing_stub(launch, **kwargs) -> None:
        observed["gate"] = kwargs.get("auth_gate")

    monkeypatch.setattr(host_module, "build_auth_gate", gate_factory)
    monkeypatch.setattr(host_module, "serve_host", composing_stub)
    monkeypatch.setattr(host_module, "daemon_request_callable", lambda: FakeDaemon())
    config = tmp_path / "config.json"
    config.write_text(json.dumps(CONFIG), encoding="utf-8")
    db = tmp_path / "access.sqlite3"
    helper = tmp_path / "derive"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o700)

    code = host_module.main(
        [
            "--tcp-port",
            "0",
            "--expose-port",
            "7443",
            "--config",
            str(config),
            "--mobile-access-db",
            str(db),
            "--node-id",
            "node:dev000000001.local",
            "--derive-command",
            str(helper),
        ]
    )

    assert code == 0
    assert built == [(db, "node:dev000000001.local", helper)]
    assert observed["gate"] is not None


class DedupingDaemon:
    """The daemon's documented send/idempotency and read contract.

    ``message.send`` deduplicates by ``idempotencyKey`` (the daemon's
    operation id): a retry returns the same operation id with
    ``duplicate: true`` and performs no second side effect.  Reads answer
    empty pages.  This port is the only stand-in in these tests; the SQLite
    authority, the AuthGate, envelope verification, admission and the
    loopback TCP facade are the real composition root.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.effects: dict[str, str] = {}
        self.first_send = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, method: str, params: dict) -> object:
        with self._lock:
            self.calls.append((method, dict(params)))
            if method == "message.send":
                key = params["idempotencyKey"]
                duplicate = key in self.effects
                if not duplicate:
                    self.effects[key] = params["message"]
                self.first_send.set()
                return {
                    "ok": True,
                    "operationId": key,
                    "conversationId": params["conversationId"],
                    "deliveries": [
                        {"target": target, "accepted": True, "duplicate": duplicate}
                        for target in params["to"]
                    ],
                }
            if method == "message.history":
                return {
                    "ok": True,
                    "conversationId": params["conversationId"],
                    "entries": [],
                    "nextCursor": None,
                }
            if method == "progress.list":
                return {"ok": True, "events": []}
            return {}


def _exchange(client, frame: dict) -> dict:
    client.send(frame)
    return client.read()


def test_a_lost_ack_retry_with_the_same_request_id_executes_once(
    tmp_path: Path,
) -> None:
    store, _clock = make_store(tmp_path)
    pair_device(store, device_id="dev-a", client_public=PEER_A)
    store.grant_session("dev-a", "s-1", session_owner=OWNER, operator="mate")
    derive = DeterministicDerive()
    gate = make_gate(store, derive=derive)
    daemon = DedupingDaemon()
    stop, thread, host, port = _start_gated_facade(tmp_path, gate, daemon=daemon)
    send_params = {
        "sessionId": "s-1",
        "request": {"text": "hello", "idempotencyKey": "lost-ack-1"},
    }
    first = signed_frame(
        derive,
        operation="sessions.send",
        params=send_params,
        nonce="send-frame-1",
    )
    retry = signed_frame(
        derive,
        operation="sessions.send",
        params=send_params,
        nonce="send-frame-2",
    )
    try:
        with TcpFrameClient(host, port) as client:
            client.send(first)
            assert daemon.first_send.wait(timeout=5.0)
            # The client leaves without reading the ACK: it cannot know
            # whether the daemon accepted the message.
        with TcpFrameClient(host, port) as client:
            answer = _exchange(client, retry)
        assert answer["ok"] is True, answer
        assert answer["result"]["commandId"] == "lost-ack-1"
        assert answer["result"]["deduplicated"] is True
        sends = [
            params for method, params in daemon.calls if method == "message.send"
        ]
        assert [params["idempotencyKey"] for params in sends] == [
            "lost-ack-1",
            "lost-ack-1",
        ], "the retry preserves the original request id; it never mints a new one"
        assert daemon.effects == {"lost-ack-1": "hello"}, "one side effect, not two"

        before = len(daemon.calls)
        with TcpFrameClient(host, port) as client:
            replay = _exchange(client, first)
        assert replay["ok"] is False
        assert replay["error"]["code"] == "auth-nonce-replayed"
        assert len(daemon.calls) == before, "a replayed envelope never dispatches"
    finally:
        stop.set()
        thread.join(timeout=3.0)
        store.close()
        assert not thread.is_alive()


def test_device_isolation_sharing_and_cancel_hold_on_the_real_host_chain(
    tmp_path: Path,
) -> None:
    store, _clock = make_store(tmp_path)
    pair_device(store, device_id="dev-a", device_uid="uid-a", client_public=PEER_A)
    pair_device(
        store,
        device_id="dev-b",
        device_uid="uid-b",
        name="mate tablet",
        client_public=PEER_B,
    )
    pair_device(
        store,
        device_id="dev-other",
        device_uid="uid-c",
        name="other phone",
        owner=OTHER_OWNER,
        client_public="peer-public-c",
    )
    store.grant_session("dev-a", "s-1", session_owner=OWNER, operator="mate")
    derive = DeterministicDerive()
    gate = make_gate(store, derive=derive)
    daemon = DedupingDaemon()
    stop, thread, host, port = _start_gated_facade(tmp_path, gate, daemon=daemon)

    def request(
        device_id: str,
        client_public: str,
        operation: str,
        params: dict,
        nonce: str,
    ) -> dict:
        with TcpFrameClient(host, port) as client:
            return _exchange(
                client,
                signed_frame(
                    derive,
                    device_id=device_id,
                    client_public=client_public,
                    operation=operation,
                    params=params,
                    nonce=nonce,
                ),
            )

    denied_calls = [
        (
            "sessions.history",
            {"sessionId": "s-1"},
            "isolated-history-1",
        ),
        (
            "sessions.send",
            {
                "sessionId": "s-1",
                "request": {"text": "hi", "idempotencyKey": "isolated-1"},
            },
            "isolated-send-1",
        ),
        ("sessions.events", {"sessionId": "s-1"}, "isolated-events-1"),
    ]
    try:
        isolated_list = request("dev-b", PEER_B, "sessions.list", {}, "isolated-list-1")
        assert isolated_list["ok"] is True
        assert isolated_list["result"] == [], "an ungranted device sees no session"
        for operation, params, nonce in denied_calls:
            denied = request("dev-b", PEER_B, operation, params, nonce)
            assert denied["ok"] is False, denied
            assert denied["error"]["code"] == "auth-session-not-granted", denied
        assert daemon.calls == [], "an ungranted device never reaches the daemon"

        direct = request(
            "dev-a", PEER_A, "sessions.history", {"sessionId": "s-1"}, "direct-1"
        )
        assert direct["ok"] is True, direct
        direct_audit = store.audit_events(device_id="dev-a", session_id="s-1")[0]
        assert direct_audit["event"] == "request-admitted"
        assert direct_audit["sharing"] == "isolated"
        assert direct_audit["sourceDeviceId"] is None

        granted = store.grant_session(
            "dev-b",
            "s-1",
            session_owner=OWNER,
            operator="mate",
            source_device_id="dev-a",
        )
        assert granted["sharing"] == "shared"
        assert granted["sharedFrom"] == "dev-a"

        shared_list = request("dev-b", PEER_B, "sessions.list", {}, "shared-list-1")
        assert shared_list["result"] == [{"sessionId": "s-1"}]
        assert request(
            "dev-b",
            PEER_B,
            "sessions.history",
            {"sessionId": "s-1"},
            "shared-history-1",
        )["ok"] is True
        shared_send = request(
            "dev-b",
            PEER_B,
            "sessions.send",
            {
                "sessionId": "s-1",
                "request": {"text": "shared hello", "idempotencyKey": "shared-1"},
            },
            "shared-send-1",
        )
        assert shared_send["ok"] is True, shared_send
        assert request(
            "dev-b", PEER_B, "sessions.events", {"sessionId": "s-1"}, "shared-events-1"
        )["ok"] is True
        assert daemon.effects == {"shared-1": "shared hello"}

        # The three session-bound calls carry the shared attribution;
        # ``sessions.list`` is sessionless by contract, so its admission row
        # has no session id and is filtered out here.
        shared_rows = [
            row
            for row in store.audit_events(device_id="dev-b", session_id="s-1")
            if row["event"] == "request-admitted"
        ]
        assert len(shared_rows) == 3, shared_rows
        assert all(
            row["sharing"] == "shared" and row["sourceDeviceId"] == "dev-a"
            for row in shared_rows
        ), shared_rows

        with pytest.raises(MobileAccessError) as cross_owner_grant:
            store.grant_session(
                "dev-other", "s-1", session_owner=OWNER, operator="mate"
            )
        assert cross_owner_grant.value.code == "owner-mismatch"
        cross_owner = request(
            "dev-other",
            "peer-public-c",
            "sessions.history",
            {"sessionId": "s-1"},
            "cross-owner-1",
        )
        assert cross_owner["ok"] is False
        assert cross_owner["error"]["code"] == "auth-not-authorized"

        store.revoke_session_grant("dev-b", "s-1", operator="mate")
        assert request(
            "dev-b", PEER_B, "sessions.list", {}, "cancel-list-1"
        )["result"] == []
        for operation, params, nonce in denied_calls:
            denied = request("dev-b", PEER_B, operation, params, f"cancel-{nonce}")
            assert denied["ok"] is False, denied
            assert denied["error"]["code"] == "auth-session-not-granted", denied
        assert daemon.effects == {"shared-1": "shared hello"}, (
            "cancelling the share neither deletes history nor repeats a send"
        )
        still_direct = request(
            "dev-a",
            PEER_A,
            "sessions.history",
            {"sessionId": "s-1"},
            "after-cancel-direct-1",
        )
        assert still_direct["ok"] is True, "dev-a's own grant is untouched"
        assert any(
            row["event"] == "request-admitted" and row["sharing"] == "shared"
            for row in store.audit_events(device_id="dev-b", session_id="s-1")
        ), "the share audit survives its cancellation"
    finally:
        stop.set()
        thread.join(timeout=3.0)
        store.close()
        assert not thread.is_alive()


def test_daemon_request_callable_needs_no_cli_runtime() -> None:
    """An independent host adapter reaches the daemon without the CLI entry.

    ``daemon_request_callable`` used to import the CLI runtime to register a
    service resolver, so "the adapter can talk to the daemon" depended on a
    side effect of the whole CLI composition.  The kernel public client plus
    the public state/home providers must be enough on their own.
    """

    code = r"""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
from pathlib import Path

root = Path(tempfile.mkdtemp(prefix="a01-host-", dir="/tmp"))
sock = root / "daemon.sock"
listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(str(sock))
listener.listen(1)


def serve():
    connection, _ = listener.accept()
    with connection:
        data = b""
        while b"\n" not in data:
            chunk = connection.recv(65536)
            if not chunk:
                break
            data += chunk
        frame = json.loads(data.partition(b"\n")[0])
        response = {"version": 1, "id": frame["id"], "result": {"pong": True}}
        connection.sendall(json.dumps(response).encode() + b"\n")


threading.Thread(target=serve, daemon=True).start()
try:
    os.environ.pop("HARNESS_STATE_DIR", None)
    os.environ.pop("HYPRIAL_HOME", None)
    os.environ["HARNESS_SOCKET_PATH"] = str(sock)
    import hyprial.shell.impl.gui.adapter.host as host

    assert "hyprial.shell.impl.cli.runtime.entry" not in sys.modules
    assert "hyprial.shell.impl.cli.commands.common.support" not in sys.modules
    assert host.daemon_request_callable()("ping", {}) == {"pong": True}
    assert "hyprial.shell.impl.cli.runtime.entry" not in sys.modules, (
        "the request call loaded the CLI runtime"
    )
    assert "hyprial.shell.impl.cli.commands.common.support" not in sys.modules, (
        "the request call loaded the CLI support module"
    )
finally:
    listener.close()
    shutil.rmtree(root, ignore_errors=True)
print("OK")
"""
    repository = Path(__file__).resolve().parents[4]
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(repository / "src") + (
        os.pathsep + existing if existing else ""
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "OK"


def test_host_adapter_endpoint_keeps_the_cli_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The adapter keeps HARNESS_STATE_DIR/HYPRIAL_HOME over HARNESS_SOCKET_PATH."""

    import hyprial.kernel as kernel

    seen: list[Path] = []

    def fake_request(method: str, params: dict, *, socket_path) -> dict:
        assert method == "ping" and params == {}
        seen.append(socket_path())
        return {"ok": True}

    monkeypatch.setattr(kernel, "daemon_request", fake_request)
    from hyprial.shell.impl.gui.adapter import host as host_module

    state = tmp_path / "state"
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("HARNESS_SOCKET_PATH", str(tmp_path / "ignored.sock"))
    assert host_module.daemon_request_callable()("ping", {}) == {"ok": True}
    assert seen[-1] == state / "daemon.sock"

    monkeypatch.delenv("HARNESS_STATE_DIR")
    monkeypatch.delenv("HYPRIAL_HOME")
    explicit = tmp_path / "explicit.sock"
    monkeypatch.setenv("HARNESS_SOCKET_PATH", str(explicit))
    assert host_module.daemon_request_callable()("ping", {}) == {"ok": True}
    assert seen[-1] == explicit.expanduser().resolve()
