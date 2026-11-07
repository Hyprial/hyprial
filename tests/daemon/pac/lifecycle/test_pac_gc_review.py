"""Destructive GC requires durable incarnation proof and retained close custody."""
import threading

import pytest

from hyprial.daemon.impl.pac.actors.gc import PacGc
from hyprial.daemon.impl.pac.graphs.authority import PacGraphAuthority
from hyprial.identity import PacError
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
from tests.daemon.pac.lifecycle.test_pac_gc import Application, Runtime, _close, _gc, _service, _start_owned
from tests.daemon.pac.workflows.runtime.test_pac_workflow_upgrade_safety import _new_application
from tests.identity.agents.registry.entity.support import application


@pytest.mark.parametrize("replacement", [False, True])
def test_gc_without_entity_receipt_preserves_current_unowned_incarnation(tmp_path, replacement):
    app = Application(tmp_path)
    service = _service(tmp_path)
    try:
        graph_id = _start_owned(service, "no-incarnation-proof")
        with_store = PacGraphStore(service.database)
        try:
            _close(with_store, graph_id)
            [receipt] = with_store.workflow_worker_receipts(graph_id)
            assert receipt["agent_entity_token"] is None
            actor_name = receipt["actor_name"]
        finally:
            with_store.close()
        current = app.agents.create(actor_name)
        if replacement:
            assert app.agents.destroy(actor_name)
            current = app.agents.create(actor_name)
        report = _gc(tmp_path, app, Runtime(), []).run_pass()
        preserved = app.agents.get(actor_name)
        assert preserved is not None, "GC deleted an incarnation with no workflow receipt"
        assert preserved.entity_token == current.entity_token
        assert report["removed"] == 0
        assert report["skipped"]["ownership-incarnation-unknown"] == 1
    finally:
        service.close()
        app.agents.close()


def test_degraded_gc_retains_handle_and_dependencies_until_close_succeeds(tmp_path, monkeypatch):
    allowed = threading.Event()
    graph_closes = []
    gc_close = PacGc.close
    graph_close = PacGraphAuthority.close

    def held_close(collector, timeout=5.0):
        collector._stop.set()
        return gc_close(collector, timeout) if allowed.is_set() else False

    def observed_graph_close(authority, timeout=5.0):
        graph_closes.append(allowed.is_set())
        return graph_close(authority, timeout)

    monkeypatch.setattr(PacGc, "close", held_close)
    monkeypatch.setattr(PacGraphAuthority, "close", observed_graph_close)
    monkeypatch.setattr(GraphWorkflowService, "recover", lambda _self: (_ for _ in ()).throw(PacError("RECOVER", "controlled failure")))
    app = _new_application(tmp_path)
    try:
        app._start_runtime()
        assert app._workflow_service is None
        collectors = [item for item in app._degraded_workflow_handles if isinstance(item, PacGc)]
        assert len(collectors) == 1
        assert graph_closes == [], "degrade closed a dependency of undrained GC"
        with pytest.raises(RuntimeError, match="GC.*did not stop"):
            app._close()
        assert collectors[0] in app._degraded_workflow_handles
        assert graph_closes == []
        allowed.set()
        app._close()
        assert collectors[0] not in app._degraded_workflow_handles
        assert graph_closes and all(graph_closes)
    finally:
        allowed.set()
        app._close()


def test_destroy_reservation_rejects_late_session_registration(tmp_path):
    app = application(tmp_path)
    reservation = None
    try:
        app.handle("agent.create", {"name": "foo"})
        agent = app.agents.require("foo")
        reservation = app._reserve_agent_destroy(agent.actor)
        with pytest.raises(Exception, match="destroy is already reserved"):
            app.handle("session.register", {
                "actor": agent.uri, "sessionRef": "gc-late-session",
                "cwd": str(tmp_path / "session-work"), "command": ["codex"],
                "source": "codex-app-server", "runtime": "codex_interactive",
            })
        assert not app._agent_session_domains.agent.read_runtime(agent.uri).online
        app._release_agent_destroy_reservation(reservation.reservation_token)
        reservation = None
        app.handle("session.register", {
            "actor": agent.uri, "sessionRef": "after-gc-reservation-release",
            "cwd": str(tmp_path / "session-work"), "command": ["codex"],
            "source": "codex-app-server", "runtime": "codex_interactive",
        })
        assert app._agent_session_domains.agent.read_runtime(agent.uri).online
    finally:
        if reservation is not None:
            app._release_agent_destroy_reservation(reservation.reservation_token)
        app._close()
        app._close_home_lease()
