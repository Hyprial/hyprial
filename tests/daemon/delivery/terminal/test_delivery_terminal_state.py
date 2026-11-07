"""Every message ends in one persisted, pullable terminal state.

The defect these pin: ``message.send`` answers ``accepted/queued/
target-not-visible``, and that answer has been observed identical on a
delivery that provably landed and on one that provably vanished.  A send-time
value cannot report an outcome that has not happened yet, so the outcome is
recorded when it happens and read back later.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import close_application, owned_applications as owned_applications

import json
from pathlib import Path

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.inbox import (
    DEFAULT_HOLD_TTL_MS,
    DeliveryLifecycle,
    DeliveryStatus,
    HoldPolicy,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
    TerminalState,
    conflicting_message_ids,
    merge_delivery_status,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

TTL_MS = DEFAULT_HOLD_TTL_MS
YEAR_MS = 365 * 86_400_000


def _message(
    message_id: str = "msg-1",
    *,
    sender: str = "agent:alice:mac:sender",
    recipient: str = "agent:bob:pc:worker",
    created_at_ms: int = 0,
    idempotency_key: str | None = None,
) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="dm:alice:bob",
        sender=sender,
        recipient=recipient,
        payload=b"opaque-frame",
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        idempotency_key=idempotency_key,
        created_at_ms=created_at_ms,
    )


class _ReceiptLosingTransport:
    """Delivers for real, then loses the receipt -- defect #4's exact shape.

    The recipient commits the message; the sender never learns it did.  This
    is the case that must not produce a confident false ``expired``.
    """

    def __init__(self, recipient: InboxService, *, receive_at_ms: int = 0) -> None:
        self.recipient = recipient
        self.receive_at_ms = receive_at_ms
        self.delivered: list[str] = []

    def is_online(self, recipient: str) -> bool:
        return True

    def deliver(self, message: InboxMessage) -> bool:
        self.delivered.append(message.message_id)
        self.recipient.receive(message, now_ms=self.receive_at_ms)
        return False

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()

    def transfer_custody(self, mailbox: str, message: InboxMessage) -> bool:
        return False


def _service(path: Path, transport: object, **kwargs: object) -> InboxService:
    return InboxService(path, transport, **kwargs)  # type: ignore[arg-type]


# --- fetched -----------------------------------------------------------------


def test_recipient_records_fetched_when_it_commits_the_message(
    tmp_path: Path,
) -> None:
    recipient = InboxService(
        tmp_path / "recipient.sqlite3", MemoryDeliveryTransport(), node_id="node-bob"
    )
    message = _message()

    recipient.receive(message, now_ms=5_000)

    records = recipient.delivery_status_records(message.sender)
    assert [record.message_id for record in records] == ["msg-1"]
    assert records[0].state is TerminalState.FETCHED
    assert records[0].holder == "node-bob"
    assert records[0].recipient == message.recipient
    assert records[0].reason == "ACK_RECEIVED"
    recipient.close()


def test_holder_records_fetched_when_the_receipt_confirms(tmp_path: Path) -> None:
    transport = MemoryDeliveryTransport()
    transport.set_online("agent:bob:pc:worker", True)
    transport.confirm_direct("msg-1")
    sender = InboxService(
        tmp_path / "sender.sqlite3", transport, node_id="node-alice"
    )
    message = _message()

    result = sender.submit(message, now_ms=1_000)

    assert result.accepted and not result.queued
    assert sender.outbox_count() == 0
    records = sender.delivery_status_records(message.sender, message_id="msg-1")
    assert records[0].state is TerminalState.FETCHED
    assert records[0].reason == "ACK_RECEIVED"
    sender.close()


def test_lost_receipt_does_not_produce_a_false_expired(tmp_path: Path) -> None:
    """The 2026-08-14 incident: delivered, replied, reported not-visible.

    The recipient's record is written from a fact it observed itself. The
    sender keeps retrying the same id until TTL because no ACK came back; at
    TTL its local outcome is expired, while the recipient's direct ACK evidence
    still wins the merged view.
    """

    recipient = InboxService(
        tmp_path / "recipient.sqlite3", MemoryDeliveryTransport(), node_id="node-bob"
    )
    transport = _ReceiptLosingTransport(recipient, receive_at_ms=1_000)
    sender = InboxService(tmp_path / "sender.sqlite3", transport, node_id="node-alice")
    message = _message()

    try:
        sender.submit(message, now_ms=1_000)
        for _ in range(3):
            sender.retry_due(
                now_ms=sender.outbox_item(message.message_id).next_attempt_ms
            )

        assert transport.delivered  # it really was handed over
        recipient_view = recipient.delivery_status_records(message.sender)
        assert recipient_view[0].state is TerminalState.FETCHED
        assert sender.delivery_status_records(message.sender) == ()
        assert sender.outbox_count() == 1
        assert sender.dlq_count() == 0

        awaiting = sender.outbox_item(message.message_id)
        assert awaiting.confirmation_started_at_ms is None
        assert awaiting.confirmation_attempts == 0
        [expired] = sender.retry_due(now_ms=TTL_MS + 1_000)
        assert expired.code == "TTL_EXPIRED"

        sender_view = sender.delivery_status_records(message.sender)
        assert sender_view[0].state is TerminalState.EXPIRED
        assert sender_view[0].reason == "TTL_EXPIRED"
        merged = merge_delivery_status([*sender_view, *recipient_view])
        assert len(merged) == 1
        assert merged[0].state is TerminalState.FETCHED
    finally:
        recipient.close()
        sender.close()


def test_mailbox_records_fetched_when_custody_is_finally_delivered(
    tmp_path: Path,
) -> None:
    transport = MemoryDeliveryTransport()
    mailbox = InboxService(
        tmp_path / "mailbox.sqlite3", transport, node_id="node-mailbox"
    )
    message = _message()
    mailbox.accept_custody(message, mailbox_node="node-mailbox", now_ms=0)
    transport.set_online(message.recipient, True)
    transport.confirm_direct(message.message_id)

    mailbox.retry_custody_due(now_ms=1_000)

    assert mailbox.custody_count() == 0
    records = mailbox.delivery_status_records(message.sender)
    assert records[0].state is TerminalState.FETCHED
    assert records[0].holder == "node-mailbox"
    mailbox.close()


# --- expired -----------------------------------------------------------------


def test_hold_ttl_expiry_is_recorded_not_silent(tmp_path: Path) -> None:
    sender = InboxService(
        tmp_path / "sender.sqlite3", MemoryDeliveryTransport(), node_id="mac"
    )
    message = _message()
    sender.submit(message, now_ms=0)
    assert sender.outbox_count() == 1

    sender.retry_due(now_ms=TTL_MS + 1)

    assert sender.outbox_count() == 0
    records = sender.delivery_status_records(message.sender)
    assert records[0].state is TerminalState.EXPIRED
    assert records[0].reason == "TTL_EXPIRED"
    notices = sender.system_notices(message.sender)
    assert len(notices) == 1
    assert notices[0].intent == "system"
    assert notices[0].message_id != message.message_id
    assert notices[0].conversation_id == message.conversation_id
    assert notices[0].sender == "system:hyprial:mac"
    assert notices[0].recipient == message.sender
    assert notices[0].payload == (
        b'{"message":"System notice: message msg-1 reached its TTL before an '
        b'ACK was received; reason=TTL_EXPIRED.",'
        b'"originalMessageId":"msg-1","reason":"TTL_EXPIRED",'
        b'"systemNotice":true}'
    )
    # A notice is outside the ordinary FIFO/ack state machine.
    assert sender.pending_count(message.sender) == 0
    sender.close()


def test_system_notice_is_drained_without_entering_or_blocking_fifo(
    tmp_path: Path,
) -> None:
    sender = InboxService(
        tmp_path / "sender.sqlite3", MemoryDeliveryTransport(), node_id="mac"
    )
    failed = _message("msg-failed")
    ordinary = _message(
        "msg-ordinary",
        sender="agent:bob:pc:worker",
        recipient=failed.sender,
    )
    sender.receive(ordinary, now_ms=2)
    sender.submit(failed, now_ms=0)
    sender.retry_due(now_ms=TTL_MS + 1)

    assert [
        item.message_id
        for item in sender.pending_messages(failed.sender, now_ms=TTL_MS + 1)
    ] == [
        "msg-ordinary"
    ]
    (notice,) = sender.drain_system_notices(failed.sender)
    assert notice.intent == "system"
    assert sender.system_notices(failed.sender) == ()
    assert [
        item.message_id
        for item in sender.pending_messages(failed.sender, now_ms=TTL_MS + 1)
    ] == [
        "msg-ordinary"
    ]
    assert not sender.is_acknowledged(notice.message_id)
    sender.close()


def test_post_fetch_failure_uses_the_same_alarm_path(tmp_path: Path) -> None:
    transport = MemoryDeliveryTransport()
    service = InboxService(
        tmp_path / "recipient.sqlite3",
        transport,
        node_id="pc",
    )
    message = _message()
    service.receive(message, now_ms=1)

    service.fail(message.recipient, message.message_id, "PROVIDER_BLOCKED")

    [(notice_node, notice)] = transport.notices
    assert notice_node == "mac"
    body = json.loads(notice.payload)
    assert body["originalMessageId"] == message.message_id
    assert body["reason"] == "PROVIDER_BLOCKED"
    records = [
        json.loads(line)
        for line in (tmp_path / "logs" / "daemon.jsonl").read_text().splitlines()
    ]
    terminal = [item for item in records if item["event"] == "terminal.failure"]
    assert len(terminal) == 1
    assert terminal[0]["correlationId"] == message.message_id
    assert terminal[0]["reason"] == "PROVIDER_BLOCKED"
    service.close()


def test_post_fetch_human_failure_uses_original_lark_alarm_route(
    tmp_path: Path,
) -> None:
    delivered: list[tuple[str, str, str]] = []

    def deliver_human(alarm, text: str) -> bool:  # type: ignore[no-untyped-def]
        delivered.append((alarm.message_id, alarm.reason, text))
        return True

    service = InboxService(
        tmp_path / "recipient.sqlite3",
        MemoryDeliveryTransport(),
        node_id="pc",
        alarm_human_delivery=deliver_human,
    )
    message = _message(sender="channel:lark:test-app")
    service.receive(message, now_ms=1)

    service.fail(message.recipient, message.message_id, "PROVIDER_BLOCKED")

    assert delivered == [
        (
            message.message_id,
            "PROVIDER_BLOCKED",
            f"消息投递失败：PROVIDER_BLOCKED（消息 {message.message_id}）。",
        )
    ]
    service.close()


def test_custody_ttl_expiry_is_recorded_by_the_mailbox(tmp_path: Path) -> None:
    mailbox = InboxService(
        tmp_path / "mailbox.sqlite3",
        MemoryDeliveryTransport(),
        node_id="node-mailbox",
    )
    message = _message()
    mailbox.accept_custody(message, mailbox_node="node-mailbox", now_ms=0)

    mailbox.retry_custody_due(now_ms=TTL_MS + 1)

    assert mailbox.custody_count() == 0
    records = mailbox.delivery_status_records(message.sender)
    assert records[0].state is TerminalState.EXPIRED
    assert records[0].reason == "TTL_EXPIRED"
    assert records[0].holder == "node-mailbox"
    mailbox.close()


def test_default_hold_policy_is_twelve_hours_and_one_hundred_items() -> None:
    policy = HoldPolicy()
    assert policy.ttl_ms == DEFAULT_HOLD_TTL_MS == 43_200_000
    assert policy.max_items == 100


def test_invalid_hold_ttl_environment_falls_back_to_product_default() -> None:
    assert HoldPolicy.from_environment({}).ttl_ms == DEFAULT_HOLD_TTL_MS
    for raw in ("", "0", "-1", "not-a-number"):
        assert HoldPolicy.from_environment({"HYPRIAL_HOLD_TTL_MS": raw}).ttl_ms == DEFAULT_HOLD_TTL_MS


def test_hold_policy_is_configurable_per_daemon(tmp_path: Path) -> None:
    policy = HoldPolicy.from_environment(
        {"HYPRIAL_HOLD_TTL_MS": "60000", "HYPRIAL_HOLD_MAX_ITEMS": "7"}
    )
    assert policy.ttl_ms == 60_000
    assert policy.max_items == 7

    sender = InboxService(
        tmp_path / "sender.sqlite3",
        MemoryDeliveryTransport(),
        node_id="node-alice",
        hold_policy=policy,
    )
    sender.submit(_message(), now_ms=0)
    sender.retry_due(now_ms=59_000)
    assert sender.outbox_count() == 1  # still inside the configured TTL

    sender.retry_due(now_ms=60_001)
    assert sender.outbox_count() == 0
    records = sender.delivery_status_records("agent:alice:mac:sender")
    assert records[0].state is TerminalState.EXPIRED
    sender.close()


def test_a_non_positive_bound_is_refused_at_construction() -> None:
    """A zero cap would evict on arrival -- refuse the config, not the traffic."""

    for broken in ({"ttl_ms": 0}, {"max_items": 0}, {"status_retention_ms": -1}):
        try:
            HoldPolicy(**broken)  # type: ignore[arg-type]
        except ValueError:
            continue
        raise AssertionError(f"HoldPolicy accepted {broken}")


def test_capacity_overflow_evicts_the_oldest_and_records_it(tmp_path: Path) -> None:
    sender = InboxService(
        tmp_path / "sender.sqlite3",
        MemoryDeliveryTransport(),
        node_id="node-alice",
        hold_policy=HoldPolicy(ttl_ms=TTL_MS, max_items=3),
    )
    for index in range(5):
        sender.submit(_message(f"msg-{index}"), now_ms=index)

    assert sender.outbox_count() == 3
    remaining = {item.message.message_id for item in sender.outbox_items()}
    assert remaining == {"msg-2", "msg-3", "msg-4"}
    evicted = {
        record.message_id: record
        for record in sender.delivery_status_records("agent:alice:mac:sender")
    }
    assert set(evicted) == {"msg-0", "msg-1"}
    assert {record.state for record in evicted.values()} == {TerminalState.EXPIRED}
    assert {record.reason for record in evicted.values()} == {
        "HOLD_CAPACITY_EXCEEDED"
    }
    sender.close()


# --- the holder's own clock --------------------------------------------------


def test_expiry_uses_the_holder_clock_not_a_sender_timestamp(tmp_path: Path) -> None:
    """A sender ten years behind must not expire on arrival.

    Computing age as ``now - message.created_at_ms`` would make this message
    ancient the instant it landed.  The deadline is the holder's own clock at
    the moment it took the message, plus the TTL.
    """

    sender = InboxService(
        tmp_path / "sender.sqlite3", MemoryDeliveryTransport(), node_id="node-alice"
    )
    holder_now = 10 * YEAR_MS
    stale_stamp = _message("msg-past", created_at_ms=holder_now - 10 * YEAR_MS)

    sender.submit(stale_stamp, now_ms=holder_now)
    sender.retry_due(now_ms=holder_now + TTL_MS - 1)

    assert sender.outbox_count() == 1
    assert sender.delivery_status_records(stale_stamp.sender) == ()

    # A retry that finds nothing online defers the row by one backoff step, so
    # the eviction lands on the first tick after that -- the TTL is a floor on
    # the hold, not a millisecond-exact alarm.
    sender.retry_due(now_ms=holder_now + TTL_MS + 1_001)

    records = sender.delivery_status_records(stale_stamp.sender)
    assert records[0].state is TerminalState.EXPIRED
    sender.close()


def test_a_sender_clock_in_the_future_still_expires_on_schedule(
    tmp_path: Path,
) -> None:
    """The mirror-image skew: a negative elapsed time must never mean immortal."""

    mailbox = InboxService(
        tmp_path / "mailbox.sqlite3",
        MemoryDeliveryTransport(),
        node_id="node-mailbox",
    )
    future_stamp = _message("msg-future", created_at_ms=10 * YEAR_MS)

    mailbox.accept_custody(future_stamp, mailbox_node="node-mailbox", now_ms=1_000)
    mailbox.retry_custody_due(now_ms=1_000 + TTL_MS + 1)

    assert mailbox.custody_count() == 0
    records = mailbox.delivery_status_records(future_stamp.sender)
    assert records[0].state is TerminalState.EXPIRED
    mailbox.close()


# --- pullable after the fact -------------------------------------------------


def test_terminal_records_survive_the_sender_being_down(tmp_path: Path) -> None:
    """A sender is routinely offline when its message ends.

    So the verdict cannot be a one-shot notification: it has to still be there
    when the sender comes back, across a process restart.
    """

    database = tmp_path / "sender.sqlite3"
    sender = InboxService(
        database, MemoryDeliveryTransport(), node_id="node-alice"
    )
    message = _message()
    sender.submit(message, now_ms=0)
    sender.retry_due(now_ms=TTL_MS + 1)
    sender.close()  # the sending daemon goes away entirely

    revived = InboxService(
        database, MemoryDeliveryTransport(), node_id="node-alice"
    )
    records = revived.delivery_status_records(message.sender)
    assert [record.state for record in records] == [TerminalState.EXPIRED]
    assert revived.delivery_status_records(message.sender, message_id="msg-1")
    revived.close()


def test_records_outlive_the_message_ttl_by_a_wide_margin(tmp_path: Path) -> None:
    sender = InboxService(
        tmp_path / "sender.sqlite3", MemoryDeliveryTransport(), node_id="node-alice"
    )
    message = _message()
    sender.submit(message, now_ms=0)
    sender.retry_due(now_ms=TTL_MS + 1)

    # A whole day later -- past the default message hold TTL -- the verdict
    # is still pullable; only the much longer retention window drops it.
    sender.retry_due(now_ms=86_400_000)
    assert sender.delivery_status_records(message.sender)

    sender.retry_due(now_ms=8 * 86_400_000)
    assert sender.delivery_status_records(message.sender) == ()
    sender.close()


# --- invariants the rework must not break ------------------------------------


def test_idempotent_dedup_still_holds_and_reports_delivery(tmp_path: Path) -> None:
    recipient = InboxService(
        tmp_path / "recipient.sqlite3", MemoryDeliveryTransport(), node_id="node-bob"
    )
    first = _message("msg-a", idempotency_key="op-1")
    replay = _message("msg-b", idempotency_key="op-1")

    accepted = recipient.receive(first, now_ms=1_000)
    duplicate = recipient.receive(replay, now_ms=2_000)

    assert accepted.accepted and not accepted.duplicate
    assert duplicate.duplicate and duplicate.acknowledged
    assert not duplicate.accepted
    assert recipient.pending_count(first.recipient) == 1

    states = {
        record.message_id: record
        for record in recipient.delivery_status_records(first.sender)
    }
    assert set(states) == {"msg-a", "msg-b"}
    assert {record.state for record in states.values()} == {TerminalState.FETCHED}
    assert states["msg-b"].reason == "ACK_RECEIVED"
    recipient.close()


def test_fifo_still_follows_arrival_id_under_skewed_send_times(
    tmp_path: Path,
) -> None:
    """Order is the recipient's arrival sequence, never the senders' clocks."""

    recipient = InboxService(
        tmp_path / "recipient.sqlite3", MemoryDeliveryTransport(), node_id="node-bob"
    )
    # Arrive first..third while their send stamps run backwards.
    recipient.receive(_message("msg-first", created_at_ms=9_000), now_ms=1)
    recipient.receive(_message("msg-second", created_at_ms=5_000), now_ms=2)
    recipient.receive(_message("msg-third", created_at_ms=1_000), now_ms=3)

    pending = recipient.pending_messages("agent:bob:pc:worker", now_ms=3)

    assert [item.message_id for item in pending] == [
        "msg-first",
        "msg-second",
        "msg-third",
    ]
    assert recipient.next("agent:bob:pc:worker").message_id == "msg-first"
    recipient.close()


def test_custody_transfer_is_not_a_terminal_state(tmp_path: Path) -> None:
    """Ownership moved; the message is alive and the mailbox owes the verdict."""

    transport = MemoryDeliveryTransport()
    transport.advertise_mailbox("node-mailbox")
    transport.confirm_custody("msg-1")
    sender = InboxService(
        tmp_path / "sender.sqlite3", transport, node_id="node-alice"
    )
    message = _message()

    result = sender.submit(message, now_ms=0)

    assert result.custody_mailbox == "node-mailbox"
    assert sender.outbox_count() == 0
    assert sender.delivery_status_records(message.sender) == ()
    sender.close()


# --- the daemon surface ------------------------------------------------------


class _Presence:
    def online_actors(self) -> tuple[str, ...]:
        return ()

    def actor_online(self, actor: str) -> bool:
        return False

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


def _application(tmp_path: Path, service: InboxService) -> DaemonApplication:
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state" / "daemon.sock",
        node_id="node-alice",
    **_deep_module_daemon_dependencies())
    app._inbox = service
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = _Presence()  # type: ignore[assignment]
    return app


def test_message_status_reports_every_stage(tmp_path: Path) -> None:
    service = InboxService(
        tmp_path / "inbox.sqlite3", MemoryDeliveryTransport(), node_id="node-alice"
    )
    app = _application(tmp_path, service)
    sender = "agent:alice:mac:sender"

    unknown = app.handle(
        "message.status", {"from": sender, "messageId": "never-sent"}
    )
    assert unknown["ok"] is True
    assert unknown["state"] == "unknown"
    assert unknown["records"] == []

    service.submit(_message("msg-held"), now_ms=0)
    held = app.handle("message.status", {"from": sender, "messageId": "msg-held"})
    assert held["state"] == "pending"
    assert held["holdExpiresAtMs"] == TTL_MS

    service.retry_due(now_ms=TTL_MS + 1)
    expired = app.handle("message.status", {"from": sender, "messageId": "msg-held"})
    assert expired["state"] == "expired"
    assert expired["records"][0]["reason"] == "TTL_EXPIRED"
    assert expired["records"][0]["holder"] == "node-alice"

    service.receive(_message("msg-in"), now_ms=TTL_MS + 2)
    everything = app.handle("message.status", {"from": sender})
    assert {record["messageId"] for record in everything["records"]} == {
        "msg-held",
        "msg-in",
    }
    assert {
        record["messageId"]: record["state"] for record in everything["records"]
    } == {"msg-held": "expired", "msg-in": "fetched"}
    service.close()


# --- precedence: evidence class, never the clock -----------------------------


def _status(
    *,
    state: TerminalState,
    holder: str,
    reason: str,
    recorded_at_ms: int,
    message_id: str = "msg-1",
) -> DeliveryStatus:
    return DeliveryStatus(
        message_id=message_id,
        sender="agent:alice:mac:sender",
        recipient="agent:bob:pc:worker",
        state=state,
        holder=holder,
        reason=reason,
        recorded_at_ms=recorded_at_ms,
    )


def test_the_recipients_record_wins_even_when_the_mirror_is_stamped_earlier() -> None:
    """Measured on 2026-08-14 across containers, and it was decided backwards.

    Same message: the sender's mirror read ``RECEIPT_CONFIRMED @…747`` and the
    recipient's own ``RECIPIENT_COMMIT @…751``.  Precedence ranked ``fetched``
    records earliest-first on the assumption that a mirror "can only be
    later", so the hearsay copy beat the record written inside the
    recipient's own commit transaction.

    The two stamps come off two machines' clocks and cannot be compared at
    all -- the same rule ``HoldPolicy`` and section 8.3 apply everywhere else.
    So the ranking is by who observed the outcome, and this holds whichever
    way the clocks happen to fall.
    """

    mirror = _status(
        state=TerminalState.FETCHED,
        holder="node-alice",
        reason="RECEIPT_CONFIRMED",
        recorded_at_ms=1_786_681_451_747,
    )
    authoritative = _status(
        state=TerminalState.FETCHED,
        holder="node-bob",
        reason="RECIPIENT_COMMIT",
        recorded_at_ms=1_786_681_451_751,
    )

    for order in ((mirror, authoritative), (authoritative, mirror)):
        merged = merge_delivery_status(order)
        assert len(merged) == 1
        assert merged[0].holder == "node-bob"
        assert merged[0].reason == "RECIPIENT_COMMIT"


def test_a_deduplicated_retry_is_first_hand_evidence_too() -> None:
    """RECIPIENT_DUPLICATE is also written by the recipient, about its own inbox."""

    merged = merge_delivery_status(
        [
            _status(
                state=TerminalState.FETCHED,
                holder="node-alice",
                reason="RECEIPT_CONFIRMED",
                recorded_at_ms=1,
            ),
            _status(
                state=TerminalState.FETCHED,
                holder="node-bob",
                reason="RECIPIENT_DUPLICATE",
                recorded_at_ms=9_999,
            ),
        ]
    )
    assert merged[0].holder == "node-bob"


def test_an_unknown_reason_is_never_promoted_over_the_recipient() -> None:
    """``reason`` is a free-form string; an unrecognised claim stays hearsay."""

    merged = merge_delivery_status(
        [
            _status(
                state=TerminalState.FETCHED,
                holder="node-future",
                reason="SOME_REASON_THIS_VERSION_HAS_NEVER_HEARD_OF",
                recorded_at_ms=1,
            ),
            _status(
                state=TerminalState.FETCHED,
                holder="node-bob",
                reason="RECIPIENT_COMMIT",
                recorded_at_ms=9_999,
            ),
        ]
    )
    assert merged[0].holder == "node-bob"


def test_fetched_still_beats_expired_including_a_mere_mirror() -> None:
    """The invariant the tiers must not disturb: an eviction is the weakest word."""

    merged = merge_delivery_status(
        [
            _status(
                state=TerminalState.EXPIRED,
                holder="node-alice",
                reason="DELIVERY_EXPIRED",
                recorded_at_ms=9_999,
            ),
            _status(
                state=TerminalState.FETCHED,
                holder="node-mailbox",
                reason="RECEIPT_CONFIRMED",
                recorded_at_ms=1,
            ),
        ]
    )
    assert merged[0].state is TerminalState.FETCHED


def test_disagreeing_holders_are_reported_not_just_silently_resolved() -> None:
    records = [
        _status(
            state=TerminalState.EXPIRED,
            holder="node-alice",
            reason="DELIVERY_EXPIRED",
            recorded_at_ms=2,
        ),
        _status(
            state=TerminalState.FETCHED,
            holder="node-bob",
            reason="RECIPIENT_COMMIT",
            recorded_at_ms=1,
        ),
        _status(
            state=TerminalState.FETCHED,
            holder="node-bob",
            reason="RECIPIENT_COMMIT",
            recorded_at_ms=1,
            message_id="msg-agreed",
        ),
    ]
    assert conflicting_message_ids(records) == ("msg-1",)


# --- a recipient asking about a message it RECEIVED ------------------------
#
# A codex carrier whose ack came back MESSAGE_ACK_UNAVAILABLE asks
# `message.status` for the INBOUND delivery, signed as itself -- the
# recipient.  Records are keyed by the original sender, so the sender-keyed
# lookup never matched and the query fanned out to every mesh holder, empty,
# every 5 s without end (codex-router, 2026-09-25: 56,194 queries).


class _CountingTransport:
    """Records every mesh query; answers none."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def get(self, selector: str, *args: object, **kwargs: object) -> list[object]:
        self.queries.append(selector)
        return []

    def close(self) -> None:
        pass


def test_a_recipient_reads_its_own_receipt_locally_without_asking_the_mesh(
    tmp_path: Path,
) -> None:
    service = InboxService(
        tmp_path / "inbox.sqlite3", MemoryDeliveryTransport(), node_id="node-bob"
    )
    app = _application(tmp_path, service)
    transport = _CountingTransport()
    app._transport = transport  # type: ignore[assignment]
    inbound = _message("msg-inbound")  # sender alice -> recipient bob's worker
    service.receive(inbound, now_ms=1_000)

    status = app.handle(
        "message.status", {"from": inbound.recipient, "messageId": "msg-inbound"}
    )

    assert status["state"] == "fetched"
    assert status["records"][0]["recipient"] == inbound.recipient
    assert transport.queries == [], "the recipient's own receipt needs no mesh fan-out"
    service.close()


def test_a_third_party_learns_nothing_about_someone_elses_message(
    tmp_path: Path,
) -> None:
    service = InboxService(
        tmp_path / "inbox.sqlite3", MemoryDeliveryTransport(), node_id="node-bob"
    )
    app = _application(tmp_path, service)
    app._transport = _CountingTransport()  # type: ignore[assignment]
    service.receive(_message("msg-inbound"), now_ms=1_000)

    status = app.handle(
        "message.status",
        {"from": "agent:mallory:pc:snoop", "messageId": "msg-inbound"},
    )

    assert status["records"] == []
    assert status["state"] == "unknown"
    service.close()


def test_the_sender_still_asks_the_mesh_for_a_message_it_sent(tmp_path: Path) -> None:
    service = InboxService(
        tmp_path / "inbox.sqlite3", MemoryDeliveryTransport(), node_id="node-alice"
    )
    app = _application(tmp_path, service)
    transport = _CountingTransport()
    app._transport = transport  # type: ignore[assignment]

    app.handle("message.status", {"from": "agent:alice:mac:sender", "messageId": "m-x"})

    assert transport.queries, "a sender's status pull still reaches the mesh"
    service.close()


def test_the_production_facade_answers_the_recipient_too(tmp_path: Path) -> None:
    """The daemon's real inbox is DeliveryCustodyFacade over a coordinator
    (application.py), whose reads run their own SQL -- pin that path too."""

    from hyprial.daemon.impl.inbox import DeliveryCustodyCoordinator, DeliveryCustodyFacade

    class _Events:
        def publish(self, _event: object) -> None:
            pass

    database = tmp_path / "inbox.sqlite3"
    coordinator = DeliveryCustodyCoordinator(
        database, MemoryDeliveryTransport(), _Events(), node_id="node-bob"
    )
    facade = DeliveryCustodyFacade(coordinator, database)
    app: DaemonApplication | None = None
    try:
        app = _application(tmp_path, facade)  # type: ignore[arg-type]
        transport = _CountingTransport()
        app._transport = transport  # type: ignore[assignment]
        inbound = _message("msg-inbound")
        assert facade.receive(inbound).accepted

        mine = app.handle(
            "message.status", {"from": inbound.recipient, "messageId": "msg-inbound"}
        )
        theirs = app.handle(
            "message.status",
            {"from": "agent:mallory:pc:snoop", "messageId": "msg-inbound"},
        )
    finally:
        if app is None:
            facade.close()
        else:
            close_application(app)

    assert mine["state"] == "fetched"
    assert transport.queries == [] or all("mallory" in q for q in transport.queries)
    assert theirs["records"] == []
