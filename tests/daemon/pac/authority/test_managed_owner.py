"""Managed ownership resolves a pinned incarnation without rewriting graph history."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import yaml

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.pac.actors.coordinator import ActorCoordinator, FileLaunchResolver, RuntimeObservation
from hyprial.daemon.impl.pac.graphs.authority import PacGraphAuthority
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
from hyprial.kernel import DaemonRequestError, HarnessLaunchSpec
from hyprial.shell.impl.daemon_wiring import daemon_dependencies


class Sender:
    def send(self, **kwargs):
        return kwargs["idempotency_key"]


class LocalWorkerPort:
    def __init__(self, app):
        self.app = app
        self.running = {}
        self.refs = {}

    def status(self):
        return tuple({"name": name, "runtime": seen.harness, "running": True} for name, seen in self.running.items())

    def projected_worker_session_refs(self):
        return dict(self.refs)

    def observe(self, name):
        return self.running.get(name, RuntimeObservation(False))

    def start(self, name, launch, *, operation_id, identity_marker):
        agent = self.app.agents.create(name)
        self.app.desired_state.upsert_harness(HarnessLaunchSpec(
            harness=launch.harness, name=name, headless=True, nickname=identity_marker,
        ))
        self.refs[launch.harness, name] = "local-owned-worker-session"
        observed = RuntimeObservation(True, identity_marker, launch.harness, operation_id,
                                      agent_entity_token=agent.entity_token)
        self.running[name] = observed
        return observed

    def stop(self, name, launch, *, operation_id, identity_marker):
        assert self.running[name].identity_marker == identity_marker
        self.running.pop(name)
        return RuntimeObservation(False)


@pytest.fixture(params=["direct", "authority"])
def world(tmp_path, monkeypatch, request):
    home, state = tmp_path / "hyprial", tmp_path / "state"
    home.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    (home / "settings.json").write_text(json.dumps({"version": 1, "owner": "owner-probe"}))
    for key, value in {
        "HOME": str(tmp_path / "os-home"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "xdg-state"), "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "HYPRIAL_HOME": str(home), "HARNESS_STATE_DIR": str(state),
        "HYPRIAL_OWNER": "owner-probe", "HYPRIAL_NETWORK_ISOLATED": "1",
        "HYPRIAL_ZENOH_LISTEN": "", "HYPRIAL_ZENOH_CONNECT": "",
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("DBUS_SESSION_BUS_ADDRESS", "HARNESS_SOCKET_PATH", "HARNESS_CONFIG",
                "HYPRIAL_WORKER_ACTOR", "HYPRIAL_WORKER_SESSION_REF", "HYPRIAL_MANAGED_WORKER"):
        monkeypatch.delenv(key, raising=False)
    app = DaemonApplication(state_dir=state, socket_path=tmp_path / "daemon.sock",
                            hyprial_home=home, node_id="owner-probe-node", owner="owner-probe",
                            **daemon_dependencies())
    runtime = LocalWorkerPort(app)
    app._harnesses = SimpleNamespace(status=runtime.status,
                                     projected_worker_session_refs=runtime.projected_worker_session_refs)
    app._inbox = SimpleNamespace(pending_messages=lambda _actor: ())
    app._presence = SimpleNamespace(online_actors=lambda: ())
    app._restore_done.set()
    from hyprial.daemon.impl.pac.storage.store import default_database_path
    authority = (PacGraphAuthority(default_database_path(state), Sender(), agent_reader=app.agents.current)
                 if request.param == "authority" else None)
    service = GraphWorkflowService(state_dir=state, owner=app.owner, machine=app.node_id,
                                   sender=Sender(), start_thread=authority is not None,
                                   graph_authority=authority, agent_reader=app.agents.current)
    app._workflow_service = service
    app._pac_graph_authority = authority
    store = PacGraphStore(service.database)
    try:
        graph = service.start(
            yaml_text=yaml.safe_dump({"version": 2, "name": "managed-owner-probe",
                                     "on_failure": "hold", "nodes": [
                                         {"id": "work", "task": "complete this node"},
                                         {"id": "later", "task": "later", "owner": "user:owner-probe", "after": ["work"]},
                                     ]}),
            sender="user:owner-probe", operation_key="managed-probe",
        )["graphId"]
        coordinator = ActorCoordinator(store, runtime, daemon_epoch="probe-epoch",
                                       resolver=FileLaunchResolver(tmp_path))
        coordinator.reconcile_all()
        if authority is None:
            service._tick()
        else:
            authority.workflow_tick(service.clock())
        row = next(row for row in service.status(run_id=graph)["nodes"] if row["nodeId"] == "work")
        receipt = store.workflow_worker_receipts(graph)[0]
        agent = app.agents.get(receipt["actor_name"])
        assert agent is not None
        assert receipt["agent_entity_token"] == agent.entity_token
        claims = {"actor": agent.uri, "sessionRef": "local-owned-worker-session"}
        assert app._worker_session_ref(agent.uri) == claims["sessionRef"]
        assert app._workflow_caller(claims) == agent.uri
        assert row["state"] == "requested" and row["requestId"]
        yield SimpleNamespace(app=app, service=service, store=store, graph=graph,
                              row=row, receipt=receipt, agent=agent, claims=claims,
                              runtime=runtime, coordinator=coordinator)
    finally:
        store.close()
        assert service.close()
        if authority is not None:
            assert authority.close()
        app._workflow_service = None
        app._pac_graph_authority = None
        app._close()
        app._close_home_lease()


def completion(world, graph=None, request=None, claims=None):
    return world.app.handle("workflow.complete", {
        **(world.claims if claims is None else claims), "graphId": graph or world.graph,
        "nodeId": "work", "requestId": request or world.row["requestId"],
        "reasonRef": "local-probe:completed",
    }, _trusted_message_origin="ipc")


@pytest.mark.parametrize("spelling", ["official", "compiled"])
def test_managed_worker_with_verified_current_identity_can_complete_its_node(world, spelling):
    assert world.receipt["actor_uri"] == world.row["owner"]
    claims = {**world.claims, "actor": world.agent.uri if spelling == "official" else world.row["owner"]}
    assert world.app._workflow_caller(claims) == world.agent.uri
    try:
        result = completion(world, claims=claims)
    except DaemonRequestError as error:
        pytest.fail(f"{error.code}: {error}", pytrace=False)
    assert result["ok"] is True


def test_same_verified_worker_completes_borrowed_official_owner_graph(world):
    graph = world.service.start(
        yaml_text=yaml.safe_dump({"version": 2, "name": "official-owner-control",
                                 "nodes": [{"id": "work", "task": "complete", "owner": world.agent.uri}]}),
        sender="user:owner-probe", operation_key="borrowed-control",
    )["graphId"]
    if world.app._pac_graph_authority is None:
        world.service._tick()
    else:
        world.app._pac_graph_authority.workflow_tick(world.service.clock())
    row = world.service.status(run_id=graph)["nodes"][0]
    result = completion(world, graph, row["requestId"])
    assert result["ok"] is True and result["actingPrincipal"] == world.agent.uri


@pytest.mark.parametrize("claims", [{"sessionRef": "stale-session"}, {"actor": "agent:foreign:other:worker"}])
def test_invalid_session_or_foreign_actor_remains_refused(world, claims):
    with pytest.raises(DaemonRequestError) as denied:
        completion(world, claims={**world.claims, **claims})
    assert denied.value.code == "PAC_PRINCIPAL_UNVERIFIED"


def operation(world, method, *, claims=None):
    params = {**(world.claims if claims is None else claims), "graphId": world.graph,
              "nodeId": "work", "reasonRef": "test:managed-owner"}
    if method == "workflow.fail":
        params["requestId"] = world.row["requestId"]
    elif method == "pac.flag.set":
        params["expectedRequest"] = world.row["requestId"]
    return world.app.handle(method, params, _trusted_message_origin="ipc")


@pytest.mark.parametrize("method", ["pac.flag.set", "pac.flag.reset", "workflow.fail"])
def test_managed_owner_operations_keep_persisted_identity_and_official_event_actor(world, method):
    if method == "pac.flag.reset":
        operation(world, "pac.flag.set")
    before = world.store.workflow_worker_receipts(world.graph)
    roster = world.store.workflow_roster(world.graph)
    outcome = operation(world, method)
    if method == "workflow.fail":
        assert outcome["state"] == "held"
    else:
        assert outcome["ok"] is True
    assert world.store.node(world.graph, "work").owner == world.row["owner"]
    assert world.store.workflow_worker_receipts(world.graph) == before
    assert world.store.workflow_roster(world.graph) == roster
    if method != "workflow.fail":
        event = world.store.flag_events(world.graph)[-1]
        assert event.actor == world.agent.uri
    else:
        receipt = world.store._db.execute("SELECT actor FROM workflow_outcome_receipts WHERE request_id=?",
                                          (world.row["requestId"],)).fetchone()
        assert receipt["actor"] == world.agent.uri


@pytest.mark.parametrize("method", ["workflow.complete", "pac.flag.set", "pac.flag.reset", "workflow.fail"])
@pytest.mark.parametrize("fault", ["missing-token", "missing-receipt", "new-incarnation"])
def test_managed_owner_proof_failures_do_not_become_name_alias_authority(world, method, fault):
    if method == "pac.flag.reset":
        operation(world, "pac.flag.set")
    claims = world.claims
    if fault == "new-incarnation":
        replacement = world.app._agent_session_domains._registry.rotate_secret_authority(world.agent.actor)
        assert world.app.agents.current(world.agent.actor) == replacement
        assert world.app._workflow_caller(claims) == world.agent.uri
    else:
        with world.store.write():
            if fault == "missing-token":
                world.store._db.execute("UPDATE workflow_worker_receipts SET agent_entity_token=NULL WHERE graph_id=?", (world.graph,))
            else:
                world.store._db.execute("DELETE FROM workflow_worker_receipts WHERE graph_id=?", (world.graph,))
    with pytest.raises(DaemonRequestError) as refused:
        if method == "workflow.complete":
            completion(world, claims=claims)
        else:
            operation(world, method, claims=claims)
    assert refused.value.code == ("WORKFLOW_NOT_OWNER" if method == "workflow.fail" else "PAC_FLAG_NOT_OWNER")


@pytest.mark.parametrize("method", ["workflow.complete", "pac.flag.set", "workflow.fail"])
def test_current_registry_read_failure_refuses_managed_owner(world, monkeypatch, method):
    def unavailable(_name):
        raise RuntimeError("isolated durable identity read unavailable")

    monkeypatch.setattr(world.app._agent_session_domains.agent, "read_current_agent", unavailable)
    assert world.app._workflow_caller(world.claims) == world.agent.uri
    with pytest.raises(DaemonRequestError) as refused:
        if method == "workflow.complete":
            completion(world)
        else:
            operation(world, method)
    assert refused.value.code == ("WORKFLOW_NOT_OWNER" if method == "workflow.fail" else "PAC_FLAG_NOT_OWNER")


@pytest.mark.parametrize("fault", [None, "missing-token", "rotate-after-preflight"])
def test_managed_expansion_checks_live_identity_at_preflight_and_commit(world, monkeypatch, fault):
    policy = {"version": 1, "workRoot": str(world.app.hyprial_home),
              "limits": {"owners": ["user:owner-probe"]}}
    (world.app.hyprial_home / "pac-expansion.json").write_text(json.dumps(policy))
    world.service.admit = world.app._workflow_admit
    parent = world.service.start(
        yaml_text=yaml.safe_dump({"version": 2, "name": "managed-expansion", "nodes": [
            {"id": "planner", "role": "plan", "expands": "slot", "task": "plan"},
            {"id": "slot", "kind": "expansion", "task": "child", "after": ["planner"]},
        ]}), sender="user:owner-probe", operation_key="expansion-owner-probe",
    )["graphId"]
    world.coordinator.reconcile_all()
    if world.app._pac_graph_authority is None:
        world.service._tick()
    else:
        world.app._pac_graph_authority.workflow_tick(world.service.clock())
    planner = next(node for node in world.service.status(run_id=parent)["nodes"] if node["nodeId"] == "planner")
    receipt = world.store.workflow_worker_receipts(parent)[0]
    agent = world.app.agents.require(receipt["actor_name"])
    claims = {"actor": agent.uri, "sessionRef": "local-owned-worker-session"}
    assert world.app._workflow_caller(claims) == agent.uri
    child = yaml.safe_dump({"version": 2, "nodes": [
        {"id": "child", "task": "child", "owner": "user:owner-probe", "timeout": "30s"},
    ]})
    for caller in (claims, {"actor": "user:owner-probe"}):
        preview = world.app.handle("workflow.expansion.plan", {
            **caller, "graphId": parent, "nodeId": "slot", "yaml": child,
        }, _trusted_message_origin="ipc")
        assert preview["ok"] is True
    roster = world.store.workflow_roster(parent)
    if fault == "missing-token":
        with world.store.write():
            world.store._db.execute("UPDATE workflow_worker_receipts SET agent_entity_token=NULL WHERE graph_id=?", (parent,))
    elif fault == "rotate-after-preflight":
        admit = world.app._workflow_admit

        def rotate_after_preflight(spec, sender):
            admitted = admit(spec, sender)
            world.app._agent_session_domains._registry.rotate_secret_authority(agent.actor)
            return admitted

        monkeypatch.setattr(world.app, "_workflow_admit", rotate_after_preflight)
    params = {**claims, "graphId": parent, "nodeId": "planner", "requestId": planner["requestId"],
              "reasonRef": "test:managed-expansion", "expansion": child}
    if fault is None:
        result = world.app.handle("workflow.complete", params, _trusted_message_origin="ipc")
        assert result["ok"] is True and result["actingPrincipal"] == agent.uri
        assert world.store.node(parent, "planner").flag
        assert world.store.workflow_worker_receipts(parent) == [receipt]
    else:
        with pytest.raises(DaemonRequestError) as refused:
            world.app.handle("workflow.complete", params, _trusted_message_origin="ipc")
        assert refused.value.code == "WORKFLOW_NOT_OWNER"
        assert not world.store.node(parent, "planner").flag
        assert world.store._db.execute("SELECT 1 FROM workflow_expansions WHERE parent_graph_id=?", (parent,)).fetchone() is None
    assert world.store.workflow_roster(parent) == roster
