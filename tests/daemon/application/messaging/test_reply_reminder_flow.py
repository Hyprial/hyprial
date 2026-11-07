"""WP7 W1-W3: the reminder lane is wired end to end on the daemon bus.

W1 drives a real inbox and a real hook bus with the shell-provided factory.
Review R1 keeps the durable-authority assertions here (summary and payload);
the pending projection's ``bodyBytes`` field belongs to WP3-A and is BLOCKED
until that package is merged (see the WP7 output note).
"""

from __future__ import annotations

import ast
import json
import time
from pathlib import Path
from typing import Any

from tests.support.runtime._owned_applications import (
    owned_applications as owned_applications,
)

from hyprial.daemon.impl import HarnessDelivery, HarnessResult, HarnessResultStatus
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.hooks import DaemonHookService
from hyprial.daemon.impl.harnesses.turn_delivery.turn import TurnDeliveryProjection
from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.service import InboxService
from hyprial.kernel import HookBus, canonical_agent_uri
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _daemon_dependencies

NODE = "wp7-node"
OWNER = "wp7-owner"
A_NAME = "worker-a"
A_URI = canonical_agent_uri(OWNER, NODE, A_NAME)
X_NAME = "peer-x"
X_URI = canonical_agent_uri(OWNER, NODE, X_NAME)
TEXT = "1 条请求未回复：r1；回复用 reply，无需回复用 ack"


class LoopbackTransport:
    def __init__(self) -> None:
        self.service: InboxService | None = None

    def bind(self, service: InboxService) -> None:
        self.service = service

    def is_online(self, recipient: str) -> bool:
        return True

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()

    def deliver(self, message: InboxMessage) -> bool:
        assert self.service is not None
        self.service.receive(message)
        return self.service.has_received(message.message_id)

    def transfer_custody(self, mailbox: str, message: InboxMessage) -> bool:
        return False


def _inbox(tmp_path: Path) -> InboxService:
    transport = LoopbackTransport()
    service = InboxService(tmp_path / "inbox.sqlite3", transport)
    transport.bind(service)
    return service


def _app(tmp_path: Path, inbox: InboxService) -> DaemonApplication:
    dependencies = dict(_daemon_dependencies())
    # The shell hands the constructor the reminder factory.  Kept explicit so
    # a missing shell seam fails loudly at construction (the WP7 red shape).
    reminder_factory = dependencies.pop("reply_reminder_consumer_factory", None)
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
        reply_reminder_consumer_factory=reminder_factory,
        **dependencies,
    )
    app._inbox = inbox
    app._presence = object()
    app._harnesses = object()
    app._adapters = None
    if app.agents.get(A_NAME) is None:
        app.handle("agent.create", {"name": A_NAME})
    return app


def _request(inbox: InboxService, message_id: str = "r1") -> InboxMessage:
    message = InboxMessage(
        message_id=message_id,
        conversation_id="conv-wp7",
        sender=X_URI,
        recipient=A_URI,
        payload=b'{"message":"please answer"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    inbox.receive(message)
    return message


def _hook_service(tmp_path: Path, bus: HookBus) -> DaemonHookService:
    return DaemonHookService(
        bus=bus,
        config_path_for_agent=lambda _actor: tmp_path / "absent" / "hooks.json",
        consumers={},
        handler_notifier=lambda *_args, **_kwargs: None,
    )


def _wire(app: DaemonApplication, daemon_hooks: DaemonHookService) -> None:
    from hyprial.daemon.impl.application.wiring.reply_reminder_hooks import (
        wire_reply_reminder_hooks,
    )

    wire_reply_reminder_hooks(
        inbox=app._inbox,
        node_id=app.node_id,
        clock_ms=lambda: time.time_ns() // 1_000_000,
        consumer_factory=app._reply_reminder_consumer_factory,
        daemon_hooks=daemon_hooks,
    )


def _run_turn(
    daemon_hooks: DaemonHookService,
    *,
    delivery_id: str,
    started_at_ms: int,
    ended_at_ms: int,
) -> None:
    projection = TurnDeliveryProjection(
        delivery_id=delivery_id,
        conversation_id="conv-wp7",
        sender=X_URI,
        recipient=A_URI,
        message="req",
    )
    daemon_hooks.observe_started(projection, started_at_ms=started_at_ms)
    daemon_hooks.observe_turn(
        HarnessDelivery(delivery_id, "conv-wp7", X_URI, A_URI, "req"),
        HarnessResult(delivery_id, A_URI, HarnessResultStatus.COMPLETED, output="ok"),
        started_at_ms=started_at_ms,
        ended_at_ms=ended_at_ms,
    )


def _wait_for(predicate: Any, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate()


def test_w1_turn_completion_writes_one_reminder_notice(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    app = _app(tmp_path, inbox)
    _request(inbox)
    assert app._reply_reminder_consumer_factory is not None

    bus = HookBus()
    daemon_hooks = _hook_service(tmp_path, bus)
    app._daemon_hooks = daemon_hooks
    _wire(app, daemon_hooks)
    try:
        _run_turn(daemon_hooks, delivery_id="r1", started_at_ms=100, ended_at_ms=200)
        _wait_for(lambda: len(inbox.system_notices(A_URI)) == 1)

        notices = inbox.system_notices(A_URI)
        assert len(notices) == 1
        notice = notices[0]
        assert notice.intent == "system"
        assert notice.summary == TEXT
        body = json.loads(notice.payload)
        assert body["reason"] == "reply-reminder"
        assert body["openRequests"] == ["r1"]

        # The notice is injectable through the consumer surface for A.
        listed = app.handle("message.pending.list", {"actor": A_NAME})
        rows = [row for row in listed["messages"] if row["messageId"] == notice.message_id]
        assert len(rows) == 1
        assert rows[0]["intent"] == "system"
    finally:
        assert bus.close(5.0)


def test_w2_notice_does_not_trigger_another_notice(tmp_path: Path) -> None:
    import hyprial.biz as biz
    from hyprial.daemon.impl.application.wiring.reply_reminder_hooks import (
        REPLY_REMINDER_EVENT_NAMES,
    )

    assert REPLY_REMINDER_EVENT_NAMES == biz.REPLY_REMINDER_EVENTS

    inbox = _inbox(tmp_path)
    app = _app(tmp_path, inbox)
    _request(inbox)

    bus = HookBus()
    daemon_hooks = _hook_service(tmp_path, bus)
    _wire(app, daemon_hooks)
    try:
        _run_turn(daemon_hooks, delivery_id="r1", started_at_ms=100, ended_at_ms=200)
        _wait_for(lambda: len(inbox.system_notices(A_URI)) == 1)
        notice_id = inbox.system_notices(A_URI)[0].message_id

        # A turn whose delivery is the reminder notice itself must not loop:
        # system notices live outside the inbox request table.
        _run_turn(
            daemon_hooks,
            delivery_id=notice_id,
            started_at_ms=300,
            ended_at_ms=400,
        )
        time.sleep(0.2)
        assert len(inbox.system_notices(A_URI)) == 1
    finally:
        assert bus.close(5.0)


def test_w3_without_a_factory_nothing_is_registered(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    app = _app(tmp_path, inbox)
    _request(inbox)

    bus = HookBus()
    daemon_hooks = _hook_service(tmp_path, bus)
    try:
        from hyprial.daemon.impl.application.wiring.reply_reminder_hooks import (
            wire_reply_reminder_hooks,
        )

        wire_reply_reminder_hooks(
            inbox=app._inbox,
            node_id=app.node_id,
            clock_ms=lambda: 1,
            consumer_factory=None,
            daemon_hooks=daemon_hooks,
        )
        assert "reply-reminder" not in {lane.name for lane in bus.projection()}

        _run_turn(daemon_hooks, delivery_id="r1", started_at_ms=100, ended_at_ms=200)
        time.sleep(0.2)
        assert inbox.system_notices(A_URI) == ()
    finally:
        assert bus.close(5.0)


def test_composition_wires_the_reminder_lane() -> None:
    """The startup composition must register the lane (source pin)."""

    transport = (
        Path(__file__).resolve().parents[4]
        / "src/hyprial/daemon/impl/application/wiring/transport.py"
    )
    tree = ast.parse(transport.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (
                isinstance(node.func, ast.Name)
                and node.func.id == "wire_reply_reminder_hooks"
            )
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "wire_reply_reminder_hooks"
            )
        )
    ]
    assert len(calls) == 1
    keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in calls[0].keywords}
    assert keywords["inbox"] == "inbox"
    assert keywords["node_id"] == "self.node_id"
    assert keywords["consumer_factory"] == "self._reply_reminder_consumer_factory"
    assert keywords["daemon_hooks"] == "daemon_hooks"
