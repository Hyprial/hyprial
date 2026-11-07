"""Consumer-facing progress.list IPC and tool plumbing (route C, T8)."""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import pytest

from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.tracking.progress import encode_progress_event, ProgressEvent
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

ACTOR = "agent:op:node-1:requester"
WORKER = "agent:op:node-1:worker"


def _message(seq: int, *, delivery_id: str = "delivery-1") -> InboxMessage:
    event = ProgressEvent(
        delivery_id=delivery_id,
        conversation_id="conversation-1",
        actor=WORKER,
        harness="pi",
        phase="tool-call",
        seq=seq,
        emitted_at_ms=1_000 + seq,
        summary=f"step {seq}",
        tool_call_id=f"call-{seq}",
        tool_name="read",
    )
    return InboxMessage(
        message_id=f"progress:{delivery_id}:{seq}",
        conversation_id="conversation-1",
        sender=WORKER,
        recipient=ACTOR,
        payload=encode_progress_event(event),
        intent="progress",
        lifecycle=DeliveryLifecycle.ONLINE_ONLY,
        created_at_ms=1_000 + seq,
    )


class _Inbox:
    def __init__(self) -> None:
        self.messages = (
            _message(0),
            _message(1),
            _message(0, delivery_id="delivery-2"),
        )
        self.calls: list[tuple[str, str | None]] = []

    def list_progress_events(
        self, recipient: str, *, delivery_id: str | None = None
    ) -> tuple[InboxMessage, ...]:
        self.calls.append((recipient, delivery_id))
        return tuple(
            message
            for message in self.messages
            if message.recipient == recipient
            and (delivery_id is None or message.message_id.startswith(f"progress:{delivery_id}:"))
        )


class _Harnesses:
    def status(self) -> tuple[()]:
        return ()


class _Presence:
    def actor_online(self, _actor: str) -> bool:
        return False

    def online_actors(self) -> tuple[()]:
        return ()

    def online_mailboxes(self) -> tuple[()]:
        return ()


@pytest.fixture
def app_context(tmp_path):  # type: ignore[no-untyped-def]
    inbox = _Inbox()
    app = DaemonApplication(
        state_dir=tmp_path,
        socket_path=tmp_path / "daemon.sock",
        node_id="node-1",
        owner="op",
    **_deep_module_daemon_dependencies())
    app._inbox = inbox  # type: ignore[assignment]
    app._harnesses = _Harnesses()  # type: ignore[assignment]
    app._presence = _Presence()  # type: ignore[assignment]
    try:
        yield app, inbox
    finally:
        app.agents.close()


def test_progress_list_returns_structured_events_without_draining(app_context) -> None:  # type: ignore[no-untyped-def]
    app, _inbox = app_context

    first = app.handle("progress.list", {"actor": ACTOR})
    second = app.handle("progress.list", {"actor": ACTOR})

    assert first["ok"] is True
    assert [event["seq"] for event in first["events"]] == [0, 1, 0]
    event = first["events"][1]
    assert event["schema"] == "hyprial-progress-event/v1"
    assert event["progressEvent"] is True
    assert event["messageId"] == "progress:delivery-1:1"
    assert event["deliveryId"] == "delivery-1"
    assert event["from"] == WORKER
    assert event["recipient"] == ACTOR
    assert event["toolCallId"] == "call-1"
    assert event["toolName"] == "read"
    # Non-destructive read: the second IPC read sees the same rows.
    assert [event["messageId"] for event in second["events"]] == [
        event["messageId"] for event in first["events"]
    ]


def test_progress_list_filters_delivery_and_since_seq(app_context) -> None:  # type: ignore[no-untyped-def]
    app, _inbox = app_context

    result = app.handle(
        "progress.list",
        {"actor": ACTOR, "deliveryId": "delivery-1", "sinceSeq": 0},
    )

    assert [event["seq"] for event in result["events"]] == [1]
    assert result["events"][0]["deliveryId"] == "delivery-1"


@pytest.mark.parametrize("value", [-1, True, "0"])
def test_progress_list_rejects_an_invalid_since_seq(app_context, value: object) -> None:  # type: ignore[no-untyped-def]
    app, _inbox = app_context

    with pytest.raises(DaemonRequestError, match="sinceSeq"):
        app.handle("progress.list", {"actor": ACTOR, "sinceSeq": value})
