"""Acking an already-settled message is success, not an error.

2026-09-18: ``MESSAGE_ACK_UNAVAILABLE`` is returned for three states whose
correct handling is opposite:

* the row does not exist            -> a real failure; the message may be lost
* the row exists and is settled     -> the caller's goal already holds
* a terminal failure settlement     -> the message is closed another way

Callers cannot tell them apart, so the second one reads as "your ack did not
work". That misreading has already been paid for once: it was the most likely
source of the withdrawn "ack never takes effect" diagnosis, which three agents
adopted and built a hypothesis space on. An interface that returns an error
for a synonym of success keeps re-manufacturing the same wrong diagnosis.

Measured on this host (read-only query by kanban, three rows, one recipient):
the rows were present with ``consumed = 1``, acknowledged 4-6 seconds after
receipt -- i.e. the write HAD landed -- while the caller was told the ack was
unavailable.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)


def _message(message_id: str, recipient: str) -> InboxMessage:
    payload = {"messageId": message_id, "to": [{"actorId": recipient}]}
    return InboxMessage(
        message_id=message_id,
        conversation_id="conv-ack-idempotence",
        sender="agent:sender:host:sender",
        recipient=recipient,
        payload=json.dumps(payload, separators=(",", ":")).encode(),
        intent="reply",
        lifecycle=DeliveryLifecycle.ONLINE_ONLY,
        idempotency_key=None,
        created_at_ms=0,
    )


class AckIdempotenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "inbox.sqlite3"
        self.transport = MemoryDeliveryTransport()
        self.service = InboxService(self.db, self.transport)
        self.addCleanup(self.service.close)
        self.recipient = "agent:alice:mac:worker"

    def test_acking_an_already_settled_message_reports_success(self) -> None:
        """⭐ The defect: a second ack is told the ack is unavailable.

        The caller's goal -- "this message is settled" -- holds after both
        calls. Reporting the second as an error makes a correct, idempotent
        retry indistinguishable from "the row is gone", and the two demand
        opposite responses (do nothing vs. raise an alarm).
        """

        item = _message("msg-ack-twice", self.recipient)
        self.service.receive(item, now_ms=0)

        first = self.service.ack(self.recipient, item.message_id)
        self.assertTrue(first.acknowledged, "the first ack must settle the row")

        second = self.service.ack(self.recipient, item.message_id)

        self.assertTrue(
            second.acknowledged,
            "acking a settled message is a no-op that already achieved its goal; "
            f"got acknowledged={second.acknowledged} code={second.code!r}",
        )
        self.assertNotEqual(
            second.code,
            "MESSAGE_ACK_UNAVAILABLE",
            "'already settled' must not share a code with 'no such row' -- the "
            "caller has to act differently on each",
        )

    def test_a_message_that_was_never_received_still_fails(self) -> None:
        """⛔ The negative control this fix must NOT weaken.

        If making the settled case succeed also made the missing case succeed,
        a genuinely lost message would be reported as handled -- the exact
        failure the error code exists to surface.
        """

        result = self.service.ack(self.recipient, "msg-never-delivered")

        self.assertFalse(result.acknowledged)
        self.assertEqual(result.code, "MESSAGE_ACK_UNAVAILABLE")

    def test_the_settled_case_is_distinguishable_from_the_missing_case(self) -> None:
        """The two must not be merged back together by a later edit."""

        item = _message("msg-distinguish", self.recipient)
        self.service.receive(item, now_ms=0)
        self.service.ack(self.recipient, item.message_id)

        settled = self.service.ack(self.recipient, item.message_id)
        missing = self.service.ack(self.recipient, "msg-absent")

        self.assertNotEqual(
            (settled.acknowledged, settled.code),
            (missing.acknowledged, missing.code),
            "settled and missing must differ in what the caller observes",
        )

    def test_the_settled_branch_is_scoped_to_the_recipient(self) -> None:
        """⛔ The control this specific fix could plausibly break.

        ``daemon/application.py`` handles ``message.ack`` by calling this
        method once per candidate spelling of the consumer key, stopping at
        the first that reports success::

            result = self._inbox.ack(actor, message_id)
            if not result.acknowledged:
                for key in self._message_consumer_keys(params)[1:]:
                    result = self._inbox.ack(key, message_id)
                    if result.acknowledged:
                        break

        So "settled" answering for the wrong recipient would not merely return
        a cosmetically wrong code -- it would let that loop report one actor's
        message as acked on behalf of another. The new query keeps
        ``recipient = ?``; this pins that it must.
        """

        item = _message("msg-scoped", self.recipient)
        self.service.receive(item, now_ms=0)
        self.service.ack(self.recipient, item.message_id)

        other = self.service.ack("agent:bob:mac:worker", item.message_id)

        self.assertFalse(
            other.acknowledged,
            "a settled row belongs to its recipient; another actor acking it "
            f"must not succeed, got code={other.code!r}",
        )
        self.assertEqual(other.code, "MESSAGE_ACK_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
