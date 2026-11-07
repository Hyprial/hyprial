from __future__ import annotations

import socket
import tempfile
import time
import unittest
from pathlib import Path

from tests.support.runtime._tmp import hyprial_test_tmp_root

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.correlation.correlation import CorrelationEventRouter
from hyprial.daemon.impl.route_registration import RouteRegistrationClient, RouteRegistrationIo
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryStatusEndpoint,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
    RetryPolicy,
    ZenohDeliveryTransport,
    ZenohInboxEndpoint,
    publish_fetch_receipt,
)
from hyprial.daemon.impl.inbox.tracking.progress import ProgressEvent, encode_progress_event
from hyprial.daemon.impl.transport import LivelinessDirectory, ZenohConfig, ZenohTransport
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies


class _Presence:
    def __init__(self, actor: str, mailbox: str | None = None) -> None:
        self.actor = actor
        self.mailbox = mailbox

    def actor_online(self, actor: str) -> bool:
        return actor == self.actor

    def online_mailboxes(self) -> tuple[str, ...]:
        return (self.mailbox,) if self.mailbox else ()


class _DiscardInboxEvents:
    def publish(self, _event: object) -> None:
        pass


class _CountingZenohDeliveryTransport(ZenohDeliveryTransport):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.delivery_attempts = 0

    def deliver(self, message: InboxMessage) -> bool:
        self.delivery_attempts += 1
        return super().deliver(message)


def _actor_owned_sender(
    database: Path,
    transport: ZenohDeliveryTransport,
) -> DeliveryCustodyFacade:
    coordinator = DeliveryCustodyCoordinator(
        database,
        transport,
        _DiscardInboxEvents(),
        retry_policy=RetryPolicy(),
        node_id="mac",
    )
    return DeliveryCustodyFacade(coordinator, database)


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class ZenohInboxIntegrationTests(unittest.TestCase):
    def test_ack_received_status_settles_restarted_sender_idempotently(
        self,
    ) -> None:
        """A durable recipient ACK retires the sender row after restart.

        The first receipt is lost, so the sender persists one failed attempt.
        The recipient then exposes its committed ``ACK_RECEIVED`` status while
        the sender is down.  The restarted sender must settle from that status
        before another delivery attempt, and later retry rounds stay inert.
        """

        endpoint = f"tcp/127.0.0.1:{_port()}"
        receiver_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
        sender_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actor = "agent:bob:pc:homepage-typewriter"
            receiver = InboxService(
                root / "receiver.sqlite3",
                MemoryDeliveryTransport(),
                node_id="pc",
            )
            status_endpoint = DeliveryStatusEndpoint(
                receiver_transport,
                receiver.delivery_status,
                holder="pc",
            )
            database = root / "sender.sqlite3"
            first_transport = _CountingZenohDeliveryTransport(
                sender_transport,
                _Presence(actor),
                receipt_timeout=0.25,
                origin_node="mac",
            )
            sender = _actor_owned_sender(database, first_transport)
            message = InboxMessage(
                message_id="msg-lost-ack-restart",
                conversation_id="conversation-fresh-from-send",
                sender="agent:alice:mac:introducer",
                recipient=actor,
                payload=b"committed despite lost ack",
                intent="request",
                lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                created_at_ms=time.time_ns() // 1_000_000,
            )
            try:
                time.sleep(0.5)
                submitted = sender.submit(message, now_ms=message.created_at_ms)
                self.assertTrue(submitted.accepted)
                self.assertTrue(submitted.queued)
                self.assertEqual(first_transport.delivery_attempts, 1)
                self.assertEqual(sender.outbox_item(message.message_id).attempts, 1)

                receiver.receive(message, now_ms=message.created_at_ms + 1)
                (committed,) = receiver.delivery_status_records(
                    message.sender, message_id=message.message_id
                )
                self.assertEqual(committed.reason, "ACK_RECEIVED")
                self.assertEqual(committed.holder, "pc")

                due_ms = sender.outbox_item(message.message_id).next_attempt_ms
                sender.close()
                restarted_transport = _CountingZenohDeliveryTransport(
                    sender_transport,
                    _Presence(actor),
                    receipt_timeout=0.25,
                    origin_node="mac",
                )
                sender = _actor_owned_sender(database, restarted_transport)

                [settled] = sender.retry_due(now_ms=due_ms)

                self.assertTrue(settled.accepted)
                self.assertFalse(settled.queued)
                self.assertEqual(restarted_transport.delivery_attempts, 0)
                self.assertEqual(sender.outbox_count(), 0)
                self.assertEqual(sender.dlq_count(), 0)

                self.assertEqual(sender.retry_due(now_ms=due_ms + 1), [])
                self.assertEqual(restarted_transport.delivery_attempts, 0)
                self.assertEqual(sender.outbox_count(), 0)
            finally:
                sender.close()
                status_endpoint.close()
                receiver.close()
                sender_transport.close()
                receiver_transport.close()

    def test_ack_status_requires_recipient_holder_case_insensitively(self) -> None:
        endpoint = f"tcp/127.0.0.1:{_port()}"
        receiver_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
        sender_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actor = "agent:bob:pc:homepage-typewriter"
            wrong_receiver = InboxService(
                root / "wrong-receiver.sqlite3",
                MemoryDeliveryTransport(),
                node_id="other-node",
            )
            wrong_status = DeliveryStatusEndpoint(
                receiver_transport,
                wrong_receiver.delivery_status,
                holder="other-node",
            )
            delivery = _CountingZenohDeliveryTransport(
                sender_transport,
                _Presence(actor),
                receipt_timeout=0.25,
                origin_node="mac",
            )
            sender = _actor_owned_sender(root / "sender.sqlite3", delivery)
            message = InboxMessage(
                message_id="msg-wrong-ack-holder",
                conversation_id="conversation-wrong-ack-holder",
                sender="agent:alice:mac:introducer",
                recipient=actor,
                payload=b"do not retire from another holder",
                intent="request",
                lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                created_at_ms=time.time_ns() // 1_000_000,
            )
            matching_receiver = None
            matching_status = None
            try:
                time.sleep(0.5)
                self.assertTrue(sender.submit(message, now_ms=message.created_at_ms).queued)
                wrong_receiver.receive(message, now_ms=message.created_at_ms + 1)

                self.assertFalse(delivery.ack_received(message))
                self.assertEqual(sender.outbox_count(), 1)
                due_ms = sender.outbox_item(message.message_id).next_attempt_ms
                [retried] = sender.retry_due(now_ms=due_ms)

                self.assertTrue(retried.queued)
                self.assertEqual(sender.outbox_count(), 1)
                self.assertEqual(sender.outbox_item(message.message_id).attempts, 2)
                self.assertEqual(delivery.delivery_attempts, 2)

                matching_receiver = InboxService(
                    root / "matching-receiver.sqlite3",
                    MemoryDeliveryTransport(),
                    node_id="PC",
                )
                matching_status = DeliveryStatusEndpoint(
                    receiver_transport,
                    matching_receiver.delivery_status,
                    holder="PC",
                )
                matching_receiver.receive(message, now_ms=message.created_at_ms + 2)
                self.assertTrue(delivery.ack_received(message))
            finally:
                if matching_status is not None:
                    matching_status.close()
                if matching_receiver is not None:
                    matching_receiver.close()
                sender.close()
                wrong_status.close()
                wrong_receiver.close()
                sender_transport.close()
                receiver_transport.close()

    def test_absent_ack_status_keeps_retrying_and_increments_attempts(
        self,
    ) -> None:
        """A retry continues until the recipient later reports its durable ACK."""

        endpoint = f"tcp/127.0.0.1:{_port()}"
        receiver_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
        sender_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actor = "agent:bob:pc:homepage-typewriter"
            receiver = InboxService(
                root / "receiver.sqlite3",
                MemoryDeliveryTransport(),
                node_id="pc",
            )
            status_endpoint = DeliveryStatusEndpoint(
                receiver_transport,
                receiver.delivery_status,
                holder="pc",
            )
            delivery = _CountingZenohDeliveryTransport(
                sender_transport,
                _Presence(actor),
                receipt_timeout=0.25,
                origin_node="mac",
            )
            sender = _actor_owned_sender(root / "sender.sqlite3", delivery)
            message = InboxMessage(
                message_id="msg-no-durable-ack",
                conversation_id="conversation-no-durable-ack",
                sender="agent:alice:mac:introducer",
                recipient=actor,
                payload=b"still needs delivery",
                intent="request",
                lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                created_at_ms=time.time_ns() // 1_000_000,
            )
            try:
                time.sleep(0.5)
                submitted = sender.submit(message, now_ms=message.created_at_ms)
                self.assertTrue(submitted.queued)
                self.assertEqual(delivery.delivery_attempts, 1)
                self.assertEqual(sender.outbox_item(message.message_id).attempts, 1)
                initial_status = receiver.delivery_status_records(
                    message.sender, message_id=message.message_id
                )
                self.assertFalse(
                    any(record.reason == "ACK_RECEIVED" for record in initial_status)
                )

                due_ms = sender.outbox_item(message.message_id).next_attempt_ms
                [retried] = sender.retry_due(now_ms=due_ms)

                self.assertTrue(retried.accepted)
                self.assertTrue(retried.queued)
                self.assertEqual(delivery.delivery_attempts, 2)
                retried_item = sender.outbox_item(message.message_id)
                self.assertIsNotNone(retried_item)
                self.assertEqual(retried_item.attempts, 2)
                self.assertEqual(sender.outbox_count(), 1)
                self.assertEqual(sender.dlq_count(), 0)

                receiver.receive(message, now_ms=due_ms + 1)
                (committed,) = receiver.delivery_status_records(
                    message.sender, message_id=message.message_id
                )
                self.assertEqual(committed.reason, "ACK_RECEIVED")

                next_due_ms = retried_item.next_attempt_ms
                [settled] = sender.retry_due(now_ms=next_due_ms)

                self.assertFalse(settled.queued)
                self.assertEqual(delivery.delivery_attempts, 2)
                self.assertEqual(sender.outbox_count(), 0)
                self.assertEqual(sender.dlq_count(), 0)

                self.assertEqual(sender.retry_due(now_ms=next_due_ms + 1), [])
                self.assertEqual(delivery.delivery_attempts, 2)
            finally:
                sender.close()
                status_endpoint.close()
                receiver.close()
                sender_transport.close()
                receiver_transport.close()

    def test_genuinely_undelivered_message_still_exhausts_and_alarms(
        self,
    ) -> None:
        endpoint = f"tcp/127.0.0.1:{_port()}"
        receiver_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
        sender_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receiver = InboxService(
                root / "receiver.sqlite3",
                MemoryDeliveryTransport(),
                node_id="pc",
            )
            status_endpoint = DeliveryStatusEndpoint(
                receiver_transport,
                receiver.delivery_status,
                holder="pc",
            )
            sender = _actor_owned_sender(
                root / "sender.sqlite3",
                ZenohDeliveryTransport(
                    sender_transport,
                    _Presence("different-actor"),
                    receipt_timeout=0.25,
                    origin_node="mac",
                ),
            )
            sent_at_ms = time.time_ns() // 1_000_000
            message = InboxMessage(
                message_id="msg-genuinely-undelivered",
                conversation_id="conversation-no-recipient",
                sender="agent:alice:mac:introducer",
                recipient="agent:bob:pc:missing",
                payload=b"nobody committed this",
                intent="request",
                lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                created_at_ms=sent_at_ms,
            )
            try:
                time.sleep(0.5)
                self.assertTrue(sender.submit(message, now_ms=sent_at_ms).queued)

                # Bounded offline rechecks (PR #1463 finding 1) replaced the
                # park-at-deadline deferral, so exhaustion is driven at the
                # row's own TTL deadline.
                sender.retry_due(
                    now_ms=sender.outbox_item(message.message_id).expires_at_ms
                )

                self.assertEqual(sender.outbox_count(), 0)
                self.assertEqual(sender.dlq_count(), 1)
                (failed,) = sender.delivery_status_records(
                    message.sender, message_id=message.message_id
                )
                self.assertEqual(failed.state.value, "expired")
                self.assertEqual(failed.reason, "TTL_EXPIRED")
                # The actor-owned sender writes the expiry alarm as a follow-up
                # effect after the DLQ commit; a slow host can read before it lands.
                deadline = time.monotonic() + 3.0
                notices = sender.system_notices(message.sender)
                while not notices and time.monotonic() < deadline:
                    time.sleep(0.02)
                    notices = sender.system_notices(message.sender)
                (alarm,) = notices
                self.assertIn(b"TTL_EXPIRED", alarm.payload)
            finally:
                sender.close()
                status_endpoint.close()
                receiver.close()
                sender_transport.close()
                receiver_transport.close()

    def test_remote_terminal_failure_reaches_sender_daemon_without_receipt(
        self,
    ) -> None:
        endpoint = f"tcp/127.0.0.1:{_port()}"
        sender_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
        holder_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sender = InboxService(
                root / "sender.sqlite3",
                MemoryDeliveryTransport(),
                node_id="mac",
            )
            sender_endpoint = ZenohInboxEndpoint(
                sender_transport,
                sender,
                "mac",
                notice_node="mac",
            )
            holder = InboxService(
                root / "holder.sqlite3",
                ZenohDeliveryTransport(holder_transport, _Presence("mac")),
                node_id="pc",
            )
            message = InboxMessage(
                message_id="msg-remote-failure",
                conversation_id="dm:alice:bob",
                sender="agent:alice:mac:sender",
                recipient="agent:bob:pc:worker",
                payload=b"opaque-proto-frame",
                intent="request",
                lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                created_at_ms=0,
            )
            try:
                time.sleep(0.5)
                holder.receive(message, now_ms=1)
                holder.fail(
                    message.recipient,
                    message.message_id,
                    "PROVIDER_BLOCKED",
                )

                deadline = time.monotonic() + 3.0
                while not sender.system_notices(message.sender):
                    if time.monotonic() >= deadline:
                        self.fail("remote system notice did not reach sender daemon")
                    time.sleep(0.05)
                (notice,) = sender.system_notices(message.sender)
                self.assertEqual(notice.intent, "system")
                self.assertIn(message.message_id.encode(), notice.payload)
                self.assertIn(b"PROVIDER_BLOCKED", notice.payload)
                self.assertEqual(sender.pending_count(message.sender), 0)
                self.assertFalse(sender.is_acknowledged(notice.message_id))
            finally:
                holder.close()
                sender_endpoint.close()
                sender.close()
                holder_transport.close()
                sender_transport.close()

    def test_registered_interactive_actor_gets_distinct_presence_and_inbox_route(
        self,
    ) -> None:
        endpoint = f"tcp/127.0.0.1:{_port()}"
        receiver_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
        sender_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
        with tempfile.TemporaryDirectory(dir=hyprial_test_tmp_root()) as directory:
            root = Path(directory)
            receiver = InboxService(
                root / "receiver.sqlite3", MemoryDeliveryTransport()
            )
            app = DaemonApplication(
                state_dir=root / "state",
                socket_path=root / "state" / "daemon.sock",
                node_id="host",
                owner="owner",
            **_deep_module_daemon_dependencies())
            app._transport = receiver_transport
            app._inbox = receiver
            app._harnesses = object()  # type: ignore[assignment]
            app._presence = object()  # type: ignore[assignment]
            app._endpoint = ZenohInboxEndpoint(
                receiver_transport, receiver, app.node_id
            )
            router = CorrelationEventRouter()

            def register_route(spec: object) -> tuple[object | None, object]:
                advertise = bool(getattr(spec, "advertise"))
                token = (
                    receiver_transport.declare_liveliness(
                        str(getattr(spec, "liveliness_key"))
                    )
                    if advertise
                    else None
                )
                route_endpoint = ZenohInboxEndpoint(
                    receiver_transport,
                    receiver,
                    str(getattr(spec, "route_id")),
                    declare_receipts=False,
                )
                return token, route_endpoint

            route_io = RouteRegistrationIo(
                receiver_transport,
                lambda _route: lambda _selector: b"ok",
                router,
                registration_factory=register_route,
            )
            app._route_registration = route_io
            app._routes = RouteRegistrationClient(route_io, router)
            app._lifecycle_router = router
            directory_view = LivelinessDirectory(sender_transport)
            sender = InboxService(
                root / "sender.sqlite3",
                ZenohDeliveryTransport(
                    sender_transport, directory_view, receipt_timeout=2.0
                ),
            )
            actor = "agent:owner:host:hq-adjutant"
            try:
                registration = app.handle(
                    "session.register",
                    {
                        # A raw/old MCP child may still send the short name;
                        # the daemon publishes only the canonical URI.
                        "actor": "hq-adjutant",
                        "sessionRef": "cc-session",
                        "cwd": str(root),
                        "command": ["claude"],
                        "source": "claude-channel",
                        "channelConfirmed": True,
                    },
                )
                self.assertEqual(registration["actor"], actor)
                deadline = time.monotonic() + 3.0
                while not directory_view.actor_online(actor):
                    if time.monotonic() >= deadline:
                        self.fail("interactive actor presence was not published")
                    time.sleep(0.05)
                message = InboxMessage(
                    message_id="msg-interactive-actor",
                    conversation_id="dm:sender:worker",
                    sender="agent:sender:host:test",
                    recipient=actor,
                    payload=b"channel wake",
                    intent="request",
                    lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                    created_at_ms=0,
                )

                result = sender.submit(message, now_ms=0)

                self.assertTrue(result.accepted)
                self.assertFalse(result.queued)
                self.assertEqual(receiver.next(actor), message)

                app._close_interactive_route(actor)
                # The agent row registered by session.register carries a
                # daemon-backed persona token (registration = deliverable),
                # so raw presence outlives the interactive route.  Drop the
                # persona route to exercise the interactive lifecycle below
                # in isolation; production keeps both layers.
                app._drop_persona_route(actor)  # type: ignore[attr-defined]
                deadline = time.monotonic() + 3.0
                while directory_view.actor_online(actor):
                    if time.monotonic() >= deadline:
                        self.fail("interactive actor presence did not withdraw")
                    time.sleep(0.05)
                app._restore_interactive_routes()
                # Persisted Channel registration is recovery intent, not proof
                # of a live stdio child. The route stays withdrawn until the
                # surviving child performs its ref-guarded refresh (legacy
                # protocol remains compatible, but never self-declares online
                # from desired state alone).
                time.sleep(0.1)
                self.assertFalse(directory_view.actor_online(actor))
                app.handle(
                    "session.refresh",
                    {"actor": actor, "sessionRef": "cc-session"},
                )
                deadline = time.monotonic() + 3.0
                while not directory_view.actor_online(actor):
                    if time.monotonic() >= deadline:
                        self.fail("persisted interactive actor route was not restored")
                    time.sleep(0.05)
            finally:
                sender.close()
                directory_view.close()
                app._close()
                receiver.close()
                sender_transport.close()
                receiver_transport.close()

    def test_cross_node_fetch_receipt_retires_offline_sender_outbox(self) -> None:
        endpoint = f"tcp/127.0.0.1:{_port()}"
        receiver_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
        sender_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actor = "agent:bob:pc:interactive"
            receiver = InboxService(
                root / "receiver.sqlite3", MemoryDeliveryTransport(), node_id="pc"
            )
            receiver_endpoint = ZenohInboxEndpoint(receiver_transport, receiver, actor)
            # Presence deliberately remains false: this is the reported hold
            # path, so convergence must not depend on a later direct publish.
            sender = InboxService(
                root / "sender.sqlite3",
                ZenohDeliveryTransport(
                    sender_transport,
                    _Presence("different-actor"),
                    receipt_timeout=0.25,
                ),
                node_id="mac",
            )
            sender_receipts = ZenohInboxEndpoint(
                sender_transport,
                sender,
                "mac",
                consume_fetch_receipts=True,
            )
            message = InboxMessage(
                message_id="msg-cross-node-fetch",
                conversation_id="dm:alice:bob",
                sender="agent:alice:mac:sender",
                recipient=actor,
                payload=b"pull me",
                intent="request",
                lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                created_at_ms=0,
            )
            try:
                time.sleep(0.5)
                self.assertTrue(sender.submit(message, now_ms=0).queued)
                receiver.receive(message, now_ms=1)

                sender.retry_due(now_ms=1_000)
                self.assertEqual(sender.outbox_count(), 1)
                self.assertFalse(receiver.has_fetched(message.message_id))

                self.assertEqual(
                    receiver.fetch_pending(actor, now_ms=1_500), (message,)
                )
                publish_fetch_receipt(receiver_transport, message)
                deadline = time.monotonic() + 2.0
                while sender.outbox_count():
                    if time.monotonic() >= deadline:
                        self.fail("fetch receipt did not retire remote sender outbox")
                    time.sleep(0.02)

                self.assertTrue(receiver.has_fetched(message.message_id))
                self.assertEqual(sender.outbox_count(), 0)
                self.assertEqual(sender.dlq_count(), 0)
                self.assertEqual(
                    sender.delivery_status_records(message.sender)[0].reason,
                    "ACK_RECEIVED",
                )
            finally:
                sender_receipts.close()
                sender.close()
                receiver_endpoint.close()
                receiver.close()
                sender_transport.close()
                receiver_transport.close()

    def test_missed_fetch_push_reaches_sender_ttl_without_second_query(self) -> None:
        endpoint = f"tcp/127.0.0.1:{_port()}"
        receiver_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
        sender_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actor = "agent:bob:pc:interactive"
            receiver = InboxService(
                root / "receiver.sqlite3", MemoryDeliveryTransport(), node_id="pc"
            )
            receiver_endpoint = ZenohInboxEndpoint(receiver_transport, receiver, actor)
            sender = InboxService(
                root / "sender.sqlite3",
                ZenohDeliveryTransport(
                    sender_transport,
                    _Presence("different-actor"),
                    receipt_timeout=0.25,
                ),
                node_id="mac",
            )
            message = InboxMessage(
                message_id="msg-missed-fetch-push",
                conversation_id="dm:alice:bob",
                sender="agent:alice:mac:sender",
                recipient=actor,
                payload=b"pull me after sender disconnect",
                intent="request",
                lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                created_at_ms=0,
            )
            try:
                time.sleep(0.5)
                self.assertTrue(sender.submit(message, now_ms=0).queued)
                receiver.receive(message, now_ms=1)
                receiver.fetch_pending(actor, now_ms=2)

                # No sender receipt subscriber existed when the fetch happened;
                # there is no separate terminal confirmation query.
                sender.retry_due(now_ms=sender.durable_ttl_ms)

                self.assertEqual(sender.outbox_count(), 0)
                self.assertEqual(sender.dlq_count(), 1)
                (expired,) = sender.delivery_status_records(
                    message.sender, message_id=message.message_id
                )
                self.assertEqual(expired.reason, "TTL_EXPIRED")
            finally:
                sender.close()
                receiver_endpoint.close()
                receiver.close()
                sender_transport.close()
                receiver_transport.close()

    def test_two_sessions_deliver_only_after_receiver_durable_receipt(self) -> None:
        endpoint = f"tcp/127.0.0.1:{_port()}"
        receiver_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
        sender_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receiver = InboxService(
                root / "receiver.sqlite3", MemoryDeliveryTransport()
            )
            endpoint_service = ZenohInboxEndpoint(
                receiver_transport, receiver, "agent:bob:pc:worker"
            )
            sender = InboxService(
                root / "sender.sqlite3",
                ZenohDeliveryTransport(
                    sender_transport,
                    _Presence("agent:bob:pc:worker"),
                    receipt_timeout=2.0,
                ),
            )
            try:
                time.sleep(0.5)
                message = InboxMessage(
                    message_id="msg-live-zenoh",
                    conversation_id="dm:alice:bob",
                    sender="agent:alice:mac:sender",
                    recipient="agent:bob:pc:worker",
                    payload=b"opaque-proto-frame",
                    intent="request",
                    lifecycle=DeliveryLifecycle.ONLINE_ONLY,
                    created_at_ms=0,
                )
                result = sender.submit(message, now_ms=0)
                self.assertTrue(result.accepted)
                self.assertFalse(result.queued)
                self.assertEqual(sender.outbox_count(), 0)
                self.assertEqual(receiver.next(message.recipient), message)
            finally:
                sender.close()
                endpoint_service.close()
                receiver.close()
                sender_transport.close()
                receiver_transport.close()

    def test_mailbox_custody_receipt_is_signed_after_persistence(self) -> None:
        endpoint = f"tcp/127.0.0.1:{_port()}"
        mailbox_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
        sender_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mailbox = InboxService(root / "mailbox.sqlite3", MemoryDeliveryTransport())
            mailbox_endpoint = ZenohInboxEndpoint(
                mailbox_transport,
                mailbox,
                "agent:mailbox:local:daemon",
                mailbox_node="node-mailbox",
            )
            sender = InboxService(
                root / "sender.sqlite3",
                ZenohDeliveryTransport(
                    sender_transport,
                    _Presence("offline-recipient", mailbox="node-mailbox"),
                    receipt_timeout=2.0,
                ),
            )
            try:
                time.sleep(0.5)
                message = InboxMessage(
                    message_id="msg-live-custody",
                    conversation_id="dm:alice:bob",
                    sender="agent:alice:mac:sender",
                    recipient="agent:bob:pc:worker",
                    payload=b"opaque-proto-frame",
                    intent="request",
                    lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                    created_at_ms=0,
                )
                result = sender.submit(message, now_ms=0)
                self.assertTrue(result.accepted)
                self.assertFalse(result.queued)
                self.assertEqual(result.custody_mailbox, "node-mailbox")
                self.assertEqual(sender.outbox_count(), 0)
                self.assertEqual(mailbox.custody_count(), 1)
            finally:
                sender.close()
                mailbox_endpoint.close()
                mailbox.close()
                sender_transport.close()
                mailbox_transport.close()


class ZenohProgressEventIntegrationTests(unittest.TestCase):
    """Route C hop 7+8+8b, cross-transport over a real TCP endpoint.

    The headline failure this guards: a subscription DECLARED in code but
    never WIRED at the construction site passes every unit test and never
    receives anything in production.
    """

    def _progress_message(self) -> InboxMessage:
        event = ProgressEvent(
            delivery_id="delivery-9",
            conversation_id="conversation-9",
            actor="agent:bob:pc:worker",
            harness="pi",
            phase="tool-call",
            seq=0,
            emitted_at_ms=1_000,
            summary="running read",
        )
        return InboxMessage(
            message_id="progress:delivery-9:0",
            conversation_id="conversation-9",
            sender="agent:bob:pc:worker",
            recipient="agent:alice:mac:sender",
            payload=encode_progress_event(event),
            intent="progress",
            lifecycle=DeliveryLifecycle.ONLINE_ONLY,
            created_at_ms=1_000,
        )

    def _wait(self, predicate, timeout: float = 3.0) -> bool:  # type: ignore[no-untyped-def]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_wired_progress_subscription_receives_events_across_transports(
        self,
    ) -> None:
        endpoint = f"tcp/127.0.0.1:{_port()}"
        receiver_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
        publisher_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receiver = InboxService(
                root / "receiver.sqlite3", MemoryDeliveryTransport()
            )
            receiver_endpoint = ZenohInboxEndpoint(
                receiver_transport,
                receiver,
                "mac",
                progress_node="mac",
            )
            publisher = ZenohDeliveryTransport(
                publisher_transport, _Presence("mac")
            )
            try:
                time.sleep(0.5)
                message = self._progress_message()
                self.assertTrue(publisher.deliver_progress("mac", message))

                self.assertTrue(
                    self._wait(
                        lambda: receiver.list_progress_events(
                            "agent:alice:mac:sender"
                        )
                    ),
                    "progress event did not reach the wired endpoint",
                )
                (stored,) = receiver.list_progress_events("agent:alice:mac:sender")
                self.assertEqual(stored.message_id, "progress:delivery-9:0")
                self.assertEqual(stored.intent, "progress")
                # And it did NOT leak into the prompt path's notice table.
                self.assertEqual(
                    receiver.system_notices("agent:alice:mac:sender"), ()
                )
            finally:
                receiver_endpoint.close()
                receiver.close()
                publisher_transport.close()
                receiver_transport.close()

    def test_unwired_endpoint_never_receives_progress_events(self) -> None:
        """The hop-8b regression guard: no progress_node, no subscription."""

        endpoint = f"tcp/127.0.0.1:{_port()}"
        receiver_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
        publisher_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receiver = InboxService(
                root / "receiver.sqlite3", MemoryDeliveryTransport()
            )
            # Same endpoint shape the actor/interactive constructors use:
            # no progress_node, deliberately.
            receiver_endpoint = ZenohInboxEndpoint(
                receiver_transport,
                receiver,
                "mac",
                notice_node="mac",
            )
            publisher = ZenohDeliveryTransport(
                publisher_transport, _Presence("mac")
            )
            try:
                time.sleep(0.5)
                self.assertTrue(
                    publisher.deliver_progress("mac", self._progress_message())
                )
                self.assertFalse(
                    self._wait(
                        lambda: receiver.list_progress_events(
                            "agent:alice:mac:sender"
                        ),
                        timeout=1.5,
                    ),
                    "an endpoint without progress_node must stay deaf",
                )
            finally:
                receiver_endpoint.close()
                receiver.close()
                publisher_transport.close()
                receiver_transport.close()


class _FakeRegistration:
    def close(self) -> None:
        pass


class _RecordingSession:
    """Captures declared subscription keys without any zenoh I/O."""

    def __init__(self) -> None:
        self.subscriptions: list[str] = []

    def subscribe(self, key: str, _callback: object) -> _FakeRegistration:
        self.subscriptions.append(key)
        return _FakeRegistration()

    def declare_queryable(self, _key: str, _handler: object) -> _FakeRegistration:
        return _FakeRegistration()


class ZenohProgressSubscriptionDeclarationTests(unittest.TestCase):
    def test_progress_node_controls_the_progress_subscription(self) -> None:
        service = InboxService(
            Path(tempfile.mkdtemp()) / "inbox.sqlite3", MemoryDeliveryTransport()
        )
        try:
            wired_session = _RecordingSession()
            ZenohInboxEndpoint(
                wired_session, service, "mac", progress_node="mac"  # type: ignore[arg-type]
            )
            self.assertIn("hyprial/v1/progress/mac/**", wired_session.subscriptions)

            deaf_session = _RecordingSession()
            ZenohInboxEndpoint(
                deaf_session, service, "mac", notice_node="mac"  # type: ignore[arg-type]
            )
            self.assertNotIn(
                "hyprial/v1/progress/mac/**", deaf_session.subscriptions
            )
            self.assertIn(
                "hyprial/v1/system-notice/mac/**", deaf_session.subscriptions
            )
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
