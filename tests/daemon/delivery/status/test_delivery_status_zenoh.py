"""The terminal record is pullable over the mesh, after the fact.

Run against the real Zenoh transport on purpose.  A fake session would happily
accept a reply keyed by a wildcard expression; Zenoh will not, and a queryable
that only ever answered concrete keys in tests would fail the first time a
sender asked for everything it had sent.
"""

from __future__ import annotations

import json
import socket
import tempfile
import time
import unittest
from pathlib import Path

from hyprial.daemon.impl.inbox import (
    DEFAULT_HOLD_TTL_MS,
    DeliveryLifecycle,
    DeliveryStatus,
    DeliveryStatusEndpoint,
    HolderReplyCount,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
    StatusQueryReport,
    TerminalState,
    conflicting_message_ids,
    encode_status_frame,
    merge_delivery_status,
    query_delivery_status,
)
from hyprial.daemon.impl.transport import KeySpace, ZenohConfig, ZenohTransport

TTL_MS = DEFAULT_HOLD_TTL_MS
SENDER = "agent:alice:mac:sender"


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _message(message_id: str, *, recipient: str = "agent:bob:pc:worker") -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="dm:alice:bob",
        sender=SENDER,
        recipient=recipient,
        payload=b"opaque-frame",
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=0,
    )


class DeliveryStatusPullTests(unittest.TestCase):
    def test_a_sender_pulls_verdicts_recorded_while_it_was_away(self) -> None:
        """The sender is absent for the whole time the outcomes happen.

        Its transport is not even opened until both records already exist --
        which is the point: a verdict pushed once, at the moment it was
        decided, would have gone to nobody.
        """

        endpoint_address = f"tcp/127.0.0.1:{_port()}"
        holder_transport = ZenohTransport(ZenohConfig(listen=(endpoint_address,)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            holder = InboxService(
                root / "holder.sqlite3",
                MemoryDeliveryTransport(),
                node_id="node-holder",
            )
            # One message the recipient took, one the holder had to evict.
            holder.receive(_message("msg-taken"), now_ms=1_000)
            holder.accept_custody(
                _message("msg-evicted"), mailbox_node="node-holder", now_ms=1_000
            )
            holder.retry_custody_due(now_ms=1_000 + TTL_MS + 1)

            status_endpoint = DeliveryStatusEndpoint(
                holder_transport, holder.delivery_status, holder="node-holder"
            )
            sender_transport = ZenohTransport(
                ZenohConfig(connect=(endpoint_address,))
            )
            try:
                time.sleep(0.5)

                everything = self._await_records(sender_transport, expected=2)

                self.assertEqual(
                    {record.message_id: record.state for record in everything},
                    {
                        "msg-taken": TerminalState.FETCHED,
                        "msg-evicted": TerminalState.EXPIRED,
                    },
                )

                one = query_delivery_status(
                    sender_transport, SENDER, message_id="msg-evicted", timeout=3.0
                )
                self.assertEqual(len(one.records), 1)
                self.assertEqual(one.records[0].message_id, "msg-evicted")
                self.assertEqual(one.records[0].holder, "node-holder")
                # The holder answered, and the answer survived decoding: this
                # verdict rests on everything that replied.
                self.assertEqual(one.replies, 1)
                self.assertEqual(one.decoded, 1)
                self.assertEqual(one.undecodable, ())
                self.assertTrue(one.no_known_loss)
                self.assertEqual(one.holders, ("node-holder",))
                self.assertEqual(one.responded_holders, ("node-holder",))

                missing = query_delivery_status(
                    sender_transport, SENDER, message_id="msg-never", timeout=1.0
                )
                self.assertEqual(missing.records, ())
                # A named empty frame proves the holder answered and had no
                # better record; absence would be a different observation.
                self.assertEqual(missing.replies, 1)
                self.assertEqual(missing.responded_holders, ("node-holder",))
                self.assertTrue(missing.no_known_loss)
            finally:
                sender_transport.close()
                status_endpoint.close()
                holder.close()
                holder_transport.close()

    def _await_records(
        self, session: ZenohTransport, *, expected: int
    ) -> tuple[object, ...]:
        deadline = time.monotonic() + 5.0
        records: tuple[object, ...] = ()
        while time.monotonic() < deadline:
            records = query_delivery_status(session, SENDER, timeout=1.0).records
            if len(records) >= expected:
                return records
            time.sleep(0.1)
        self.fail(f"expected {expected} terminal records, saw {len(records)}")

    def test_two_holders_answering_one_query_both_reach_the_querier(self) -> None:
        """The set must contain every holder that answered, not one of them.

        Measured across containers on 2026-08-14: two nodes each logged that
        they had answered the same status query, and the querier reported one
        reply.  Zenoh's default consolidation keeps replies in a map keyed by
        key expression, and every queryable replies under the key that was
        queried -- so N holders collapse to 1, picked by arrival race, with no
        error anywhere.

        That is worse than losing an answer.  The merge rule "``fetched``
        beats ``expired``" is only sound over the complete set: drop the
        recipient's ``fetched`` and a delivered message is reported
        ``expired``, confidently.  This asserts what actually protects that
        rule -- both records present -- rather than the merged verdict, which
        looked perfectly healthy the whole time the set was half missing.
        """

        first_address = f"tcp/127.0.0.1:{_port()}"
        second_address = f"tcp/127.0.0.1:{_port()}"
        first_transport = ZenohTransport(ZenohConfig(listen=(first_address,)))
        second_transport = ZenohTransport(ZenohConfig(listen=(second_address,)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # The exact disagreement the merge exists to resolve: the
            # recipient committed the message, its receipt was lost, and the
            # sender's holder evicted its copy.
            recipient = InboxService(
                root / "recipient.sqlite3",
                MemoryDeliveryTransport(),
                node_id="node-recipient",
            )
            recipient.receive(_message("msg-contested"), now_ms=2_000)
            holder = InboxService(
                root / "holder.sqlite3",
                MemoryDeliveryTransport(),
                node_id="node-holder",
            )
            holder.accept_custody(
                _message("msg-contested"), mailbox_node="node-holder", now_ms=1_000
            )
            holder.retry_custody_due(now_ms=1_000 + TTL_MS + 1)

            endpoints = [
                DeliveryStatusEndpoint(
                    first_transport,
                    recipient.delivery_status,
                    holder="node-recipient",
                ),
                DeliveryStatusEndpoint(
                    second_transport,
                    holder.delivery_status,
                    holder="node-holder",
                ),
            ]
            querier = ZenohTransport(
                ZenohConfig(connect=(first_address, second_address))
            )
            try:
                report = self._await_report(querier, expected_holders=2)
                self.assertEqual(
                    report.holders, ("node-holder", "node-recipient")
                )
                self.assertEqual(
                    report.responded_holders, ("node-holder", "node-recipient")
                )
                self.assertGreaterEqual(report.replies, 2)
                self.assertEqual(report.undecodable, ())
                self.assertTrue(report.no_known_loss)
                # Both claims are present and they disagree -- which is the
                # only case where precedence decides anything.
                self.assertEqual(
                    {record.state for record in report.records},
                    {TerminalState.FETCHED, TerminalState.EXPIRED},
                )
                self.assertEqual(
                    conflicting_message_ids(report.records), ("msg-contested",)
                )
                # And with the whole set in hand, the recipient's first-hand
                # record is the one that survives the merge.
                merged = merge_delivery_status(report.records)
                self.assertEqual(len(merged), 1)
                self.assertEqual(merged[0].state, TerminalState.FETCHED)
                self.assertEqual(merged[0].holder, "node-recipient")
            finally:
                querier.close()
                for endpoint in endpoints:
                    endpoint.close()
                holder.close()
                recipient.close()
                second_transport.close()
                first_transport.close()

    def _await_report(
        self, session: ZenohTransport, *, expected_holders: int
    ) -> StatusQueryReport:
        deadline = time.monotonic() + 10.0
        report = StatusQueryReport()
        while time.monotonic() < deadline:
            report = query_delivery_status(
                session, SENDER, message_id="msg-contested", timeout=2.0
            )
            if len(report.holders) >= expected_holders:
                return report
            time.sleep(0.2)
        self.fail(
            f"expected {expected_holders} holders in the set, saw "
            f"{report.holders} from {report.replies} replies"
        )

    def test_two_daemons_with_one_name_are_counted_not_collapsed(self) -> None:
        """A duplicated node identity answers twice under one name.

        Two daemons sharing a node identity each serve ``msg/status/**`` and
        each answer the same query, under the same holder name.  Name-keyed
        diagnostics (``responded_holders``) collapse them to one entry, which
        is exactly how a 34-hour duplicate went unnoticed: this asserts the
        pull keeps the replies apart and counts them, so the diagnostic can
        say "this name answered twice" instead of "this name answered".
        """

        first_address = f"tcp/127.0.0.1:{_port()}"
        second_address = f"tcp/127.0.0.1:{_port()}"
        first_transport = ZenohTransport(ZenohConfig(listen=(first_address,)))
        second_transport = ZenohTransport(ZenohConfig(listen=(second_address,)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Two stores that have diverged -- the realistic shape: the copy
            # fell behind, so the same message has different verdicts behind
            # the same name.
            original = InboxService(
                root / "original.sqlite3",
                MemoryDeliveryTransport(),
                node_id="node-dup",
            )
            original.receive(_message("msg-contested"), now_ms=2_000)
            stale_copy = InboxService(
                root / "copy.sqlite3",
                MemoryDeliveryTransport(),
                node_id="node-dup",
            )
            stale_copy.accept_custody(
                _message("msg-contested"), mailbox_node="node-dup", now_ms=1_000
            )
            stale_copy.retry_custody_due(now_ms=1_000 + TTL_MS + 1)

            endpoints = [
                DeliveryStatusEndpoint(
                    first_transport,
                    original.delivery_status,
                    holder="node-dup",
                ),
                DeliveryStatusEndpoint(
                    second_transport,
                    stale_copy.delivery_status,
                    holder="node-dup",
                ),
            ]
            querier = ZenohTransport(
                ZenohConfig(connect=(first_address, second_address))
            )
            try:
                time.sleep(0.5)

                report = self._await_replies(querier, expected_replies=2)

                # The name-keyed view collapses both daemons into one
                # holder -- that is the blind spot this test pins.
                self.assertEqual(report.responded_holders, ("node-dup",))
                # The reply-counted view keeps them apart.
                self.assertEqual(
                    report.duplicate_holders,
                    (
                        HolderReplyCount(
                            holder="node-dup", replies=2, records=2
                        ),
                    ),
                )
                # And the two verdicts really did disagree behind one name.
                self.assertEqual(
                    conflicting_message_ids(report.records), ("msg-contested",)
                )
            finally:
                querier.close()
                for endpoint in endpoints:
                    endpoint.close()
                original.close()
                stale_copy.close()
                second_transport.close()
                first_transport.close()

    def _await_replies(
        self, session: ZenohTransport, *, expected_replies: int
    ) -> StatusQueryReport:
        deadline = time.monotonic() + 10.0
        report = StatusQueryReport()
        while time.monotonic() < deadline:
            report = query_delivery_status(
                session, SENDER, message_id="msg-contested", timeout=2.0
            )
            if report.replies >= expected_replies:
                return report
            time.sleep(0.2)
        self.fail(
            f"expected {expected_replies} replies under one name, saw "
            f"{report.replies} from {report.responded_holders}"
        )

    def test_old_decoder_ignores_the_additive_frame_holder(self) -> None:
        record = DeliveryStatus(
            message_id="msg-compatible",
            sender=SENDER,
            recipient="agent:bob:pc:worker",
            state=TerminalState.EXPIRED,
            holder="node-holder",
            reason="DELIVERY_EXPIRED",
            recorded_at_ms=1,
        )
        frame = encode_status_frame((record,), holder="node-holder")

        # This is the pre-change decoder shape: schema + records only.  The
        # additive envelope key must not make an older peer reject the frame.
        value = json.loads(frame)
        if value.get("schema") != "hyprial-delivery-status/v1":
            raise ValueError("unsupported delivery status frame schema")
        decoded = tuple(DeliveryStatus.from_json(item) for item in value["records"])

        self.assertEqual(decoded, (record,))
        self.assertEqual(value["holder"], "node-holder")

    def test_status_keys_stay_inside_the_msg_namespace(self) -> None:
        keys = KeySpace()
        self.assertEqual(
            keys.message_status_root(SENDER), f"hyprial/v1/msg/status/{SENDER}"
        )
        self.assertEqual(
            keys.message_status(SENDER, "msg-1"), f"hyprial/v1/msg/status/{SENDER}/msg-1"
        )
        self.assertEqual(keys.message_status_any(), "hyprial/v1/msg/status/**")
        # The agent-entity line owns registry/; the two never overlap.
        self.assertTrue(keys.registry("actor").startswith("hyprial/v1/registry/"))


if __name__ == "__main__":
    unittest.main()
