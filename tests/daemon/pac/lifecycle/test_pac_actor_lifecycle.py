from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import time
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.pac.actors.daemon import (
    DaemonActorRuntime,
    DaemonPacNotificationSender,
    PacActorService,
)
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    LocalFirstDeliveryTransport,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.links.io import CorrelatedInboxEventRouter, InboxDeliveryIoAdapter
from hyprial.daemon.impl.pac.views.context import node_context
from hyprial.daemon.impl.pac.graphs.edits import activate_graph, add_edge, add_node, close_graph, create_graph
from hyprial.daemon.impl.pac.actors.coordinator import (
    ActorCoordinator,
    FileLaunchResolver,
    LaunchSpec,
    ResolvedLaunch,
    RuntimeObservation,
    request_actor_stop,
)
from hyprial.daemon.impl.pac.graphs.reactor import NullSender, PacReactor
from hyprial.daemon.impl.pac.graphs.authority import PacGraphAuthority
from hyprial.kernel import AdmissionResult
from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path
from hyprial.daemon.impl.pac.workflows.runtime import close_workflow

USER_ALLEN = "user:allen"
#: the fixture actor's execution binding (owner allen on machine test-node)
AGENT_W = "agent:allen:node:w"


class FakeRuntime:
    def __init__(self) -> None:
        self.running: dict[str, RuntimeObservation] = {}
        self.starts: list[tuple[str, str, str]] = []
        self.stops: list[tuple[str, str, str]] = []
        self.start_error: Exception | None = None

    def observe(self, actor_name: str) -> RuntimeObservation:
        return self.running.get(actor_name, RuntimeObservation(False))

    def start(
        self,
        actor_name: str,
        launch: LaunchSpec,
        *,
        operation_id: str,
        identity_marker: str,
    ) -> RuntimeObservation:
        self.starts.append((actor_name, operation_id, identity_marker))
        if self.start_error is not None:
            raise self.start_error
        observed = RuntimeObservation(True, identity_marker, launch.harness, operation_id)
        self.running[actor_name] = observed
        return observed

    def stop(
        self,
        actor_name: str,
        launch: LaunchSpec,
        *,
        operation_id: str,
        identity_marker: str,
    ) -> RuntimeObservation:
        self.stops.append((actor_name, operation_id, identity_marker))
        self.running.pop(actor_name, None)
        return RuntimeObservation(False)


def test_daemon_runtime_validates_harness_and_never_resumes_pac_marker() -> None:
    marker = "pac:g:n:1:effect"
    spec = DaemonActorRuntime._launch_spec(
        "worker", LaunchSpec("codex", None, None, None, ()), marker
    )
    assert spec.nickname == marker and spec.session_ref is None
    with pytest.raises(Exception, match="invalid harness"):
        DaemonActorRuntime._launch_spec(
            "bad", LaunchSpec("not-a-harness", None, None, None, ()), marker
        )


@pytest.mark.parametrize(
    ("state", "expected"),
    [("completed", "graph-settled"), ("cancelled", "graph-cancelled")],
)
def test_daemon_runtime_derives_interruption_reason_from_graph_close(
    graph: tuple[PacGraphStore, str], tmp_path: Path, state: str, expected: str
) -> None:
    store, graph_id = graph
    db = store.write()
    try:
        current = store.graph(graph_id)
        assert current is not None
        close_workflow(
            store,
            current,
            state=state,
            reason="test-close",
            at=int(time.time() * 1000),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise

    runtime = DaemonActorRuntime(SimpleNamespace(state_dir=tmp_path / "state"))
    assert (
        runtime._interruption_reason(f"pac:{graph_id}:worker:1:effect")
        == expected
    )


def test_daemon_runtime_keeps_nonrunning_desired_worker_retirable() -> None:
    marker = "pac:cancelled:worker:1:effect"
    desired = SimpleNamespace(
        harness_spec=lambda _name, excluding=None: SimpleNamespace(
            nickname=marker,
            harness="pi",
        )
    )
    application = SimpleNamespace(
        desired_state=desired,
        _canonical_harness_uri=lambda name, _spec: f"agent:test:node:{name}",
        _managed_worker_running=lambda _actor: False,
        agents=SimpleNamespace(
            get=lambda _name: SimpleNamespace(entity_token="worker-entity")
        ),
    )

    observation = DaemonActorRuntime(application).observe("worker")

    assert observation.present is False
    assert observation.retirement_pending is True
    assert observation.identity_marker == marker
    assert observation.agent_entity_token == "worker-entity"


def test_reactor_notification_uses_typed_inbox_not_message_send() -> None:
    """The typed inbox carries the PLANNED sender, not a service identity.

    Allen, 2026-09-25: the sender is the principal whose verified action caused
    the notification (or the graph creator when nothing did); a hard-coded
    service identity is not acceptable.
    """

    calls = []
    class Delivery:
        def deliver(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(message_id="m1")
    class Application:
        _pac_notification_io = Delivery()
        _dispatch_service_actor = "agent:allen:node:dispatch-coordinator"
    sender = DaemonPacNotificationSender(Application())
    assert sender.send(recipient="worker", text="turn", sender="agent:allen:node:worker-1",
                       conversation_id="pac:g", idempotency_key="key", summary="turn") == "m1"
    assert calls[0]["sender"] == "agent:allen:node:worker-1"
    assert calls[0]["effect_id"] == "pac:key"


def test_pac_notification_and_actor_run_without_workflow_service(
    graph, tmp_path: Path
) -> None:
    """PAC's resident actor and real inbox custody path have no v1 service edge."""

    store, graph_id = graph
    launch = tmp_path / "launch" / "w.yaml"
    launch.parent.mkdir()
    launch.write_text("harness: codex\nargs: [--headless]\n", encoding="utf-8")

    router = CorrelatedInboxEventRouter()
    network = MemoryDeliveryTransport()
    local = LocalFirstDeliveryTransport(
        network,
        origin_node="node",
        local_node_id="node",
        local_owner="allen",
    )
    inbox_db = tmp_path / "inbox.sqlite3"
    custody = DeliveryCustodyCoordinator(
        inbox_db,
        local,
        router,
        node_id="node",
    )
    inbox = DeliveryCustodyFacade(custody, inbox_db, timeout=1.0)
    local.bind_receiver(inbox.receive)
    io = InboxDeliveryIoAdapter(
        custody,
        router,
        # mirrors the daemon's typed-IO target resolution: a full URI rides
        # through verbatim, only a bare name is expanded (application.py
        # _resolve_agent_alias/normalize_agent_recipient)
        resolve_target=lambda target: target
        if ":" in target
        else f"agent:allen:node:{target}",
        completion_timeout=1.0,
    )
    application = SimpleNamespace(
        _pac_notification_io=io,
        _dispatch_service_actor="agent:allen:node:dispatch-coordinator",
        _workflow_service=None,
    )
    runtime = FakeRuntime()
    service: PacActorService | None = None
    try:
        service = PacActorService(
            state_dir=store.path.parent,
            reference_root=tmp_path,
            runtime=runtime,
            sender=DaemonPacNotificationSender(application),
            daemon_epoch="epoch-without-v1",
            logger=lambda *_args, **_kwargs: None,
            workers=1,
        )
        PacReactor(store).set_flag(graph_id, "a", actor=USER_ALLEN)
        _pend_worker(store, graph_id)
        service.submit_tick()
        deadline = time.monotonic() + 2.0
        while not runtime.starts:
            assert time.monotonic() < deadline, "PAC actor service did not run"
            time.sleep(0.01)
        recipient = AGENT_W
        while not inbox.pending_all():
            assert time.monotonic() < deadline, "PAC notification did not reach inbox"
            time.sleep(0.01)
        assert application._workflow_service is None
        assert runtime.observe("w").present is True
        [message] = inbox.pending_all()
        assert message.recipient == recipient
        # `w`'s turn follows the system's actor-node flip ("reactor" is the
        # recorded actor): nothing a principal did caused it, so it comes from
        # the graph's creator (USER_ALLEN here), never a service identity or
        # the bare "reactor" placeholder (Allen, 2026-09-25)
        assert message.sender == USER_ALLEN
        assert "轮到你:b" in message.payload.decode()
        delivered = []
        while not delivered:
            delivered = [
                item
                for item in store.notifications(graph_id)
                if item.recipient == AGENT_W and item.message_id is not None
            ]
            if delivered:
                break
            assert time.monotonic() < deadline, "PAC delivery was not recorded"
            time.sleep(0.01)
        assert len(delivered) == 1 and delivered[0].message_id == message.message_id
    finally:
        if service is not None:
            assert service.close(2.0)
        assert custody.drain(2.0).complete


def resolver(_ref: str, _node: Any) -> ResolvedLaunch:
    return ResolvedLaunch(
        LaunchSpec("codex", None, "gpt-5.6-luna", "/tmp", ("--headless",)),
        "a" * 64,
    )


@pytest.fixture()
def graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path))
    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    monkeypatch.setenv("HYPRIAL_NODE_ID", "node")
    state = tmp_path / "state"
    state.mkdir()
    store = PacGraphStore(default_database_path(state))
    gid = create_graph(store, name="actor-life", created_by=USER_ALLEN)["graphId"]
    version = 1
    for kwargs in (
        {"node_id": "a", "owner": USER_ALLEN, "brief_ref": "brief#a"},
        {
            "node_id": "worker",
            "owner": USER_ALLEN,
            "brief_ref": "brief#worker",
            "kind": "actor",
            "actor_name": "w",
            "launch_ref": "launch/w.yaml",
        },
        {"node_id": "b", "owner": AGENT_W, "brief_ref": "brief#b"},
        {"node_id": "c", "owner": USER_ALLEN, "brief_ref": "brief#c", "kind": "end"},
    ):
        version = add_node(
            store, state, graph_id=gid, expect_version=version, **kwargs
        )["version"]
    for source, target in (("a", "worker"), ("worker", "b"), ("b", "c")):
        version = add_edge(
            store,
            graph_id=gid,
            from_node=source,
            to_node=target,
            expect_version=version,
        )["version"]
    activate_graph(store, gid, actor=USER_ALLEN)
    yield store, gid
    store.close()


def coordinator(store: PacGraphStore, runtime: FakeRuntime, epoch: str, sender=None):
    return ActorCoordinator(
        store,
        runtime,
        daemon_epoch=epoch,
        resolver=resolver,
        sender=sender,
        clock=lambda: 100,
    )


def test_actor_reconcile_effects_return_to_pac_writer(graph, tmp_path: Path) -> None:
    store, graph_id = graph
    launch = tmp_path / "launch" / "w.yaml"
    launch.parent.mkdir()
    launch.write_text("harness: codex\nmodel: gpt-5.6-luna\ncwd: /tmp\nargs: [--headless]\n")
    PacReactor(store).set_flag(graph_id, "a", actor=USER_ALLEN)
    runtime = FakeRuntime()
    authority = PacGraphAuthority(store.path, NullSender())
    authority.attach_reconciler(runtime, tmp_path, "epoch-1")
    try:
        assert authority.reconcile_actor(graph_id, "worker") is AdmissionResult.ACCEPTED
        deadline = time.monotonic() + 3.0
        while True:
            row = store._db.execute(
                "SELECT * FROM actor_activations WHERE graph_id=? AND node_id='worker'",
                (graph_id,),
            ).fetchone()
            if row is not None and row["op"] == "done" and runtime.starts:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert store.node(graph_id, "worker").flag
        assert len(runtime.starts) == 1
        close_graph(store, graph_id, actor=USER_ALLEN)
        assert authority.reconcile_actor(graph_id, "worker") is AdmissionResult.ACCEPTED
        deadline = time.monotonic() + 3.0
        while True:
            row = activation(store, graph_id)
            if row["desired"] == "down" and row["op"] == "done" and runtime.stops:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert len(runtime.stops) == 1
    finally:
        assert authority.close(3.0)


def test_blocked_actor_start_does_not_hold_pac_writer(graph, tmp_path: Path) -> None:
    store, graph_id = graph
    launch = tmp_path / "launch" / "w.yaml"
    launch.parent.mkdir()
    launch.write_text("harness: codex\nargs: [--headless]\n")
    PacReactor(store).set_flag(graph_id, "a", actor=USER_ALLEN)
    entered = threading.Event()
    release = threading.Event()

    class SlowRuntime(FakeRuntime):
        def start(self, *args, **kwargs):
            entered.set()
            assert release.wait(3.0)
            return super().start(*args, **kwargs)

    authority = PacGraphAuthority(store.path, NullSender())
    authority.attach_reconciler(SlowRuntime(), tmp_path, "epoch-1")
    try:
        assert authority.reconcile_actor(graph_id, "worker") is AdmissionResult.ACCEPTED
        assert entered.wait(1.0)
        started = time.monotonic()
        assert authority.close_graph(graph_id, actor=USER_ALLEN)["ok"]
        assert time.monotonic() - started < 0.25
    finally:
        release.set()
        assert authority.close(3.0)


def test_pac_service_submits_actor_reconcile_to_graph_authority(
    graph, tmp_path: Path,
) -> None:
    store, graph_id = graph
    launch = tmp_path / "launch" / "w.yaml"
    launch.parent.mkdir()
    launch.write_text("harness: codex\nargs: [--headless]\n")
    PacReactor(store).set_flag(graph_id, "a", actor=USER_ALLEN)
    _pend_worker(store, graph_id)
    runtime = FakeRuntime()
    authority = PacGraphAuthority(store.path, NullSender())
    service = PacActorService(
        state_dir=store.path.parent, reference_root=tmp_path,
        runtime=runtime, sender=NullSender(), daemon_epoch="epoch-1",
        logger=lambda *_args, **_kwargs: None, workers=1,
        graph_authority=authority,
    )
    try:
        service.submit_tick()
        deadline = time.monotonic() + 3.0
        while not runtime.starts:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert authority.actor_effect_stats()["capacity"] == 128
    finally:
        assert service.close(3.0)
        assert authority.close(3.0)


def test_actor_effect_failure_is_fenced_and_journaled_by_graph_writer(
    graph, tmp_path: Path,
) -> None:
    store, graph_id = graph
    launch = tmp_path / "launch" / "w.yaml"
    launch.parent.mkdir()
    launch.write_text("harness: codex\nargs: [--headless]\n")
    PacReactor(store).set_flag(graph_id, "a", actor=USER_ALLEN)
    runtime = FakeRuntime()
    runtime.start_error = RuntimeError("injected-start-failure")
    authority = PacGraphAuthority(store.path, NullSender())
    authority.attach_reconciler(runtime, tmp_path, "epoch-1")
    try:
        assert authority.reconcile_actor(graph_id, "worker") is AdmissionResult.ACCEPTED
        deadline = time.monotonic() + 3.0
        while True:
            row = store._db.execute(
                "SELECT op,identity_marker FROM actor_activations "
                "WHERE graph_id=? AND node_id='worker'", (graph_id,),
            ).fetchone()
            if row is not None and row["op"] == "done":
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert row["identity_marker"] is None
        assert "launch_failed" in event_types(store, graph_id)
        assert len(runtime.starts) == 1
    finally:
        assert authority.close(3.0)


def test_missing_launch_reference_does_not_kill_shared_graph_writer(
    graph, tmp_path: Path,
) -> None:
    store, graph_id = graph
    PacReactor(store).set_flag(graph_id, "a", actor=USER_ALLEN)
    runtime = FakeRuntime()
    failures: list[str] = []
    authority = PacGraphAuthority(store.path, NullSender())
    authority.attach_reconciler(
        runtime, tmp_path, "epoch-1",
        logger=lambda _level, _component, _event, **fields: failures.append(
            str(fields.get("detail"))
        ),
    )
    try:
        assert authority.reconcile_actor(graph_id, "worker") is AdmissionResult.ACCEPTED
        deadline = time.monotonic() + 3.0
        while True:
            row = store._db.execute(
                "SELECT op FROM actor_activations WHERE graph_id=? AND node_id='worker'",
                (graph_id,),
            ).fetchone()
            if row is not None and row["op"] == "done":
                break
            assert time.monotonic() < deadline, failures
            time.sleep(0.01)
        assert runtime.starts == []
        assert authority.close_graph(graph_id, actor=USER_ALLEN)["ok"]
    finally:
        assert authority.close(3.0)


def activation(store: PacGraphStore, gid: str) -> dict[str, Any]:
    return dict(
        store._db.execute(
            "SELECT * FROM actor_activations WHERE graph_id=? AND node_id='worker'",
            (gid,),
        ).fetchone()
    )


def event_types(store: PacGraphStore, gid: str) -> list[str]:
    return [item["type"] for item in store.journal_events(gid)]


def test_a5_happy_path_launch_assignment_end_close_and_down(graph) -> None:
    store, gid = graph
    runtime = FakeRuntime()
    sender = NullSender()
    actor = coordinator(store, runtime, "epoch-1", sender)
    actor.reconcile_all()
    assert runtime.starts == [], "actor waits for its forward predecessor"

    PacReactor(store).set_flag(gid, "a", actor=USER_ALLEN, reason_ref="result#a")
    actor.reconcile_all()
    row = activation(store, gid)
    assert row["desired"] == "up" and row["op"] == "done" and row["incarnation"] == 1
    worker = store.node(gid, "worker")
    assert worker is not None and worker.flag
    assert worker.flag_set_by == "reactor" and worker.flag_reason_ref == row["effect_id"]
    turns = [item for item in store.notifications(gid) if item.kind == "turn"]
    assert any(item.plan["nodeId"] == "b" and item.recipient == AGENT_W for item in turns)
    context = node_context(store.path, gid, "b")
    assert context["blockedOn"] == "agent"
    assert context["actor"]["actorName"] == "w"
    assert context["actor"]["launchRef"] == "launch/w.yaml"
    assert [item["nodeId"] for item in context["actor"]["assignments"]] == ["b"]
    assert node_context(store.path, gid, "c")["blockedOn"] == "none"

    PacReactor(store).set_flag(gid, "b", actor=AGENT_W, reason_ref="result#b")
    PacReactor(store).set_flag(gid, "c", actor=USER_ALLEN, reason_ref="result#c")
    assert store.graph(gid)["closed_by"] == "c"
    actor.reconcile_all()
    assert runtime.observe("w").present is False
    assert store.node(gid, "worker").flag is False
    assert activation(store, gid)["desired"] == "down"
    types = event_types(store, gid)
    assert types.count("actor_up") == 1
    assert types.count("actor_down") == 1
    assert types.count("graph_closed") == 1


def test_launch_failure_is_durable_not_flagged_not_retried_and_notifies_owner(graph) -> None:
    store, gid = graph
    PacReactor(store).set_flag(gid, "a", actor=USER_ALLEN)
    runtime = FakeRuntime()
    runtime.start_error = RuntimeError(
        "HARNESS_CWD_MISSING: /absolute/missing-worktree"
    )
    sender = NullSender()
    actor = coordinator(store, runtime, "epoch-1", sender)
    actor.reconcile_all()
    actor.reconcile_all()
    assert len(runtime.starts) == 1
    assert store.node(gid, "worker").flag is False
    assert event_types(store, gid).count("launch_failed") == 1
    assert any("launch_failed:w" in item["text"] for item in sender.sent)
    assert any(
        "HARNESS_CWD_MISSING" in item["text"]
        and "/absolute/missing-worktree" in item["text"]
        for item in sender.sent
    )
    # Failure of this branch does not prevent an independent node from moving.
    assert store.node(gid, "a").flag is True


def test_restart_reconciles_current_activation_and_is_idempotent(graph) -> None:
    store, gid = graph
    PacReactor(store).set_flag(gid, "a", actor=USER_ALLEN)
    runtime = FakeRuntime()
    first = coordinator(store, runtime, "epoch-1")
    first.reconcile_all()
    marker = activation(store, gid)["identity_marker"]
    assert activation(store, gid)["incarnation"] == 1

    restored = coordinator(store, runtime, "epoch-2")
    restored.reconcile_all()
    restored.reconcile_all()
    assert activation(store, gid)["incarnation"] == 1
    assert event_types(store, gid).count("actor_restored") == 1
    assert runtime.starts[0][2] == marker


@pytest.mark.parametrize("loss_epoch", ["epoch-1", "epoch-2"], ids=["same-epoch", "restart"])
def test_lost_current_runtime_is_terminal_unflagged_and_not_relaunched(graph, loss_epoch) -> None:
    store, gid = graph
    PacReactor(store).set_flag(gid, "a", actor=USER_ALLEN)
    runtime = FakeRuntime()
    coordinator(store, runtime, "epoch-1").reconcile_all()
    assert store.node(gid, "worker").flag is True
    runtime.running.clear()
    sender = NullSender()
    observer = coordinator(store, runtime, loss_epoch, sender)
    observer.reconcile_all()
    observer.reconcile_all()
    assert len(runtime.starts) == 1, "lost up/done runtime is reported, never auto-relaunched"
    assert event_types(store, gid).count("actor_lost") == 1
    assert event_types(store, gid).count("flag_reset") == 1
    assert store.node(gid, "worker").flag is False
    assert activation(store, gid)["desired"] == "down"
    assert any("actor_lost:w" in item["text"] for item in sender.sent)


def test_actor_lost_event_flag_reset_and_notification_plan_roll_back_together(
    graph, monkeypatch
) -> None:
    store, gid = graph
    PacReactor(store).set_flag(gid, "a", actor=USER_ALLEN)
    runtime = FakeRuntime()
    actor = coordinator(store, runtime, "epoch-1")
    actor.reconcile_all()
    runtime.running.clear()
    before_events = event_types(store, gid)
    reached = 0

    def crash(*_args, **_kwargs):
        nonlocal reached
        reached += 1
        raise RuntimeError("injected lost transaction crash")

    monkeypatch.setattr(actor.reactor, "_insert_notifications", crash)
    worker = store.node(gid, "worker")
    assert worker is not None
    with pytest.raises(RuntimeError, match="injected lost"):
        actor._complete_lost(
            gid,
            worker,
            activation(store, gid),
            RuntimeObservation(False),
        )
    assert reached == 1, "fault injection must execute inside _complete_lost"
    assert activation(store, gid)["desired"] == "up"
    assert store.node(gid, "worker").flag is True
    assert event_types(store, gid) == before_events


def test_pending_start_with_matching_connector_completes_same_operation(graph) -> None:
    store, gid = graph
    PacReactor(store).set_flag(gid, "a", actor=USER_ALLEN)
    runtime = FakeRuntime()
    actor = coordinator(store, runtime, "epoch-2")
    row = actor._prepare_up(gid, store.node(gid, "worker"))
    runtime.running["w"] = RuntimeObservation(
        True, row["identity_marker"], "codex", row["operation_id"]
    )
    actor.reconcile_all()
    assert runtime.starts == []
    assert activation(store, gid)["op"] == "done"
    assert activation(store, gid)["incarnation"] == 1


def test_early_stop_inflight_restart_continues_down_and_never_resurrects(graph) -> None:
    store, gid = graph
    PacReactor(store).set_flag(gid, "a", actor=USER_ALLEN)
    runtime = FakeRuntime()
    first = coordinator(store, runtime, "epoch-1")
    first.reconcile_all()
    request_actor_stop(store, gid, "w", actor=USER_ALLEN)
    assert activation(store, gid)["desired"] == "down" and activation(store, gid)["op"] == "pending"

    restarted = coordinator(store, runtime, "epoch-2")
    restarted.reconcile_all()
    restarted.reconcile_all()
    assert len(runtime.starts) == 1
    assert len(runtime.stops) == 1
    assert activation(store, gid)["desired"] == "down" and activation(store, gid)["op"] == "done"
    assert store.node(gid, "worker").flag is False


def test_unowned_same_name_is_reported_and_never_touched(graph) -> None:
    store, gid = graph
    PacReactor(store).set_flag(gid, "a", actor=USER_ALLEN)
    runtime = FakeRuntime()
    actor = coordinator(store, runtime, "epoch-1")
    actor._prepare_up(gid, store.node(gid, "worker"))
    runtime.running["w"] = RuntimeObservation(True, "somebody-elses-marker", "codex")
    sender = NullSender()
    restarted = coordinator(store, runtime, "epoch-2", sender)
    restarted.reconcile_all()
    restarted.reconcile_all()
    assert runtime.starts == [] and runtime.stops == []
    assert event_types(store, gid).count("actor_unowned") == 1
    assert runtime.observe("w").present


def test_closed_pending_start_with_live_connector_goes_down_without_up_flag(graph) -> None:
    store, gid = graph
    PacReactor(store).set_flag(gid, "a", actor=USER_ALLEN)
    runtime = FakeRuntime()
    actor = coordinator(store, runtime, "epoch-1")
    row = actor._prepare_up(gid, store.node(gid, "worker"))
    runtime.running["w"] = RuntimeObservation(True, row["identity_marker"], "codex")
    close_graph(store, gid, actor=USER_ALLEN)
    actor.reconcile_all()
    assert runtime.observe("w").present is False
    assert store.node(gid, "worker").flag is False
    assert "actor_up" not in event_types(store, gid)
    assert event_types(store, gid).count("actor_down") == 1


def test_cancelled_graph_retires_nonrunning_desired_worker_without_restart(
    graph,
) -> None:
    store, gid = graph
    PacReactor(store).set_flag(gid, "a", actor=USER_ALLEN)

    class FailedStartRuntime(FakeRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.failed_start = False
            self.desired = False

        def start(self, *args, **kwargs):
            observation = super().start(*args, **kwargs)
            self.desired = True
            return observation

        def observe(self, actor_name: str) -> RuntimeObservation:
            observation = super().observe(actor_name)
            if self.failed_start and self.desired:
                return RuntimeObservation(
                    False,
                    observation.identity_marker,
                    observation.harness,
                    observation.operation_id,
                    observation.agent_entity_token,
                    retirement_pending=True,
                )
            return observation

        def stop(self, *args, **kwargs):
            observation = super().stop(*args, **kwargs)
            self.desired = False
            return observation

    runtime = FailedStartRuntime()
    actor = coordinator(store, runtime, "epoch-1")
    actor.reconcile_all()
    assert runtime.desired and len(runtime.starts) == 1

    runtime.failed_start = True
    close_graph(store, gid, actor=USER_ALLEN)
    actor.reconcile_all()
    actor.reconcile_all()

    assert not runtime.desired
    assert len(runtime.stops) == 1
    assert len(runtime.starts) == 1
    assert activation(store, gid)["desired"] == "down"
    assert activation(store, gid)["op"] == "done"


@pytest.mark.parametrize(
    ("observed_marker", "expected_stop_count"),
    [("expected", 1), (None, 0), ("pac:another:worker:1:effect", 0)],
)
def test_nonrunning_desired_worker_only_matches_its_activation_marker(
    graph, observed_marker: str | None, expected_stop_count: int
) -> None:
    store, gid = graph
    PacReactor(store).set_flag(gid, "a", actor=USER_ALLEN)
    runtime = FakeRuntime()
    actor = coordinator(store, runtime, "epoch-1")
    row = actor._prepare_up(gid, store.node(gid, "worker"))
    marker = row["identity_marker"]
    runtime.running["w"] = RuntimeObservation(
        False,
        marker if observed_marker == "expected" else observed_marker,
        "codex",
        retirement_pending=True,
    )
    close_graph(store, gid, actor=USER_ALLEN)

    actor.reconcile_all()

    assert len(runtime.stops) == expected_stop_count
    if expected_stop_count:
        assert activation(store, gid)["op"] == "done"
    else:
        assert activation(store, gid)["op"] == "pending"


def test_actor_requires_tier_uses_static_matrix_selection(
    graph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PC3 through PAC: requires.tier resolves statically and probes nothing.

    Migration: replaces ``..._uses_accepted_425_matrix_and_audits_probe``,
    which injected one ``readings`` entry and asserted it was carried into the
    launch spec.  Selection no longer probes, so the settled reading is that
    the real resolver decides and ``probes`` stays empty.
    """

    store, _gid = graph
    from hyprial.daemon.impl.dispatch import matrix

    node = replace(
        store.node(_gid, "worker"),
        requires={"tier": "fast"},
    )
    launch = tmp_path / "launch.yaml"
    launch.write_text("cwd: /tmp\nargs: [--headless]\n")
    monkeypatch.setattr(
        "hyprial.daemon.impl.squire.probe._subprocess_runner",
        lambda *_: pytest.fail("PAC selection probed a harness"),
    )
    resolved = FileLaunchResolver(tmp_path)("launch.yaml", node)
    expected = matrix.resolve_minimum_tier("fast").selected
    assert resolved.spec.harness == expected.harness
    assert resolved.spec.model == expected.model
    assert resolved.spec.tier == "fast"
    assert resolved.spec.probes == ()


def test_actor_up_activation_flag_plan_and_journal_rollback_together(graph, monkeypatch) -> None:
    store, gid = graph
    PacReactor(store).set_flag(gid, "a", actor=USER_ALLEN)
    runtime = FakeRuntime()
    actor = coordinator(store, runtime, "epoch-1")
    row = actor._prepare_up(gid, store.node(gid, "worker"))
    runtime.running["w"] = RuntimeObservation(True, row["identity_marker"], "codex")
    before = event_types(store, gid)

    def crash(*_args, **_kwargs):
        raise RuntimeError("injected transaction crash")

    monkeypatch.setattr(actor.reactor, "_insert_notifications", crash)
    with pytest.raises(RuntimeError, match="injected"):
        actor.reconcile_all()
    assert activation(store, gid)["op"] == "pending"
    assert store.node(gid, "worker").flag is False
    assert event_types(store, gid) == before


# --- a reconcile that leaves an activation where it was says why ----------
# 2026-09-24: a closed graph's workers were reclaimed only 73 s after close on
# a 1 s tick, and nothing was logged in between.  Both paths below used to
# return silently.


def _skip_recorder() -> tuple[list[tuple[str, str, str, dict[str, Any]]], Any]:
    seen: list[tuple[str, str, str, dict[str, Any]]] = []
    return seen, lambda graph_id, node_id, reason, detail: seen.append(
        (graph_id, node_id, reason, detail)
    )


class StickyRuntime(FakeRuntime):
    """A stop that returns without the connector going away."""

    def stop(
        self,
        actor_name: str,
        launch: LaunchSpec,
        *,
        operation_id: str,
        identity_marker: str,
    ) -> RuntimeObservation:
        self.stops.append((actor_name, operation_id, identity_marker))
        return self.running[actor_name]


def test_closed_graph_same_epoch_marker_mismatch_is_reported_not_silent(graph) -> None:
    store, gid = graph
    PacReactor(store).set_flag(gid, "a", actor=USER_ALLEN)
    runtime = FakeRuntime()
    seen, on_skip = _skip_recorder()
    actor = ActorCoordinator(
        store, runtime, daemon_epoch="epoch-1", resolver=resolver, clock=lambda: 100,
        on_skip=on_skip,
    )
    actor.reconcile_all()  # up, in this epoch
    assert activation(store, gid)["daemon_epoch"] == "epoch-1"
    runtime.running["w"] = RuntimeObservation(True, "pac:another-incarnation", "codex")
    close_graph(store, gid, actor=USER_ALLEN)

    actor.reconcile_all()

    # Unchanged: a same-name connector we cannot prove is ours is not stopped.
    assert runtime.stops == [] and runtime.observe("w").present
    assert [(node, reason) for _graph, node, reason, _detail in seen] == [
        ("worker", "marker_mismatch")
    ]
    detail = seen[0][3]
    assert detail["desired"] == "down"
    assert detail["observed"] == "pac:another-incarnation"


def test_a_stop_that_leaves_the_connector_up_is_reported(graph) -> None:
    store, gid = graph
    PacReactor(store).set_flag(gid, "a", actor=USER_ALLEN)
    runtime = StickyRuntime()
    seen, on_skip = _skip_recorder()
    actor = ActorCoordinator(
        store, runtime, daemon_epoch="epoch-1", resolver=resolver, clock=lambda: 100,
        on_skip=on_skip,
    )
    actor.reconcile_all()
    close_graph(store, gid, actor=USER_ALLEN)

    actor.reconcile_all()

    assert len(runtime.stops) == 1
    assert activation(store, gid)["desired"] == "down"
    assert activation(store, gid)["op"] == "pending"
    assert [reason for _graph, _node, reason, _detail in seen] == [
        "still_present_after_stop"
    ]
    assert seen[0][3]["operationId"] == activation(store, gid)["operation_id"]


def _service_logs(
    store: PacGraphStore, tmp_path: Path, runtime: FakeRuntime
) -> tuple[PacActorService, list[tuple[str, dict[str, Any]]]]:
    logs: list[tuple[str, dict[str, Any]]] = []
    # The service resolves the node's launch_ref against reference_root, as
    # the daemon does (see test_pac_notification_and_actor_run_...).
    launch = tmp_path / "launch" / "w.yaml"
    launch.parent.mkdir(exist_ok=True)
    launch.write_text("harness: codex\nargs: [--headless]\n", encoding="utf-8")
    service = PacActorService(
        state_dir=store.path.parent,
        reference_root=tmp_path,
        runtime=runtime,
        sender=NullSender(),
        daemon_epoch="epoch-1",
        logger=lambda _level, _component, event, **fields: logs.append((event, fields)),
        workers=1,
    )
    return service, logs


def _tick_until(service: PacActorService, predicate: Any, *, ticks: int = 40) -> None:
    for _ in range(ticks):
        # Probes need one supply per tick; force the fallback sweep due.
        service._actor_fallback_due = 0.0
        service.submit_tick()
        time.sleep(0.02)
        if predicate():
            return
    raise AssertionError("PAC actor service never reached the expected state")


def _pend_worker(store: PacGraphStore, graph_id: str, *, node_id: str = "worker") -> None:
    """Commit an in-transition activation so the fallback sweep supplies it."""
    db = store.write()
    try:
        db.execute(
            "INSERT OR IGNORE INTO actor_activations "
            "(graph_id,node_id,incarnation,desired,op,effect_id,operation_id,"
            "identity_marker,updated_at) "
            "VALUES (?,?,0,'up','pending',?,?,?,0)",
            (graph_id, node_id, f"effect:{node_id}", f"operation:{node_id}",
             f"pac:{graph_id}:{node_id}:1:{node_id}"),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def test_service_logs_a_repeating_skip_once_not_every_tick(graph, tmp_path: Path) -> None:
    store, gid = graph
    PacReactor(store).set_flag(gid, "a", actor=USER_ALLEN)
    _pend_worker(store, gid)
    runtime = StickyRuntime()
    service, logs = _service_logs(store, tmp_path, runtime)
    try:
        _tick_until(service, lambda: runtime.starts)
        close_graph(store, gid, actor=USER_ALLEN)
        _tick_until(service, lambda: len(runtime.stops) >= 5)
    finally:
        assert service.close(2.0)
    skipped = [fields for event, fields in logs if event == "pac.actor.reconcile_skipped"]
    assert len(runtime.stops) >= 5
    assert [(item["nodeId"], item["reason"]) for item in skipped] == [
        ("worker", "still_present_after_stop")
    ]


def test_service_logs_a_slow_reconcile(
    graph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hyprial.daemon.impl.pac.actors.daemon as pac_actor

    class SlowRuntime(FakeRuntime):
        def observe(self, actor_name: str) -> RuntimeObservation:
            time.sleep(0.05)
            return super().observe(actor_name)

    monkeypatch.setattr(pac_actor, "_PAC_RECONCILE_SLOW_MS", 20)
    store, gid = graph
    PacReactor(store).set_flag(gid, "a", actor=USER_ALLEN)
    _pend_worker(store, gid)
    runtime = SlowRuntime()
    service, logs = _service_logs(store, tmp_path, runtime)
    try:
        _tick_until(
            service,
            lambda: any(event == "pac.actor.reconcile_slow" for event, _fields in logs),
        )
    finally:
        assert service.close(2.0)
    slow = [fields for event, fields in logs if event == "pac.actor.reconcile_slow"]
    assert slow[0]["graphId"] == gid and slow[0]["nodeId"] == "worker"
    assert slow[0]["elapsedMs"] >= 20
