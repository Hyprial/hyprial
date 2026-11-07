"""Online recovery wakes must survive settlement of older failed sends."""

import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.inbox import DeliveryCustodyCoordinator, DeliveryCustodyFacade
from hyprial.daemon.impl.inbox.actor import DispatchIoCompleted
from hyprial.daemon.impl.inbox.links.io import CorrelatedInboxEventRouter
from hyprial.daemon.impl.inbox.memory import MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.contracts.ports import BoolMutationCompleted
from hyprial.daemon.impl.inbox.links.wake import RecipientWakeCoordinator
from hyprial.daemon.impl.transport import LivelinessDirectory
from tests.integration.actor_preservation.delivery.test_actor_preserved_outbox_final_attempt_wake import _PresenceSession, _message, RECIPIENT


class Events:
    def publish(self, event):
        pass


class PresenceProbe:
    def __init__(self, directory):
        self.directory = directory

    def actor_online(self, actor):
        return self.directory.actor_online(actor)

    def online_mailboxes(self):
        return ()


class SessionProbe:
    def __init__(self, session):
        self.session = session
        self.drop_frame = True
        self.native_publishes = 0
        self.native_acks = 0

    def __getattr__(self, name):
        return getattr(self.session, name)

    def put(self, key, payload):
        if not self.drop_frame:
            self.native_publishes += 1
            return self.session.put(key, payload)

    def get(self, key, **kwargs):
        samples = self.session.get(key, **kwargs)
        self.native_acks += any(s.payload == b"ack" for s in samples)
        return samples


def eventually(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.005)


class WakeRouter(CorrelatedInboxEventRouter):
    def __init__(self):
        super().__init__()
        self.wake_settled = threading.Event()
        self.wakes = []

    def publish(self, event):
        super().publish(event)
        if (
            isinstance(event, BoolMutationCompleted)
            and event.operation == "wake_outbox_recipient"
        ):
            self.wakes.append({"result": event.result, "version": event.version})
            self.wake_settled.set()


@pytest.mark.parametrize("native", [False, True], ids=["memory", "zenoh"])
@pytest.mark.parametrize(
    "wake_first",
    [True, False],
    ids=["wake-before-failure-settlement", "wake-after-failure-settlement"],
)
def test_online_wake_survives_inflight_failed_receipt(
    tmp_path, monkeypatch, wake_first, native
):
    import hyprial.daemon.impl.inbox.actor.internal.projection as projection_module
    import hyprial.daemon.impl.inbox.actor.internal.receipts as receipts_module
    import hyprial.daemon.impl.inbox.actor.worker as worker_module
    import hyprial.daemon.impl.inbox.actor.coordinator as coordinator_module
    import hyprial.daemon.impl.inbox.service.core as service_module

    clock = [time.time_ns() // 1_000_000]
    epoch = clock[0]
    fake_time = SimpleNamespace(
        time_ns=lambda: clock[0] * 1_000_000,
        monotonic=time.monotonic,
        sleep=time.sleep,
    )
    monkeypatch.setattr(worker_module, "time", fake_time)
    monkeypatch.setattr(coordinator_module, "time", fake_time)
    monkeypatch.setattr(receipts_module, "time", fake_time)
    monkeypatch.setattr(projection_module, "time", fake_time)
    monkeypatch.setattr(service_module, "time", fake_time)
    transport = MemoryDeliveryTransport()
    transport.set_online(RECIPIENT, True)
    receiver = endpoint = actor_token = a = b = None
    if native:
        import socket
        from hyprial.daemon.impl.transport import ZenohTransport, ZenohConfig, KeySpace
        from hyprial.daemon.impl.transport.session_actor import TransportSessionAuthority
        from hyprial.daemon.impl.inbox import ZenohDeliveryTransport, ZenohInboxEndpoint

        with socket.socket() as port:
            port.bind(("127.0.0.1", 0))
            address = f"tcp/127.0.0.1:{port.getsockname()[1]}"
        b = TransportSessionAuthority(ZenohTransport(ZenohConfig(listen=(address,))))
        a = TransportSessionAuthority(ZenohTransport(ZenohConfig(connect=(address,))))
        directory = LivelinessDirectory(a)
        wire = SessionProbe(a)
        wire.drop_frame = True
        transport = ZenohDeliveryTransport(
            wire, PresenceProbe(directory), origin_node="phase5-a"
        )
        rpath = tmp_path / "receiver.sqlite3"
        receiver = DeliveryCustodyFacade(
            DeliveryCustodyCoordinator(rpath, MemoryDeliveryTransport(), Events()),
            rpath,
        )
        endpoint = ZenohInboxEndpoint(b, receiver, RECIPIENT)
        actor_token = b.declare_liveliness(KeySpace().actor_liveliness(RECIPIENT))
        eventually(lambda: directory.actor_online(RECIPIENT))
    router = WakeRouter()
    path = tmp_path / "sender.sqlite3"
    owner = DeliveryCustodyCoordinator(path, transport, router)
    inbox = DeliveryCustodyFacade(owner, path)
    if not native:
        session = _PresenceSession()
        directory = LivelinessDirectory(session)
    wakes = RecipientWakeCoordinator(
        owner, router, inbox, directory.presence_projection, clock_ms=lambda: clock[0]
    )
    stop = None
    captured, release = threading.Event(), threading.Event()
    original_sink = owner._io_worker._completion_sink
    receipts, errors = [], []
    worker = None
    message = replace(
        _message("wake-receipt-order"),
        created_at_ms=epoch,
        expires_at_ms=epoch + 300_000,
    )
    observation = {"wake_first": wake_first, "native": native}

    def held_sink(request, receipt):
        if request.kind.value == "delivery" and any(i.retry for i in request.items):
            # The hold is armed only after three real failed attempts below.
            receipts.append(receipt)
            captured.set()
            assert release.wait(3), "held real completion not released"
        return original_sink(request, receipt)

    def retry():
        try:
            inbox.retry_due(now_ms=clock[0])
        except BaseException as error:
            errors.append(repr(error))

    try:
        assert inbox.submit(message).queued
        for _ in range(2):
            clock[0] = inbox.outbox_item(message.message_id).next_attempt_ms
            assert inbox.retry_due(now_ms=clock[0])[0].queued
        item = inbox.outbox_item(message.message_id)
        assert item.attempts == 3
        assert epoch < item.next_attempt_ms < message.expires_at_ms
        owner._io_worker._completion_sink = held_sink
        clock[0] = item.next_attempt_ms
        worker = threading.Thread(target=retry, name="phase5-held-real-receipt")
        worker.start()
        assert captured.wait(3), "no real fourth completion"
        receipt = receipts[0]
        assert isinstance(receipt, DispatchIoCompleted)
        observation["receipt"] = {
            "completed_at": receipt.completed_at_ms - epoch,
            "outcomes": [
                {
                    "kind": o.kind.value,
                    "recipient_online": o.recipient_online,
                    "direct_attempted": o.direct_attempted,
                }
                for o in receipt.outcomes
            ],
        }
        clock[0] = epoch + 37_000
        if native:
            # Arrange the actual native online transition after binding, rather
            # than manufacturing a wake or relying on a history replay.
            actor_token.close()
            actor_token = None
            eventually(lambda: not directory.actor_online(RECIPIENT))
            wire.drop_frame = False
        else:
            transport.confirm_direct(message.message_id)
        stop = directory.bind_actor_online(wakes.submit)
        if not wake_first:
            release.set()
            worker.join(3)
            assert not worker.is_alive() and not errors
        if native:
            actor_token = b.declare_liveliness(KeySpace().actor_liveliness(RECIPIENT))
        else:
            session.actor_event(RECIPIENT)
        assert router.wake_settled.wait(3)
        observation["after_wake_due"] = (
            inbox.outbox_item(message.message_id).next_attempt_ms - epoch
        )
        observation["wake_receipts"] = router.wakes
        if wake_first:
            release.set()
            worker.join(3)
            assert not worker.is_alive() and not errors
        owner._io_worker._completion_sink = original_sink
        item = inbox.outbox_item(message.message_id)
        observation["final_due"] = item.next_attempt_ms - epoch
        observation["attempts"] = item.attempts
        observation["online"] = directory.actor_online(RECIPIENT)
        clock[0] = epoch + 90_000
        result = inbox.retry_due(now_ms=clock[0])
        observation["retry_at_90"] = [
            {"queued": x.queued, "accepted": x.accepted} for x in result
        ]
        observation["outbox_at_90"] = inbox.outbox_count()
        if native:
            observation["receiver_has_received"] = receiver.has_received(
                message.message_id
            )
            observation["receiver_pending"] = [
                m.message_id for m in receiver.pending_messages(RECIPIENT)
            ]
            observation["native_publishes"] = wire.native_publishes
            observation["native_ack_seen"] = wire.native_acks > 0
            assert receiver.has_received(message.message_id), (
                "online wake lost: receiver still empty after recovery"
            )
        assert inbox.outbox_count() == 0, (
            "committed online wake was lost behind failed receipt settlement"
        )
    finally:
        release.set()
        if worker is not None:
            worker.join(3)
        owner._io_worker._completion_sink = original_sink
        if stop is not None:
            stop()
        observation["wake_close"] = wakes.close(1)
        directory.close()
        inbox.close()
        if actor_token is not None:
            actor_token.close()
        if endpoint is not None:
            endpoint.close()
        if receiver is not None:
            receiver.close()
        if a is not None:
            try:
                a.close(5)
            finally:
                b.close(5)
        observation["errors"] = errors
        (tmp_path / "observation.json").write_text(json.dumps(observation, indent=2))


@pytest.mark.parametrize(
    "outcome", ["queued", "error", "delivered", "expired", "pruned"]
)
def test_retained_wakes_coalesce_and_close_drains(tmp_path, monkeypatch, outcome):
    import hyprial.daemon.impl.inbox.actor.internal.projection as projection_module
    import hyprial.daemon.impl.inbox.actor.internal.receipts as receipts_module
    import hyprial.daemon.impl.inbox.actor.worker as worker_module
    import hyprial.daemon.impl.inbox.actor.coordinator as coordinator_module
    import hyprial.daemon.impl.inbox.service.core as service_module

    clock = [time.time_ns() // 1_000_000]
    start = clock[0]
    fake_time = SimpleNamespace(
        time_ns=lambda: clock[0] * 1_000_000,
        monotonic=time.monotonic,
        sleep=time.sleep,
    )
    monkeypatch.setattr(worker_module, "time", fake_time)
    monkeypatch.setattr(coordinator_module, "time", fake_time)
    monkeypatch.setattr(receipts_module, "time", fake_time)
    monkeypatch.setattr(projection_module, "time", fake_time)
    monkeypatch.setattr(service_module, "time", fake_time)
    entered, release = threading.Event(), threading.Event()
    transport = MemoryDeliveryTransport()
    transport.set_online(RECIPIENT, True)

    hold_budget_seconds = 60

    def held_deliver(message):
        entered.set()
        assert release.wait(hold_budget_seconds)
        if outcome == "error":
            raise OSError("controlled delivery failure")
        return outcome == "delivered"

    monkeypatch.setattr(transport, "deliver", held_deliver)
    path = tmp_path / "close.sqlite3"
    owner = DeliveryCustodyCoordinator(path, transport, Events())
    inbox = DeliveryCustodyFacade(owner, path)
    submit_inbox = DeliveryCustodyFacade(owner, path, timeout=hold_budget_seconds)
    message = replace(
        _message("held-close"), created_at_ms=start, expires_at_ms=start + 1000
    )
    results, errors = [], []

    def submit():
        try:
            results.append(submit_inbox.submit(message))
        except BaseException as error:
            errors.append(repr(error))

    thread = threading.Thread(target=submit)
    thread.start()
    try:
        assert entered.wait(3)
        for index in range(64):
            assert inbox.wake_outbox_recipient(RECIPIENT, now_ms=start + index)
        assert not inbox.wake_outbox_recipient(
            "agent:owner:node-c:unrelated", now_ms=start
        )
        [pending] = owner._active_handler._pending_io.values()
        assert pending.online_wakes == ((message.message_id, start + 63),)
        assert len(pending.online_wakes) <= len(pending.request.items)
        if outcome == "pruned":
            assert inbox.prune_outbox(undeliverable=lambda _: True, now_ms=start)
            assert inbox.outbox_count() == 0
        assert not owner.drain(0.01).complete
        assert owner._active_handler._pending_io
        if outcome == "expired":
            clock[0] = start + 2000
        release.set()
        thread.join(3)
        assert not thread.is_alive() and not errors
        assert len(results) == 1
        eventually(lambda: owner._active_handler._pending_io == {})
        if outcome in {"delivered", "pruned"}:
            assert inbox.outbox_count() == 0
        else:
            row = inbox.outbox_item(message.message_id)
            assert row.attempts == 1
            assert row.expires_at_ms == start + 1000
            if outcome == "expired":
                assert row.next_attempt_ms == start + 1000
            else:
                assert row.next_attempt_ms == start + 63
    finally:
        release.set()
        thread.join(3)
        assert owner.drain(2).complete
