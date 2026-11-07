"""An unordered DELETE is checked against current tokens, not guessed old/new."""

import queue
import socket
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.daemon.impl.transport import KeySpace, LivelinessDirectory, TransportSample, ZenohConfig, ZenohTransport
from hyprial.daemon.impl.transport.presence_actor import PresenceAuthority


HOST = "node-b"
DRIVER = "agent:owner:node-b:driver"


class Session:
    def __init__(self):
        self.generation = 1
        self.live = set()
        self.hooks = []
        self.calls = []
        self.error = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def projection(self):
        return SimpleNamespace(generation=self.generation, closed=False, callbacks_complete=True)

    def on_rebuild(self, callback):
        self.hooks.append(callback)
        return lambda: self.hooks.remove(callback)

    def observe_liveliness(self, key, callback, *, history=True):
        return SimpleNamespace(close=lambda: None)

    def get_liveliness(self, key, *, timeout=1.0):
        self.calls.append((key, timeout, threading.current_thread().name))
        captured = key in self.live
        self.entered.set()
        assert self.release.wait(3), "query control never released"
        if self.error is not None:
            raise self.error
        return [TransportSample(key, b"")] if captured else []

    def sample(self, actor, kind="put"):
        return TransportSample(KeySpace().actor_liveliness(actor), b"", kind, self.generation)

    def put(self, directory, actor):
        self.live.add(KeySpace().actor_liveliness(actor))
        directory._actor_event(self.sample(actor))

    def rebuild(self):
        self.generation += 1
        for hook in tuple(self.hooks):
            hook()


def wait(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.001)


def drain(directory):
    # A FIFO owner barrier, followed by acknowledgement of all query custody.
    # The optional lane lookup is ONLY to run this same regression on old dev.
    marker = f"barrier-{time.monotonic_ns()}"
    assert directory._authority.change("mailbox", marker, True) is AdmissionResult.ACCEPTED
    wait(lambda: marker in directory._authority.projection().mailboxes)
    lane = getattr(directory._authority, "_delete_effects", None)
    if lane is not None:
        wait(lambda: lane.snapshot().outstanding == 0)


@pytest.mark.parametrize("identity", [HOST, DRIVER])
def test_stale_delete_preserves_current_live_token(identity):
    session = Session()
    directory = LivelinessDirectory(session)
    try:
        session.put(directory, identity)
        drain(directory)
        # Old DELETE is captured before a later PUT, then delivered after it.
        old_delete = session.sample(identity, "delete")
        session.put(directory, identity)
        drain(directory)
        directory._actor_event(old_delete)
        drain(directory)
        assert directory.actor_online(identity), "old DELETE replaced a newer live token"
        assert len(session.calls) == 1
        assert session.calls[0][2].startswith("presence-delete-reconcile")
        assert 0 < session.calls[0][1] <= 1.0
    finally:
        directory.close()


def test_real_delete_accepts_offline_and_new_put_recovers():
    session = Session()
    directory = LivelinessDirectory(session)
    try:
        session.put(directory, DRIVER)
        drain(directory)
        session.live.clear()
        directory._actor_event(session.sample(DRIVER, "delete"))
        drain(directory)
        assert not directory.actor_online(DRIVER)
        session.put(directory, DRIVER)
        drain(directory)
        assert directory.actor_online(DRIVER)
    finally:
        directory.close()


@pytest.mark.parametrize("failure", [TimeoutError("query deadline"), OSError("query failed")])
def test_query_failure_accepts_offline(failure):
    session = Session()
    directory = LivelinessDirectory(session)
    try:
        session.put(directory, DRIVER)
        drain(directory)
        session.error = failure
        directory._actor_event(session.sample(DRIVER, "delete"))
        drain(directory)
        assert not directory.actor_online(DRIVER)
    finally:
        directory.close()


def test_query_overload_accepts_offline(monkeypatch):
    session = Session()
    directory = LivelinessDirectory(session)
    try:
        session.put(directory, DRIVER)
        drain(directory)
        monkeypatch.setattr(directory._authority._delete_effects, "submit", lambda _: AdmissionResult.OVERLOADED)
        directory._actor_event(session.sample(DRIVER, "delete"))
        drain(directory)
        assert not directory.actor_online(DRIVER)
        assert not session.calls
    finally:
        directory.close()


@pytest.mark.parametrize("old_result", ["empty", "timeout"])
def test_new_put_fences_inflight_negative_query_and_mailbox_keeps_progressing(old_result):
    session = Session()
    directory = LivelinessDirectory(session)
    transitions = []
    stop = directory.bind_actor_online(transitions.append)
    try:
        session.put(directory, DRIVER)
        drain(directory)
        session.live.clear()
        if old_result == "timeout":
            session.error = TimeoutError("old query deadline")
        session.release.clear()
        directory._actor_event(session.sample(DRIVER, "delete"))
        assert session.entered.wait(1)
        session.put(directory, DRIVER)
        session.put(directory, HOST)
        wait(lambda: directory.actor_online(HOST))
        session.release.set()
        drain(directory)
        assert directory.actor_online(DRIVER)
        assert [event.actor for event in transitions].count(DRIVER) == 2
    finally:
        session.release.set()
        stop()
        directory.close()


def test_mailbox_delete_uses_the_mailbox_key_and_accepts_real_departure():
    session = Session()
    directory = LivelinessDirectory(session)
    key = KeySpace().mailbox_liveliness(HOST)
    try:
        session.live.add(key)
        directory._mailbox_event(TransportSample(key, b""))
        drain(directory)
        directory._mailbox_event(TransportSample(key, b"", "delete"))
        drain(directory)
        assert HOST in directory.online_mailboxes()
        assert session.calls[-1][0] == key
        session.live.clear()
        directory._mailbox_event(TransportSample(key, b"", "delete"))
        drain(directory)
        assert HOST not in directory.online_mailboxes()
    finally:
        directory.close()


def test_rebuild_fences_inflight_positive_query():
    session = Session()
    directory = LivelinessDirectory(session)
    try:
        session.put(directory, DRIVER)
        drain(directory)
        session.release.clear()
        directory._actor_event(session.sample(DRIVER, "delete"))
        assert session.entered.wait(1)
        session.rebuild()
        session.release.set()
        drain(directory)
        assert not directory.actor_online(DRIVER)
        session.put(directory, DRIVER)
        drain(directory)
        assert directory.actor_online(DRIVER)
    finally:
        session.release.set()
        directory.close()


def test_cross_generation_old_delete_is_ignored_without_query():
    session = Session()
    directory = LivelinessDirectory(session)
    try:
        session.put(directory, DRIVER)
        drain(directory)
        old_delete = session.sample(DRIVER, "delete")
        session.rebuild()
        session.put(directory, DRIVER)
        drain(directory)
        directory._actor_event(old_delete)
        drain(directory)
        assert directory.actor_online(DRIVER)
        assert not session.calls
    finally:
        directory.close()


def test_close_keeps_accepted_query_owned_until_settled():
    session = Session()
    directory = LivelinessDirectory(session)
    try:
        session.put(directory, DRIVER)
        drain(directory)
        session.release.clear()
        directory._actor_event(session.sample(DRIVER, "delete"))
        assert session.entered.wait(1)
        assert not directory._authority.close(0.01)
        assert directory._authority._delete_effects.snapshot().outstanding == 1
        session.release.set()
        assert directory._authority.close(1)
        assert directory._authority._delete_effects.snapshot().outstanding == 0
    finally:
        session.release.set()
        directory.close()


def test_directory_close_retry_does_not_unregister_successful_cleanup_twice(monkeypatch):
    session = Session()
    directory = LivelinessDirectory(session)
    original = directory._authority.close
    attempts = []

    def first_incomplete(timeout=1.0):
        attempts.append(timeout)
        return False if len(attempts) == 1 else original(timeout)

    monkeypatch.setattr(directory._authority, "close", first_incomplete)
    try:
        with pytest.raises(TimeoutError, match="did not drain"):
            directory.close()
        assert not session.hooks
        assert directory._registrations == []
        directory.close()
        assert attempts == [1.0, 1.0]
    finally:
        directory.close()


def test_query_custody_uses_the_existing_mailbox_plus_handler_bound(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    handling, release_handler = threading.Event(), threading.Event()
    original = PresenceAuthority._receive

    def held_receive(self, command):
        if getattr(command, "identity", None) == "block-handler":
            handling.set()
            assert release_handler.wait(3)
        original(self, command)

    def blocked_query(*_args):
        entered.set()
        assert release.wait(3)
        return False

    monkeypatch.setattr(PresenceAuthority, "_receive", held_receive)
    owner = PresenceAuthority(capacity=1, confirm_present=blocked_query)
    try:
        assert owner.change("actor", DRIVER, False) is AdmissionResult.ACCEPTED
        assert entered.wait(1)
        assert owner.change("actor", "block-handler", True) is AdmissionResult.ACCEPTED
        assert handling.wait(1)
        assert owner.change("actor", "cannot-grow", True) is AdmissionResult.OVERLOADED
        assert len(owner._accepted) == 2  # original mailbox1 + executing handler1
        assert not owner.projection().complete
    finally:
        release.set()
        release_handler.set()
        assert owner.close(3)
        assert not owner._accepted


def test_late_positive_and_expired_queued_query_both_accept_offline(monkeypatch):
    from hyprial.daemon.impl.transport import presence_actor as module

    clock = [0.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    session = Session()
    directory = LivelinessDirectory(session)
    try:
        session.put(directory, HOST)
        session.put(directory, DRIVER)
        drain(directory)
        session.release.clear()
        directory._actor_event(session.sample(HOST, "delete"))
        assert session.entered.wait(1)
        directory._actor_event(session.sample(DRIVER, "delete"))
        wait(lambda: directory._authority._delete_effects.snapshot().outstanding == 2)
        # No wallclock sleep: expire the shared admission-to-result budget.
        clock[0] = 2.0
        session.release.set()
        drain(directory)
        assert not directory.actor_online(HOST)  # positive result arrived too late
        assert not directory.actor_online(DRIVER)  # expired in the worker queue
        assert len(session.calls) == 1  # no native I/O for the expired request
    finally:
        session.release.set()
        directory.close()


def test_confirmed_live_token_wakes_durable_outbox_without_another_put(tmp_path):
    from hyprial.daemon.impl.inbox import DeliveryCustodyCoordinator, DeliveryCustodyFacade
    from hyprial.daemon.impl.inbox.links.io import CorrelatedInboxEventRouter
    from hyprial.daemon.impl.inbox.memory import MemoryDeliveryTransport
    from hyprial.daemon.impl.inbox.links.wake import RecipientWakeCoordinator
    from tests.integration.actor_preservation.delivery.test_actor_preserved_outbox_final_attempt_wake import _message

    session = Session()
    directory = LivelinessDirectory(session)

    class Delivery(MemoryDeliveryTransport):
        offline = False

        def is_online(self, recipient):
            return not self.offline and directory.actor_online(recipient)

    transport = Delivery()
    events = CorrelatedInboxEventRouter()
    database = tmp_path / "outbox.sqlite3"
    commands = DeliveryCustodyCoordinator(database, transport, events)
    inbox = DeliveryCustodyFacade(commands, database)
    wakes = RecipientWakeCoordinator(commands, events, inbox, directory.presence_projection)
    def stop():
        pass
    try:
        session.put(directory, DRIVER)
        drain(directory)
        stop = directory.bind_actor_online(wakes.submit)
        wait(lambda: wakes.pending_count == 0)
        now = time.time_ns() // 1_000_000
        message = replace(_message("reconciled-live", DRIVER), created_at_ms=now, expires_at_ms=now + 60_000)
        inbox.submit(message, now_ms=now, defer_direct=True)
        # A deferred submit is due at once (#1239); one offline round puts it
        # behind a real backoff, which the wake must then bring forward.
        transport.offline = True
        offline_round = inbox.retry_due(now_ms=now)
        assert [(r.accepted, r.queued) for r in offline_round] == [(True, True)]
        assert transport.delivered == []
        transport.offline = False
        scheduled = inbox.outbox_item(message.message_id).next_attempt_ms
        assert scheduled > now
        transport.confirm_direct(message.message_id)
        directory._actor_event(session.sample(DRIVER, "delete"))
        drain(directory)
        wait(lambda: inbox.outbox_item(message.message_id).next_attempt_ms < scheduled)
        # The DB due-time write precedes the typed completion. Join that wake
        # before taking retry_due's generation/version fence (dev #1036).
        wait(lambda: wakes.pending_count == 0)
        due = inbox.outbox_item(message.message_id).next_attempt_ms
        [result] = inbox.retry_due(now_ms=due)
        assert result.accepted and not result.queued
        assert transport.delivered == [message.message_id]
        assert inbox.outbox_count() == 0
    finally:
        stop()
        assert wakes.close(3)
        directory.close()
        inbox.close()


def test_directory_constructor_failure_retires_query_worker(monkeypatch):
    from hyprial.daemon.impl.transport.zenoh import presence as module

    owners = []
    cleanup = []
    original = module.PresenceAuthority

    def track(**kwargs):
        owner = original(**kwargs)
        owners.append(owner)
        return owner

    class BrokenSession(Session):
        def on_rebuild(self, callback):
            return lambda: cleanup.append("hook")

        def observe_liveliness(self, key, callback, *, history=True):
            if "/mailbox/" in key:
                raise OSError("injected declaration failure")
            return SimpleNamespace(close=lambda: cleanup.append("registration"))

    monkeypatch.setattr(module, "PresenceAuthority", track)
    with pytest.raises(OSError, match="injected declaration"):
        LivelinessDirectory(BrokenSession())
    assert cleanup == ["hook", "registration"]
    assert len(owners) == 1
    assert owners[0]._delete_effects.close(0)
    assert owners[0].close(0)


def test_failed_constructor_stops_owner_after_accepted_query_settles(monkeypatch):
    import pykka
    from hyprial.daemon.impl.transport.zenoh import presence as module

    owners = []
    existing_actors = {ref.actor_urn for ref in pykka.ActorRegistry.get_all()}
    stop_requested = threading.Event()
    authority_type = module.PresenceAuthority
    real_close = authority_type.close

    def track(**kwargs):
        owner = authority_type(**kwargs)
        owners.append(owner)
        stop = owner._runtime.stop

        def observed_stop(handle, timeout=1.0):
            if timeout == 0:
                stop_requested.set()
            return stop(handle, timeout)

        monkeypatch.setattr(owner._runtime, "stop", observed_stop)
        # Keep the same incomplete-close semantics without a 1s test sleep.
        monkeypatch.setattr(owner, "close", lambda: real_close(owner, 0.01))
        return owner

    class BrokenSession(Session):
        def observe_liveliness(self, key, callback, *, history=True):
            if "/mailbox/" in key:
                raise OSError("second declaration failed during query")
            actor_key = KeySpace().actor_liveliness(DRIVER)
            self.live.add(actor_key)
            callback(TransportSample(actor_key, b""))
            callback(TransportSample(actor_key, b"", "delete"))
            assert self.entered.wait(1)
            return SimpleNamespace(close=lambda: None)

    session = BrokenSession()
    session.release.clear()
    monkeypatch.setattr(module, "PresenceAuthority", track)
    try:
        with pytest.raises(OSError, match="second declaration"):
            LivelinessDirectory(session)
        assert len(owners) == 1
        created_actors = [
            ref for ref in pykka.ActorRegistry.get_all()
            if ref.actor_urn not in existing_actors
        ]
        assert len(created_actors) == 1
        assert owners[0]._delete_effects.snapshot().outstanding == 1
        assert not stop_requested.is_set()
        session.release.set()
        wait(lambda: owners[0]._delete_effects.snapshot().outstanding == 0)
        assert stop_requested.wait(1), "unreturned presence owner was left alive after I/O"
        assert owners[0]._delete_effects.close(1)
        wait(lambda: all(not ref.is_alive() for ref in created_actors))
    finally:
        session.release.set()
        for owner in owners:
            assert real_close(owner, 1)


@pytest.mark.parametrize("identity", [HOST, DRIVER])
def test_native_live_query_reconciles_reordered_delete_and_real_departure(identity):
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    publisher = ZenohTransport(ZenohConfig(listen=(f"tcp/127.0.0.1:{port}",)))
    observer = ZenohTransport(ZenohConfig(connect=(f"tcp/127.0.0.1:{port}",)))
    captured = queue.Queue()

    class CaptureSession:
        def on_rebuild(self, callback):
            return observer.on_rebuild(callback)

        def observe_liveliness(self, key, callback, *, history=True):
            return observer.observe_liveliness(
                key, lambda sample: captured.put((callback, sample)), history=history,
            )

        def get_liveliness(self, key, *, timeout=1.0):
            return observer.get_liveliness(key, timeout=timeout)

    directory = LivelinessDirectory(CaptureSession())
    old = new = None
    try:
        key = KeySpace().actor_liveliness(identity)
        old = publisher.declare_liveliness(key)
        callback, put = captured.get(timeout=3)
        assert put.kind == "put"
        callback(put)
        drain(directory)
        old.close()
        old = None
        callback, delayed_delete = captured.get(timeout=3)
        assert delayed_delete.kind == "delete"
        new = publisher.declare_liveliness(key)
        new_callback, new_put = captured.get(timeout=3)
        assert new_put.kind == "put"
        new_callback(new_put)
        drain(directory)
        callback(delayed_delete)
        drain(directory)
        assert directory.actor_online(identity), "reconcile ignored the current native token"

        new.close()
        new = None
        callback, real_delete = captured.get(timeout=3)
        assert real_delete.kind == "delete"
        callback(real_delete)
        drain(directory)
        assert not directory.actor_online(identity), "genuine departure must still go offline"
    finally:
        if old is not None:
            old.close()
        if new is not None:
            new.close()
        directory.close()
        observer.close()
        publisher.close()


def test_non_draining_close_stops_owner_after_late_query_without_retry():
    import pykka

    existing = {ref.actor_urn for ref in pykka.ActorRegistry.get_all()}
    session = Session()
    directory = LivelinessDirectory(session)
    created = [ref for ref in pykka.ActorRegistry.get_all() if ref.actor_urn not in existing]
    try:
        assert len(created) == 1
        session.put(directory, DRIVER)
        drain(directory)
        session.release.clear()
        directory._actor_event(session.sample(DRIVER, "delete"))
        assert session.entered.wait(1)
        assert not directory._authority.close(0.01)
        assert directory._authority._delete_effects.snapshot().outstanding == 1
        assert created[0].is_alive()
        session.release.set()
        # No second close: the timed-out caller has relinquished ownership.
        wait(lambda: not created[0].is_alive())
        assert directory._authority._delete_effects.snapshot().outstanding == 0
    finally:
        session.release.set()
        directory.close()


def test_directory_native_undeclare_precedes_separate_owner_drain_budget(monkeypatch):
    from hyprial.daemon.impl.transport.zenoh.transport import _Registration

    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    session = Session()
    directory = LivelinessDirectory(session)
    drain_budgets = []
    errors = []
    real_close = directory._authority.close

    def native_undeclare():
        entered.set()
        assert release.wait(3), "native undeclare control was not released"

    def owner_close(timeout=1.0):
        drain_budgets.append(timeout)
        return real_close(timeout)

    directory._registrations.append(_Registration(SimpleNamespace(undeclare=native_undeclare)))
    monkeypatch.setattr(directory._authority, "close", owner_close)

    def close():
        try:
            directory.close()
        except BaseException as error:
            errors.append(error)
        finally:
            completed.set()

    thread = threading.Thread(target=close)
    try:
        thread.start()
        assert entered.wait(1)
        # The1s owner budget has not even begun. Native unregister accepts no
        # deadline; classifying the whole directory as1s would be false.
        assert not completed.is_set()
        assert drain_budgets == []
        assert directory._authority._closed
        release.set()
        assert completed.wait(2)
        assert errors == []
        assert drain_budgets == [1.0]
    finally:
        release.set()
        thread.join(3)
        assert not thread.is_alive()
        directory.close()
