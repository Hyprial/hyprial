"""UA2b item 3: ``user:<owner>`` is queued here only while a recorder will ack it.

Without a composed, open conversation recorder or with the cell runtime gone,
nothing would ever acknowledge the queued row and its sender would be told
"expired unread" after the hold TTL; such a send keeps the push-only path,
which for an unbound sender is the push through this node's user agent
cell (I7a).  With the cell runtime gone an agent sender's binding is
unknown, so its push-only send is refused (I3, contract 3.2 row 6).
A message recorded only as a placeholder (S2: body kept in the queue) is
acknowledged once its owner push went out, because the owner already has
the text; a placeholder that was never pushed (a reply) stays queued.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.inbox import DEFAULT_HOLD_TTL_MS
from tests.daemon.acceptance.acceptance_support import HOUR_MS, send
from tests.daemon.conversations.bypass.bypass_support import settle, stored
from tests.daemon.conversations.user_target.user_target_support import boot_with_user_agent
from tests.support.golden.app import ADAPTER, ALPHA, OWNER_USER, golden_daemon as golden_daemon
from tests.support.identity.cell_push import bind_user_agent, fail_chats


def _assert_push_only(daemon, answer) -> None:
    [delivery] = answer["deliveries"]
    assert (delivery["queued"], delivery["via"]) == (False, "user-agent")
    assert "pushed" not in delivery and "receiverAdapter" not in delivery
    assert stored(daemon.app, delivery["messageId"]) == {}
    assert [call[0] for call in daemon.gateway.calls] == ["oc_dm_chat"]
    assert daemon.user_delivery.requests == []


def test_without_a_composed_recorder_the_send_keeps_the_push_only_path(golden_daemon) -> None:
    daemon = golden_daemon(user_agent=True, links=False)
    assert getattr(daemon.app, "_conversation_recorder", None) is None
    bind_user_agent(daemon.app, ADAPTER)

    _assert_push_only(daemon, send(daemon.app, ALPHA, OWNER_USER, "no-recorder"))
    assert daemon.app.handle("targets", {"kind": "user"})["targets"] == []


def test_a_closed_recorder_keeps_the_push_only_path(golden_daemon, monkeypatch) -> None:
    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    bind_user_agent(booted.app, ADAPTER)
    booted.app._conversation_recorder_closed = True

    _assert_push_only(booted.daemon, send(booted.app, ALPHA, OWNER_USER, "gone-recorder"))


def test_a_closed_cell_runtime_refuses_an_agent_push_only_send(golden_daemon, monkeypatch) -> None:
    """I3 (contract 3.2 row 6, 3.4): an unknown binding must not choose a chat.

    The sender's cell cannot be read, so its copy is not pushed, and the
    push-only send is refused.
    """

    from hyprial.kernel import DaemonRequestError, ipc_errors

    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    booted.app._cell_runtime_closed = True

    with pytest.raises(DaemonRequestError) as refused:
        send(booted.app, ALPHA, OWNER_USER, "gone-cells")

    assert refused.value.code == ipc_errors.USER_DELIVERY_UNAVAILABLE


def test_a_pushed_placeholder_is_acknowledged_and_never_expires_unread(
    golden_daemon, monkeypatch
) -> None:
    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    app = booted.app
    bind_user_agent(app, ADAPTER)
    answer = send(app, ALPHA, OWNER_USER, "big-note")
    message_id = answer["messageId"]
    assert answer["deliveries"][0]["pushed"] is True
    monkeypatch.setattr(
        booted.recorder, "_redact",
        lambda draft: None if draft.message.message_id == message_id else draft,
    )

    settle(booted)

    assert app._inbox.pending_messages(OWNER_USER) == ()
    booted.daemon.clock.advance(DEFAULT_HOLD_TTL_MS + HOUR_MS)
    pruned = app._inbox.prune_inbox(now_ms=booted.daemon.clock.ms())
    assert message_id not in {item.message_id for item in pruned}


def test_a_placeholder_recorded_before_its_push_went_out_is_acked_by_the_retry(
    golden_daemon, monkeypatch
) -> None:
    """The first push has an unknown outcome (answered, A-7), the placeholder
    is recorded, and the caller's same-key replay pushes again: the row is
    acknowledged then, not left to the TTL."""

    from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import LarkSdkError
    from hyprial.kernel import ipc_errors

    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    app = booted.app
    bind_user_agent(app, ADAPTER)
    gateway = booted.daemon.gateway
    send_chat = gateway.send_chat
    fail_chats(gateway, monkeypatch, {"oc_dm_chat": LarkSdkError("returned no message id")})

    first = send(app, ALPHA, OWNER_USER, "late-push")
    message_id = first["messageId"]
    assert (first["deliveries"][0]["pushed"], first["deliveries"][0]["pushCode"]) == (
        False, ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
    )
    monkeypatch.setattr(gateway, "send_chat", send_chat)
    monkeypatch.setattr(
        booted.recorder, "_redact",
        lambda draft: None if draft.message.message_id == message_id else draft,
    )
    settle(booted)
    assert [m.message_id for m in app._inbox.pending_messages(OWNER_USER)] == [message_id]

    again = send(app, ALPHA, OWNER_USER, "late-push")
    settle(booted)

    assert again["deliveries"][0]["pushed"] is True
    assert app._inbox.pending_messages(OWNER_USER) == ()
