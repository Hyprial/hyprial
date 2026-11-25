from __future__ import annotations

import json
from pathlib import Path
import pytest
from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.adapters.user_delivery.receiver import UserDeliveryResult

from ..support import (
    FakeUserDelivery,
    _PendingFakeInbox,
    _application,
    _close_test_applications as _close_test_applications,
)


def test_daemon_rejects_channel_route_target_instead_of_silently_queuing(
    tmp_path: Path,
) -> None:
    delivery = FakeUserDelivery(
        UserDeliveryResult(message_id="msg-user-1", accepted=True)
    )
    app, inbox = _application(tmp_path, delivery)

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "message.send",
            {
                "from": app.agents.get("worker").uri,
                "to": ["channel:alice:mac:feishu-main:hyprial-all-member"],
                "summary": "kanban broadcast",
                "message": "kanban broadcast",
                "idempotencyKey": "operation-1",
            },
        )

    assert failure.value.code == "UNSUPPORTED_TARGET"
    assert "user:<owner>" in str(failure.value)
    # the message must never reach the agent inbox, where it would queue
    # forever behind ok:true
    assert inbox.submitted == []
    assert delivery.requests == []


def test_daemon_rejects_four_part_channel_target_as_well(tmp_path: Path) -> None:
    delivery = FakeUserDelivery(
        UserDeliveryResult(message_id="msg-user-1", accepted=True)
    )
    app, inbox = _application(tmp_path, delivery)

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "message.send",
            {
                "from": app.agents.get("worker").uri,
                "to": ["channel:alice:mac:feishu-main"],
                "summary": "hello",
                "message": "hello",
            },
        )

    assert failure.value.code == "UNSUPPORTED_TARGET"
    assert inbox.submitted == []


def test_daemon_rejects_four_part_adapter_target_the_same_as_channel(
    tmp_path: Path,
) -> None:
    """classify_target_identity has no per-scheme special case for
    channel:/adapter: -- both fall through to the generic unknown verdict,
    so the send gate rejects the current adapter: spelling identically to
    the legacy channel: spelling with zero code change required here; this
    test pins that fact so it stays true across future refactors."""

    delivery = FakeUserDelivery(
        UserDeliveryResult(message_id="msg-user-1", accepted=True)
    )
    app, inbox = _application(tmp_path, delivery)

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "message.send",
            {
                "from": app.agents.get("worker").uri,
                "to": ["adapter:alice:mac:feishu-main"],
                "summary": "hello",
                "message": "hello",
            },
        )

    assert failure.value.code == "UNSUPPORTED_TARGET"
    assert inbox.submitted == []


def test_pi_harness_reply_to_lark_inbound_enters_the_reply_bridge(
    tmp_path: Path,
) -> None:
    delivery = FakeUserDelivery(
        UserDeliveryResult(message_id="msg-user-1", accepted=True)
    )
    app, _inbox = _application(tmp_path, delivery)
    original = InboxMessage(
        message_id="msg-lark-inbound",
        conversation_id="lark:oc-group:oc-group",
        sender="adapter:lark:mac-studio-feishu",
        recipient=app.agents.get("worker").uri,
        payload=b'{"message":"ping"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    inbox = _PendingFakeInbox(original)
    app._inbox = inbox

    result = app.handle(
        "message.reply",
        {
            "from": app.agents.get("worker").uri,
            "messageId": "msg-lark-inbound",
            "summary": "pong",
            "message": "pong",
        },
    )

    assert result["ok"] is True
    assert result["replied"] is True
    assert result["acknowledged"] is True
    assert len(inbox.submitted) == 1
    reply = inbox.submitted[0]
    assert reply.recipient == "adapter:lark:mac-studio-feishu"
    assert reply.intent == "reply"
    assert reply.idempotency_key == "reply:msg-lark-inbound"
    assert json.loads(reply.payload) == {
        "message": "pong",
        "origin": {"via": "daemon"},
        "replyTo": "msg-lark-inbound",
    }


def test_harness_reply_fails_loudly_for_unconfigured_lark_adapter(
    tmp_path: Path,
) -> None:
    delivery = FakeUserDelivery(
        UserDeliveryResult(message_id="msg-user-1", accepted=True)
    )
    app, _inbox = _application(tmp_path, delivery)
    original = InboxMessage(
        message_id="msg-lark-inbound",
        conversation_id="lark:oc-group:oc-group",
        sender="adapter:lark:missing-adapter",
        recipient=app.agents.get("worker").uri,
        payload=b'{"message":"ping"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    inbox = _PendingFakeInbox(original)
    app._inbox = inbox

    class Adapters:
        gateway_names: tuple[str, ...] = ("configured-adapter",)

    app._adapters = Adapters()  # type: ignore[assignment]

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "message.reply",
            {
                "from": app.agents.get("worker").uri,
                "messageId": "msg-lark-inbound",
                "summary": "pong",
                "message": "pong",
            },
        )

    assert failure.value.code == "MESSAGE_REPLY_UNAVAILABLE"
    assert inbox.submitted == []


def test_harness_reply_rejects_bare_four_part_adapter_actor_without_reply_bridge(
    tmp_path: Path,
) -> None:
    """The four-segment adapter:<owner>:<machine>:<name> form (not the
    three-segment adapter:lark:<adapter> reply-bridge form) has no reply
    bridge and must be rejected by the ADAPTER_URI_PREFIX "no reply bridge"
    gate in message.reply."""

    delivery = FakeUserDelivery(
        UserDeliveryResult(message_id="msg-user-1", accepted=True)
    )
    app, _inbox = _application(tmp_path, delivery)
    original = InboxMessage(
        message_id="msg-bare-adapter",
        conversation_id="lark:oc-group:oc-group",
        sender="adapter:alice:mac:feishu-main",
        recipient=app.agents.get("worker").uri,
        payload=b'{"message":"ping"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    inbox = _PendingFakeInbox(original)
    app._inbox = inbox

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "message.reply",
            {
                "from": app.agents.get("worker").uri,
                "messageId": "msg-bare-adapter",
                "summary": "pong",
                "message": "pong",
            },
        )

    assert failure.value.code == "MESSAGE_REPLY_UNAVAILABLE"
    assert inbox.submitted == []


@pytest.mark.parametrize(
    "sender",
    [
        "channel:lark:golden-lark",
        "channel:telegram:inst:chat1",
        "bogus:x:y",
    ],
)
def test_harness_reply_refuses_sender_with_no_deliverable_reply_path(
    tmp_path: Path, sender: str
) -> None:
    """A pending row whose sender has no reply bridge and no deliverable
    address form (the retired channel: spelling, or any unknown scheme) is
    refused with MESSAGE_REPLY_UNAVAILABLE and stays pending, instead of
    being acked into an outbox row nothing can deliver."""

    delivery = FakeUserDelivery(
        UserDeliveryResult(message_id="msg-user-1", accepted=True)
    )
    app, _inbox = _application(tmp_path, delivery)
    original = InboxMessage(
        message_id="msg-retired-sender",
        conversation_id="lark:oc-group:oc-group",
        sender=sender,
        recipient=app.agents.get("worker").uri,
        payload=b'{"message":"ping"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    inbox = _PendingFakeInbox(original)
    app._inbox = inbox

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "message.reply",
            {
                "from": app.agents.get("worker").uri,
                "messageId": "msg-retired-sender",
                "summary": "pong",
                "message": "pong",
            },
        )

    assert failure.value.code == "MESSAGE_REPLY_UNAVAILABLE"
    assert inbox.submitted == []
    assert inbox.pending_messages(app.agents.get("worker").uri) == (original,)
