"""message.send and ingress after the send-link gate was removed (T1b).

Decision notes (CM-PLAN.md「T1b 摘掉调用」; owner rulings 2026-11-19 and
2026-11-20):

- A first send between two managed agents delivers immediately even with
  enforcement explicitly switched on (``bypass=False``): nothing
  asks a gate any more, so there is no mint/hold window and no holdReason.
- The two reply-association checks survive without any link service: a
  ``replyTo`` naming no open request is refused with
  ``MESSAGE_REPLY_UNAVAILABLE``, one naming another sender's request is
  refused with ``INVALID_ARGUMENT``, and a row that is itself a reply does
  not count as an open request.
- A send claiming a non-local agent identity is still refused before any
  delivery (Δ3: sender resolution owns that refusal, not the gate).
- An inbound message from a remote non-member sender is received (Δ1, owner
  ruling 2026-11-20: the tailnet connection itself is the permission).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from hyprial.kernel import DaemonRequestError, ipc_errors
from tests.daemon.acceptance.acceptance_support import session
from tests.support.golden.app import (
    ALPHA,
    BETA,
    GAMMA,
    golden_daemon as golden_daemon,
)

STRANGER = "agent:other-owner:node-9:remote"
FOREIGN_NODE = "agent:op:node-9:spy"


def _send(
    app: Any,
    sender: str,
    to: str,
    key: str,
    *,
    reply_to: str | None = None,
    via_session: bool = True,
) -> dict[str, Any]:
    params: dict[str, Any] = session(sender) if via_session else {"from": sender}
    request: dict[str, Any] = {
        **params,
        "to": [to],
        "summary": f"summary {key}",
        "message": f"body {key}",
        "conversationId": f"conv-{key}",
        "idempotencyKey": f"key-{key}",
    }
    if reply_to is not None:
        request["replyTo"] = reply_to
    return app.handle("message.send", request)


def _pending_ids(app: Any, actor: str) -> set[str]:
    return {row.message_id for row in app._inbox.pending_messages(actor)}


def _message_id(result: dict[str, Any]) -> str:
    assert result["ok"] is True, result
    return str(result["messageId"])


def _deliver(
    app: Any,
    message_id: str,
    *,
    sender: str,
    recipient: str,
    reply_to: str | None = None,
) -> None:
    body: dict[str, Any] = {"message": f"body {message_id}"}
    if reply_to is not None:
        body["replyTo"] = reply_to
    result = app._inbox.receive(
        InboxMessage(
            message_id=message_id,
            conversation_id=f"conv-{message_id}",
            sender=sender,
            recipient=recipient,
            payload=json.dumps(body).encode(),
            intent="reply" if reply_to else "request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
            summary=f"summary {message_id}",
        )
    )
    assert result.accepted is True, result


def test_first_send_between_managed_agents_delivers_with_enforcement_on(
    golden_daemon,
) -> None:
    daemon = golden_daemon(bypass=False)
    app = daemon.app

    result = _send(app, ALPHA, BETA, "enforced-first")
    [delivery] = result["deliveries"]
    assert delivery["accepted"] is True, result
    assert delivery.get("queued") is not True, result
    assert "holdReason" not in delivery and "holdReason" not in result, result
    assert _pending_ids(app, BETA) == {_message_id(result)}


def test_reply_to_a_missing_request_is_refused_without_a_link_service(
    golden_daemon,
) -> None:
    daemon = golden_daemon(links=False)
    app = daemon.app

    with pytest.raises(DaemonRequestError) as refused:
        _send(app, ALPHA, BETA, "dangling", reply_to="no-such-request")

    assert refused.value.code == ipc_errors.MESSAGE_REPLY_UNAVAILABLE
    assert "no open request" in str(refused.value)


def test_reply_to_another_senders_request_is_invalid_argument(
    golden_daemon,
) -> None:
    daemon = golden_daemon(links=False)
    app = daemon.app
    _deliver(app, "wrong-target-origin", sender=BETA, recipient=ALPHA)

    with pytest.raises(DaemonRequestError) as refused:
        _send(app, ALPHA, GAMMA, "wrong-target", reply_to="wrong-target-origin")

    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert "came from" in str(refused.value)


def test_a_row_that_is_itself_a_reply_is_not_an_open_request(
    golden_daemon,
) -> None:
    daemon = golden_daemon(links=False)
    app = daemon.app
    _deliver(app, "q-and-a", sender=ALPHA, recipient=BETA)
    _deliver(
        app, "q-and-a-answer", sender=BETA, recipient=ALPHA, reply_to="q-and-a"
    )

    with pytest.raises(DaemonRequestError) as refused:
        _send(app, ALPHA, BETA, "reply-to-reply", reply_to="q-and-a-answer")

    assert refused.value.code == ipc_errors.MESSAGE_REPLY_UNAVAILABLE


def test_a_valid_reply_association_delivers_without_a_link_service(
    golden_daemon,
) -> None:
    daemon = golden_daemon(links=False)
    app = daemon.app
    _deliver(app, "valid-origin", sender=ALPHA, recipient=BETA)

    result = _send(app, BETA, ALPHA, "valid-reply", reply_to="valid-origin")

    [delivery] = result["deliveries"]
    assert delivery["accepted"] is True, result
    assert "holdReason" not in delivery and "holdReason" not in result, result
    assert _message_id(result) in _pending_ids(app, ALPHA)


def test_a_send_claiming_a_non_local_agent_is_refused(golden_daemon) -> None:
    daemon = golden_daemon(links=False)
    app = daemon.app

    for intruder in (STRANGER, FOREIGN_NODE):
        with pytest.raises(DaemonRequestError) as refused:
            _send(app, intruder, BETA, "intruder", via_session=False)
        assert refused.value.code == ipc_errors.SENDER_UNRESOLVED, intruder
    assert _pending_ids(app, BETA) == set()


def test_an_inbound_message_from_a_remote_non_member_is_received(
    golden_daemon,
) -> None:
    daemon = golden_daemon(bypass=False)
    app = daemon.app

    result = app._inbox.receive(
        InboxMessage(
            message_id="m-stranger",
            conversation_id="conv-stranger",
            sender=STRANGER,
            recipient=BETA,
            payload=b'{"message": "hello from outside"}',
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
            summary="stranger says hello",
        )
    )

    assert result.accepted is True, result
    assert "m-stranger" in _pending_ids(app, BETA)
