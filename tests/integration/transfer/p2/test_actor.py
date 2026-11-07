"""The P2 mailbox must never perform SQLite work or drop accepted completion."""

from concurrent.futures import TimeoutError
import threading

import pytest

from hyprial.daemon.impl.transfer.p2.actor import AdmissionActor, Command, Operation
from hyprial.daemon.impl.transfer.p2.model import AdmissionConflict, Reason


def test_actor_orders_prepare_revoke_and_replay(ledger, binding):
    store, _, _ = ledger
    actor = AdmissionActor(store)
    try:
        first = actor.submit(Command(Operation.PREPARE, "op", binding=binding))
        revoked = actor.submit(Command(Operation.REVOKE, "op"))
        replay = actor.submit(Command(Operation.PREPARE, "op", binding=binding))
        assert first.result(3).revoked_at_ms is None
        assert replay.result(3) == revoked.result(3)
        assert revoked.result(3).revoked_at_ms is not None
    finally:
        assert actor.close(timeout=3)


def test_expected_rejection_does_not_crash_the_actor(ledger, binding):
    store, _, _ = ledger
    actor = AdmissionActor(store)
    try:
        actor.submit(Command(Operation.PREPARE, "op", binding=binding)).result(3)
        with pytest.raises(AdmissionConflict) as error:
            actor.submit(Command(Operation.PREPARE, "other", binding=binding)).result(3)
        assert error.value.reason is Reason.RESERVED
        assert actor.submit(Command(Operation.GET, "op")).result(3).operation_id == "op"
    finally:
        assert actor.close(timeout=3)


def test_slow_effect_does_not_block_mailbox_and_capacity_is_bounded(ledger, binding, monkeypatch):
    store, _, _ = ledger
    entered, release = threading.Event(), threading.Event()
    original = store.prepare
    threads = []

    def blocked(*args):
        threads.append(threading.current_thread().name)
        entered.set()
        assert release.wait(3), "test must release the held SQLite effect"
        return original(*args)

    monkeypatch.setattr(store, "prepare", blocked)
    actor = AdmissionActor(store, capacity=1)
    try:
        pending = actor.submit(Command(Operation.PREPARE, "op", binding=binding))
        assert entered.wait(3)
        with pytest.raises(TimeoutError):
            pending.result(0)
        # This response requires a responsive mailbox while the worker is held.
        with pytest.raises(RuntimeError, match="overloaded"):
            actor.submit(Command(Operation.GET, "op")).result(1)
        release.set()
        assert pending.result(3).operation_id == "op"
        assert threads[0].startswith("transfer-p2-io")
    finally:
        release.set()
        assert actor.close(timeout=3)


def test_cancelled_caller_does_not_cancel_durable_effect(ledger, binding):
    store, _, _ = ledger
    actor = AdmissionActor(store)
    try:
        pending = actor.submit(Command(Operation.PREPARE, "op", binding=binding))
        pending.cancel()
        # Same writer's subsequent read is a barrier for the accepted mutation.
        assert actor.submit(Command(Operation.GET, "op")).result(3).operation_id == "op"
    finally:
        assert actor.close(timeout=3)


def test_close_drains_accepted_commands_and_rejects_new_ones(ledger, binding):
    store, _, _ = ledger
    actor = AdmissionActor(store)
    result = actor.submit(Command(Operation.PREPARE, "op", binding=binding))
    assert actor.close(timeout=3)
    assert result.result(0).operation_id == "op"
    with pytest.raises(RuntimeError, match="closed"):
        actor.submit(Command(Operation.GET, "op")).result(0)
    assert store.get("op") is not None


def test_close_timeout_retains_accepted_effect_until_later_drain(ledger, binding, monkeypatch):
    store, _, _ = ledger
    entered, release = threading.Event(), threading.Event()
    original = store.prepare

    def held(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(store, "prepare", held)
    actor = AdmissionActor(store)
    try:
        result = actor.submit(Command(Operation.PREPARE, "op", binding=binding))
        assert entered.wait(3)
        assert actor.close(timeout=0.01) is False
        assert not result.done()
        release.set()
        assert result.result(3).operation_id == "op"
        assert actor.close(timeout=3)
        assert store.get("op") is not None
    finally:
        release.set()
        assert actor.close(timeout=3)
