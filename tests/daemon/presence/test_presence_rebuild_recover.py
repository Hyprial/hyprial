"""A rebuild restores remote recipients omitted by liveliness history replay.

The outbound daemon gates delivery on actor presence.  If a rebuilt transport
misses an already-live remote token, a durable message remains in the outbox
with zero attempts even though the remote node is reachable.  The directory
must recheck presence after replay completes and make that recipient visible
again without requiring a new remote PUT.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
    RecipientWakeCoordinator,
)
from hyprial.daemon.impl.inbox.contracts.ports import (
    BoolMutationCompleted,
    WakeOutboxRecipientCommand,
)
from hyprial.daemon.impl.inbox.links.io import CorrelatedInboxEventRouter
from hyprial.kernel import PortAdmission
from hyprial.daemon.impl.transport import (
    KeySpace,
    LivelinessDirectory,
    TransportSample,
)
from hyprial.daemon.impl.transport.zenoh import presence as presence_module


RECIPIENT = "agent:owner:node-b:worker"


class Session:
    """Controllable transport seam for session rebuilds."""

    def __init__(self) -> None:
        self.generation = 1
        self.callbacks_complete = True
        self.live: set[str] = set()
        self.observers = []
        self.rebuild_hooks = []
        self.complete_hooks = []
        self.calls: list[tuple[str, float]] = []
        self.query_error: Exception | None = None

    def projection(self):
        return SimpleNamespace(
            generation=self.generation,
            closed=False,
            callbacks_complete=self.callbacks_complete,
        )

    def on_rebuild(self, callback):
        self.rebuild_hooks.append(callback)
        return lambda: self.rebuild_hooks.remove(callback)

    def on_rebuild_complete(self, callback):
        self.complete_hooks.append(callback)
        return lambda: self.complete_hooks.remove(callback)

    def observe_liveliness(self, key, callback, *, history=True):
        self.observers.append((key, callback))
        return SimpleNamespace(close=lambda: self.observers.remove((key, callback)))

    def get_liveliness(self, key, *, timeout=1.0):
        self.calls.append((key, timeout))
        if self.query_error is not None:
            raise self.query_error
        return [TransportSample(key, b"")] if key in self.live else []

    def sample(self, actor: str, kind: str = "put") -> TransportSample:
        return TransportSample(
            KeySpace().actor_liveliness(actor), b"", kind, self.generation
        )

    def put(self, directory: LivelinessDirectory, actor: str) -> None:
        self.live.add(KeySpace().actor_liveliness(actor))
        directory._actor_event(self.sample(actor))

    def begin_rebuild(self) -> None:
        """Fence callbacks and forget presence; replay deliberately has no PUT."""

        self.generation += 1
        self.callbacks_complete = False
        for hook in tuple(self.rebuild_hooks):
            hook()

    def finish_rebuild(self) -> None:
        self.callbacks_complete = True
        for hook in tuple(self.complete_hooks):
            hook()


class Delivery(MemoryDeliveryTransport):
    def __init__(self, presence: LivelinessDirectory) -> None:
        super().__init__()
        self.presence = presence

    def is_online(self, recipient: str) -> bool:
        return self.presence.actor_online(recipient)


def wait(predicate, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.005)


def actor_calls(session: Session) -> list[tuple[str, float]]:
    key = KeySpace().actor_liveliness(RECIPIENT)
    return [call for call in session.calls if call[0] == key]


class WakeSink:
    def __init__(
        self,
        sender: InboxService,
        router: CorrelatedInboxEventRouter,
    ) -> None:
        self.sender = sender
        self.router = router

    def submit(self, command: WakeOutboxRecipientCommand) -> PortAdmission:
        self.sender.wake_outbox_recipient(
            command.recipient,
            now_ms=command.now_ms,
        )
        self.sender.retry_due(now_ms=command.now_ms)
        self.router.publish(
            BoolMutationCompleted(
                command.correlation_id,
                1,
                1,
                "wake_outbox_recipient",
                True,
            )
        )
        return PortAdmission.ACCEPTED


class RecipientReader:
    def outbox_recipient_page(
        self,
        *,
        after: str | None = None,
        limit: int = 64,
    ) -> tuple[str, ...]:
        return (RECIPIENT,) if after is None else ()


def test_remote_outbox_recovers_after_rebuild_misses_presence(tmp_path) -> None:
    session = Session()
    directory = LivelinessDirectory(session)
    transport = Delivery(directory)
    sender = InboxService(tmp_path / "sender.sqlite3", transport)
    router = CorrelatedInboxEventRouter()
    wakes = RecipientWakeCoordinator(
        WakeSink(sender, router),
        router,
        RecipientReader(),
        directory.presence_projection,
        completion_timeout=0.01,
        clock_ms=lambda: 2,
    )
    unbind = directory.bind_actor_online(wakes.submit)
    message = InboxMessage(
        message_id="remote-after-rebuild",
        conversation_id="dm:sender:remote",
        sender="agent:owner:node-a:sender",
        recipient=RECIPIENT,
        payload=b"cross-node message",
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    try:
        session.put(directory, RECIPIENT)
        wait(lambda: directory.actor_online(RECIPIENT))
        session.begin_rebuild()

        transport.confirm_direct(message.message_id)
        result = sender.submit(message, now_ms=1)
        assert result.accepted and result.queued
        held = sender.outbox_item(message.message_id)
        assert held.attempts == 0

        session.finish_rebuild()
        wait(lambda: directory.actor_online(RECIPIENT))
        wait(lambda: sender.outbox_count() == 0)
        assert transport.delivered == [message.message_id]
    finally:
        unbind()
        assert wakes.close(1.0)
        sender.close()
        directory.close()


def test_rebuild_recheck_keeps_departed_actor_offline() -> None:
    session = Session()
    directory = LivelinessDirectory(session)
    try:
        session.put(directory, RECIPIENT)
        wait(lambda: directory.actor_online(RECIPIENT))
        session.live.clear()

        session.begin_rebuild()
        session.finish_rebuild()

        wait(lambda: actor_calls(session))
        time.sleep(0.05)
        assert not directory.actor_online(RECIPIENT)
    finally:
        directory.close()


def test_rebuild_recheck_is_bounded_and_fails_closed_on_query_failure() -> None:
    session = Session()
    directory = LivelinessDirectory(session)
    try:
        session.put(directory, RECIPIENT)
        wait(lambda: directory.actor_online(RECIPIENT))
        session.query_error = TimeoutError("native liveliness read unanswered")

        session.begin_rebuild()
        session.finish_rebuild()

        attempts = getattr(presence_module, "_REBUILD_RECONCILE_ATTEMPTS", 3)
        wait(lambda: len(actor_calls(session)) >= attempts)
        time.sleep(0.05)
        assert len(actor_calls(session)) == attempts
        assert not directory.actor_online(RECIPIENT)
    finally:
        directory.close()
