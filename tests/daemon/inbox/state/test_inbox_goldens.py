from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hyprial.daemon.impl.inbox import (
    DEFAULT_HOLD_TTL_MS,
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)

ROOT = Path(__file__).resolve().parents[4]


def _corpus() -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    envelope_doc = json.loads(
        (ROOT / "contract/wire/golden/envelopes.json").read_text(encoding="utf-8")
    )
    expectation_doc = json.loads(
        (ROOT / "contract/wire/golden/expectations.json").read_text(encoding="utf-8")
    )
    envelopes = {item["fixture_id"]: item for item in envelope_doc["envelopes"]}
    expectations = {item["id"]: item for item in expectation_doc["cases"]}
    return envelopes, expectations


ENVELOPES, EXPECTATIONS = _corpus()


def message(
    fixture_id: str, *, lifecycle: DeliveryLifecycle | None = None
) -> InboxMessage:
    envelope = ENVELOPES[fixture_id]
    recipient = envelope["to"][0].get("actorId") or envelope["to"][0]["actorKey"]
    inferred = (
        DeliveryLifecycle.DURABLE_SERVICE
        if str(recipient).startswith("channel:")
        else DeliveryLifecycle.ONLINE_ONLY
    )
    return InboxMessage(
        message_id=str(envelope["messageId"]),
        conversation_id=str(envelope["conversationId"]),
        sender=str(envelope["from"]["actorId"]),
        recipient=str(recipient),
        payload=json.dumps(envelope, separators=(",", ":")).encode(),
        intent=str(envelope["intent"]),
        lifecycle=lifecycle or inferred,
        idempotency_key=envelope.get("idempotencyKey"),
        created_at_ms=int(envelope["timestamp"]),
    )


class GoldenInboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "inbox.sqlite3"
        self.transport = MemoryDeliveryTransport()
        self.service = InboxService(self.db, self.transport)

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def test_wg_001_online_request_is_durable_before_delivery(self) -> None:
        item = message("request-basic")
        self.transport.set_online(item.recipient, True)
        self.transport.confirm_direct(item.message_id)
        result = self.service.submit(item, now_ms=item.created_at_ms)
        self.assertTrue(result.accepted)
        self.assertFalse(result.queued)
        self.assertEqual(self.service.outbox_count(), 0)
        self.assertEqual(self.transport.delivered, [item.message_id])

    def test_wg_007_message_id_deduplicates(self) -> None:
        item = message("dedup-message-id")
        first = self.service.receive(item, now_ms=item.created_at_ms)
        second = self.service.receive(item, now_ms=item.created_at_ms + 1)
        self.assertTrue(first.accepted)
        self.assertFalse(second.accepted)
        self.assertTrue(second.duplicate)
        self.assertEqual(self.service.pending_count(item.recipient), 1)

    def test_wg_008_idempotency_key_deduplicates_distinct_ids(self) -> None:
        first = message("dedup-key-a")
        second = message("dedup-key-b")
        self.assertTrue(
            self.service.receive(first, now_ms=first.created_at_ms).accepted
        )
        duplicate = self.service.receive(second, now_ms=first.created_at_ms + 1)
        self.assertFalse(duplicate.accepted)
        self.assertTrue(duplicate.duplicate)
        self.assertTrue(self.service.has_received(second.message_id))
        self.assertEqual(self.service.pending_count(first.recipient), 1)

    def test_wg_009_dedup_tombstone_expires(self) -> None:
        item = message("dedup-message-id")
        self.assertTrue(self.service.receive(item, now_ms=0).accepted)
        self.assertTrue(self.service.ack(item.recipient, item.message_id).acknowledged)
        after_window = self.service.dedup_window_ms + 1
        self.assertTrue(self.service.receive(item, now_ms=after_window).accepted)

    def test_wg_010_online_only_offline_fails_without_queue(self) -> None:
        item = message("online-only")
        result = self.service.submit(item, now_ms=item.created_at_ms)
        self.assertFalse(result.accepted)
        self.assertEqual(result.code, "TARGET_OFFLINE")
        self.assertFalse(result.queued)
        self.assertEqual(self.service.outbox_count(), 0)

    def test_wg_011_durable_service_offline_is_held_until_the_hold_ttl(self) -> None:
        item = message("durable-route")
        result = self.service.submit(item, now_ms=item.created_at_ms)
        self.assertTrue(result.accepted)
        self.assertTrue(result.queued)
        queued = self.service.outbox_item(item.message_id)
        # The published default hold remains bounded and expiry observable.
        self.assertEqual(queued.expires_at_ms - item.created_at_ms, DEFAULT_HOLD_TTL_MS)
        self.assertEqual(self.transport.delivered, [])

    def test_wg_012_queue_survives_service_restart_and_deduplicates_delivery(
        self,
    ) -> None:
        item = message("durable-route")
        self.service.submit(item, now_ms=0)
        self.service.close()
        self.transport.set_online(item.recipient, True)
        self.transport.confirm_direct(item.message_id)
        self.service = InboxService(self.db, self.transport)
        self.service.wake_outbox_recipient(item.recipient, now_ms=1_000)
        outcomes = self.service.retry_due(now_ms=1_000)
        self.assertEqual([result.message_id for result in outcomes], [item.message_id])
        self.assertEqual(self.service.outbox_count(), 0)
        self.assertEqual(self.transport.delivered.count(item.message_id), 1)

    def test_wg_013_fifo_exposes_only_head_until_ack(self) -> None:
        first = message("request-basic")
        second = message("request-second")
        self.service.receive(first, now_ms=0)
        self.service.receive(second, now_ms=1)
        self.assertEqual(
            self.service.next(first.recipient).message_id, first.message_id
        )
        self.service.ack(first.recipient, first.message_id)
        self.assertEqual(
            self.service.next(first.recipient).message_id, second.message_id
        )

    def test_wg_014_online_retries_continue_until_ttl(self) -> None:
        item = message("durable-route")
        self.transport.set_online(item.recipient, True)
        self.service.submit(item, now_ms=0)
        for _ in range(4):
            self.service.retry_due(
                now_ms=self.service.outbox_item(item.message_id).next_attempt_ms
            )
            self.assertEqual(self.service.dlq_count(), 0)
        self.assertEqual(self.service.outbox_item(item.message_id).attempts, 5)
        self.assertEqual(self.service.outbox_count(), 1)
        self.assertEqual(self.service.dlq_count(), 0)
        [expired] = self.service.retry_due(now_ms=DEFAULT_HOLD_TTL_MS)
        self.assertEqual(expired.code, "TTL_EXPIRED")
        self.assertEqual(self.service.outbox_count(), 0)
        self.assertEqual(self.service.dlq_count(), 1)

    def test_wg_015_capacity_rejects_without_eviction_or_dlq(self) -> None:
        self.service.close()
        self.service = InboxService(self.db, self.transport, max_inbox_items=1)
        first = message("durable-route")
        second = message("capacity-reject")
        self.assertTrue(self.service.receive(first, now_ms=0).accepted)
        rejected = self.service.receive(second, now_ms=1)
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.code, "DELIVERY_REJECTED")
        self.assertEqual(self.service.pending_count(first.recipient), 1)
        self.assertEqual(self.service.dlq_count(), 0)

    def test_wg_016_post_acceptance_failure_moves_to_dlq(self) -> None:
        item = message("postaccept-failure")
        self.service.receive(item, now_ms=0)
        result = self.service.fail(
            item.recipient, item.message_id, "unsupported payload"
        )
        self.assertEqual(result.code, "DELIVERY_FAILED_AFTER_ACCEPTANCE")
        self.assertEqual(result.reply_to, item.message_id)
        self.assertEqual(self.service.pending_count(item.recipient), 0)
        self.assertEqual(self.service.dlq_count(), 1)

    def test_wg_028_ack_is_not_settled_until_durable_commit(self) -> None:
        item = message("request-basic")
        self.service.receive(item, now_ms=0)
        self.assertFalse(self.service.is_acknowledged(item.message_id))
        self.assertTrue(self.service.ack(item.recipient, item.message_id).acknowledged)
        self.service.close()
        self.service = InboxService(self.db, self.transport)
        self.assertTrue(self.service.is_acknowledged(item.message_id))
        self.assertIsNone(self.service.next(item.recipient))

    def test_wg_029_inactive_ack_fails_explicitly(self) -> None:
        result = self.service.ack("agent:alice:mac:sender", "msg-request-basic")
        self.assertFalse(result.acknowledged)
        self.assertEqual(result.code, "MESSAGE_ACK_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
