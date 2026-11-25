"""I7a: another owner's message to this node's owner is pushed through the user agent cell.

Decision notes (contract i7-0 v2 section 4):

- The receiving node posts to its own user agent cell's targets, attributed
  to the remote sender; the receiver ledger deduplicates by the request's
  idempotency key, and the per-target keys derive from that key.
- Fold of the per-target outcomes into the receiver's result: no target is
  the existing ``TARGET_UNRESOLVED`` refusal (not recorded); all not-sent is
  a refusal with the first code (not recorded, a retry attempts again); any
  sent is accepted (recorded); none sent with an unknown outcome is
  ``USER_DELIVERY_FAILED``, recorded, so a retry never posts again.
- A retry that arrives while the first delivery is still posting joins it
  (one active delivery per key), so a sender whose receipt wait ran out
  gets the recorded result and the chat gets one post.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import LarkApiError, LarkSdkError
from hyprial.daemon.impl.adapters.user_delivery.actor import UserDeliveryActor
from hyprial.daemon.impl.adapters.user_delivery.receiver import (
    ReceiverUserDelivery,
    UserDeliveryLedger,
    UserDeliveryRequest,
)
from hyprial.kernel import ipc_errors
from tests.daemon.conversations.user_target.user_target_support import boot_with_user_agent
from tests.support.golden.app import ADAPTER, golden_daemon as golden_daemon
from tests.support.identity.cell_push import (
    edit_gateway,
    fail_chats,
    install_cell,
    user_agent_cell,
)

DM, GROUP = "oc_dm_chat", "oc_group_chat"
SENDER = "agent:bob:bob-mac:helper"


def _receiver(app: Any, tmp_path: Path, owns: Any = None) -> ReceiverUserDelivery:
    return ReceiverUserDelivery(
        node_id=app.node_id,
        owns=owns or app._accepts_owner_copy,
        ledger=UserDeliveryLedger(tmp_path / "user-deliveries.json"),
        owner_push=app._push_owner_copy,
    )


def _request(app: Any, key: str = "xo-1") -> UserDeliveryRequest:
    return UserDeliveryRequest(
        message_id=f"m-{key}", idempotency_key=key, owner=app.owner,
        sender=SENDER, message="hello from bob", conversation_id="conv-xo",
    )


def _booted(golden_daemon: Any, monkeypatch: pytest.MonkeyPatch, **cell: Any) -> Any:
    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    edit_gateway(booted.app, defaultRoute="dm")
    members = cell.pop("members", {"im": ADAPTER})
    document = user_agent_cell(booted.app, members, **cell)
    install_cell(booted.app, document)
    booted.cell = document
    return booted


def test_a_remote_message_is_posted_once_through_the_user_agent_cell(
    golden_daemon, monkeypatch, tmp_path
) -> None:
    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app
    receiver = _receiver(app, tmp_path)

    first = receiver.handle(_request(app))
    again = receiver.handle(_request(app))

    assert (first.accepted, first.native_message_id is not None) == (True, True)
    assert (again.accepted, again.duplicate) == (True, True)
    assert booted.daemon.gateway.calls == [(
        DM, f"转述自 {SENDER}：\n\nhello from bob",
        f"xo-1:push:{booted.cell.cell_id}:im:",
    )]


def test_no_user_agent_cell_target_is_refused_and_not_recorded(
    golden_daemon, monkeypatch, tmp_path
) -> None:
    from hyprial.daemon.impl.adapters.user_delivery.receiver import NO_USER_AGENT_TARGET_MESSAGE

    booted = _booted(golden_daemon, monkeypatch, members={})
    app = booted.app
    receiver = _receiver(app, tmp_path)

    result = receiver.handle(_request(app))

    assert (result.accepted, result.code) == (False, ipc_errors.TARGET_UNRESOLVED)
    assert result.message == NO_USER_AGENT_TARGET_MESSAGE
    assert "user proxy" not in result.message and "user-proxy" not in result.message
    assert receiver.ledger.get("xo-1") is None
    assert booted.daemon.gateway.calls == []


def test_all_not_sent_is_not_recorded_and_a_retry_attempts_again(
    golden_daemon, monkeypatch, tmp_path
) -> None:
    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app
    receiver = _receiver(app, tmp_path)
    fail_chats(booted.daemon.gateway, monkeypatch, {DM: LarkApiError("send message", 230002, "x")})

    first = receiver.handle(_request(app))
    second = receiver.handle(_request(app))

    assert (first.accepted, first.code, first.definitely_not_sent) == (
        False, ipc_errors.USER_DELIVERY_FAILED, True,
    )
    assert receiver.ledger.get("xo-1") is None
    assert not second.duplicate
    assert len(booted.daemon.gateway.calls) == 2


def test_only_an_unknown_outcome_is_recorded_as_failed_and_never_posted_again(
    golden_daemon, monkeypatch, tmp_path
) -> None:
    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app
    receiver = _receiver(app, tmp_path)
    fail_chats(booted.daemon.gateway, monkeypatch, {DM: LarkSdkError("returned no message id")})

    first = receiver.handle(_request(app))
    second = receiver.handle(_request(app))

    assert (first.accepted, first.code, first.definitely_not_sent) == (
        False, ipc_errors.USER_DELIVERY_FAILED, False,
    )
    assert "not attempted again" in str(first.message)
    assert second.duplicate is True
    assert len(booted.daemon.gateway.calls) == 1


def test_one_sent_target_of_two_is_accepted_and_recorded(golden_daemon, monkeypatch, tmp_path) -> None:
    booted = _booted(golden_daemon, monkeypatch, connections=(
        {"from": "im", "on": "user", "to": "im", "route": "group"},
        {"from": "im", "on": "user", "to": "im", "route": "dm"},
    ))
    app = booted.app
    receiver = _receiver(app, tmp_path)
    fail_chats(booted.daemon.gateway, monkeypatch, {GROUP: LarkSdkError("returned no message id")})

    result = receiver.handle(_request(app))

    assert result.accepted is True
    assert receiver.ledger.get("xo-1") is not None
    assert [call[0] for call in booted.daemon.gateway.calls] == [GROUP, DM]


def test_an_unreadable_user_agent_cell_is_unavailable_and_not_recorded(
    golden_daemon, monkeypatch, tmp_path
) -> None:
    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app
    receiver = _receiver(app, tmp_path, owns=lambda owner: True)
    runtime = app._cell_runtime
    lookup = runtime.lookup
    monkeypatch.setattr(
        runtime, "lookup",
        lambda cell_id: None if cell_id == booted.cell.cell_id else lookup(cell_id),
    )

    result = receiver.handle(_request(app))

    assert (result.accepted, result.code, result.definitely_not_sent) == (
        False, ipc_errors.USER_DELIVERY_UNAVAILABLE, True,
    )
    assert receiver.ledger.get("xo-1") is None
    assert booted.daemon.gateway.calls == []


def test_without_a_user_agent_on_the_receiving_node_the_push_answers_nothing(
    golden_daemon, monkeypatch
) -> None:
    from tests.daemon.conversations.bypass.bypass_support import boot

    app = boot(golden_daemon, monkeypatch).app

    assert app._push_owner_copy(_request(app)) is None


def _wait_receipt(actor: UserDeliveryActor, attempt: str, timeout: float = 5.0) -> Any:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        receipt = actor.receipt(attempt)
        if receipt is not None:
            return receipt
        time.sleep(0.005)
    raise AssertionError(f"no receipt for {attempt}")


def test_a_retry_during_a_slow_post_joins_it_and_the_chat_gets_one_post(
    golden_daemon, monkeypatch, tmp_path, actor_host
) -> None:
    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app
    receiver = _receiver(app, tmp_path)
    gateway = booted.daemon.gateway
    entered, release = threading.Event(), threading.Event()
    send_chat = gateway.send_chat

    def slow(chat_id: str, text: str, *, idempotency_key: str) -> str:
        entered.set()
        assert release.wait(5)
        return send_chat(chat_id, text, idempotency_key=idempotency_key)

    monkeypatch.setattr(gateway, "send_chat", slow)
    actor = UserDeliveryActor(receiver, actor_host=actor_host)
    request = replace(_request(app), attempt_id="first")
    try:
        actor.admit(request)
        assert entered.wait(2)
        assert actor.receipt("first") is None
        actor.admit(replace(request, attempt_id="retry"))
        release.set()
        first = _wait_receipt(actor, "first")
        retry = _wait_receipt(actor, "retry")
    finally:
        release.set()
        assert actor.close(5)

    assert first.accepted and retry.accepted
    assert len(gateway.calls) == 1


def test_an_unexpected_error_after_the_post_is_recorded_and_never_posted_again(
    golden_daemon, monkeypatch, tmp_path
) -> None:
    from hyprial.daemon.impl.application.messaging.inbox_surface.send_targets import user

    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app
    receiver = _receiver(app, tmp_path)

    def broken_fold(outcomes: Any) -> Any:
        raise RuntimeError("fold")

    monkeypatch.setattr(user, "fold_outcomes", broken_fold)

    first = receiver.handle(_request(app))
    again = receiver.handle(_request(app))

    assert (first.accepted, first.code) == (False, ipc_errors.USER_DELIVERY_FAILED)
    assert first.definitely_not_sent is False
    assert receiver.ledger.get("xo-1") is not None
    assert (again.accepted, again.code) == (False, ipc_errors.USER_DELIVERY_FAILED)
    assert len(booted.daemon.gateway.calls) == 1
