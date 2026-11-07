"""Notice ack keeps authentication and the production actor facade intact."""

from types import SimpleNamespace

import pytest

from tests.daemon.inbox.authority.full_writer.support import authority as authority
from tests.daemon.delivery.message.test_authenticated_message_sender import (
    ACTOR as IPC_ACTOR,
    NODE,
    OWNER,
    SESSION,
    isolated_app as isolated_app,
)
from hyprial.kernel import DaemonRequestError
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.application.messaging.inbox_surface.reply_reminders import (
    InboxReplyReminders,
)
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage

ACTOR = "agent:owner:node-b:worker"


def notice(mid, recipient):
    return InboxMessage(
        message_id=mid,
        conversation_id="notices",
        sender="system:hyprial:node-b",
        recipient=recipient,
        payload=b"{}",
        intent="system",
        lifecycle=DeliveryLifecycle.ONLINE_ONLY,
        created_at_ms=0,
    )


def application(facade, subject, via):
    app = object.__new__(DaemonApplication)
    app._inbox = facade
    app.node_id = "node-b"
    app.owner = "owner"
    app._authenticated_message_caller = lambda *a, **kw: SimpleNamespace(
        subject=subject, via=via
    )
    app._queue_agent_activity = lambda actor: None
    return app


@pytest.mark.parametrize(
    "subject,via,recipient,allowed",
    [
        ("user:owner", "operator", "node-b", True),
        (ACTOR, "session", ACTOR, True),
        (ACTOR, "session", "node-b", False),
        ("node-b", "unfenced", "node-b", False),
    ],
)
def test_ipc_notice_ack_is_recipient_scoped_and_idempotent(
    authority, subject, via, recipient, allowed
):
    facade, *_ = authority
    facade.receive_system_notice(notice("target", recipient))
    facade.receive_system_notice(notice("other", recipient))
    app = application(facade, subject, via)
    params = {"actor": subject, "messageId": "target"}
    result = app._ipc_message_ack("message.ack", params)
    assert result["acknowledged"] is allowed
    assert len(facade.system_notices(recipient)) == (1 if allowed else 2)
    again = app._ipc_message_ack("message.ack", params)
    assert again["acknowledged"] is allowed
    if allowed:
        assert again["code"] == "MESSAGE_ALREADY_SETTLED"


@pytest.mark.parametrize("enforced", [True, False])
def test_authenticated_ipc_allows_only_operator_to_ack_node_notice(
    isolated_app, enforced
):
    app = isolated_app
    app._sender_auth_enforced = enforced
    app._inbox.receive_system_notice(notice("node-notice", NODE))
    for params in (
        {"from": NODE},
        {"from": IPC_ACTOR, "sessionRef": SESSION},
    ):
        try:
            result = app.handle(
                "message.ack",
                {**params, "messageId": "node-notice"},
                _trusted_message_origin="ipc",
            )
        except DaemonRequestError:
            result = {"acknowledged": False}
        assert not result["acknowledged"]
        assert len(app._inbox.system_notices(NODE)) == 1
    for attempt in range(2):
        result = app.handle(
            "message.ack",
            {"from": f"user:{OWNER}", "messageId": "node-notice"},
            _trusted_message_origin="ipc",
        )
        assert result["acknowledged"]
        if attempt:
            assert result["code"] == "MESSAGE_ALREADY_SETTLED"
    assert app._inbox.system_notices(NODE) == ()


def test_quarantined_notice_count_remains_visible_after_ackable_pending_is_zero(
    authority,
):
    facade, *_ = authority
    for mid, recipient in (
        ("actor", ACTOR),
        ("node", "node-b"),
        ("old", "old-node"),
        ("unknown", "unknown"),
    ):
        facade.receive_system_notice(notice(mid, recipient))
    assert facade.system_notice_counts("node-b") == {"pending": 2, "quarantined": 2}
    assert facade.ack(ACTOR, "actor").acknowledged
    assert facade.ack("node-b", "node").acknowledged
    assert facade.system_notice_counts("node-b") == {"pending": 0, "quarantined": 2}
    assert len(facade.system_notices("old-node")) == 1
    assert len(facade.system_notices("unknown")) == 1


def test_daemon_status_exposes_notice_counts_read_only(isolated_app):
    app = isolated_app
    app._inbox.receive_system_notice(notice("quarantined", "old-node"))
    app._inbox.receive_system_notice(notice("operator", app.node_id))
    result = app.handle("ps", {})
    assert result["daemon"]["systemNotices"] == {"pending": 1, "quarantined": 1}
    assert len(app._inbox.system_notices("old-node")) == 1
    assert len(app._inbox.system_notices(app.node_id)) == 1


def test_reply_reminder_replay_crosses_real_facade(authority):
    facade, *_ = authority
    port = InboxReplyReminders(facade, node_id="node-b", clock_ms=lambda: 500)
    assert port.post_reply_reminder(ACTOR, ("request",), "reply required")
    [item] = facade.system_notices(ACTOR)
    assert facade.ack(ACTOR, item.message_id).acknowledged
    assert port.post_reply_reminder(ACTOR, ("request",), "reply required")
    assert len(facade.system_notices(ACTOR)) == 1
    assert facade.ack(ACTOR, item.message_id).acknowledged
    assert facade.receive_system_notice(item)
    assert facade.system_notices(ACTOR) == ()  # ordinary producers cannot replay
