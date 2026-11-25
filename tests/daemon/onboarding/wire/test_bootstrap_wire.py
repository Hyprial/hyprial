"""The bootstrap wire host over a real unix socket, spoken to by the real kernel IPC client.

Decision: daemon-first-z1-wire-tests-20261115.  Every test runs in an isolated
ownerless home under a short temporary root; the client is the unchanged
``daemon_request_once``.  Foreign peers are simulated by injecting the accepted
peer's identity view, since a test cannot become another uid.

Decision: ipc-bootstrap 2026-11-19.  The host listens through the kernel local
IPC seam, so one case removes ``socket.AF_UNIX`` outright and injects the
in-memory pipe backend: that is the Windows contract (no unix domain sockets)
exercised as a capability absence, with the same ``daemon_request_once`` client
reaching the bootstrap phase over the injected transport.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
import time
from dataclasses import replace

import pytest

from hyprial import __version__
from hyprial.daemon import (
    BootstrapWireHost,
    DaemonOwnershipBusy,
    DaemonStateOwnershipFence,
)
from hyprial.identity import IdentityTransactionBusy, IdentityTransactionLock
from hyprial.daemon.impl.application.onboarding.bootstrap import wire as wire_module
from hyprial.kernel import (
    DaemonLaunchResult,
    DaemonRequestError,
    RequestPortError,
    daemon_request_once,
    ipc_errors,
)
from hyprial.kernel.impl.manysys.ipc import local_ipc_backend
from tests.kernel.ipc.local_ipc.memory_backend import MemoryPipeBackend

from wire_support import BUSY, ServedWire, assert_home_released


def _ask(running: ServedWire, method, params=None, timeout=10.0):
    return daemon_request_once(
        method, params, socket_path=running.host.socket_path, timeout=timeout
    )


def _raw(path, payload: bytes, *, read: bool = True) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10)
        client.connect(str(path))
        try:
            client.sendall(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass
        if not read:
            return b""
        buffer = b""
        while b"\n" not in buffer:
            chunk = client.recv(65536)
            if not chunk:
                break
            buffer += chunk
        return buffer


def _login(running: ServedWire, key: str) -> dict:
    begun = _ask(
        running, "auth.login.begin", {"intent": "login", "operationKey": key}
    )
    operation = _ask(
        running, "auth.operation.get", {"operationId": begun["operationId"]}
    )
    assert operation["operation"]["state"] == "authenticated"
    return begun


def test_bootstrap_application_logs_after_pretrajectory_migration(tmp_path):
    state = tmp_path / "state"
    logs = state / "logs"
    logs.mkdir(parents=True)
    legacy = b'{"event":"legacy"}\n'
    (logs / "daemon.jsonl").write_bytes(legacy)
    app = wire_module._WireApplication(state)

    app._log("error", "daemon", "onboarding.handler_failed", detail="safe")

    archive = logs / "archive-pre-trajectory"
    assert (archive / "daemon.jsonl").read_bytes() == legacy
    assert (archive / ".migrated").exists()
    records = (logs / "daemon.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(records) == 1
    assert '"event":"onboarding.handler_failed"' in records[0]


def test_ping_answers_the_bootstrap_phase_over_the_daemon_socket(open_wire):
    running = open_wire()
    ping = _ask(running, "ping")
    assert ping["phase"] == "bootstrap"
    assert ping["running"] is True
    assert ping["owner"] is None
    assert ping["pid"] == os.getpid()
    assert ping["version"] == __version__
    assert ping["epoch"] == running.host.dispatcher.epoch
    assert ping["socket"] == str(running.host.socket_path)
    assert ping["restorePending"] is False
    assert isinstance(ping["nodeId"], str) and ping["nodeId"]


def test_ping_carries_what_the_launch_result_and_status_readers_need(open_wire):
    running = open_wire()
    ping = _ask(running, "ping")
    launched = DaemonLaunchResult.existing(ping)
    assert launched.running is True
    assert launched.pid == os.getpid()
    assert launched.epoch == ping["epoch"]
    assert launched.phase == "bootstrap"
    for key in ("running", "pid", "epoch", "nodeId", "owner", "socket", "phase", "version"):
        assert key in ping


def test_catalog_shares_the_ping_epoch_and_lists_the_eight_callable_methods(open_wire):
    running = open_wire()
    catalog = _ask(running, "daemon.catalog")
    assert catalog["epoch"] == _ask(running, "ping")["epoch"]
    callable_names = {
        entry["name"] for entry in catalog["methods"] if entry["callable"]
    }
    assert callable_names == {
        "onboarding.status",
        "auth.login.begin",
        "auth.operation.get",
        "auth.operation.cancel",
        "auth.relogin",
        "auth.logout",
        "onboarding.device.ensure",
        "onboarding.initialize",
    }
    stable = {
        entry["name"]: (entry["state"], entry["reason"])
        for entry in catalog["methods"]
        if entry["httpExposed"] and not entry["callable"]
    }
    assert stable
    assert set(stable.values()) == {("not-ready", "ACCOUNT_REQUIRED")}


def test_status_login_and_operation_methods_are_reachable(open_wire):
    running = open_wire()
    assert _ask(running, "onboarding.status", {})["phase"] == "bootstrap"
    begun = _ask(
        running,
        "auth.login.begin",
        {"intent": "login", "operationKey": "wire-login-0000001"},
    )
    assert begun["verification"]["userCode"] == "WIRE-CODE"
    assert "token" not in json.dumps(begun).lower()
    fetched = _ask(running, "auth.operation.get", {"operationId": begun["operationId"]})
    assert fetched["operation"]["state"] == "authenticated"
    with pytest.raises(RequestPortError) as unknown:
        _ask(running, "auth.operation.cancel", {"operationId": "no-such-operation"})
    assert unknown.value.code != "ACCOUNT_REQUIRED"
    assert unknown.value.code != ipc_errors.METHOD_NOT_FOUND


def test_relogin_alias_begins_a_login(open_wire):
    running = open_wire()
    begun = _ask(running, "auth.relogin", {"operationKey": "wire-relogin-000001"})
    assert begun["verification"]["userCode"] == "WIRE-CODE"


def test_device_ensure_is_refused_before_the_commit_and_succeeds_after_it(open_wire):
    running = open_wire()
    with pytest.raises(RequestPortError) as early:
        _ask(running, "onboarding.device.ensure", {"operationKey": "wire-device-00000001"})
    assert early.value.code == ipc_errors.IDENTITY_UNBOUND
    _login(running, "wire-login-0000002")
    receipt = _ask(
        running, "onboarding.device.ensure", {"operationKey": "wire-device-00000002"}
    )
    assert receipt["ok"] is True and receipt["deviceReady"] is True


@pytest.mark.parametrize(
    "method",
    [
        "agent.list",
        "message.send",
        "agent.start",
        "daemon.status",
        "ps",
        "agent.grant",
        "workflow.start",
        "message.ack",
        "routine.add",
        "lifecycle.start",
    ],
)
def test_business_methods_are_refused_with_account_required(open_wire, method):
    running = open_wire()
    with pytest.raises(RequestPortError) as refused:
        _ask(running, method, {})
    assert refused.value.code == "ACCOUNT_REQUIRED"
    assert refused.value.data == {
        "reason": "ACCOUNT_REQUIRED",
        "phase": "bootstrap",
        "method": method,
    }
    assert _ask(running, "ping")["phase"] == "bootstrap"


def test_an_undeclared_method_name_is_an_unknown_method_as_in_the_serving_daemon(
    open_wire,
):
    running = open_wire()
    with pytest.raises(RequestPortError) as unknown:
        _ask(running, "no.such.method", {})
    assert unknown.value.code == ipc_errors.METHOD_NOT_FOUND
    assert _ask(running, "ping")["phase"] == "bootstrap"


def test_malformed_frames_are_answered_with_the_daemon_error_shape(open_wire):
    running = open_wire()
    path = running.host.socket_path

    def error_of(payload: bytes) -> dict:
        reply = json.loads(_raw(path, payload))
        assert reply["version"] == 1 and "error" in reply
        return reply

    assert error_of(b"not json\n")["error"]["code"] == ipc_errors.INVALID_REQUEST
    assert error_of(b"[1]\n")["error"]["code"] == ipc_errors.INVALID_REQUEST
    wrong = error_of(b'{"version":2,"id":"a","method":"ping"}\n')
    assert wrong["error"]["code"] == ipc_errors.VERSION_MISMATCH and wrong["id"] == "a"
    missing = error_of(b'{"version":1,"id":"b"}\n')
    assert missing["error"]["code"] == ipc_errors.INVALID_ARGUMENT
    params = error_of(b'{"version":1,"id":"c","method":"ping","params":[]}\n')
    assert params["error"]["code"] == ipc_errors.INVALID_ARGUMENT
    big = error_of(b"x" * (1024 * 1024 + 70000))
    assert big["error"]["code"] == ipc_errors.IPC_REQUEST_TOO_LARGE
    assert _ask(running, "ping")["phase"] == "bootstrap"


def test_state_directory_and_socket_modes_match_the_serving_daemon(open_wire):
    running = open_wire()
    path = running.host.socket_path
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    observed = path.lstat()
    assert stat.S_ISSOCK(observed.st_mode)
    assert stat.S_IMODE(observed.st_mode) == 0o600
    assert observed.st_uid == os.geteuid()


def test_a_foreign_peer_uid_is_refused_before_any_byte_is_read(open_wire):
    foreign = os.geteuid() + 1
    running = open_wire(peer_view=lambda peer: replace(peer, uid=foreign))
    calls: list[str] = []
    original = running.host.dispatcher.call

    def spy(method, params=None):
        calls.append(method)
        return original(method, params)

    running.host.dispatcher.call = spy
    with pytest.raises(ipc_errors.DaemonDisconnectedError):
        _ask(running, "ping")
    with pytest.raises(ipc_errors.DaemonDisconnectedError):
        _ask(running, "onboarding.status", {})
    assert calls == []
    assert running.host.rejected_peers == 2


def test_a_peer_the_transport_already_limited_to_this_user_is_admitted(open_wire):
    foreign = os.geteuid() + 1
    running = open_wire(
        peer_view=lambda peer: replace(peer, uid=foreign, same_user_enforced=True)
    )
    assert _ask(running, "ping")["phase"] == "bootstrap"
    assert running.host.rejected_peers == 0


def test_an_unreadable_peer_credential_is_a_refusal(open_wire):
    running = open_wire(
        peer_view=lambda peer: replace(peer, uid=None, uid_unreadable=True)
    )
    with pytest.raises(ipc_errors.DaemonDisconnectedError):
        _ask(running, "ping")
    assert running.host.rejected_peers == 1


def test_a_platform_without_peer_credentials_relies_on_the_file_modes(open_wire):
    running = open_wire(peer_view=lambda peer: replace(peer, uid=None))
    assert _ask(running, "ping")["phase"] == "bootstrap"
    assert running.host.rejected_peers == 0


def test_the_matching_peer_uid_is_accepted(open_wire):
    running = open_wire(peer_view=lambda peer: replace(peer, uid=os.geteuid()))
    assert _ask(running, "ping")["phase"] == "bootstrap"
    assert running.host.rejected_peers == 0


def test_the_wire_host_serves_without_af_unix_through_the_injected_backend(
    open_wire, monkeypatch
):
    monkeypatch.delattr(socket, "AF_UNIX")
    with local_ipc_backend(MemoryPipeBackend()):
        running = open_wire()
        assert _ask(running, "ping")["phase"] == "bootstrap"
        assert running.stop() == "stop"


def test_shutdown_answers_then_stops_and_releases_everything(open_wire, ownerless):
    running = open_wire()
    reply = _ask(running, "shutdown")
    assert reply["ok"] is True and reply["stopping"] is True
    assert reply["pid"] == os.getpid()
    assert reply["epoch"] == running.host.dispatcher.epoch
    assert reply["stateDir"] == str(ownerless.state)
    assert reply["lockPath"] == str(ownerless.state / "daemon.lock")
    assert running.wait() == "stop"
    assert not ownerless.socket.exists()
    running.host.close()
    assert not (ownerless.state / "bootstrap.json").exists()
    again = BootstrapWireHost.open(ownerless.home, state_dir=ownerless.state)
    again.close()


def test_shutdown_with_a_stale_epoch_is_refused_and_serving_continues(open_wire):
    running = open_wire()
    with pytest.raises(RequestPortError) as stale:
        _ask(running, "shutdown", {"expectedEpoch": "not-this-epoch"})
    assert stale.value.code == "DAEMON_EPOCH_MISMATCH"
    assert _ask(running, "ping")["phase"] == "bootstrap"
    reply = _ask(
        running, "shutdown", {"expectedEpoch": running.host.dispatcher.epoch}
    )
    assert reply["stopping"] is True
    assert running.wait() == "stop"


def test_a_second_instance_on_the_same_home_is_refused_by_the_locks(
    open_wire, ownerless
):
    running = open_wire()
    with pytest.raises(BUSY):
        BootstrapWireHost.open(ownerless.home, state_dir=ownerless.state)
    assert ownerless.socket.exists()
    assert _ask(running, "ping")["epoch"] == running.host.dispatcher.epoch


def test_stop_releases_locks_record_and_socket_and_joins_the_workers(
    open_wire, ownerless
):
    running = open_wire()
    assert _ask(running, "ping")["phase"] == "bootstrap"
    assert running.stop() == "stop"
    assert not ownerless.socket.exists()
    running.host.close()
    running.host.close()
    assert not (ownerless.state / "bootstrap.json").exists()
    assert not running.host._workers
    reopened = BootstrapWireHost.open(ownerless.home, state_dir=ownerless.state)
    reopened.close()
    assert not ownerless.socket.exists()


def test_close_without_serving_releases_and_removes_the_socket(ownerless):
    host = BootstrapWireHost.open(ownerless.home, state_dir=ownerless.state)
    assert ownerless.socket.exists()
    host.close()
    assert not ownerless.socket.exists()
    assert not (ownerless.state / "bootstrap.json").exists()
    with pytest.raises(RuntimeError):
        host.serve()


def test_a_replacement_socket_is_not_unlinked_by_the_old_host(open_wire, ownerless):
    running = open_wire()
    ownerless.socket.unlink()
    ownerless.socket.write_text("someone else")
    running.host.request_stop()
    running.wait()
    assert ownerless.socket.read_text() == "someone else"
    running.host.close()


def test_initialize_promotes_only_after_the_whole_reply_was_written(
    open_wire, ownerless, monkeypatch
):
    running = open_wire()
    order: list[str] = []
    original_write = running.host._write
    original_finish = running.host._finish

    def write(connection, frame):
        written = original_write(connection, frame)
        order.append("written" if written else "write-failed")
        return written

    def finish(outcome):
        order.append(f"finish:{outcome}")
        original_finish(outcome)

    monkeypatch.setattr(running.host, "_write", write)
    monkeypatch.setattr(running.host, "_finish", finish)
    _login(running, "wire-login-0000003")
    accepted = _ask(
        running, "onboarding.initialize", {"operationKey": "wire-init-000000001"}
    )
    assert accepted["state"] == "promoting" and accepted["initialized"] is False
    assert running.wait() == "promote"
    assert order[-2:] == ["written", "finish:promote"]
    assert order.count("finish:promote") == 1
    assert not ownerless.socket.exists()
    record = json.loads((ownerless.state / "bootstrap.json").read_text())
    assert record["phase"] == "promoting"
    with pytest.raises(DaemonOwnershipBusy):
        DaemonStateOwnershipFence.acquire(ownerless.state, timeout=0)
    with pytest.raises(IdentityTransactionBusy):
        IdentityTransactionLock.acquire(ownerless.home, timeout=0)
    assert running.host.dispatcher.runtime.view.claimed_operation_id is not None


def test_a_failed_reply_write_never_promotes_and_is_reported_unacknowledged(open_wire, ownerless):
    running = open_wire()
    _login(running, "wire-login-0000004")
    release = threading.Event()
    original = running.host.dispatcher.call

    def held(method, params=None):
        if method == "onboarding.initialize":
            assert release.wait(10)
        return original(method, params)

    running.host.dispatcher.call = held
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(ownerless.socket))
        request = {
            "version": 1,
            "id": "gone",
            "method": "onboarding.initialize",
            "params": {"operationKey": "wire-init-000000002"},
        }
        client.sendall(json.dumps(request).encode() + b"\n")
    release.set()
    deadline = threading.Event()
    for _ in range(100):
        record = json.loads((ownerless.state / "bootstrap.json").read_text())
        if record["phase"] == "promoting":
            break
        deadline.wait(0.05)
    assert record["phase"] == "promoting"
    assert running.wait() == wire_module.UNACKNOWLEDGED
    assert running.host.dispatcher.take_post_reply() is None
    assert not ownerless.socket.exists()
    record = json.loads((ownerless.state / "bootstrap.json").read_text())
    assert record["phase"] == "promoting"


def test_a_completed_logout_ends_serving_with_reopen(open_wire, ownerless):
    running = open_wire()
    _login(running, "wire-login-0000005")
    reply = _ask(
        running,
        "auth.logout",
        {"operationKey": "wire-logout-00000001", "confirm": True},
    )
    assert reply["state"] == "completed" and reply["loggedOut"] is True
    assert running.wait() == "reopen"
    assert not ownerless.socket.exists()
    running.host.close()
    after = BootstrapWireHost.open(ownerless.home, state_dir=ownerless.state)
    after.close()


def test_request_stop_before_serving_returns_stop_immediately(ownerless):
    host = BootstrapWireHost.open(ownerless.home, state_dir=ownerless.state)
    try:
        host.request_stop()
        assert host.serve() == "stop"
        assert wire_module.STOP == "stop"
    finally:
        host.close()


def _connect(path) -> socket.socket:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    client.connect(str(path))
    return client


def test_a_silent_client_is_disconnected_at_the_idle_deadline(open_wire, monkeypatch):
    monkeypatch.setattr(wire_module, "_IDLE_SECONDS", 0.5)
    running = open_wire()
    started = time.monotonic()
    with _connect(running.host.socket_path) as silent:
        assert silent.recv(1) == b""
    elapsed = time.monotonic() - started
    assert 0.4 <= elapsed < 4.0
    assert _ask(running, "ping")["phase"] == "bootstrap"


def test_connections_beyond_the_cap_are_closed_and_a_freed_slot_is_reused(open_wire):
    assert wire_module._MAX_CONNECTIONS == 32
    running = open_wire()
    path = running.host.socket_path
    held = [_connect(path) for _ in range(wire_module._MAX_CONNECTIONS)]
    try:
        for _ in range(200):
            if len(running.host._workers) == wire_module._MAX_CONNECTIONS:
                break
            time.sleep(0.05)
        assert len(running.host._workers) == wire_module._MAX_CONNECTIONS
        with _connect(path) as over:
            assert over.recv(1) == b""
        held.pop().close()
        for _ in range(200):
            if len(running.host._workers) < wire_module._MAX_CONNECTIONS:
                break
            time.sleep(0.05)
        assert _ask(running, "ping")["phase"] == "bootstrap"
    finally:
        for client in held:
            client.close()


def test_a_failed_bind_releases_both_locks_and_the_record(ownerless, monkeypatch):
    def broken(self):
        raise OSError("bind refused")

    monkeypatch.setattr(BootstrapWireHost, "_bind", broken)
    with pytest.raises(OSError) as failed:
        BootstrapWireHost.open(ownerless.home, state_dir=ownerless.state)
    assert "bind refused" in str(failed.value)
    assert_home_released(ownerless)


@pytest.mark.parametrize("leftover", ["socket", "file"])
def test_a_stale_file_at_the_socket_path_is_replaced(ownerless, leftover):
    ownerless.state.mkdir(mode=0o700)
    if leftover == "socket":
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(ownerless.socket))
        stale.close()
    else:
        ownerless.socket.write_text("stale")
    host = BootstrapWireHost.open(ownerless.home, state_dir=ownerless.state)
    running = ServedWire(host)
    try:
        assert stat.S_ISSOCK(ownerless.socket.lstat().st_mode)
        assert _ask(running, "ping")["phase"] == "bootstrap"
    finally:
        running.stop()
        host.close()


def test_the_socket_is_private_before_it_listens(ownerless, monkeypatch):
    seen: list[int] = []
    original = socket.socket

    class Spy(original):
        def listen(self, backlog=None):
            seen.append(stat.S_IMODE(os.stat(self.getsockname()).st_mode))
            super().listen(backlog)

    previous = os.umask(0o022)
    monkeypatch.setattr(socket, "socket", Spy)
    try:
        host = BootstrapWireHost.open(ownerless.home, state_dir=ownerless.state)
    finally:
        os.umask(previous)
    try:
        assert seen == [0o600]
    finally:
        host.close()


def test_an_existing_state_directory_keeps_its_mode_like_the_serving_daemon(ownerless):
    ownerless.state.mkdir(mode=0o755)
    os.chmod(ownerless.state, 0o755)
    host = BootstrapWireHost.open(ownerless.home, state_dir=ownerless.state)
    try:
        assert stat.S_IMODE(ownerless.state.stat().st_mode) == 0o755
        assert stat.S_IMODE(ownerless.socket.lstat().st_mode) == 0o600
    finally:
        host.close()


class _Recorder:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.requested = 0

    def settimeout(self, _seconds):
        pass

    def recv(self, size):
        self.requested += size
        return self.chunks.pop(0)[:size]


def test_the_frame_cap_is_exact_and_nothing_is_read_past_it(ownerless):
    host = BootstrapWireHost.open(ownerless.home, state_dir=ownerless.state)
    try:
        cap = wire_module._MAX_FRAME_BYTES
        exact = _Recorder([b"a" * 65536] * 16 + [b"\n"])
        assert host._read_frame(exact) == b"a" * cap
        flood = _Recorder([b"a" * 65536] * 40)
        with pytest.raises(DaemonRequestError) as refused:
            host._read_frame(flood)
        assert refused.value.code == ipc_errors.IPC_REQUEST_TOO_LARGE
        assert flood.requested == cap + 1
    finally:
        host.close()


def test_an_unwritable_initialize_reply_ends_serving_as_unacknowledged(
    open_wire, ownerless, monkeypatch
):
    running = open_wire()
    _login(running, "wire-login-0000006")
    monkeypatch.setattr(running.host, "_write", lambda _connection, _frame: False)
    with pytest.raises(ipc_errors.DaemonDisconnectedError):
        _ask(
            running, "onboarding.initialize", {"operationKey": "wire-init-000000003"}
        )
    assert running.wait() == wire_module.UNACKNOWLEDGED == "initialize-unacknowledged"
    record = json.loads((ownerless.state / "bootstrap.json").read_text())
    assert record["phase"] == "promoting"
    assert not ownerless.socket.exists()
