"""The in-use guard on the real daemon runtime, after the worker spec is gone.

Terminal cleanup completes only once the worker's desired spec has been
deactivated, so PAC GC and the destroy recheck always observe a worker
without a spec.  The interactive-session signal must therefore come from the
agent record, and the guard must fail closed when presence cannot answer.
These tests use a real ``DaemonApplication`` (registry, session register,
destroy reservation) and the real ``DaemonActorRuntime``; presence is the
shared stub of the registry test-suite.
"""

from __future__ import annotations

from pathlib import Path

from hyprial.daemon.impl.pac.actors.daemon import DaemonActorRuntime
from hyprial.daemon.impl.pac.actors.gc import PacGc
from hyprial.daemon.impl.pac.graphs.edits import close_graph
from hyprial.daemon.impl.pac.graphs.reactor import NullSender
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
from tests.identity.agents.registry.entity.support import (
    NODE,
    OWNER,
    _close_test_applications as _close_test_applications,
    application,
    register_interactive,
)


def _attached(tmp_path: Path, name: str = "wf-attached", *, online: bool = True):
    app = application(tmp_path)
    app.handle("agent.create", {"name": name})
    agent = app.agents.require(name)
    app._presence._online = (agent.uri,) if online else ()
    register_interactive(app, agent.uri)
    return app, agent


def test_observe_reports_an_online_session_without_a_worker_spec(tmp_path: Path) -> None:
    app, _agent = _attached(tmp_path)
    observation = DaemonActorRuntime(app).observe("wf-attached")
    assert observation.present is False
    assert observation.interactive_online is True


def test_observe_reports_no_use_for_an_offline_or_absent_session(tmp_path: Path) -> None:
    app, _agent = _attached(tmp_path, online=False)
    assert DaemonActorRuntime(app).observe("wf-attached").interactive_online is False
    app.handle("agent.create", {"name": "wf-idle"})
    assert DaemonActorRuntime(app).observe("wf-idle").interactive_online is False


def test_guard_fails_closed_when_presence_cannot_answer(tmp_path: Path) -> None:
    app, _agent = _attached(tmp_path, online=False)
    app._presence = None
    assert DaemonActorRuntime(app).observe("wf-attached").interactive_online is True

    class _Broken:
        def actor_online(self, actor: str) -> bool:
            raise RuntimeError("presence transport not ready")

    app._presence = _Broken()
    assert DaemonActorRuntime(app).observe("wf-attached").interactive_online is True


def test_destroy_recheck_refuses_an_interactively_used_agent(tmp_path: Path) -> None:
    app, agent = _attached(tmp_path)
    result = app._destroy_agent(
        agent.uri,
        expected_entity_token=agent.entity_token,
        require_unpinned=True,
        require_offline=True,
    )
    assert result["destroyed"] is False
    assert result["reason"] == "worker-in-use"
    assert app.agents.get("wf-attached") is not None


def test_gc_on_the_real_runtime_skips_an_interactively_used_worker(
    tmp_path: Path,
) -> None:
    app = application(tmp_path)
    state = tmp_path
    service = GraphWorkflowService(
        state_dir=state, owner=OWNER, machine=NODE, sender=NullSender(),
        start_thread=False,
    )
    try:
        graph_id = service.start(
            yaml_text="version: 2\nname: in-use-gc\nnodes:\n  - id: work\n    task: work\n",
            sender=f"user:{OWNER}",
            operation_key="in-use-gc",
        )["graphId"]
        store = PacGraphStore(service.database)
        try:
            close_graph(store, graph_id, actor=f"user:{OWNER}")
            [receipt] = store.workflow_worker_receipts(graph_id)
            name = str(receipt["actor_name"])
            app.handle("agent.create", {"name": name})
            agent = app.agents.require(name)
            assert agent.uri == receipt["actor_uri"]
            with store.write():
                store._db.execute(
                    "UPDATE workflow_worker_receipts SET agent_entity_token=?,"
                    "state='down' WHERE graph_id=?",
                    (agent.entity_token, graph_id),
                )
                store._db.execute(
                    "UPDATE workflow_worker_cleanup_intents SET state='complete' "
                    "WHERE graph_id=?",
                    (graph_id,),
                )
        finally:
            store.close()
    finally:
        assert service.close()
    app._presence._online = (agent.uri,)
    register_interactive(app, agent.uri)

    gc = PacGc(
        state_dir=state,
        application=app,
        runtime=DaemonActorRuntime(app),
        logger=lambda *args, **kwargs: None,
        start_thread=False,
    )
    try:
        preview = gc.preview()
        report = gc.run_pass()
    finally:
        gc.close()
    assert preview["skipped"].get("worker-in-use") == 1, preview
    assert report["removed"] == 0
    assert app.agents.get(name) is not None


def test_guard_fails_closed_when_the_session_registry_cannot_answer(
    tmp_path: Path,
) -> None:
    app, _agent = _attached(tmp_path)

    def unavailable():
        raise RuntimeError("session domain closed")

    app._registered_interactive_actors = unavailable
    assert DaemonActorRuntime(app).observe("wf-attached").interactive_online is True


def test_gc_report_names_the_reason_the_destroy_recheck_refused(
    tmp_path: Path,
) -> None:
    from hyprial.daemon.impl.pac.actors.coordinator import RuntimeObservation

    class _Blind:
        def observe(self, _actor_name: str) -> RuntimeObservation:
            return RuntimeObservation(False)

    app = application(tmp_path)
    service = GraphWorkflowService(
        state_dir=tmp_path, owner=OWNER, machine=NODE, sender=NullSender(),
        start_thread=False,
    )
    try:
        graph_id = service.start(
            yaml_text="version: 2\nname: refused\nnodes:\n  - id: work\n    task: work\n",
            sender=f"user:{OWNER}",
            operation_key="refused-destroy",
        )["graphId"]
        store = PacGraphStore(service.database)
        try:
            close_graph(store, graph_id, actor=f"user:{OWNER}")
            [receipt] = store.workflow_worker_receipts(graph_id)
            name = str(receipt["actor_name"])
            app.handle("agent.create", {"name": name})
            agent = app.agents.require(name)
            with store.write():
                store._db.execute(
                    "UPDATE workflow_worker_receipts SET agent_entity_token=?,"
                    "state='down' WHERE graph_id=?",
                    (agent.entity_token, graph_id),
                )
                store._db.execute(
                    "UPDATE workflow_worker_cleanup_intents SET state='complete' "
                    "WHERE graph_id=?",
                    (graph_id,),
                )
        finally:
            store.close()
    finally:
        assert service.close()
    app._presence._online = (agent.uri,)
    register_interactive(app, agent.uri)

    gc = PacGc(
        state_dir=tmp_path,
        application=app,
        runtime=_Blind(),
        logger=lambda *args, **kwargs: None,
        start_thread=False,
    )
    try:
        report = gc.run_pass()
    finally:
        gc.close()
    assert report["selected"] == 1 and report["removed"] == 0
    assert report["skipped"].get("worker-in-use") == 1, report["skipped"]
    assert "destroy-noop" not in report["skipped"]
    assert app.agents.get(name) is not None


def test_gc_report_carries_a_pinned_refusal_too(tmp_path: Path) -> None:
    """Kills review mutation Y2: the reason is passed through, not fixed."""

    from hyprial.daemon.impl.pac.actors.coordinator import RuntimeObservation

    class _Blind:
        def observe(self, _actor_name: str) -> RuntimeObservation:
            return RuntimeObservation(False)

    app = application(tmp_path)
    app.handle("agent.create", {"name": "wf-pinned"})
    agent = app.agents.require("wf-pinned")
    real_destroy = app._destroy_agent

    def destroy_after_pin(name, **kwargs):
        app.agents.pin("gc-late-adapter", "wf-pinned")
        return real_destroy(name, **kwargs)

    class _Application:
        agents = app.agents
        _destroy_agent = staticmethod(destroy_after_pin)

    gc = PacGc(
        state_dir=tmp_path,
        application=_Application(),
        runtime=_Blind(),
        logger=lambda *args, **kwargs: None,
        start_thread=False,
    )
    from hyprial.daemon.impl.pac.actors.gc import _Candidate, _Selection

    gc._select = lambda: _Selection(
        (_Candidate("wf-graph", agent.uri, agent.actor, agent.entity_token),),
        {},
        (),
    )
    try:
        report = gc.run_pass()
    finally:
        gc.close()
    assert report["skipped"] == {"pinned": 1}, report["skipped"]
    assert app.agents.get("wf-pinned") is not None
