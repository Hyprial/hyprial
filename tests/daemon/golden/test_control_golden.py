"""G0 goldens for the ``session.*``, ``routine.*`` and PAC/workflow methods.

These are the control-plane methods phase A touches outside ``message.*``:
S3 registers agents as cells next to session registration.

``agents`` and ``workflows`` record the degraded daemon (no routine or
workflow service: ``AGENT_DEFAULT_ROUTINE_UNAVAILABLE`` and
``WORKFLOW_UNAVAILABLE``).  ``routines`` ends with ``agent.create`` against
the live routine service, which binds the default ``agent-home-setup``
routine, and ``workflows-live`` runs a real
``GraphWorkflowService``: start, list, status, node.inspect, complete, fail,
cancel and the PAC worker's ``message.reply`` to a ``workflow-<uuid>``
dispatch, whose outbox row carries ``reply:workflow-<uuid>``.
The routine service is the real ``RoutineService`` behind its shell adapter
(as the agent-entity suite wires it); PAC graphs live in the real
``PacGraphStore`` of the daemon state directory.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
import yaml

from hyprial.biz.impl.routine.execution.service import RoutineService
from hyprial.daemon.impl.pac.graphs.edits import add_node, create_graph
from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path
from hyprial.shell.impl.daemon_wiring.routine_service import RoutineServiceAdapter
from tests.support.golden.app import (
    ALPHA,
    BETA,
    GAMMA,
    OWNER_USER,
    SESSIONS,
    GoldenDaemon,
    golden_daemon as golden_daemon,
)
from tests.support.golden.snapshot import Golden

GROUP = "control"


class _RoutinePac:
    def rearm_boundary(self, *, routine_name: str) -> int:
        del routine_name
        return 0


def _routine(name: str, actor: str, *, interval: str = "15m") -> str:
    return yaml.safe_dump(
        {
            "version": 2,
            "mode": "scheduled",
            "name": name,
            "schedule": {"interval": interval},
            "task": "golden duty",
            "actor": actor,
            "role": "review",
            "on_task_timeout": {"action": "escalate", "escalate_to": OWNER_USER},
        }
    )


def _workflow(owner: str) -> str:
    return yaml.safe_dump(
        {
            "version": 2,
            "name": "golden-flow",
            "nodes": [{"id": "work", "owner": owner, "task": "work", "role": "review"}],
        }
    )


def scenario_sessions(daemon: GoldenDaemon) -> Golden:
    golden = daemon.golden(GROUP, "sessions")
    alpha = {"actor": "alpha", "sessionRef": SESSIONS["alpha"]}
    register = {"cwd": str(daemon.root / "work"), "command": ["golden-carrier"],
                "source": "claude-channel", "runtime": "claude_interactive"}
    golden.call("session.heartbeat", alpha)
    golden.call("session.refresh", alpha)
    golden.call("session.turn.started", {**alpha, "reportId": "turn-1"})
    golden.call("session.turn.ended", {**alpha, "reportIds": ["turn-1"]})
    golden.call(
        "session.turn.refresh",
        {**alpha, "schemaVersion": 1, "producerEpoch": "producer-1", "seq": 1,
         "openRuns": [{"reportId": "run-1", "phase": "executing", "elapsedMs": 10}],
         "capabilities": {"wholeSessionTurns": True, "preparation": False,
                          "permissionWait": False, "producerActivity": False}},
    )
    golden.call(
        "session.turn.refresh",
        {**alpha, "schemaVersion": 1, "producerEpoch": "producer-1", "seq": 2,
         "daemonEpoch": daemon.app.epoch, "openRuns": [],
         "capabilities": {"wholeSessionTurns": True, "preparation": False,
                          "permissionWait": False, "producerActivity": False}},
        label="current-epoch",
    )
    golden.call("session.turn.refresh", {**alpha, "schemaVersion": 2}, label="schema")
    golden.call("session.turn.started", {**alpha}, label="missing-report-id")
    golden.call("session.heartbeat", {"actor": "alpha", "sessionRef": "session-bogus"},
                label="stale")
    golden.call("session.register", {"actor": "alpha", "sessionRef": SESSIONS["alpha"],
                                     **register}, label="same-session")
    golden.call("session.register", {"actor": "alpha", "sessionRef": "session-alpha-2",
                                     **register}, label="supersede")
    golden.call("session.heartbeat", alpha, label="superseded")
    golden.call("session.register_guarded", {"actor": "alpha", "sessionRef": SESSIONS["alpha"],
                                             **register}, label="guarded-reclaim")
    golden.call("session.register_guarded", {"actor": "delta", "sessionRef": "session-delta",
                                             **register}, label="guarded-unknown-agent")
    golden.call("session.unregister", {"actor": "beta", "sessionRef": SESSIONS["beta"]})
    golden.call("session.heartbeat", {"actor": "beta", "sessionRef": SESSIONS["beta"]},
                label="after-unregister")
    golden.call("session.unregister", {"actor": "beta", "sessionRef": SESSIONS["beta"]},
                label="repeat")
    golden.call("session.register", {"actor": "gamma"}, label="missing-fields")
    golden.call("session.register_guarded", {"actor": "beta", "sessionRef": "session-beta-2",
                                             **register}, label="guarded-after-unregister")
    golden.call("session.heartbeat", {"actor": "beta", "sessionRef": "session-beta-2"},
                label="after-guarded-register")
    return golden


def _enable_routines(daemon: GoldenDaemon) -> None:
    service = RoutineService(
        pac=_RoutinePac(), alarm=object(), state_dir=daemon.app.state_dir
    )
    daemon.app._routine_service = RoutineServiceAdapter(service)


def scenario_routines(daemon: GoldenDaemon) -> Golden:
    golden = daemon.golden(GROUP, "routines")
    golden.call("routine.list", {"actor": OWNER_USER}, label="unavailable")
    golden.call("routine.add", {"yaml": _routine("golden-duty", ALPHA), "actor": OWNER_USER},
                label="unavailable")
    _enable_routines(daemon)
    golden.call("routine.list", {"actor": OWNER_USER}, label="empty")
    golden.call("routine.add", {"yaml": _routine("golden-duty", ALPHA), "actor": OWNER_USER})
    golden.call("routine.add", {"yaml": _routine("golden-duty", ALPHA), "actor": OWNER_USER},
                label="duplicate")
    golden.call("routine.add", {"yaml": "version: 2\nname: [", "actor": OWNER_USER},
                label="bad-yaml")
    golden.call("routine.list", {"actor": OWNER_USER})
    golden.call("routine.status", {"name": "golden-duty", "actor": OWNER_USER})
    golden.call("routine.set", {"name": "golden-duty", "actor": OWNER_USER,
                                "yaml": _routine("golden-duty", ALPHA, interval="30m")})
    golden.call("routine.pause", {"name": "golden-duty", "actor": OWNER_USER})
    golden.call("routine.resume", {"name": "golden-duty", "actor": OWNER_USER})
    golden.call("routine.status", {"name": "golden-duty", "actor": BETA,
                                   "sessionRef": SESSIONS["beta"]}, label="not-owner")
    golden.call("routine.status", {"name": "missing", "actor": OWNER_USER}, label="missing")
    golden.call("routine.audit", {"actor": OWNER_USER})
    golden.call("routine.remove", {"name": "golden-duty", "actor": OWNER_USER},
                label="last-binding")
    golden.call("routine.add", {"yaml": _routine("golden-duty-2", ALPHA), "actor": OWNER_USER},
                label="second-binding")
    golden.call("routine.remove", {"name": "golden-duty", "actor": OWNER_USER})
    golden.call("routine.list", {"actor": OWNER_USER}, label="after-remove")
    golden.call("agent.create", {"name": "delta"}, label="default-routine")
    golden.call("routine.list", {"actor": OWNER_USER}, label="after-agent-create")
    golden.call("agent.create", {"name": "delta", "existing": "reuse"},
                label="reuse-routine-bound")
    golden.call("agent.create", {"name": "delta"}, label="duplicate")
    golden.call("routine.list", {"actor": OWNER_USER}, label="after-reuse")
    return golden


def _graph(daemon: GoldenDaemon) -> str:
    store = PacGraphStore(default_database_path(daemon.app.state_dir))
    try:
        graph_id = create_graph(store, name="golden", created_by=ALPHA)["graphId"]
        add_node(
            store,
            daemon.app.state_dir,
            graph_id=graph_id,
            node_id="work",
            owner=ALPHA,
            brief_ref="r#work",
            expect_version=1,
        )
        return str(graph_id)
    finally:
        store.close()


def scenario_workflows(daemon: GoldenDaemon) -> Golden:
    golden = daemon.golden(GROUP, "workflows")
    golden.call("workflow.plan", {"yaml": _workflow(ALPHA), "actor": OWNER_USER})
    golden.call("workflow.plan", {"yaml": _workflow("agent:op:node-1:nobody"),
                                  "actor": OWNER_USER}, label="unknown-owner")
    golden.call("workflow.plan", {"yaml": "version: 2\n", "actor": OWNER_USER}, label="schema")
    golden.call("workflow.start", {"yaml": _workflow(ALPHA), "actor": OWNER_USER},
                label="no-service")
    golden.call("workflow.list", {"actor": OWNER_USER}, label="no-service")
    for method in ("workflow.status", "workflow.cancel", "workflow.node.inspect"):
        golden.call(method, {"actor": OWNER_USER, "runId": "workflow-missing"},
                    label="no-service")
    golden.call("workflow.history.list", {"actor": OWNER_USER}, label="legacy-history-empty")
    golden.call("workflow.history.status", {"actor": OWNER_USER}, label="missing-run-id")
    for method in ("workflow.complete", "workflow.fail"):
        golden.call(method, {"actor": ALPHA, "sessionRef": SESSIONS["alpha"],
                             "workflowId": "workflow-missing", "nodeId": "work"},
                    label="no-service")
    golden.call("pac.gc.preview", {})
    graph_id = golden.alias(_graph(daemon), "<graph:golden>")
    alpha = {"actor": ALPHA, "sessionRef": SESSIONS["alpha"]}
    golden.call("pac.graph.activate", {"graphId": graph_id}, label="unbound")
    golden.call("pac.graph.activate", {"graphId": graph_id, "actor": BETA,
                                       "sessionRef": SESSIONS["beta"]}, label="not-owner")
    golden.call("pac.graph.activate", {"graphId": graph_id, **alpha})
    golden.call("pac.flag.set", {"graphId": graph_id, "nodeId": "work", **alpha})
    golden.call("pac.flag.reset", {"graphId": graph_id, "nodeId": "work", **alpha})
    golden.call("pac.flag.set", {"graphId": "no-such-graph", "nodeId": "work", **alpha},
                label="missing-graph")
    golden.call("pac.graph.close", {"graphId": graph_id, **alpha})
    golden.call("pac.graph.close", {"graphId": graph_id, **alpha}, label="repeat")
    golden.call("pac.actor.stop", {"graphId": graph_id, "nodeId": "work", **alpha},
                label="missing-actor-name")
    golden.call("pac.actor.stop", {"graphId": graph_id, "actorName": "wf-golden-work", **alpha})
    golden.call("message.pending.list", {"from": ALPHA, "sessionRef": SESSIONS["alpha"]},
                label="pac-notifications")
    return golden


def _node(status: object, node_id: str = "work") -> dict:
    assert isinstance(status, dict), status
    return next(node for node in status["nodes"] if node["nodeId"] == node_id)


def scenario_workflows_live(daemon: GoldenDaemon) -> Golden:
    golden = daemon.golden(GROUP, "workflows-live")
    daemon.enable_workflows()
    beta = {"actor": BETA, "sessionRef": SESSIONS["beta"]}
    owner = {"actor": OWNER_USER}
    golden.call("workflow.list", owner, label="empty")
    start = {"yaml": _workflow(BETA), **owner, "operationKey": "golden-run"}
    started = golden.call("workflow.start", start)
    golden.call("workflow.start", start, label="idempotent-replay")
    run = str(started["graphId"])
    daemon.workflow_tick()
    golden.call("workflow.list", owner)
    golden.call("workflow.list", beta, label="node-owner")
    golden.call("workflow.list", {"actor": GAMMA, "sessionRef": SESSIONS["gamma"]},
                label="non-participant")
    status = golden.call("workflow.status", {**owner, "runId": run})
    golden.call("workflow.status", {"actor": GAMMA, "sessionRef": SESSIONS["gamma"],
                                    "runId": run}, label="non-participant")
    golden.call("workflow.status", {**owner, "runId": "workflow-missing"}, label="missing")
    golden.call("workflow.node.inspect", {**owner, "runId": run, "target": "work"})
    golden.call("workflow.node.inspect", {**owner, "runId": run, "target": "nope"},
                label="missing-node")
    pending = golden.call("message.pending.list", {"from": BETA, "sessionRef": SESSIONS["beta"]},
                          label="dispatch-arrived")
    dispatch = str(pending["messages"][0]["messageId"])
    worker = {"from": BETA, "sessionRef": SESSIONS["beta"]}
    golden.call("message.read_body", {**worker, "messageId": dispatch})
    reply = golden.call("message.reply", {**worker, "messageId": dispatch,
                                          "summary": "worker answer",
                                          "message": "worker answer"}, label="pac-worker")
    golden.call("message.reply", {**worker, "messageId": dispatch, "summary": "worker answer",
                                  "message": "worker answer"}, label="pac-worker-repeat")
    golden.call("message.status", {**worker, "messageId": dispatch}, label="dispatch")
    golden.call("message.status", {**worker, "messageId": str(reply["replyMessageId"])},
                label="pac-worker-reply")
    golden.call("message.query", {"from": BETA, "view": "outbox"}, label="pac-worker-reply")
    golden.observe("outbox-row-keys", daemon.outbox_keys())
    golden.call("message.pending.list", worker, label="after-reply")
    request = str(_node(status)["requestId"])
    complete = {**beta, "graphId": run, "nodeId": "work", "requestId": request,
                "reasonRef": "result:golden"}
    golden.call("workflow.complete", {**complete, "requestId": "request-stale"},
                label="stale-request")
    golden.call("workflow.complete", {**complete, "sessionRef": "session-bogus"},
                label="unverified-session")
    golden.call("workflow.complete", complete)
    golden.call("workflow.complete", complete, label="repeat")
    daemon.workflow_tick()
    golden.call("workflow.status", {**owner, "runId": run}, label="after-complete")
    golden.call("workflow.cancel", {**owner, "runId": run}, label="after-complete")
    second = golden.call("workflow.start", {"yaml": _workflow(BETA), **owner,
                                            "operationKey": "golden-run-2"}, label="second")
    second_run = str(second["graphId"])
    daemon.workflow_tick()
    second_status = golden.call("workflow.status", {**owner, "runId": second_run}, label="second")
    golden.call("workflow.fail", {**beta, "graphId": second_run, "nodeId": "work",
                                  "requestId": str(_node(second_status)["requestId"]),
                                  "reasonRef": "result:failed"})
    daemon.workflow_tick()
    golden.call("workflow.status", {**owner, "runId": second_run}, label="after-fail")
    third = golden.call("workflow.start", {"yaml": _workflow(BETA), **owner,
                                           "operationKey": "golden-run-3"}, label="third")
    third_run = str(third["graphId"])
    daemon.workflow_tick()
    golden.call("workflow.cancel", {**beta, "runId": third_run}, label="node-owner")
    golden.call("workflow.cancel", {**owner, "runId": third_run})
    daemon.workflow_tick()
    golden.call("workflow.status", {**owner, "runId": third_run}, label="after-cancel")
    golden.call("workflow.list", owner, label="final")
    golden.call("workflow.history.list", owner, label="legacy-empty")
    golden.call("workflow.history.status", {**owner, "runId": run}, label="legacy-missing")
    golden.call("message.pending.list", worker, label="final")
    golden.observe(
        "user-deliveries",
        [
            {"messageId": r.message_id, "key": r.idempotency_key, "owner": r.owner,
             "sender": r.sender, "conversationId": r.conversation_id}
            for r in daemon.user_delivery.requests
        ],
    )
    return golden


def scenario_agents(daemon: GoldenDaemon) -> Golden:
    from hyprial.daemon.impl.composition.cells import cell_runtime

    runtime = cell_runtime(daemon.app)
    assert runtime is not None
    golden = daemon.golden(GROUP, "agents")
    golden.call("agent.list", {})
    golden.call("agent.get", {"name": "alpha"})
    golden.call("agent.resolve", {"name": "alpha"})
    golden.call("agent.create", {"name": "delta"})
    golden.call("agent.create", {"name": "delta"}, label="duplicate")
    golden.call("agent.create", {"name": "delta", "existing": "reuse"}, label="reuse")
    golden.call("agent.get", {"name": "nobody"}, label="missing")
    golden.call("agent.list", {}, label="after-create")
    golden.call("agent.cell.get", {"name": "delta"})
    golden.call(
        "agent.cell.update",
        {
            "name": "delta",
            "expectedRevision": 1,
            "members": [
                {"ref": "delta", "role": "harness"},
                {"ref": "lark-golden", "role": "adapter"},
            ],
            "params": {
                "cell.entry": "delta",
                "cell.members": {"delta": {}, "lark-golden": {"adapter": "lark-golden"}},
            },
        },
        label="bind-adapter",
    )
    golden.call("agent.cell.get", {"name": "delta"}, label="after-bind")
    golden.call(
        "agent.cell.update",
        {"name": "delta", "expectedRevision": 9, "cellName": "stale"},
        label="cas-conflict",
    )
    golden.call("agent.cell.get", {"name": "nobody"}, label="missing")
    return golden


SCENARIOS: dict[str, tuple[dict[str, bool], Callable[[GoldenDaemon], Golden]]] = {
    "sessions": ({}, scenario_sessions),
    "routines": ({}, scenario_routines),
    "workflows": ({}, scenario_workflows),
    "workflows-live": ({}, scenario_workflows_live),
    "agents": ({}, scenario_agents),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_control_golden(golden_daemon: Callable[..., GoldenDaemon], name: str) -> None:
    options, scenario = SCENARIOS[name]
    scenario(golden_daemon(**options)).check()
