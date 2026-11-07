"""Virtual-clock transport regression from the MT review's no-sleep probe."""
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.ipc import server
from hyprial.kernel import MEMBERSHIP_BUDGETS, MEMBERSHIP_REPLY_MARGIN_SECONDS


def dispatch_at(monkeypatch, method, ready_at, response):
    clock = [0.0]
    result = SimpleNamespace(response=response, error=None)

    def wait(seconds):
        clock[0] += seconds
        return clock[0] >= ready_at

    result.ready = SimpleNamespace(wait=wait)
    owner = SimpleNamespace(
        _ipc_timeout_response=server._IpcServerMixin._ipc_timeout_response,
        _ipc_peer_disconnected=lambda connection: False,
    )
    thread = SimpleNamespace(start=lambda: None)
    monkeypatch.setattr(server, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(server, "threading", SimpleNamespace(Thread=lambda **kwargs: thread))
    monkeypatch.setattr(server, "_IpcRequestResult", lambda: result)
    outcome = server._IpcServerMixin._dispatch_ipc_response(
        owner, object(), b"{}", request_id="review", method=method, peer_pid=None,
    )
    return outcome, clock[0]


@pytest.mark.parametrize("method", ["org.join", "org.execute"])
@pytest.mark.parametrize("ready_at", [100.0, 105.0])
def test_transport_preserves_join_result_and_domain_timeout(monkeypatch, method, ready_at):
    response = {"version": 1, "id": "review"}
    if ready_at == 100:
        response["result"] = {"joined": True}
    else:
        response["error"] = {
            "code": "MEMBERSHIP_SIDECAR_TIMEOUT",
            "message": "Org join leg bootstrap exhausted the 105s join budget.",
            "data": {"operation": "join", "leg": "bootstrap", "budgetSeconds": 105},
        }
    outcome, elapsed = dispatch_at(monkeypatch, method, ready_at, response)
    assert outcome == response
    assert ready_at <= elapsed < ready_at + 0.11


@pytest.mark.parametrize("method", ["org.join", "org.execute", "org.list", None])
def test_transport_deadline_remains_finite_and_method_specific(monkeypatch, method):
    outcome, elapsed = dispatch_at(monkeypatch, method, float("inf"), {})
    expected = MEMBERSHIP_BUDGETS["join-ipc"].seconds if method in {"org.join", "org.execute"} else 90
    assert outcome["error"]["code"] == "IPC_TIMEOUT"
    assert elapsed == expected


@pytest.mark.parametrize("method", ["org.join", "org.execute"])
@pytest.mark.parametrize("override", [None, "1"])
def test_explicit_join_wait_reaches_socket_without_default_cap(monkeypatch, tmp_path, method, override):
    import json

    from hyprial.kernel.impl.ipc import client
    from hyprial.shell.impl.invites.join import request_join

    if override is None:
        monkeypatch.delenv("HYPRIAL_IPC_ROUNDTRIP_SECONDS", raising=False)
    else:
        monkeypatch.setenv("HYPRIAL_IPC_ROUNDTRIP_SECONDS", override)
    assert client._daemon_ipc_roundtrip_seconds() == (15 if override is None else 1)
    waits = []
    frames = []
    sock = SimpleNamespace(
        settimeout=waits.append,
        connect=lambda path: None,
        sendall=lambda frame: frames.append(json.loads(frame)),
        recv=lambda size: json.dumps({
            "version": 1, "id": frames[-1]["id"], "result": {"joined": True},
        }).encode() + b"\n",
        close=lambda: None,
    )
    monkeypatch.setattr(client.socket, "socket", lambda *args: sock)
    runtime = SimpleNamespace(request=lambda method, params, **kwargs: client.daemon_request(
        method, params, socket_path=tmp_path / "isolated.sock", **kwargs,
    ))
    assert request_join(runtime, "test-invite", method=method) == {"joined": True}
    assert frames[0]["method"] == method
    assert waits == [MEMBERSHIP_BUDGETS["join-cli"].seconds]


def test_join_transport_budget_fits_between_domain_and_cli():
    assert (
        MEMBERSHIP_BUDGETS["join"].seconds + MEMBERSHIP_REPLY_MARGIN_SECONDS
        < MEMBERSHIP_BUDGETS["join-ipc"].seconds
        < MEMBERSHIP_BUDGETS["join-cli"].seconds
    )
