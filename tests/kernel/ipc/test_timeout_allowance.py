"""A caller-specific budget allowance preserves the shared timeout policy."""

from pathlib import Path

import pytest

import hyprial.kernel.impl.ipc.client as client
from hyprial.kernel import ipc_errors


@pytest.mark.parametrize(("raw", "explicit", "expected"), [
    (None, None, 20.0), ("1", None, 6.0), ("100", None, 105.0),
    ("invalid", None, 20.0), ("100", 7.0, 12.0),
])
def test_allowance_is_added_to_resolved_request_timeout(monkeypatch, raw, explicit, expected):
    if raw is None:
        monkeypatch.delenv("HYPRIAL_IPC_ROUNDTRIP_SECONDS", raising=False)
    else:
        monkeypatch.setenv("HYPRIAL_IPC_ROUNDTRIP_SECONDS", raw)
    seen = []
    monkeypatch.setattr(client, "daemon_request_once", lambda *a, **kw: seen.append(kw["timeout"]) or {"ok": True})
    assert client.daemon_request("ping", {}, socket_path=Path("/unused"), timeout=explicit,
                                 timeout_allowance=5.0) == {"ok": True}
    assert seen == [expected]


def test_restore_retries_do_not_accumulate_allowance(monkeypatch):
    seen = []

    def once(*args, **kwargs):
        seen.append(kwargs["timeout"])
        if len(seen) < 3:
            raise ipc_errors.DaemonRestoringError("restoring")
        return {"ok": True}

    class Clock:
        now = 0.0
        def monotonic(self):
            return self.now
        def sleep(self, seconds):
            self.now += seconds

    monkeypatch.setenv("HYPRIAL_IPC_ROUNDTRIP_SECONDS", "1")
    monkeypatch.setattr(client, "daemon_request_once", once)
    assert client.daemon_request("ping", {}, socket_path=Path("/unused"), timeout_allowance=5.0,
                                 clock=Clock(), restore_wait=10.0) == {"ok": True}
    assert seen == [6.0, 6.0, 6.0]


def test_other_requests_keep_existing_default_timeout(monkeypatch):
    monkeypatch.setenv("HYPRIAL_IPC_ROUNDTRIP_SECONDS", "100")
    seen = []
    monkeypatch.setattr(client, "daemon_request_once", lambda *a, **kw: seen.append(kw["timeout"]) or {"ok": True})
    client.daemon_request("ping", {}, socket_path=Path("/unused"))
    assert seen == [100.0]


def test_allowance_reaches_real_unix_socket(monkeypatch, tmp_path):
    import json
    import socket
    import threading

    path = tmp_path / "daemon.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    original_connect = client._connect_daemon_socket
    budgets = []

    def connect(socket_path, timeout, **kwargs):
        budgets.append(timeout)
        return original_connect(socket_path, timeout, **kwargs)

    def serve():
        connection, _ = listener.accept()
        with connection:
            line = connection.makefile("rb").readline()
            frame = json.loads(line)
            response = {"version": 1, "id": frame["id"], "result": {"ok": True}}
            connection.sendall((json.dumps(response) + "\n").encode())

    monkeypatch.setattr(client, "_connect_daemon_socket", connect)
    worker = threading.Thread(target=serve)
    worker.start()
    try:
        assert client.daemon_request("ping", {}, socket_path=path, timeout=2.0,
                                     timeout_allowance=5.0) == {"ok": True}
        assert budgets == [7.0]
    finally:
        worker.join(5)
        listener.close()
    assert not worker.is_alive()
