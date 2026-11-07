"""The A3 dispatch gate: what the daemon counts, and what it must not.

design-dispatch-always-pac-2026-09-03 §三② pinned the record-only gate;
spec-dispatch-gate-classifier-2026-09-04 narrows its judgment.  The first
reading (card 8754fbff, 2026-09-04) showed the old shape — request intent,
coordinator sender, local agent recipient — counted adjudications, progress
reports, and conversational replies: 21 events, none a dispatch.  The gate
now requires BOTH:

* (a) 新会话 — the send opens its conversation (first traversal of this
  conversationId through ``message.send``; an idempotent replay of the SAME
  send still opens it, a different send reusing the id does not);
* (b) 派活特征 — the body carries a PAC-template delivery-section keyword
  (交付物 / 分支 / spec / 首个可核产出) or the payload carries a
  ``task``/``deliverable`` field.

Everything else in the old shape is a conversation: logged as
``dispatch.conversation(reason)`` and counted in the ps/top
``dispatchConversationCount`` — observable, never in the
``dispatchWithoutPacCount`` numerator.  Still record-only: nothing here
blocks or alters a delivery.

The four spec §3 samples each get a test, and the three not-counted classes
assert the counter does not move — a classifier that counts everything must
go red here, not just pass the one true-dispatch pin.  Mutations (drop (a),
drop (b)) are verified outside this file by editing the classifier, running
these samples, and reading red.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon import summary_from_text
from hyprial.kernel import canonical_agent_uri
from hyprial.daemon.impl.operations.top import build_top_snapshot
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryLifecycle,
    InboxMessage,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.service import InboxService
from hyprial.kernel import Logger
from hyprial.daemon.impl.inbox.links.io import (
    CorrelatedInboxEventRouter,
    InboxDeliveryIoAdapter as WorkflowInboxIoAdapter,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

NODE = "gate-node"
PI_NAME = "pi-worker"
PI_URI = canonical_agent_uri("allen", NODE, PI_NAME)
# Registered CLI sender persona (Allen b: resolve-or-reject — no minting):
# the coordinator that dispatches work it never wrapped in a workflow run.
DRIVER = "hq-adjutant"
DRIVER_URI = canonical_agent_uri("allen", NODE, DRIVER)

DOC_EVENT = "dispatch.without_pac"
CONV_EVENT = "dispatch.conversation"

# ── The four spec §3 sample bodies ──────────────────────────────────────
# Each is one real observed class from the 2026-09-04 reading (card 8754fbff).
# The three conversation classes deliberately exercise BOTH failing
# conditions: adjudication and progress report fail only (b) (new
# conversation, no feature); the reply fails only (a) (existing
# conversation, feature present) — so dropping either condition must turn
# its sample red.
ADJUDICATION_TEXT = "裁定:卡 8754fbff 维持「先记后拦」,按原读数推进。"
PROGRESS_TEXT = "进度回报:焦点 3/3 绿,读数已落盘,无阻塞,等我下一报。"
THREAD_OPEN_TEXT = "收到,这单我接。"  # establishes a conversation, no feature
REPLY_TEXT = "spec 第 2 条已照改,ps/top 字段都加了。"  # feature, old conversation
TRUE_DISPATCH_TEXT = (
    "派活:分支 fix/sample-classifier,交付物:PR → dev,spec 见卡 8754fbff;"
    "60 分钟内出首个可核产出。"
)


class LoopbackTransport:
    """Single-node loopback so submit() -> deliver() -> receive() dedups."""

    def __init__(self) -> None:
        self.service: InboxService | None = None
        self.online: set[str] = set()

    def bind(self, service: InboxService) -> None:
        self.service = service

    def is_online(self, recipient: str) -> bool:
        return recipient in self.online

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()

    def deliver(self, message: object) -> bool:
        assert self.service is not None
        self.service.receive(message)  # type: ignore[arg-type]
        return self.service.has_received(message.message_id)  # type: ignore[attr-defined]

    def transfer_custody(self, mailbox: str, message: object) -> bool:
        return False


class LoopbackPresence:
    """The ps-level presence double every daemon test uses (actor_online + mailboxes)."""

    def __init__(self, online: tuple[str, ...] = ()) -> None:
        self._online = online

    def actor_online(self, actor: str) -> bool:
        return actor in self._online

    def online_actors(self) -> tuple[str, ...]:
        return self._online

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


class StatusOnlySupervisor:
    """Enough of ManagedHarnessRuntime for the ps connector view."""

    def status(self) -> tuple[dict[str, object], ...]:
        return ()


def _application(tmp_path: Path, inbox: InboxService) -> DaemonApplication:
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
        owner="allen",
    **_deep_module_daemon_dependencies())
    app._inbox = inbox
    app._presence = LoopbackPresence()
    app._harnesses = StatusOnlySupervisor()
    app._adapters = None
    return app


@pytest.fixture
def harness(tmp_path: Path):
    """The daemon app and its daemon.jsonl reader, wired the standard way."""

    transport = LoopbackTransport()
    service = InboxService(tmp_path / "inbox.sqlite3", transport)
    transport.bind(service)
    transport.online.update({NODE, PI_URI, DRIVER_URI})
    app = _application(tmp_path, service)

    def events() -> list[dict]:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = app._logger.writer_status()
            if status is None or status.written + status.failed >= status.accepted:
                break
            time.sleep(0.005)
        path = tmp_path / "state" / "logs" / "daemon.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def gate_events() -> list[dict]:
        return [item for item in events() if item.get("event") == DOC_EVENT]

    def conv_events() -> list[dict]:
        return [item for item in events() if item.get("event") == CONV_EVENT]

    def ps() -> dict:
        return app.handle("ps", {})["daemon"]

    yield app, events, gate_events, conv_events, ps

    # Daemon construction now owns several actor/effect threads as well as DBs.
    app._close()


def _register_driver(app: DaemonApplication) -> None:
    if app.agents.get(DRIVER) is None:
        app.handle("agent.create", {"name": DRIVER})


def _send(app: DaemonApplication, **extra) -> dict:
    params = {
        "actor": DRIVER,
        "to": [PI_URI],
        "message": TRUE_DISPATCH_TEXT,
        "conversationId": "conv-gate",
        "idempotencyKey": "op-gate-1",
    }
    params.update(extra)
    params.setdefault("summary", summary_from_text(params["message"]))
    return app.handle("message.send", params)


# ── The four-class samples (spec §3) ────────────────────────────────────


def test_adjudication_notice_is_not_counted(harness) -> None:
    """裁定消息 sample: new conversation, no dispatch feature ⇒ conversation.

    Fails only condition (b): a bare ruling/notice opens a fresh
    conversation with the coordinator's decision and no delivery-section
    keyword.  The counter must not move.
    """

    app, _events, gate_events, conv_events, ps = harness
    _register_driver(app)

    sent = _send(
        app,
        message=ADJUDICATION_TEXT,
        conversationId="conv-adjudication",
        idempotencyKey="op-adjudication",
    )

    assert gate_events() == []
    assert ps()["dispatchWithoutPacCount"] == 0
    assert ps()["dispatchConversationCount"] == 1
    logged = conv_events()
    assert len(logged) == 1
    assert logged[0]["reason"] == "no-dispatch-feature"
    assert logged[0]["sender"] == DRIVER_URI
    assert logged[0]["recipient"] == PI_URI
    assert logged[0]["messageId"] == sent["deliveries"][0]["messageId"]


def test_progress_report_is_not_counted(harness) -> None:
    """进度回报 sample: worker report, no dispatch feature ⇒ conversation.

    Fails only condition (b): the report carries readings, not a delivery
    section.  The counter must not move.
    """

    app, _events, gate_events, conv_events, ps = harness
    _register_driver(app)

    _send(
        app,
        message=PROGRESS_TEXT,
        conversationId="conv-progress",
        idempotencyKey="op-progress",
    )

    assert gate_events() == []
    assert ps()["dispatchWithoutPacCount"] == 0
    assert ps()["dispatchConversationCount"] == 1
    assert [item["reason"] for item in conv_events()] == ["no-dispatch-feature"]


def test_reply_in_existing_conversation_is_not_counted(harness) -> None:
    """回复 sample: an answer inside an existing conversation ⇒ conversation.

    The thread was opened by a feature-less send (counter stays 0), then a
    second send reuses the SAME conversationId with a body that does carry a
    keyword — fails only condition (a).  The counter must not move; a
    replay under a dropped (a) miscounts this send.
    """

    app, _events, gate_events, conv_events, ps = harness
    _register_driver(app)

    _send(
        app,
        message=THREAD_OPEN_TEXT,
        conversationId="conv-thread",
        idempotencyKey="op-thread-1",
    )
    assert ps()["dispatchWithoutPacCount"] == 0

    sent = _send(
        app,
        message=REPLY_TEXT,
        conversationId="conv-thread",
        idempotencyKey="op-thread-2",
    )

    assert gate_events() == []
    assert ps()["dispatchWithoutPacCount"] == 0
    assert ps()["dispatchConversationCount"] == 2
    logged = conv_events()
    assert [item["reason"] for item in logged] == [
        "no-dispatch-feature",
        "existing-conversation",
    ]
    assert logged[1]["messageId"] == sent["deliveries"][0]["messageId"]


def test_true_dispatch_is_counted_and_pinned(harness) -> None:
    """真派活 sample: new conversation + delivery-section keywords ⇒ one event."""

    app, _events, gate_events, conv_events, ps = harness
    _register_driver(app)

    sent = _send(app)

    logged = gate_events()
    assert len(logged) == 1
    event = logged[0]
    assert event["sender"] == DRIVER_URI
    assert event["recipient"] == PI_URI
    assert event["messageId"] == sent["deliveries"][0]["messageId"]
    assert event["level"] == "info"
    assert event["component"] == "daemon"

    # The counted send is NOT a conversation: no event, no counter move.
    assert conv_events() == []
    assert ps()["dispatchWithoutPacCount"] == 1
    assert ps()["dispatchConversationCount"] == 0


# ── Condition (b) second disjunct: payload task/deliverable fields ─────


def test_payload_task_or_deliverable_field_is_a_dispatch_feature(harness) -> None:
    """Payload disjunct of condition (b), pinned at the predicate.

    No wire path mints a task/deliverable payload field yet (message.send
    builds {"message", "topic"} itself), so the second disjunct is pinned
    here directly against the production InboxMessage type — the predicate
    the gate calls, not a stub.
    """

    app, *_rest = harness

    def message(payload: dict) -> InboxMessage:
        return InboxMessage(
            message_id="m-feature",
            conversation_id="conv-feature",
            sender=DRIVER_URI,
            recipient=PI_URI,
            payload=json.dumps(payload, separators=(",", ":")).encode(),
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            idempotency_key="op-feature",
            created_at_ms=1,
        )

    assert app._dispatch_send_has_feature(message({"task": "fix the gate"}))
    assert app._dispatch_send_has_feature(message({"deliverable": "PR to dev"}))
    assert app._dispatch_send_has_feature(message({"message": TRUE_DISPATCH_TEXT}))
    assert not app._dispatch_send_has_feature(message({"message": PROGRESS_TEXT}))
    assert not app._dispatch_send_has_feature(message({"topic": "offhand"}))


# ── Boundary shapes carried over from the §三② suite ───────────────────


def test_pac_dispatch_leaves_no_gate_event(tmp_path: Path) -> None:
    """The engine's deliver:target reaches the worker without a gate event.

    Uses the one delivery adapter the daemon wires into the workflow service
    (composition: WorkflowInboxIoAdapter over the custody coordinator), with
    the coordinator holding the same daemon logger production passes it — so
    a detection that drifted into the inbox layer would still land in
    daemon.jsonl and fail this test.
    """

    state_dir = tmp_path / "state"
    daemon_logger = Logger.daemon(state_dir, name=NODE)
    router = CorrelatedInboxEventRouter()
    transport = MemoryDeliveryTransport()
    transport.set_online(PI_URI, True)
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox-wf.sqlite3",
        transport,
        router,
        node_id=NODE,
        service_options={"logger": daemon_logger},
    )
    try:
        adapter = WorkflowInboxIoAdapter(
            coordinator,
            router,
            resolve_target=lambda target: target,
            completion_timeout=2.0,
        )
        delivered = adapter.deliver(
            effect_id="wf-effect-a3",
            sender=canonical_agent_uri("allen", NODE, "dispatch-coordinator"),
            target=PI_URI,
            conversation_id="wf-run-a3",
            text="PING {{nonce}}",
            summary=summary_from_text("PING {{nonce}}"),
        )
        # The dispatch really happened: the workflow message reached the
        # worker, so the absence of a gate event below is not vacuous.
        assert delivered.message_id in transport.delivered
    finally:
        assert coordinator.drain(2.0).complete

    path = state_dir / "logs" / "daemon.jsonl"
    logged = (
        [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if path.exists()
        else []
    )
    assert [
        item for item in logged if item.get("event") in (DOC_EVENT, CONV_EVENT)
    ] == []


def test_reply_is_not_a_dispatch(harness) -> None:
    """A replyTo send carries intent=reply: an answer, never any gate event."""

    app, _events, gate_events, conv_events, ps = harness
    _register_driver(app)

    _send(app, replyTo="wf-msg-the-run-awaited")

    assert gate_events() == []
    assert conv_events() == []
    assert ps()["dispatchWithoutPacCount"] == 0
    assert ps()["dispatchConversationCount"] == 0


def test_non_local_agent_targets_are_not_dispatches(harness) -> None:
    """§三② says 本机 agent: a foreign-node recipient is not assigned here."""

    app, _events, gate_events, conv_events, ps = harness
    _register_driver(app)
    foreign = canonical_agent_uri("allen", "other-node", "far-worker")

    _send(app, to=[foreign], idempotencyKey="op-gate-foreign")

    assert gate_events() == []
    assert conv_events() == []
    assert ps()["dispatchWithoutPacCount"] == 0
    assert ps()["dispatchConversationCount"] == 0


def test_top_snapshot_carries_the_counter() -> None:
    """hyprial agent top reads the same numerators ps exposes (render shows both)."""

    payload = build_top_snapshot(
        state_dir=Path("/tmp"),
        owner="allen",
        node_id=NODE,
        epoch="epoch-1",
        daemon_pid=1,
        connectors=[],
        pending={},
        stranded=[],
        now_ms=1_000,
        dispatch_without_pac_count=3,
        dispatch_conversation_count=5,
    )
    assert payload["daemon"]["dispatchWithoutPacCount"] == 3
    assert payload["daemon"]["dispatchConversationCount"] == 5


def test_gate_logs_through_the_daemons_own_logger(harness) -> None:
    """The event file is THE daemon.jsonl, not a side channel."""

    app, events, gate_events, _conv_events, _ps = harness
    _register_driver(app)
    _send(app)
    # The gate event lives inside the ordinary daemon stream, alongside the
    # send.received marker the handler always writes for the same message.
    all_events = events()
    assert [item.get("event") for item in all_events].count("send.received") >= 1
    assert len(gate_events()) == 1
