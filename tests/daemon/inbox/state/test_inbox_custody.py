from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)


def _message(message_id: str = "msg-custody") -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="dm:alice:bob",
        sender="agent:alice:mac:sender",
        recipient="agent:bob:pc:worker",
        payload=b"opaque-irc-frame",
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=0,
    )


class MailboxCustodyTests(unittest.TestCase):
    def test_custody_ack_removes_sender_outbox_only_after_mailbox_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transport = MemoryDeliveryTransport()
            transport.advertise_mailbox("node-mailbox")
            transport.confirm_custody("msg-custody")
            service = InboxService(Path(directory) / "sender.sqlite3", transport)
            item = _message()
            result = service.submit(item, now_ms=0)
            self.assertTrue(result.accepted)
            self.assertFalse(result.queued)
            self.assertEqual(service.outbox_count(), 0)
            self.assertEqual(
                transport.custody_transfers,
                [("node-mailbox", item.message_id)],
            )
            service.close()

    def test_mailbox_custody_is_persistent_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "mailbox.sqlite3"
            service = InboxService(db, MemoryDeliveryTransport(), max_custody_bytes=20)
            item = _message()
            accepted = service.accept_custody(
                item, mailbox_node="node-mailbox", now_ms=0
            )
            self.assertTrue(accepted.acknowledged)
            self.assertEqual(service.custody_count(), 1)
            service.close()

            service = InboxService(db, MemoryDeliveryTransport(), max_custody_bytes=20)
            self.assertEqual(service.custody_count(), 1)
            too_large = _message("msg-too-large")
            too_large = too_large.with_payload(b"x" * 21)
            rejected = service.accept_custody(
                too_large, mailbox_node="node-mailbox", now_ms=1
            )
            self.assertFalse(rejected.acknowledged)
            self.assertEqual(rejected.code, "MAILBOX_CAPACITY_EXCEEDED")
            service.close()


if __name__ == "__main__":
    unittest.main()
