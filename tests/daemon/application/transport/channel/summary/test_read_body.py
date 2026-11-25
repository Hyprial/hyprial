"""T12/T13: ``message.read_body`` consumes one row, under the session fence.

read_body stamps ``fetched``, retires the sender's receipt and publishes a
fetch receipt, so it needs the same session fence as an explicit
``message.pending.list`` fetch (review blocking R4) and must not commit
anything for a row it refuses.  T1b removed the link-gate admission: every
row the consumer's keys hold is readable; the tests that pinned held,
denied and fail-closed reads were deleted with it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.kernel import AdmissionResult, DaemonRequestError, ipc_errors
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage

from ..support import (
    FakeInbox,
    _close_application_domains as _close_application_domains,
    application,
    official_address,
    durable_application,
    pending_message,
    register,
    register_actor,
)

READER = "agent:op:node-1:reader"
DRIVER = "agent:op:node-1:driver"


def _seed(
    app,  # type: ignore[no-untyped-def]
    *,
    message_id: str,
    recipient: str | None = None,
    sender: str = DRIVER,
    summary: str | None = "S",
    body: str = "B",
    intent: str = "request",
    reply_to: str | None = None,
    expires_at_ms: int | None = None,
) -> None:
    assert app._inbox.receive(
        InboxMessage(
            message_id=message_id,
            conversation_id=f"c-{message_id}",
            sender=sender,
            recipient=recipient or app.agents.require("reader").uri,
            payload=json.dumps(
                {"message": body, **({"replyTo": reply_to} if reply_to else {})}
            ).encode(),
            intent=intent,
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
            expires_at_ms=expires_at_ms,
            summary=summary,
        )
    ).accepted


def _fetched_at(app, message_id: str) -> object:  # type: ignore[no-untyped-def]
    row = app._inbox._db.execute(
        "SELECT fetched_at_ms FROM inbox WHERE message_id = ?", (message_id,)
    ).fetchone()
    return row["fetched_at_ms"]


def _read(app, message_id: str, **extra: object) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return app.handle(
        "message.read_body",
        {"actor": "reader", "sessionRef": "session-1", "messageId": message_id, **extra},
    )


def test_read_body_stamps_one_row_and_keeps_the_first_stamp(tmp_path: Path) -> None:
    app = durable_application(tmp_path)
    register_actor(app, "reader")
    _seed(app, message_id="x", summary="SX", body="BX")
    _seed(app, message_id="y", summary="SY", body="BY")

    listed = app.handle(
        "message.pending.list", {"actor": "reader", "sessionRef": "session-1"}
    )
    assert {row["messageId"] for row in listed["messages"]} == {"x", "y"}
    assert app._inbox.has_fetched("x") is False

    body = _read(app, "x")

    assert body["message"] == "BX"
    assert body["bodyBytes"] == len(b"BX")
    assert body["pending"] is True
    assert body["fetched"] is True
    assert body["summary"] == "SX"
    assert app._inbox.has_fetched("x") is True
    assert app._inbox.has_fetched("y") is False

    first_stamp = _fetched_at(app, "x")
    assert _read(app, "x")["fetched"] is True
    assert _fetched_at(app, "x") == first_stamp

    batch = app.handle(
        "message.pending.batch", {"actor": "reader", "sessionRef": "session-1"}
    )
    assert [item["messageId"] for item in batch["items"]] == ["x", "y"]
    assert _fetched_at(app, "x") == first_stamp
    second_stamp = _fetched_at(app, "y")
    assert second_stamp is not None
    assert _read(app, "y")["fetched"] is True
    assert _fetched_at(app, "y") == second_stamp


def test_read_body_refuses_another_consumers_row_without_stamping(tmp_path: Path) -> None:
    app = durable_application(tmp_path)
    register_actor(app, "reader")
    _seed(app, message_id="theirs", recipient=DRIVER, sender=READER)

    with pytest.raises(DaemonRequestError) as error:
        _read(app, "theirs")

    assert error.value.code == ipc_errors.MESSAGE_BODY_UNAVAILABLE
    assert error.value.data == {"messageId": "theirs"}
    assert app._inbox.has_fetched("theirs") is False


def test_read_body_refuses_an_unknown_id(tmp_path: Path) -> None:
    app = durable_application(tmp_path)
    register_actor(app, "reader")

    with pytest.raises(DaemonRequestError) as error:
        _read(app, "no-such-message")

    assert error.value.code == ipc_errors.MESSAGE_BODY_UNAVAILABLE


def test_read_body_keeps_the_existing_fence_codes(tmp_path: Path) -> None:
    app = durable_application(tmp_path)
    register_actor(app, "reader", "session-1")
    register_actor(app, "reader", "session-2")

    with pytest.raises(DaemonRequestError) as error:
        app.handle(
            "message.read_body",
            {"actor": "reader", "sessionRef": "session-1", "messageId": "x"},
        )

    assert error.value.code == "SESSION_SUPERSEDED"


def test_read_body_returns_a_consumed_row_read_only(tmp_path: Path) -> None:
    app = durable_application(tmp_path)
    register_actor(app, "reader")
    _seed(app, message_id="x", summary="SX", body="BX")
    assert app.handle(
        "message.ack", {"actor": "reader", "sessionRef": "session-1", "messageId": "x"}
    )["acknowledged"] is True

    body = _read(app, "x")

    assert body["message"] == "BX"
    assert body["pending"] is False


# --- B1: daemon-internal actors are exempt from link governance -------------


def test_a_daemon_internal_forward_is_readable(
    tmp_path: Path,
) -> None:
    """B1: a dispatch-coordinator forward is an ordinary readable row."""

    app = durable_application(tmp_path)
    register_actor(app, "reader")
    _seed(app, message_id="forward", sender=app._dispatch_service_actor)

    body = _read(app, "forward")

    assert body["message"] == "B"
    assert body["pending"] is True
    assert body["fetched"] is True


def test_owner_and_self_traffic_is_readable(tmp_path: Path) -> None:
    app = durable_application(tmp_path)
    register_actor(app, "reader")
    _seed(app, message_id="from-owner", sender="user:op")
    reader = app.agents.require("reader").uri
    _seed(app, message_id="self", sender=reader, recipient=reader)

    assert _read(app, "from-owner")["message"] == "B"
    assert _read(app, "self")["message"] == "B"


# --- R4: the IPC origin really fences a body read ---------------------------


class _CountingPublisher:
    def __init__(self) -> None:
        self.calls = 0

    def submit(self, message: object) -> AdmissionResult:
        self.calls += 1
        return AdmissionResult.ACCEPTED

    def close(self, _timeout: float) -> bool:
        return True


def _ipc_read(app, params: dict[str, object]):  # type: ignore[no-untyped-def]
    return app.handle("message.read_body", params, _trusted_message_origin="ipc")


def test_read_body_over_ipc_rejects_an_unauthenticated_caller(tmp_path: Path) -> None:
    inbox = FakeInbox(pending_message())
    app = application(tmp_path, inbox)
    register(app)
    app._sender_auth_enforced = True
    publisher = _CountingPublisher()
    app._fetch_receipt_publisher = publisher  # type: ignore[assignment]

    with pytest.raises(DaemonRequestError) as error:
        _ipc_read(app, {"actor": "alice", "messageId": "message-1"})

    assert error.value.code == ipc_errors.SENDER_NOT_AUTHENTICATED
    assert inbox.fetches == []
    assert publisher.calls == 0


def test_read_body_over_ipc_requires_a_fence_even_in_observe_mode(
    tmp_path: Path,
) -> None:
    inbox = FakeInbox(pending_message())
    app = application(tmp_path, inbox)
    register(app)
    app._sender_auth_enforced = False
    publisher = _CountingPublisher()
    app._fetch_receipt_publisher = publisher  # type: ignore[assignment]

    with pytest.raises(DaemonRequestError) as error:
        _ipc_read(app, {"actor": "alice", "messageId": "message-1"})

    assert error.value.code == ipc_errors.STALE_SESSION
    assert inbox.fetches == []
    assert publisher.calls == 0


def test_read_body_over_ipc_rejects_a_fabricated_fence(tmp_path: Path) -> None:
    inbox = FakeInbox(pending_message())
    app = application(tmp_path, inbox)
    app._sender_auth_enforced = True
    publisher = _CountingPublisher()
    app._fetch_receipt_publisher = publisher  # type: ignore[assignment]

    with pytest.raises(DaemonRequestError) as error:
        _ipc_read(
            app,
            {"actor": "alice", "sessionRef": "ghost", "messageId": "message-1"},
        )

    assert error.value.code == ipc_errors.STALE_SESSION
    assert inbox.fetches == []
    assert publisher.calls == 0


def test_read_body_over_ipc_rejects_a_superseded_fence(tmp_path: Path) -> None:
    inbox = FakeInbox(pending_message())
    app = application(tmp_path, inbox)
    register(app, "session-1")
    register(app, "session-2")
    app._sender_auth_enforced = True
    publisher = _CountingPublisher()
    app._fetch_receipt_publisher = publisher  # type: ignore[assignment]

    with pytest.raises(DaemonRequestError) as error:
        _ipc_read(
            app,
            {
                "actor": "alice",
                "sessionRef": "session-1",
                "messageId": "message-1",
            },
        )

    assert error.value.code == ipc_errors.SESSION_SUPERSEDED
    assert inbox.fetches == []
    assert publisher.calls == 0


def test_read_body_over_ipc_accepts_a_valid_worker_fence(tmp_path: Path) -> None:
    inbox = FakeInbox(pending_message())
    app = application(tmp_path, inbox)
    register(app, "session-1")
    publisher = _CountingPublisher()
    app._fetch_receipt_publisher = publisher  # type: ignore[assignment]

    body = _ipc_read(
        app,
        {"actor": "alice", "sessionRef": "session-1", "messageId": "message-1"},
    )

    assert body["message"] == "durable body"
    assert body["pending"] is True
    assert inbox.fetches == [official_address(app)]
    assert publisher.calls == 1


def test_owner_on_behalf_of_reads_only_the_named_consumers_body(
    tmp_path: Path,
) -> None:
    inbox = FakeInbox(pending_message())
    app = application(tmp_path, inbox)
    register(app, "session-1")

    body = _ipc_read(
        app,
        {"actor": "user:op", "onBehalfOf": "alice", "messageId": "message-1"},
    )
    assert body["message"] == "durable body"

    other = InboxMessage(
        message_id="theirs",
        conversation_id="c-theirs",
        sender="agent:op:node-1:driver",
        recipient="agent:op:node-1:driver",
        payload=json.dumps({"message": "not yours"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    inbox.pending = other
    with pytest.raises(DaemonRequestError) as error:
        _ipc_read(
            app,
            {"actor": "user:op", "onBehalfOf": "alice", "messageId": "theirs"},
        )
    assert error.value.code == ipc_errors.MESSAGE_BODY_UNAVAILABLE
