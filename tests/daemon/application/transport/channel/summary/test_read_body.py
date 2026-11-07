"""T12/T13: ``message.read_body`` consumes one row, under the session fence.

read_body stamps ``fetched``, retires the sender's receipt and publishes a
fetch receipt, so it needs the same session fence as an explicit
``message.pending.list`` fetch (review blocking R4) and must not commit
anything for a row it refuses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.kernel import AdmissionResult, DaemonRequestError, ipc_errors
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage

from ..support import (
    CANONICAL,
    FakeInbox,
    FakeLinkGate,
    _close_application_domains as _close_application_domains,
    application,
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
    recipient: str = READER,
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
            recipient=recipient,
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
    app.link_admission.bind(FakeLinkGate())
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


def test_read_body_refuses_another_consumers_row_without_stamping(tmp_path: Path) -> None:
    app = durable_application(tmp_path)
    register_actor(app, "reader")
    app.link_admission.bind(FakeLinkGate())
    _seed(app, message_id="theirs", recipient=DRIVER, sender=READER)

    with pytest.raises(DaemonRequestError) as error:
        _read(app, "theirs")

    assert error.value.code == ipc_errors.MESSAGE_BODY_UNAVAILABLE
    assert error.value.data == {"messageId": "theirs"}
    assert app._inbox.has_fetched("theirs") is False


def test_read_body_refuses_an_unknown_id(tmp_path: Path) -> None:
    app = durable_application(tmp_path)
    register_actor(app, "reader")
    app.link_admission.bind(FakeLinkGate())

    with pytest.raises(DaemonRequestError) as error:
        _read(app, "no-such-message")

    assert error.value.code == ipc_errors.MESSAGE_BODY_UNAVAILABLE


def test_read_body_keeps_the_existing_fence_codes(tmp_path: Path) -> None:
    app = durable_application(tmp_path)
    register_actor(app, "reader", "session-1")
    register_actor(app, "reader", "session-2")
    app.link_admission.bind(FakeLinkGate())

    with pytest.raises(DaemonRequestError) as error:
        app.handle(
            "message.read_body",
            {"actor": "reader", "sessionRef": "session-1", "messageId": "x"},
        )

    assert error.value.code == "SESSION_SUPERSEDED"


def test_read_body_returns_a_consumed_row_read_only(tmp_path: Path) -> None:
    app = durable_application(tmp_path)
    register_actor(app, "reader")
    app.link_admission.bind(FakeLinkGate())
    _seed(app, message_id="x", summary="SX", body="BX")
    assert app.handle(
        "message.ack", {"actor": "reader", "sessionRef": "session-1", "messageId": "x"}
    )["acknowledged"] is True

    body = _read(app, "x")

    assert body["message"] == "BX"
    assert body["pending"] is False


# --- B1: daemon-internal actors are exempt from link governance -------------


def test_a_daemon_internal_forward_is_readable_without_a_gate(
    tmp_path: Path,
) -> None:
    """B1: a dispatch-coordinator forward has no link file, so no gate is needed."""

    app = durable_application(tmp_path)
    register_actor(app, "reader")
    _seed(app, message_id="forward", sender=app._dispatch_service_actor)

    body = _read(app, "forward")

    assert body["message"] == "B"
    assert body["pending"] is True
    assert body["fetched"] is True


# --- R3: the link gate owns read_body admission (W, mutual reference) -------


def _snapshot(app) -> tuple[tuple[object, ...], ...]:  # type: ignore[no-untyped-def]
    return tuple(
        (row["message_id"], row["fetched_at_ms"], row["consumed"])
        for row in app._inbox._db.execute(
            "SELECT message_id, fetched_at_ms, consumed FROM inbox ORDER BY message_id"
        ).fetchall()
    )


def test_read_body_refuses_a_governed_row_the_gate_denies(tmp_path: Path) -> None:
    """A held request (W full or the mutual reference not synced) stays unread."""

    app = durable_application(tmp_path)
    register_actor(app, "reader")
    gate = FakeLinkGate(admit=False)
    app.link_admission.bind(gate)
    _seed(app, message_id="held")
    before = _snapshot(app)

    with pytest.raises(DaemonRequestError) as error:
        _read(app, "held")

    assert error.value.code == ipc_errors.MESSAGE_BODY_UNAVAILABLE
    assert error.value.data == {"messageId": "held"}
    assert _snapshot(app) == before
    assert app._inbox.has_fetched("held") is False
    # The retained-row fallback would serve this id read-only: the refusal
    # above must come from the pending+denied path, never from that fallback.
    assert app._inbox.harness_failure_original("held") is not None
    assert gate.admitted == ["held"]


def test_read_body_refuses_a_governed_row_with_no_gate_and_logs_it(
    tmp_path: Path,
) -> None:
    """Unbound gate is fail-closed (ruling 26) and observable."""

    app = durable_application(tmp_path)
    register_actor(app, "reader")
    events: list[tuple[object, ...]] = []
    app._log = lambda *args, **kwargs: events.append((args, kwargs))  # type: ignore[method-assign]
    _seed(app, message_id="unbound")
    before = _snapshot(app)

    with pytest.raises(DaemonRequestError) as error:
        _read(app, "unbound")

    assert error.value.code == ipc_errors.MESSAGE_BODY_UNAVAILABLE
    assert _snapshot(app) == before
    assert app._inbox.harness_failure_original("unbound") is not None
    assert any(
        "link.gate_unbound" in args
        for args, _kwargs in events
    )


def test_read_body_admits_once_the_gate_flips_to_allow(tmp_path: Path) -> None:
    """False -> True must release the same id with body and summary intact."""

    app = durable_application(tmp_path)
    register_actor(app, "reader")
    gate = FakeLinkGate(admit=False)
    app.link_admission.bind(gate)
    _seed(app, message_id="later", summary="S-later", body="B-later")
    with pytest.raises(DaemonRequestError):
        _read(app, "later")

    gate.admit = True
    body = _read(app, "later")

    assert body["message"] == "B-later"
    assert body["summary"] == "S-later"
    assert body["fetched"] is True


def test_repeated_reads_do_not_re_ask_the_gate_or_re_stamp(
    tmp_path: Path,
) -> None:
    """A repeat read is read-only: one admission, one stamp, one receipt.

    The daemon itself owns the idempotency (not a double's internal set): the
    first read admits, stamps and publishes once; the second finds the row
    already fetched and returns the same body without asking the gate again or
    publishing a second fetch receipt.  The gate is a plain double that
    records every call, so a second daemon-side ask would be visible.
    """

    app = durable_application(tmp_path)
    register_actor(app, "reader")
    gate = FakeLinkGate()
    app.link_admission.bind(gate)
    publisher = _CountingPublisher()
    app._fetch_receipt_publisher = publisher  # type: ignore[assignment]
    _seed(app, message_id="x")

    first = _read(app, "x")
    first_stamp = _fetched_at(app, "x")
    assert first["fetched"] is True
    assert publisher.calls == 1

    second = _read(app, "x")

    assert second == first
    assert _fetched_at(app, "x") == first_stamp
    assert publisher.calls == 1
    assert gate.admitted == ["x"]


def test_read_body_asks_the_one_composed_gate_slot(tmp_path: Path) -> None:
    """Ruling 67: ``link_admission.bind(gate)`` is the only bind point.

    There is no second slot on ``DaemonApplication``: binding the gate through
    the composed admission object is enough for ``read_body`` to consult it.
    """

    app = durable_application(tmp_path)
    register_actor(app, "reader")
    gate = FakeLinkGate(admit=False)
    app.link_admission.bind(gate)
    _seed(app, message_id="slot", body="SLOT-BODY")

    with pytest.raises(DaemonRequestError) as error:
        _read(app, "slot")

    assert error.value.code == ipc_errors.MESSAGE_BODY_UNAVAILABLE
    assert [call["message_id"] for call in gate.calls] == ["slot"]

    gate.admit = True
    assert _read(app, "slot")["message"] == "SLOT-BODY"
    assert [call["message_id"] for call in gate.calls] == ["slot", "slot"]


def test_a_held_row_acked_without_admission_is_not_readable(tmp_path: Path) -> None:
    """Acking a held message must not turn it into a readable body."""

    app = durable_application(tmp_path)
    register_actor(app, "reader")
    gate = FakeLinkGate(admit=False)
    app.link_admission.bind(gate)
    _seed(app, message_id="held", body="SECRET")
    with pytest.raises(DaemonRequestError):
        _read(app, "held")
    assert app.handle(
        "message.ack",
        {"actor": "reader", "sessionRef": "session-1", "messageId": "held"},
    )["acknowledged"] is True
    before = _snapshot(app)

    with pytest.raises(DaemonRequestError) as error:
        _read(app, "held")

    assert error.value.code == ipc_errors.MESSAGE_BODY_UNAVAILABLE
    assert error.value.data == {"messageId": "held"}
    assert _snapshot(app) == before
    assert [call["message_id"] for call in gate.calls] == ["held", "held"]


def test_an_expired_governed_row_never_admitted_is_not_readable(
    tmp_path: Path,
) -> None:
    app = durable_application(tmp_path)
    register_actor(app, "reader")
    gate = FakeLinkGate(admit=False)
    app.link_admission.bind(gate)
    _seed(app, message_id="expired", body="SECRET", expires_at_ms=2)
    before = _snapshot(app)

    with pytest.raises(DaemonRequestError) as error:
        _read(app, "expired")

    assert error.value.code == ipc_errors.MESSAGE_BODY_UNAVAILABLE
    assert _snapshot(app) == before
    assert [call["message_id"] for call in gate.calls] == ["expired"]


class _ExplodingGate(FakeLinkGate):
    def admit_dispatch(self, **kwargs: object) -> bool:  # type: ignore[override]
        raise RuntimeError("link gate is down")


def test_a_failing_gate_reads_fail_closed_and_logs(tmp_path: Path) -> None:
    app = durable_application(tmp_path)
    register_actor(app, "reader")
    app.link_admission.bind(_ExplodingGate())
    events: list[tuple[object, ...]] = []
    app._log = lambda *args, **kwargs: events.append((args, kwargs))  # type: ignore[method-assign]
    _seed(app, message_id="x")

    with pytest.raises(DaemonRequestError) as error:
        _read(app, "x")

    assert error.value.code == ipc_errors.MESSAGE_BODY_UNAVAILABLE
    assert app._inbox.has_fetched("x") is False
    assert any("link.gate_error" in args for args, _kwargs in events)

def test_owner_and_self_traffic_ignores_a_denying_gate(tmp_path: Path) -> None:
    app = durable_application(tmp_path)
    register_actor(app, "reader")
    app.link_admission.bind(FakeLinkGate(admit=False))
    _seed(app, message_id="from-owner", sender="user:op")
    _seed(app, message_id="self", sender=READER, recipient=READER)

    assert _read(app, "from-owner")["message"] == "B"
    assert _read(app, "self")["message"] == "B"


class _RepliesAreFree(FakeLinkGate):
    """A gate whose policy frees replies: the read path must forward the
    explicit reply_to and intent instead of guessing reply-ness itself."""

    def admit_dispatch(
        self,
        *,
        recipient: str,
        sender: str,
        message_id: str,
        now_ms: int,
        intent: str = "request",
        reply_to: str | None = None,
    ) -> bool:
        self.calls.append(
            {"message_id": message_id, "intent": intent, "reply_to": reply_to}
        )
        self.admitted.append(message_id)
        return reply_to is not None


def test_an_effective_reply_is_admitted_by_the_gate_policy(tmp_path: Path) -> None:
    app = durable_application(tmp_path)
    register_actor(app, "reader")
    gate = _RepliesAreFree(admit=False)
    app.link_admission.bind(gate)
    _seed(app, message_id="request")
    _seed(app, message_id="reply", intent="reply", reply_to="request")

    with pytest.raises(DaemonRequestError):
        _read(app, "request")
    body = _read(app, "reply")

    assert body["message"] == "B"
    assert gate.calls == [
        {"message_id": "request", "intent": "request", "reply_to": None},
        {"message_id": "reply", "intent": "reply", "reply_to": "request"},
    ]


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
    app = application(tmp_path, inbox, gate=FakeLinkGate())
    register(app, "session-1")
    publisher = _CountingPublisher()
    app._fetch_receipt_publisher = publisher  # type: ignore[assignment]

    body = _ipc_read(
        app,
        {"actor": "alice", "sessionRef": "session-1", "messageId": "message-1"},
    )

    assert body["message"] == "durable body"
    assert body["pending"] is True
    assert inbox.fetches == [CANONICAL]
    assert publisher.calls == 1


def test_owner_on_behalf_of_reads_only_the_named_consumers_body(
    tmp_path: Path,
) -> None:
    inbox = FakeInbox(pending_message())
    app = application(tmp_path, inbox, gate=FakeLinkGate())
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
