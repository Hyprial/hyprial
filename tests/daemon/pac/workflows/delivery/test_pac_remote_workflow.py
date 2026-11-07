"""Two authorities, real PAC stores, explicit forwarding and restart custody."""

import threading
from types import SimpleNamespace

import pytest
import yaml

from hyprial.daemon.impl.dispatch.identity import dispatch_message_id
from hyprial.daemon.impl.inbox.contracts.summary import summary_from_text
from hyprial.daemon.impl.dispatch.remote import RemoteWorkflow
from hyprial.identity import PacError
from hyprial.daemon.impl.pac.graphs.authority import PacGraphAuthority
from hyprial.daemon.impl.pac.graphs.reactor import NullSender, PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
from hyprial.daemon.impl.transport.api import TransportSample


class Mesh:
    def __init__(self):
        self.subscribers = []
        self.drop = False

    def subscribe(self, key, callback):
        record = (key, callback)
        self.subscribers.append(record)
        return SimpleNamespace(close=lambda: self.subscribers.remove(record))

    def put(self, key, payload):
        if self.drop:
            return
        for pattern, callback in tuple(self.subscribers):
            if key == pattern or (
                pattern.endswith("*") and key.startswith(pattern[:-1])
            ):
                callback(TransportSample(key, payload))


class IO:
    def __init__(self):
        self.messages = {}

    @staticmethod
    def derive_summary(text: str) -> str:
        return summary_from_text(text)

    def deliver(self, **data):
        message_id = dispatch_message_id(data["effect_id"])
        self.messages.setdefault(message_id, data)
        return SimpleNamespace(message_id=message_id)


class Sender(NullSender):
    def __init__(self, app):
        super().__init__()
        self.app = app

    def send_workflow_request(self, request, **kwargs):
        return self.app.remote.send(request, **kwargs)


def application(tmp_path, name, mesh):
    actor = f"agent:test:{name}:borrowed"
    app = SimpleNamespace(
        owner="test",
        node_id=name,
        state_dir=tmp_path / name,
        _dispatch_service_actor=f"agent:test:{name}:dispatch-coordinator",
        _log=lambda *a, **kw: None,
        _pac_notification_io=IO(),
    )
    app.agents = SimpleNamespace(
        get=lambda uri: (
            SimpleNamespace(capabilities={"interactive": False})
            if uri == actor
            else None
        )
    )
    app._resolve_send_sender = lambda uri: uri
    app._workflow_service = GraphWorkflowService(
        state_dir=app.state_dir,
        owner="test",
        machine=name,
        sender=Sender(app),
        start_thread=False,
    )
    app.remote = RemoteWorkflow(app, mesh)
    return app


@pytest.fixture
def pair(tmp_path):
    mesh = Mesh()
    a, b = [application(tmp_path, name, mesh) for name in ("origin", "executor")]
    yield a, b, mesh
    for app in (a, b):
        app.remote.close()
        app._workflow_service.close()


def dispatch(pair, *, timeout="5m", nodes=None):
    a, b, _ = pair
    graph = a._workflow_service.start(
        yaml_text=yaml.safe_dump(
            {
                "version": 2,
                "name": "remote",
                "nodes": nodes
                or [
                    {
                        "id": "work",
                        "owner": "agent:test:executor:borrowed",
                        "task": "work",
                        "timeout": timeout,
                    }
                ],
            }
        ),
        sender="user:test",
    )["graphId"]
    a._workflow_service._tick()
    message_id = next(iter(b._pac_notification_io.messages))
    grant = b.remote._lookup(message_id=message_id)
    assert grant["graphId"] == graph
    return graph, grant


def params(grant, reason="evidence:one", output_text=None):
    result = dict(
        graphId=grant["graphId"],
        nodeId=grant["nodeId"],
        requestId=grant["requestId"],
        reasonRef=reason,
    )
    if output_text is not None:
        result["outputText"] = output_text
    return result


def test_native_failure_stages_remote_return_without_network_on_caller(
    pair, monkeypatch,
):
    _origin, executor, _mesh = pair
    _graph, grant = dispatch(pair)
    executor.remote.stopping.set()

    def forbidden_flush(_request_id):
        raise AssertionError("native outcome called remote transport inline")

    monkeypatch.setattr(executor.remote, "_flush", forbidden_flush)
    result = SimpleNamespace(
        delivery_id=grant["messageId"],
        recipient=grant["owner"],
        status=SimpleNamespace(value="failed"),
        failure_code="TEST_FAILURE",
    )
    assert executor.remote.outcome(result)
    store = PacGraphStore(executor.remote.database, read_only=True)
    try:
        row = store._db.execute(
            "SELECT action, reason_ref, result_json FROM remote_workflow_outbox "
            "WHERE request_id=?",
            (grant["requestId"],),
        ).fetchone()
        assert tuple(row) == ("fail", "harness:TEST_FAILURE", None)
    finally:
        store.close()


def test_remote_current_query_runs_on_remote_authority(pair, monkeypatch):
    origin, _executor, _mesh = pair
    _graph, grant = dispatch(pair)
    threads: list[str] = []
    original = origin.remote.current

    def observed_current(value):
        threads.append(threading.current_thread().name)
        return original(value)

    monkeypatch.setattr(origin.remote, "current", observed_current)
    assert origin.remote.handle("current", {"grant": grant})["current"]
    assert len(threads) == 1 and threads[0] != threading.current_thread().name


def test_remote_borrow_complete_exactly_once_and_no_capability_in_prompt(pair):
    a, b, _ = pair
    graph, grant = dispatch(pair)
    body = b._pac_notification_io.messages[grant["messageId"]]["text"]
    assert "hyprial workflow complete" in body and grant["capability"] not in body
    assert a.remote.current(grant)["current"]
    result = b.remote.forward("workflow.complete", params(grant), grant["owner"])
    assert result["returnState"] == "accepted"
    assert (
        b.remote.forward("workflow.complete", params(grant), grant["owner"]) == result
    )
    a._workflow_service._tick()
    assert a._workflow_service.status(run_id=graph)["state"] == "completed"
    store = PacGraphStore(a._workflow_service.database)
    assert len(store.flag_events(graph)) == 1
    assert not any(n.kind == "actor" for n in store.nodes(graph))
    store.close()
    assert (
        b.remote.forward(
            "workflow.complete", params(grant), "agent:test:executor:intruder"
        )
        is None
    )
    with pytest.raises(PacError, match="different|queued"):
        b.remote.forward("workflow.fail", params(grant), grant["owner"])


def test_capability_binds_owner_request_inputs_and_deadline(pair):
    a, b, _ = pair
    _, grant = dispatch(pair)
    for key, value in [
        ("owner", "agent:test:executor:intruder"),
        ("inputToken", "fake"),
        ("deadlineMs", grant["deadlineMs"] + 1),
        ("requestId", "fake"),
        ("nodeId", "other"),
        ("origin", b.remote.authority),
        ("capability", "fake"),
    ]:
        with pytest.raises(PacError) as error:
            a.remote.handle(
                "outcome",
                {
                    "grant": {**grant, key: value},
                    "action": "complete",
                    "reasonRef": "x",
                },
            )
        assert error.value.code == "CALLER_NOT_AUTHORIZED"
    assert a.remote.current(grant)["current"]


def test_withdrawal_and_fixed_deadline_refuse_return(pair):
    a, b, _ = pair
    graph, grant = dispatch(pair)
    a._workflow_service.cancel(run_id=graph, actor="user:test")
    assert not b.remote.delivery_current(grant["messageId"])["current"]
    with pytest.raises(PacError) as error:
        b.remote.forward("workflow.complete", params(grant), grant["owner"])
    assert error.value.code == "WORKFLOW_REQUEST_STALE"


def test_remote_failure_applies_origin_policy(pair):
    a, b, _ = pair
    graph, grant = dispatch(pair)
    result = b.remote.forward("workflow.fail", params(grant), grant["owner"])
    assert result["returnState"] == "accepted"
    assert a._workflow_service.status(run_id=graph)["state"] == "failed"
    assert b.remote.forward("workflow.fail", params(grant), grant["owner"]) == result


def test_remote_outbox_mutations_use_pac_writer_authority(pair):
    origin, executor, _mesh = pair
    graph, grant = dispatch(pair)
    writer = PacGraphAuthority(executor.remote.database, NullSender())
    executor.remote.graph_authority = writer
    try:
        result = executor.remote.forward(
            "workflow.fail", params(grant), grant["owner"]
        )
        assert result["returnState"] == "accepted"
        store = PacGraphStore(executor.remote.database, read_only=True)
        try:
            row = store._db.execute(
                "SELECT action, attempted_at, result_json FROM remote_workflow_outbox "
                "WHERE request_id=?", (grant["requestId"],),
            ).fetchone()
            assert row["action"] == "fail"
            assert row["attempted_at"] is not None
            assert row["result_json"] is not None
        finally:
            store.close()
        assert origin._workflow_service.status(run_id=graph)["state"] == "failed"
    finally:
        executor.remote.graph_authority = None
        assert writer.close()


def test_remote_key_reopen_uses_pac_writer_authority(pair):
    _origin, executor, mesh = pair
    original_secret = executor.remote.secret
    executor.remote.close()
    writer = PacGraphAuthority(executor.remote.database, NullSender())
    executor._pac_graph_authority = writer
    try:
        executor.remote = RemoteWorkflow(executor, mesh)
        assert executor.remote.secret == original_secret
        executor.remote.close()
    finally:
        executor._pac_graph_authority = None
        executor.remote = RemoteWorkflow(executor, mesh)
        assert writer.close()


def test_remote_outbox_claim_coalesces_concurrent_returns(pair, monkeypatch):
    _origin, executor, _mesh = pair
    _graph, grant = dispatch(pair)
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    original = executor.remote.wire.call

    def slow_outcome(target, method, data, **kwargs):
        if method == "outcome":
            calls.append(method)
            entered.set()
            assert release.wait(3.0)
        return original(target, method, data, **kwargs)

    monkeypatch.setattr(executor.remote.wire, "call", slow_outcome)
    result: list[dict] = []
    worker = threading.Thread(target=lambda: result.append(
        executor.remote.forward("workflow.fail", params(grant), grant["owner"])
    ))
    worker.start()
    try:
        assert entered.wait(1.0)
        assert executor.remote._flush(grant["requestId"])["returnState"] == "pending"
    finally:
        release.set()
        worker.join(3.0)
    assert len(calls) == 1
    assert result[0]["returnState"] == "accepted"


@pytest.mark.parametrize("code", ["PAC_GRAPH_OVERLOADED", "PAC_GRAPH_PENDING"])
def test_remote_graph_transient_keeps_outbox_pending(pair, monkeypatch, code):
    _origin, executor, _mesh = pair
    _graph, grant = dispatch(pair)
    executor.remote._enqueue(
        grant, "complete", "evidence:transient", flush=False
    )

    def transient(*_args, **_kwargs):
        raise PacError(code, "origin graph authority remains unsettled")

    monkeypatch.setattr(executor.remote.wire, "call", transient)
    result = executor.remote._flush_once(grant["requestId"])

    assert result["returnState"] == "pending"
    store = PacGraphStore(executor.remote.database, read_only=True)
    try:
        row = store._db.execute(
            "SELECT result_json FROM remote_workflow_outbox WHERE request_id=?",
            (grant["requestId"],),
        ).fetchone()
        assert row["result_json"] is None
    finally:
        store.close()


def test_remote_completion_uses_effect_worker_and_graph_writer(pair, monkeypatch):
    origin, executor, _mesh = pair
    graph, grant = dispatch(pair)
    writer = PacGraphAuthority(origin.remote.database, NullSender())
    origin.remote.graph_authority = writer
    threads: list[str] = []
    original = writer.set_flag

    def observed_set_flag(*args, **kwargs):
        threads.append(threading.current_thread().name)
        return original(*args, **kwargs)

    monkeypatch.setattr(writer, "set_flag", observed_set_flag)
    try:
        result = executor.remote.forward(
            "workflow.complete", params(grant), grant["owner"]
        )
        assert result["returnState"] == "accepted"
        assert threads and all(name.startswith("pac-remote-effect-") for name in threads)
        store = PacGraphStore(origin.remote.database, read_only=True)
        try:
            assert store.node(graph, "work").flag
        finally:
            store.close()
    finally:
        origin.remote.graph_authority = None
        assert writer.close()


def test_remote_inline_output_survives_executor_custody_and_lands_at_origin(pair):
    a, b, mesh = pair
    graph, grant = dispatch(pair)
    output = "remote inline verdict"
    original = b.remote.wire.call

    def offline(*args, **kwargs):
        raise PacError("WORKFLOW_REMOTE_UNAVAILABLE", "offline")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(b.remote.wire, "call", offline)
    try:
        pending = b.remote.forward(
            "workflow.complete", params(grant, output_text=output), grant["owner"]
        )
        assert pending["returnState"] == "pending"
        monkeypatch.setattr(b.remote.wire, "call", original)
        b.remote.close()
        b.remote = RemoteWorkflow(b, mesh)
        assert b.remote._flush(grant["requestId"])["returnState"] == "accepted"
    finally:
        monkeypatch.undo()
    assert a._workflow_service.status(run_id=graph)["nodes"][0]["outputText"] == output


def test_remote_pruned_request_preserves_deadline_and_fails_origin(pair):
    a, b, _ = pair
    graph, grant = dispatch(pair, timeout="2h")
    delivered = b._pac_notification_io.messages[grant["messageId"]]
    assert delivered["expires_at_ms"] == grant["deadlineMs"]

    assert b.remote.request_pruned(grant["messageId"], grant["owner"])
    result = b.remote._flush(grant["requestId"])

    assert result["returnState"] == "accepted"
    snapshot = a._workflow_service.status(run_id=graph)
    assert snapshot["state"] == "failed"
    assert snapshot["nodes"][0]["reasonRef"] == "pac:request-expired"


def test_offline_return_durable_across_executor_restart(pair, monkeypatch):
    a, b, mesh = pair
    graph, grant = dispatch(pair)
    original = b.remote.wire.call

    def offline(*args, **kwargs):
        raise PacError("WORKFLOW_REMOTE_UNAVAILABLE", "offline")

    monkeypatch.setattr(b.remote.wire, "call", offline)
    pending = b.remote.forward("workflow.complete", params(grant), grant["owner"])
    assert pending["returnState"] == "pending"
    assert not a._workflow_service.status(run_id=graph)["nodes"][0]["flag"]
    monkeypatch.setattr(b.remote.wire, "call", original)
    b.remote.close()
    b.remote = RemoteWorkflow(b, mesh)
    assert b.remote._flush(grant["requestId"])["returnState"] == "accepted"
    a._workflow_service._tick()
    assert a._workflow_service.status(run_id=graph)["state"] == "completed"


def test_origin_restart_and_lost_ack_replay(pair):
    a, b, mesh = pair
    graph, grant = dispatch(pair)
    a.remote.handle(
        "outcome", {"grant": grant, "action": "complete", "reasonRef": "evidence:one"}
    )
    a.remote.close()
    a.remote = RemoteWorkflow(a, mesh)
    assert (
        b.remote.forward("workflow.complete", params(grant), grant["owner"])[
            "returnState"
        ]
        == "accepted"
    )
    store = PacGraphStore(a._workflow_service.database)
    assert len(store.flag_events(graph)) == 1
    store.close()


def test_remote_offer_replay_and_missing_actor_admission(pair):
    a, b, _ = pair
    _, grant = dispatch(pair)
    text = b._pac_notification_io.messages[grant["messageId"]]["text"]
    assert (
        b.remote.handle("offer", {"grant": grant, "text": text})["messageId"]
        == grant["messageId"]
    )
    assert len(b._pac_notification_io.messages) == 1
    with pytest.raises(PacError):
        b.remote.handle("offer", {"grant": grant, "text": text + "injection"})
    with pytest.raises(PacError) as error:
        a.remote.wire.call(
            "agent:test:executor:missing",
            "admit",
            {"owner": "agent:test:executor:missing", "role": "execute"},
        )
    assert error.value.code == "WORKFLOW_REMOTE_ACTOR_NOT_FOUND"


def test_remote_guard_classifies_exact_binding_and_preserves_ordinary_messages(
    pair, monkeypatch
):
    from hyprial.daemon.impl.pac.contracts.delivery import delivery_current, is_workflow_delivery
    from hyprial.daemon.impl.pac.contracts.bindings import RemoteWorkflowUnavailable

    a, b, _ = pair
    graph, grant = dispatch(pair)
    assert is_workflow_delivery(b.state_dir, grant["messageId"])
    assert not is_workflow_delivery(b.state_dir, "workflow-ordinary")
    assert delivery_current(b.state_dir, "workflow-ordinary")
    with pytest.raises(RemoteWorkflowUnavailable):
        delivery_current(
            b.state_dir, grant["messageId"]
        )  # no local IPC in this fixture
    assert not delivery_current(
        b.state_dir, grant["messageId"], now_ms=grant["deadlineMs"] + 1
    )


def test_reworked_parent_invalidates_remote_request(pair):
    a, b, _ = pair
    graph, grant = dispatch(
        pair,
        nodes=[
            {"id": "parent", "owner": "agent:test:executor:borrowed", "task": "first"},
            {
                "id": "child",
                "owner": "agent:test:executor:borrowed",
                "task": "second",
                "after": ["parent"],
            },
        ],
    )
    b.remote.forward("workflow.complete", params(grant), grant["owner"])
    a._workflow_service._tick()
    child = b.remote._lookup(graph_id=graph, node_id="child")
    assert child and a.remote.current(child)["current"]
    store = PacGraphStore(a._workflow_service.database)
    PacReactor(store).reset_flag(
        graph, "parent", actor=grant["owner"], reason_ref="rework"
    )
    store.close()
    assert not a.remote.current(child)["current"]
    with pytest.raises(PacError):
        b.remote.forward("workflow.complete", params(child), child["owner"])


def test_remote_reset_is_fenced_and_replay_does_not_reset_new_round(pair):
    a, b, _ = pair
    graph, grant = dispatch(
        pair,
        nodes=[
            {"id": "work", "owner": "agent:test:executor:borrowed", "task": "work"},
            {
                "id": "accept",
                "kind": "end",
                "owner": "user:test",
                "task": "accept",
                "after": ["work"],
            },
        ],
    )
    b.remote.forward("workflow.complete", params(grant), grant["owner"])
    a._workflow_service._tick()
    result = b.remote.forward("pac.flag.reset", params(grant, "rework"), grant["owner"])
    assert result["ok"]
    a._workflow_service._tick()
    fresh = b.remote._lookup(graph_id=graph, node_id="work")
    assert fresh["requestId"] != grant["requestId"]
    b.remote.forward("workflow.complete", params(fresh), fresh["owner"])
    replay = a.remote.handle("reset", {"grant": grant, "reasonRef": "rework"})
    assert replay == result
    assert next(
        n
        for n in a._workflow_service.status(run_id=graph)["nodes"]
        if n["nodeId"] == "work"
    )["flag"]


def test_authority_rejects_late_completion_and_failure(pair, monkeypatch):
    import hyprial.daemon.impl.dispatch.remote as remote

    a, b, _ = pair
    graph, grant = dispatch(pair)
    monkeypatch.setattr(
        remote, "time_ns", lambda: (grant["deadlineMs"] + 1) * 1_000_000
    )
    for action in ("complete", "fail"):
        with pytest.raises(PacError) as error:
            a.remote.handle(
                "outcome", {"grant": grant, "action": action, "reasonRef": "late"}
            )
        assert error.value.code == "WORKFLOW_REQUEST_STALE"
    store = PacGraphStore(a._workflow_service.database)
    assert not store.flag_events(graph)
    store.close()


def test_wire_shutdown_budget_holds_when_a_handler_is_stuck():
    import threading
    from time import monotonic
    from hyprial.daemon.impl.dispatch.remote import RemoteWire

    entered, release = threading.Event(), threading.Event()
    mesh = Mesh()

    def handler(_method, _data):
        entered.set()
        release.wait(5)
        return {"ok": True}

    left = RemoteWire(mesh, "agent:test:left:daemon", lambda *_: {})
    right = RemoteWire(mesh, "agent:test:right:daemon", handler)
    errors = []

    def call():
        try:
            left.call("agent:test:right:daemon", "probe", {})
        except PacError as error:
            errors.append(error.code)

    caller = threading.Thread(target=call)
    caller.start()
    assert entered.wait(1)
    try:
        started = monotonic()
        right.stop()
        assert right.drain(0.02) is False
        assert monotonic() - started < 0.5
    finally:
        release.set()
        left.close()
        caller.join(1)
        assert right.drain(1) is True
        assert right.close() is True
    assert errors == ["WORKFLOW_REMOTE_UNAVAILABLE"]
