"""agent.resolve retains its existing IPC behavior through the catalog."""

import os
import threading

import pytest

from hyprial.daemon.impl.ipc import router
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.kernel import DaemonRequestError, RequestPortError, daemon_request_once, ipc_errors
from tests.kernel.actors.identity.support import PI_WORKER, application
from tests.support.runtime._owned_applications import owned_applications as owned_applications


def test_registered_contract_preserves_handler_validation():
    binding = METHOD_REGISTRY.get("agent.resolve")
    assert binding is not None
    assert binding.spec.ipc_param_validation == "handler"
    assert binding.spec.http_exposed is False
    assert binding.spec.mutates is False
    assert "agent.resolve" in router.MIGRATED_METHODS


def test_real_handler_preserves_resolution_and_ambiguity(tmp_path):
    moved = dict(PI_WORKER, name="moved", pinnedOwner="someone-else")
    app = application(tmp_path, providers=(PI_WORKER, moved))
    app.handle("agent.create", {"name": "moved"})
    alias = app.handle("agent.resolve", {"name": "kanban-sim", "ignored": [1]})
    assert alias == {"ok": True, "input": "kanban-sim", "resolved": "agent:op:node-1:kanban-sim", "known": True, "reason": "alias"}
    assert app.handle("agent.resolve", {"name": "nobody"}) == {"ok": True, "input": "nobody", "resolved": "nobody", "known": False, "reason": "unknown"}
    assert app.handle("agent.resolve", {"name": "unvalidated:scheme"})["reason"] == "scheme"
    with pytest.raises(DaemonRequestError) as caught:
        app.handle("agent.resolve", {"name": "moved"})
    assert caught.value.code == ipc_errors.AMBIGUOUS_TARGET
    assert caught.value.data == {"target": "moved", "candidates": sorted([app.agents.get("moved").uri, "agent:someone-else:node-1:moved"])}
    for value in (None, "", 1, [], {}):
        with pytest.raises(DaemonRequestError) as invalid:
            app.handle("agent.resolve", {"name": value})
        assert invalid.value.code == ipc_errors.INVALID_ARGUMENT
        assert str(invalid.value) == "name must be a non-empty string"


def test_real_unix_socket_unknown_and_invalid(tmp_path, monkeypatch):
    app = application(tmp_path, providers=(PI_WORKER,))
    monkeypatch.setattr(app, "_start_maintenance_scheduler", lambda: None)
    from tests.daemon.application.transport.resilience.support import _IdleRuntime
    app._runtime = _IdleRuntime()
    app._start_server()
    thread = threading.Thread(target=app._serve, daemon=True)
    thread.start()
    try:
        def call(params):
            return daemon_request_once("agent.resolve", params, socket_path=app.socket_path, timeout=10)
        assert call({"name": "missing", "ignored": True}) == {"ok": True, "input": "missing", "resolved": "missing", "known": False, "reason": "unknown"}
        with pytest.raises(RequestPortError) as invalid:
            call({})
        assert invalid.value.code == ipc_errors.INVALID_ARGUMENT
        assert str(invalid.value) == "name must be a non-empty string"
    finally:
        app.stop_event.set()
        app._server.close()
        if app._accept_reserve_fd is not None:
            os.close(app._accept_reserve_fd)
            app._accept_reserve_fd = None
        thread.join(timeout=5)
        assert not thread.is_alive()
        app._close_ipc_clients()
        if app._ipc_request_owner is not None:
            app._ipc_request_owner.close(1.0)
        if app._dispatch_diagnostics is not None:
            app._dispatch_diagnostics.close(1.0)


def test_restore_gate_and_injected_agent_error(tmp_path, monkeypatch):
    from hyprial.identity import AgentError

    app = application(tmp_path)
    app._restore_done.clear()
    with pytest.raises(ipc_errors.DaemonRestoringError):
        app.handle("agent.resolve", {"name": "unknown"})
    app._restore_done.set()

    def unavailable(_name):
        raise AgentError("registry unavailable")

    monkeypatch.setattr(app.agents, "get", unavailable)
    with pytest.raises(DaemonRequestError) as caught:
        app.handle("agent.resolve", {"name": "broken"})
    assert caught.value.code == "AGENT_ERROR"
    assert str(caught.value) == "registry unavailable"


def test_name_form_legacy_records_match_examples(tmp_path, monkeypatch):
    from hyprial.identity.impl.agents import addressing

    monkeypatch.setattr(addressing, "ID_FORM_MINTING", False)
    moved = dict(PI_WORKER, name="moved", pinnedOwner="someone-else")
    app = application(tmp_path, providers=(PI_WORKER, moved))
    app.handle("agent.create", {"name": "moved"})
    with pytest.raises(DaemonRequestError) as caught:
        app.handle("agent.resolve", {"name": "moved"})
    assert caught.value.code == ipc_errors.AMBIGUOUS_TARGET
    assert str(caught.value) == "actor name 'moved' matches more than one agent; address the canonical URI directly"
    assert caught.value.data == {"target": "moved", "candidates": ["agent:op:node-1:moved", "agent:someone-else:node-1:moved"]}


def test_worker_relay_does_not_admit_resolve():
    from hyprial.kernel import WorkerBinding, bound_request
    from hyprial.kernel.impl.contracts.channels.worker_channel import WorkerChannelError

    binding = WorkerBinding("agent:op:node-1:worker", "session-1", "epoch-1")
    with pytest.raises(WorkerChannelError) as caught:
        bound_request({"version": 1, "id": "1", "method": "agent.resolve", "params": {"name": "worker"}}, binding)
    assert caught.value.code == ipc_errors.METHOD_NOT_FOUND


def test_existing_agent_resolution_golden_is_pinned():
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[4] / "golden" / "snapshots"
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["methods"]["agent.resolve"]["scenarios"] == ["control/agents"]
    snapshot = json.loads((root / "control" / "agents.json").read_text())
    assert any(row.get("method") == "agent.resolve" and row.get("result", {}).get("reason") == "alias" for row in snapshot["steps"])
