"""Final outbox attempt and recipient-online wake regressions."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication, _BoundedRecipientWakes
from hyprial.daemon.impl.inbox import DEFAULT_HOLD_TTL_MS, DeliveryCustodyCoordinator, DeliveryCustodyFacade
from hyprial.daemon.impl.inbox.actor.custody import DeliveryCustody
from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.memory import MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.contracts.ports import WakeOutboxRecipientCommand
from hyprial.daemon.impl.inbox.service import InboxService
from hyprial.daemon.impl.transport.api import TransportSample
from hyprial.daemon.impl.transport.keys import KeySpace
from hyprial.daemon.impl.transport.zenoh.presence import LivelinessDirectory

SENDER = "agent:owner:node-a:sender"
RECIPIENT = "agent:owner:node-b:recipient"
OTHER_RECIPIENT = "agent:owner:node-c:other"


def _message(
    message_id: str,
    recipient: str = RECIPIENT,
    *,
    expires_at_ms: int | None = None,
) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="conversation-final-attempt",
        sender=SENDER,
        recipient=recipient,
        payload=b'{"message":"keep trying"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=0,
        expires_at_ms=expires_at_ms,
    )


def _advance_beyond_old_attempt_budget(service: object, message_id: str) -> None:
    for _ in range(6):
        due = service.outbox_item(message_id).next_attempt_ms  # type: ignore[attr-defined]
        service.retry_due(now_ms=due)  # type: ignore[attr-defined]
    assert service.outbox_item(message_id).attempts == 6  # type: ignore[attr-defined]


def test_reported_timeline_last_round_delivers_when_recipient_is_online(
    tmp_path: Path,
) -> None:
    """Reported 2026-09-28 timeline: retries at 00:05:21, 00:05:55, and
    00:07:55; recipient online at 00:14; terminal failure at 00:18:03 without
    an intervening delivery attempt.  The scheduled last round must try first.
    """

    transport = MemoryDeliveryTransport()
    service = InboxService(tmp_path / "inbox.sqlite3", transport)
    try:
        transport.set_online(RECIPIENT, True)
        service.submit(_message("final-online"), now_ms=0, defer_direct=True)
        _advance_beyond_old_attempt_budget(service, "final-online")
        transport.confirm_direct("final-online")

        [result] = service.retry_due(
            now_ms=service.outbox_item("final-online").next_attempt_ms
        )

        assert result.accepted is True
        assert result.queued is False
        assert result.code is None
        assert transport.delivered == ["final-online"] * 7
        assert service.outbox_count() == 0
        assert service.dlq_count() == 0
    finally:
        service.close()


def test_actor_authority_last_round_matches_the_service_contract(tmp_path: Path) -> None:
    transport = MemoryDeliveryTransport()
    events = _Events()
    database = tmp_path / "authority.sqlite3"
    coordinator = DeliveryCustodyCoordinator(database, transport, events)
    facade = DeliveryCustodyFacade(coordinator, database)
    try:
        now_ms = time.time_ns() // 1_000_000
        transport.set_online(RECIPIENT, True)
        facade.submit(
            _message("authority-final"), now_ms=now_ms, defer_direct=True
        )
        _advance_beyond_old_attempt_budget(facade, "authority-final")
        transport.confirm_direct("authority-final")

        [result] = facade.retry_due(
            now_ms=facade.outbox_item("authority-final").next_attempt_ms
        )

        assert result.accepted is True
        assert result.queued is False
        assert result.code is None
        assert transport.delivered == ["authority-final"] * 7
        assert facade.outbox_count() == 0
        assert facade.dlq_count() == 0
    finally:
        facade.close()


def test_offline_row_stays_parked_until_ttl(tmp_path: Path) -> None:
    transport = MemoryDeliveryTransport()
    service = InboxService(tmp_path / "inbox.sqlite3", transport)
    try:
        service.submit(_message("final-offline"), now_ms=0)
        held = service.outbox_item("final-offline")
        assert held.attempts == 0
        [result] = service.retry_due(now_ms=held.expires_at_ms)

        assert result.accepted is False
        assert result.code == "TTL_EXPIRED"
        assert transport.delivered == []
        assert transport.custody_transfers == []
        assert service.outbox_count() == 0
        assert service.dlq_count() == 1
    finally:
        service.close()


def test_expired_row_is_never_delivered(tmp_path: Path) -> None:
    transport = MemoryDeliveryTransport()
    service = InboxService(tmp_path / "inbox.sqlite3", transport)
    try:
        service.submit(_message("expired", expires_at_ms=500), now_ms=0)
        transport.set_online(RECIPIENT, True)
        transport.confirm_direct("expired")

        [result] = service.retry_due(now_ms=500)

        assert result.accepted is False
        assert result.code == "TTL_EXPIRED"
        assert transport.delivered == []
        assert service.outbox_count() == 0
        assert service.dlq_count() == 1
    finally:
        service.close()


class _Events:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.items: list[object] = []

    def publish(self, event: object) -> None:
        with self._lock:
            self.items.append(event)


class _Registration:
    def close(self) -> None:
        return None


class _PresenceSession:
    def __init__(self) -> None:
        self._live: set[str] = set()
        self._actor_callback: Callable[[TransportSample], None] | None = None

    def on_rebuild(self, _callback: Callable[[], None]) -> Callable[[], None]:
        return lambda: None

    def observe_liveliness(
        self,
        key_expr: str,
        callback: Callable[[TransportSample], None],
        *,
        history: bool = True,
    ) -> _Registration:
        del history
        if key_expr.endswith("/liveliness/actor/*"):
            self._actor_callback = callback
        return _Registration()

    def get_liveliness(self, key: str, *, timeout: float = 1.0):
        return [TransportSample(key, b"")] if key in self._live else []

    def actor_event(self, actor: str, *, kind: str = "put") -> None:
        key = KeySpace().actor_liveliness(actor)
        if kind == "delete":
            self._live.discard(key)
        else:
            self._live.add(key)
        assert self._actor_callback is not None
        self._actor_callback(
            TransportSample(KeySpace().actor_liveliness(actor), b"", kind=kind)
        )
        # The actor adapter admits asynchronously. Keep this fixture's original
        # synchronous event boundary by joining only the Presence owner, never
        # the Inbox worker whose nonblocking admission is tested below.
        authority = self._actor_callback.__self__._authority
        deadline = time.monotonic() + 1.0
        while True:
            state = authority._runtime.snapshot(authority._handle)
            if state.queued == 0 and state.in_flight == 0:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("presence fixture event did not settle")
            time.sleep(0.001)


def _wake_application(facade: DeliveryCustodyFacade) -> DaemonApplication:
    application = object.__new__(DaemonApplication)
    application._inbox = facade
    application._outbox_recipient_wakes = _BoundedRecipientWakes()
    return application


def test_online_callback_does_not_wait_on_inbox_authority_and_close_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wake_entered = threading.Event()
    release_wake = threading.Event()
    callback_started = threading.Event()
    callback_returned = threading.Event()
    close_started = threading.Event()
    close_returned = threading.Event()
    callback_errors: list[BaseException] = []
    close_errors: list[BaseException] = []
    original_handle = DeliveryCustody._handle

    def stall_wake(self: DeliveryCustody, command: object) -> None:
        if isinstance(command, WakeOutboxRecipientCommand):
            wake_entered.set()
            assert release_wake.wait(1.0)
        original_handle(self, command)

    monkeypatch.setattr(DeliveryCustody, "_handle", stall_wake)
    transport = MemoryDeliveryTransport()
    database = tmp_path / "nonblocking-wake.sqlite3"
    coordinator = DeliveryCustodyCoordinator(database, transport, _Events())
    facade = DeliveryCustodyFacade(coordinator, database)
    application = _wake_application(facade)
    presence = _PresenceSession()
    directory = LivelinessDirectory(presence)  # type: ignore[arg-type]
    application._bind_outbox_recipient_wake(directory, facade)

    def announce_online() -> None:
        callback_started.set()
        try:
            presence.actor_event(RECIPIENT)
        except BaseException as error:
            callback_errors.append(error)
        finally:
            callback_returned.set()

    def close_inbox() -> None:
        close_started.set()
        try:
            facade.close()
        except BaseException as error:
            close_errors.append(error)
        finally:
            close_returned.set()

    callback_thread = threading.Thread(target=announce_online, daemon=True)
    close_thread = threading.Thread(target=close_inbox, daemon=True)
    callback_thread.start()
    assert callback_started.wait(1.0)
    callback_prompt = callback_returned.wait(0.2)
    close_thread.start()
    assert close_started.wait(1.0)
    close_prompt = close_returned.wait(0.2)
    release_wake.set()
    callback_thread.join(timeout=1.0)
    close_thread.join(timeout=1.0)
    directory.close()

    assert callback_prompt, "online callback waited on the inbox authority"
    assert close_prompt, "inbox close queued behind an online wake"
    assert callback_errors == []
    assert close_errors == []


def test_online_callback_wake_is_applied_and_delivered_by_next_pump(
    tmp_path: Path,
) -> None:
    transport = MemoryDeliveryTransport()
    database = tmp_path / "deferred-wake.sqlite3"
    coordinator = DeliveryCustodyCoordinator(database, transport, _Events())
    facade = DeliveryCustodyFacade(coordinator, database)
    application = _wake_application(facade)
    presence = _PresenceSession()
    directory = LivelinessDirectory(presence)  # type: ignore[arg-type]
    try:
        application._bind_outbox_recipient_wake(directory, facade)
        facade.submit(_message("deferred-wake"), now_ms=0, defer_direct=True)
        facade.retry_due(now_ms=0)

        presence.actor_event(RECIPIENT)

        assert facade.outbox_item("deferred-wake").next_attempt_ms == DEFAULT_HOLD_TTL_MS
        application._flush_outbox_recipient_wakes(now_ms=250)
        assert facade.outbox_item("deferred-wake").next_attempt_ms == 250

        transport.set_online(RECIPIENT, True)
        transport.confirm_direct("deferred-wake")
        [result] = facade.retry_due(now_ms=250)

        assert result.accepted is True
        assert result.queued is False
        assert transport.delivered == ["deferred-wake"]
        assert facade.outbox_count() == 0
    finally:
        directory.close()
        facade.close()


def test_online_transition_wakes_only_that_recipients_unexpired_rows(
    tmp_path: Path,
) -> None:
    transport = MemoryDeliveryTransport()
    events = _Events()
    database = tmp_path / "wake.sqlite3"
    coordinator = DeliveryCustodyCoordinator(database, transport, events)
    facade = DeliveryCustodyFacade(coordinator, database)
    presence = _PresenceSession()
    wakes: list[str] = []

    def wake(recipient: str) -> None:
        wakes.append(recipient)
        facade.wake_outbox_recipient(recipient, now_ms=250)

    directory: LivelinessDirectory | None = None
    try:
        directory = LivelinessDirectory(  # type: ignore[arg-type,call-arg]
            presence,
            on_actor_online=wake,
        )
        facade.submit(_message("target"), now_ms=0, defer_direct=True)
        facade.submit(
            _message("target-expired", expires_at_ms=200),
            now_ms=0,
            defer_direct=True,
        )
        facade.submit(
            _message("other", OTHER_RECIPIENT),
            now_ms=0,
            defer_direct=True,
        )
        facade.retry_due(now_ms=0)
        before = {
            item.message.message_id: item.next_attempt_ms
            for item in facade.outbox_items()
        }
        assert before == {
            "target": DEFAULT_HOLD_TTL_MS,
            "target-expired": 200,
            "other": DEFAULT_HOLD_TTL_MS,
        }

        presence.actor_event(RECIPIENT)

        after_first = {
            item.message.message_id: item.next_attempt_ms
            for item in facade.outbox_items()
        }
        assert after_first == {
            "target": 250,
            "target-expired": 200,
            "other": DEFAULT_HOLD_TTL_MS,
        }
        assert wakes == [RECIPIENT]

        presence.actor_event(RECIPIENT)

        assert {
            item.message.message_id: item.next_attempt_ms
            for item in facade.outbox_items()
        } == after_first
        assert wakes == [RECIPIENT]
    finally:
        if directory is not None:
            directory.close()
        facade.close()


def test_late_callback_binding_replays_online_actor_once() -> None:
    presence = _PresenceSession()
    directory = LivelinessDirectory(presence)  # type: ignore[arg-type]
    wakes: list[str] = []
    try:
        presence.actor_event(RECIPIENT)

        directory.set_actor_online_callback(wakes.append)
        presence.actor_event(RECIPIENT)

        assert wakes == [RECIPIENT]
    finally:
        directory.close()
