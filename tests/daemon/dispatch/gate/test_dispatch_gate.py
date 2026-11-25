"""Dispatch role/metadata admission survives replacement of legacy workflow."""

from types import SimpleNamespace

import pytest
import yaml

from hyprial.identity import PacError
from hyprial.kernel import ipc_errors
from hyprial.daemon.impl.dispatch.admission import dispatch_gate
from hyprial.daemon.impl.dispatch.remote import RemoteWorkflow
from hyprial.daemon.impl.pac.graphs.reactor import NullSender
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
from hyprial.daemon.impl.pac.contracts.workflow import WorkflowSchemaError, load_workflow_text
from hyprial.biz.impl.routine.contracts.schema import load_routine_text
from hyprial.biz.impl.routine.templates import render_template
from tests.daemon.dispatch.without.test_dispatch_without_pac import DRIVER_URI, PI_URI, _register_driver, _send
from tests.daemon.dispatch.without.test_dispatch_without_pac import harness as harness


def interactive_entity(app):
    """Register the pi TUI session and return the agent's official address.

    Registration creates the agent, so its name URI ``PI_URI`` is retired
    (ruling U1): sends, graph owners and routine actors address its id URI.
    """

    app.handle(
        "session.register",
        {
            "actor": PI_URI,
            "sessionRef": "capmx-tui",
            "cwd": "/work",
            "command": ["pi", "--provider", "kimi-coding", "--model", "k3"],
            "source": "pi-attach",
            "runtime": "pi_interactive",
        },
    )
    agent = app.agents.require(PI_URI)
    assert agent.capabilities["interactive"] is True
    assert agent.uri != PI_URI
    return agent.uri


def test_execute_interactive_rejected_before_inbox(harness):
    app, *_ = harness
    _register_driver(app)
    address = interactive_entity(app)
    with pytest.raises(ipc_errors.DaemonRequestError) as error:
        _send(app, role="execute", to=[address])
    assert error.value.code == ipc_errors.DISPATCH_ROLE_MISMATCH
    assert app._inbox.pending_all() == ()


@pytest.mark.parametrize("role", ["review", "plan", "dispatch", None])
def test_non_execution_and_conversation_allowed(harness, role):
    app, *_ = harness
    _register_driver(app)
    address = interactive_entity(app)
    assert _send(app, to=[address], **({"role": role} if role else {}))["ok"] is True


def test_graph_admission_cannot_bypass_interactive_gate(harness, tmp_path):
    app, *_ = harness
    _register_driver(app)
    address = interactive_entity(app)
    service = GraphWorkflowService(
        state_dir=tmp_path / "pac",
        owner=app.owner,
        machine=app.node_id,
        sender=NullSender(),
        admit=app._workflow_admit,
        start_thread=False,
    )
    try:
        raw = yaml.safe_dump(
            {
                "version": 2,
                "name": "dispatch",
                "nodes": [{"id": "work", "owner": address, "task": "implement"}],
            }
        )
        with pytest.raises(ipc_errors.DaemonRequestError) as error:
            service.start(yaml_text=raw, sender=DRIVER_URI, operation_key="same")
        assert error.value.code == ipc_errors.DISPATCH_ROLE_MISMATCH
        store = PacGraphStore(service.database)
        try:
            assert store._db.execute("SELECT COUNT(*) FROM graphs").fetchone()[0] == 0
            assert (
                store._db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
                == 0
            )
        finally:
            store.close()
    finally:
        service.close()


@pytest.mark.parametrize("borrowed", [True, False])
def test_routine_add_rejects_execute_targeting_known_interactive(
    harness, borrowed
):
    app, *_ = harness
    address = interactive_entity(app)
    routine_service = app._routine_service
    app._routine_service = object()
    if borrowed:
        raw = yaml.safe_dump(
            {
                "version": 2,
                "mode": "scheduled",
                "name": "interactive-routine",
                "schedule": {"interval": "15m"},
                "task": "do work",
                "actor": address,
                "role": "execute",
                "on_task_timeout": {
                    "action": "escalate",
                    "escalate_to": "user:allen",
                },
            }
        )
    else:
        document = yaml.safe_load(
            render_template("selfdrive", owner=address, escalate_to="user:allen")
        )
        document["role"] = "execute"
        raw = yaml.safe_dump(document)

    try:
        with pytest.raises(ipc_errors.DaemonRequestError) as error:
            app.handle("routine.add", {"yaml": raw, "actor": "user:allen"})
        assert error.value.code == ipc_errors.DISPATCH_ROLE_MISMATCH
    finally:
        app._routine_service = routine_service


def test_routine_admission_warns_but_allows_unknown_capabilities(harness):
    app, events, *_ = harness
    unknown = "agent:allen:gate-node:not-created-yet"
    document = yaml.safe_load(
        render_template("selfdrive", owner=unknown, escalate_to="user:allen")
    )
    document["role"] = "execute"

    app._routine_admit(load_routine_text(yaml.safe_dump(document)))

    assert any(
        item.get("event") == "dispatch.capabilities_unknown"
        and item.get("target") == unknown
        and item.get("source") == "routine.add"
        for item in events()
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"role": "owner"},
        {"human_gates": False},
        {"human_gates": [{"who": "a"}]},
        {"human_gates": [{"who": "a", "what": "b", "extra": 1}]},
        {"first_output_eta": 1},
    ],
)
def test_workflow_rejects_invalid_dispatch_declarations(fields):
    with pytest.raises(WorkflowSchemaError):
        load_workflow_text(
            yaml.safe_dump(
                {
                    "version": 2,
                    "name": "test",
                    "nodes": [{"id": "work", "task": "do work", **fields}],
                }
            )
        )


def test_rhythm_lint_remains_a_warning_and_explicit_gates_suppress_it():
    events = []
    arguments = dict(
        target="worker",
        capabilities={"interactive": False},
        role="execute",
        first_output_eta="tomorrow",
        emit=lambda *a, **k: events.append(k),
        source="test",
    )
    assert len(dispatch_gate(**arguments)) == 1
    assert dispatch_gate(**arguments, human_gates_declared=True) == ()


@pytest.mark.parametrize("remote_user", [False, True])
def test_remote_owner_requires_available_home_daemon(harness, remote_user):
    app, *_ = harness
    owner = (
        f"user:{app.owner}-other"
        if remote_user
        else f"agent:{app.owner}:{app.node_id}-remote:worker"
    )
    spec = load_workflow_text(
        yaml.safe_dump(
            {
                "version": 2,
                "name": "remote",
                "nodes": [{"id": "work", "owner": owner, "task": "work"}],
            }
        )
    )
    with pytest.raises(ipc_errors.DaemonRequestError) as error:
        app._workflow_admit(spec, f"user:{app.owner}")
    assert error.value.code == ("WORKFLOW_REMOTE_OWNER_UNSUPPORTED" if remote_user else "WORKFLOW_REMOTE_UNAVAILABLE")


@pytest.mark.parametrize("peer", ["ordinary", "expansion", "missing", "interactive"])
def test_borrowed_remote_routine_uses_complete_admission_shape(harness, monkeypatch, peer):
    """Real routine/remote admission; only the external wire response is synthetic."""

    app, *_ = harness
    target = "agent:other:remote-node:a.aaaaaaaaaaaaaaaaaaaaaaaaaa"
    calls = []

    def wire_call(owner, method, fields):
        calls.append((owner, method, fields))
        if peer in ("missing", "interactive"):
            raise PacError(
                "WORKFLOW_REMOTE_ACTOR_NOT_FOUND" if peer == "missing" else "DISPATCH_ROLE_MISMATCH",
                "remote admission refused",
            )
        return {"ok": True, "features": ["pac-expansion-v1"] if peer == "expansion" else []}

    remote = RemoteWorkflow.__new__(RemoteWorkflow)
    remote.app = app
    remote.wire = SimpleNamespace(call=wire_call)
    spec = load_routine_text(yaml.safe_dump({
        "version": 2, "mode": "scheduled", "name": "remote-borrowed",
        "schedule": {"interval": "15m"}, "task": "work",
        "actor": target, "role": "execute",
        "on_task_timeout": {"action": "escalate", "escalate_to": "user:allen"},
    }))
    with monkeypatch.context() as patched:
        patched.setattr(app, "_remote_workflow", remote)
        if peer in ("missing", "interactive"):
            with pytest.raises(ipc_errors.DaemonRequestError) as failure:
                app._routine_admit(spec)
            assert failure.value.code == (
                "WORKFLOW_REMOTE_ACTOR_NOT_FOUND" if peer == "missing" else "DISPATCH_ROLE_MISMATCH"
            )
        else:
            app._routine_admit(spec)
    assert calls == [(target, "admit", {
        "owner": target, "role": "execute", "firstOutputEta": None, "humanGatesDeclared": False,
    })]
    if peer == "ordinary":
        with pytest.raises(PacError) as failure:
            remote.admit(SimpleNamespace(
                owner=target, role="plan", first_output_eta=None, human_gates=None, expands="slot",
            ))
        assert failure.value.code == "WORKFLOW_REMOTE_INVALID"
