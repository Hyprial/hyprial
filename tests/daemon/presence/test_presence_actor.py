import threading
import time

import pytest

from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.daemon.impl.transport.presence_actor import PresenceAuthority


def _wait(predicate):
    deadline = time.monotonic() + 1
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate()


def test_rebuild_fences_already_admitted_presence(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = PresenceAuthority._receive

    def stalled(self, command):
        entered.set()
        assert release.wait(2)
        original(self, command)

    monkeypatch.setattr(PresenceAuthority, "_receive", stalled)
    owner = PresenceAuthority()
    try:
        owner.change("actor", "old", True)
        assert entered.wait(1)
        owner.reset()
        owner.change("actor", "new", True)
        release.set()
        _wait(lambda: owner.projection().actors == frozenset({"new"}))
    finally:
        release.set()
        assert owner.close()


def test_lost_delete_invalidates_generation_instead_of_retaining_phantom(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = PresenceAuthority._receive

    def stalled(self, command):
        if command.identity == "block":
            entered.set()
            assert release.wait(2)
        original(self, command)

    monkeypatch.setattr(PresenceAuthority, "_receive", stalled)
    owner = PresenceAuthority(capacity=1)
    try:
        owner.change("actor", "present", True)
        _wait(lambda: "present" in owner.projection().actors)
        owner.change("actor", "block", True)
        assert entered.wait(1)
        owner.change("mailbox", "queued", True)
        assert owner.change("actor", "present", False) is AdmissionResult.OVERLOADED
        assert not owner.projection().complete
        assert not owner.projection().actors
        assert owner.projection().rejected == 1
        release.set()
    finally:
        release.set()
        assert owner.close()


def test_callback_that_started_before_reset_cannot_adopt_new_generation():
    owner = PresenceAuthority()
    old = owner.projection().generation
    try:
        owner.reset()
        owner.change("actor", "stale", True, generation=old)
        owner.change("actor", "current", True)
        _wait(lambda: "current" in owner.projection().actors)
        assert owner.projection().actors == frozenset({"current"})
    finally:
        assert owner.close()


def test_independent_online_observers_are_bounded_and_retired_separately():
    owner = PresenceAuthority()
    first, second, legacy = [], [], []
    stops = []
    try:
        owner.change("actor", "present", True)
        _wait(lambda: "present" in owner.projection().actors)
        stop_first = owner.observe_actor_online(first.append)
        stops.append(stop_first)
        stops.append(owner.observe_actor_online(second.append))
        owner.bind_actor_online(legacy.append)
        assert [event.actor for event in first] == ["present"]
        stop_first()
        stop_first()
        for _ in range(15):
            stops.append(owner.observe_actor_online(lambda _: None))
        with pytest.raises(RuntimeError, match="capacity exhausted"):
            owner.observe_actor_online(lambda _: None)
        owner.change("actor", "later", True)
        _wait(lambda: len(second) == len(legacy) == 2)
        assert [event.actor for event in first] == ["present"]
        assert [event.actor for event in second] == ["present", "later"]
        assert [event.actor for event in legacy] == ["present", "later"]
    finally:
        for stop in stops:
            stop()
        assert owner.close()
    with pytest.raises(RuntimeError, match="closed"):
        owner.observe_actor_online(lambda _: None)
