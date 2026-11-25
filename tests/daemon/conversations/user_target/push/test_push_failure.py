"""UA2b item 2, kept by I7a: a failed owner push is never silently dropped, and never fails the send.

The message to ``user:<owner>`` stays queued and recorded either way.  The
push of an unbound sender's copy goes through this node's user agent cell
(I7a) and is best-effort (owner item A-7): an unknown outcome or a refusal
answers ok with ``pushed: false`` and ``pushCode``; nothing retries it, and
a caller's own same-key replay finds the queued message (no second row, no
second record) and pushes again under the same per-target key.  An
exception while reading a cell answers ``USER_DELIVERY_UNAVAILABLE`` with
its type logged.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import LarkApiError, LarkSdkError
from hyprial.daemon.impl.application.messaging.inbox_surface.send_targets import user as user_target
from hyprial.kernel import ipc_errors
from tests.daemon.acceptance.acceptance_support import send
from tests.daemon.conversations.bypass.bypass_support import recorded, settle, stored
from tests.daemon.conversations.user_target.user_target_support import boot_with_user_agent
from tests.support.golden.app import ADAPTER, ALPHA, OWNER_USER, golden_daemon as golden_daemon
from tests.support.identity.cell_push import bind_user_agent, fail_chats

DM = "oc_dm_chat"


def _queued_ids(app) -> list[str]:
    return [m.message_id for m in app._inbox.pending_messages(OWNER_USER)]


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (LarkSdkError("returned no message id"), ipc_errors.SUBMIT_OUTCOME_UNKNOWN),
        (LarkApiError("send message", 230002, "bot not in chat"), ipc_errors.USER_DELIVERY_FAILED),
    ],
    ids=["unknown", "refused"],
)
def test_a_failed_push_answers_ok_and_the_same_key_replay_pushes_again(
    golden_daemon, monkeypatch, failure, code
) -> None:
    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    app = booted.app
    bind_user_agent(app, ADAPTER)
    gateway = booted.daemon.gateway
    send_chat = gateway.send_chat
    fail_chats(gateway, monkeypatch, {DM: failure})

    first = send(app, ALPHA, OWNER_USER, "flaky-push")
    [delivery] = first["deliveries"]
    assert (delivery["accepted"], delivery["pushed"], delivery["pushCode"]) == (True, False, code)
    message_id = delivery["messageId"]
    assert _queued_ids(app) == [message_id]
    assert set(stored(app, message_id)) == {"inbox", "sender_copies"}

    monkeypatch.setattr(gateway, "send_chat", send_chat)
    again = send(app, ALPHA, OWNER_USER, "flaky-push")

    assert again["deliveries"][0]["messageId"] == message_id
    assert again["deliveries"][0]["pushed"] is True
    [(_c1, _t1, first_key), (_c2, _t2, replay_key)] = gateway.calls
    assert first_key == replay_key
    settle(booted)
    assert [line["messageId"] for line in recorded(app)].count(message_id) == 2


def test_an_exception_reading_the_user_agent_cell_answers_unavailable(
    golden_daemon, monkeypatch
) -> None:
    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    app = booted.app
    bind_user_agent(app, ADAPTER)

    def broken(_app):
        raise RuntimeError("cell index is gone")

    monkeypatch.setattr(user_target, "_user_agent_cell", broken)

    [delivery] = send(app, ALPHA, OWNER_USER, "broken-cell")["deliveries"]

    assert (delivery["accepted"], delivery["pushed"], delivery["pushCode"]) == (
        True, False, ipc_errors.USER_DELIVERY_UNAVAILABLE,
    )
    assert _queued_ids(app) == [delivery["messageId"]]
    assert booted.daemon.gateway.calls == []
    [record] = booted.logs.named("send.user_push")
    assert (record["pushSkipped"], record["errorType"]) == ("cell-unavailable", "RuntimeError")
