"""Characterize the legacy history list before its exact registration."""

import json
from contextlib import closing, contextmanager
import sqlite3

import pytest

from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from tests.daemon.ipc.contracts.agent.selector_fence.test_selector_fence import (
    _register_live_session, app as app,
)
from tests.kernel.actors.identity.support import application
from tests.support.runtime._owned_applications import owned_applications as owned_applications


@contextmanager
def database(path):
    with closing(sqlite3.connect(path)) as db:
        with db:
            yield db


@pytest.fixture
def archive(tmp_path):
    app = application(tmp_path)
    with database(app.state_dir / "workflows.sqlite3") as db:
        db.executescript("""
            CREATE TABLE runs(run_id TEXT PRIMARY KEY,name TEXT,state TEXT,sender TEXT,
                              created_at_ms INTEGER,finished_at_ms INTEGER);
            INSERT INTO runs VALUES ('own', 'one', 'done', 'user:op', 2, NULL);
            INSERT INTO runs VALUES ('foreign', 'two', 'done', 'user:someone', 1, 3);
        """)
    return app


def wire(app, params):
    return app._response(json.dumps({"version": 1, "id": "history", "method": "workflow.history.list", "params": params}).encode())


def test_list_gets_an_exact_registered_binding():
    assert METHOD_REGISTRY.get("workflow.history.list") is not None


@pytest.mark.parametrize("value,count", [(None, None), (True, 1), (False, 1), (0, 1), (-4, 1), (2, 2), (700, 2), (1.9, 1), ("  +2 ", 2), ("1", 1), ("", None), ("1.0", None), ([], None), ({}, None), (float("nan"), None), (float("inf"), None), (-float("inf"), None)])
def test_old_wire_limit_conversion_and_clamp(archive, value, count):
    result = wire(archive, {"actor": "user:op", "limit": value, "ignored": True})
    if count is None:
        assert result["error"]["code"] == "DAEMON_ERROR"
    else:
        assert len(result["result"]["runs"]) == count


def test_default_owner_projection_and_missing_identity_precedence(archive):
    rows = wire(archive, {"actor": "user:op"})["result"]["runs"]
    assert [r["runId"] for r in rows] == ["own", "foreign"]
    assert rows[0] == {"run_id": "own", "name": "one", "state": "done", "sender": "user:op", "created_at_ms": 2, "finished_at_ms": None, "runId": "own", "backend": "legacy-archive", "readOnly": True}
    refusal = wire(archive, {"limit": None})
    assert refusal["error"]["code"] == "PAC_PRINCIPAL_UNVERIFIED"
    assert refusal["error"]["data"] == {"cause": "CALLER_NOT_AUTHORIZED"}


def test_nonowner_archive_is_filtered_by_verified_caller(app):
    app.agents.create("other")
    _register_live_session(app, "other", "history-session")
    caller = app.agents.uri_for("other")
    with database(app.state_dir / "workflows.sqlite3") as db:
        db.executescript("CREATE TABLE runs(run_id TEXT,name TEXT,state TEXT,sender TEXT,created_at_ms INTEGER,finished_at_ms INTEGER)")
        db.executemany("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)", [
            ("own", "one", "done", caller, 2, None),
            ("foreign", "two", "done", "user:other", 1, None),
        ])
    result = wire(app, {"actor": "other", "sessionRef": "history-session"})
    assert [row["runId"] for row in result["result"]["runs"]] == ["own"]


def test_missing_binding_cannot_fall_back_to_legacy(archive, monkeypatch):
    from hyprial.daemon.impl.ipc import router
    from tests.daemon.ipc.contracts.fallback_guard.test_migrated_fallbacks import _WithoutOne

    name = "workflow.history.list"
    assert name in router.MIGRATED_METHODS
    monkeypatch.setattr(router, "METHOD_REGISTRY", _WithoutOne(router.METHOD_REGISTRY, name))
    assert wire(archive, {"actor": "user:op"})["error"]["code"] == "METHOD_NOT_FOUND"


def test_limit_is_clamped_to_500_with_descending_archive_order(archive):
    with database(archive.state_dir / "workflows.sqlite3") as db:
        db.executemany("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)", [
            (f"extra-{i}", "n", "done", "user:op", 100 + i, None)
            for i in range(505)
        ])
    runs = wire(archive, {"actor": "user:op", "limit": 10000})["result"]["runs"]
    assert len(runs) == 500
    assert runs[0]["runId"] == "extra-504"
    assert runs[-1]["runId"] == "extra-5"


def test_worker_relay_still_refuses_history_list():
    from hyprial.kernel import WorkerBinding, WorkerChannelError, bound_request

    binding = WorkerBinding("agent:op:node-1:worker", "session", "epoch")
    with pytest.raises(WorkerChannelError) as caught:
        bound_request({"version": 1, "id": "1", "method": "workflow.history.list", "params": {}}, binding)
    assert caught.value.code == "METHOD_NOT_FOUND"


def test_archive_nulls_and_dynamic_sqlite_scalars_remain_in_projection(archive):
    with database(archive.state_dir / "workflows.sqlite3") as db:
        db.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
                   ("legacy", None, None, "user:op", "legacy-time", "legacy-finish"))
    rows = wire(archive, {"actor": "user:op"})["result"]["runs"]
    row = next(row for row in rows if row["runId"] == "legacy")
    assert row["name"] is None and row["state"] is None
    assert row["created_at_ms"] == "legacy-time" and row["finished_at_ms"] == "legacy-finish"


def test_socket_preserves_nonobject_validation_and_restore(tmp_path, monkeypatch):
    import os
    import threading

    from hyprial.kernel import RequestPortError, daemon_request_once, ipc_errors
    from tests.daemon.application.transport.resilience.support import _IdleRuntime

    app = application(tmp_path)
    with database(app.state_dir / "workflows.sqlite3") as db:
        db.execute("CREATE TABLE runs(run_id TEXT,name TEXT,state TEXT,sender TEXT,created_at_ms INTEGER,finished_at_ms INTEGER)")
        db.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)", ("own", "one", "done", "user:op", 2, None))
    app._runtime = _IdleRuntime()
    monkeypatch.setattr(app, "_start_maintenance_scheduler", lambda: None)
    app._start_server()
    thread = threading.Thread(target=app._serve, daemon=True)
    thread.start()
    try:
        result = daemon_request_once("workflow.history.list", {"actor": "user:op"}, socket_path=app.socket_path, timeout=10)
        assert result["runs"][0]["runId"] == "own"
        with pytest.raises(RequestPortError) as invalid:
            daemon_request_once("workflow.history.list", [], socket_path=app.socket_path, timeout=10)
        assert invalid.value.code == "INVALID_ARGUMENT"
        assert str(invalid.value) == "params must be an object"
        app._restore_done.clear()
        with pytest.raises(ipc_errors.DaemonRestoringError) as restoring:
            daemon_request_once("workflow.history.list", {"actor": "user:op"}, socket_path=app.socket_path, timeout=10)
        assert restoring.value.code == "DAEMON_RESTORING"
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


def test_existing_empty_history_golden_is_pinned():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "golden" / "snapshots"
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["methods"]["workflow.history.list"]["covered"] is True
    assert "control/workflows-live" in manifest["methods"]["workflow.history.list"]["scenarios"]
    snapshot = json.loads((root / "control" / "workflows-live.json").read_text())
    assert any(step.get("method") == "workflow.history.list" and step.get("result") == {"runs": []} for step in snapshot["steps"])
