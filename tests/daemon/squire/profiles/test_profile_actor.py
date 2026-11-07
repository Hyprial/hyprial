import threading
import time

import pytest

from hyprial.identity import UserProfileStore
from hyprial.daemon.impl.squire.actors.profile import UserProfileAuthority
from hyprial.daemon.impl.squire.actors.profile import ProfileResult
from hyprial.kernel.impl.actor_runtime.effects import EffectCompleted


def test_duplicate_completion_cannot_replace_newer_profile_projection(tmp_path):
    actor = UserProfileAuthority(tmp_path / "users.json")
    try:
        actor.ensure(
            owner="alice",
            owner_key="alice-key",
            login_name="alice",
            machine="node",
            machine_key="node-key",
        )
        before = actor.projection()
        actor._runtime.tell(
            actor._handle, EffectCompleted("already-settled", 1, ProfileResult(()))
        )
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            snapshot = actor._runtime.snapshot(actor._handle)
            if snapshot.queued == snapshot.in_flight == 0:
                break
            time.sleep(0.005)
        assert actor.projection() == before
    finally:
        assert actor.close()


def test_profile_writes_publish_before_reply_and_reads_do_not_open_files(
    tmp_path, monkeypatch
):
    actor = UserProfileAuthority(tmp_path / "users.json")
    try:
        profile, changed = actor.ensure(
            owner="alice",
            owner_key="alice-key",
            login_name="alice",
            machine="node",
            machine_key="node-key",
        )
        assert changed == ("users.profile",)
        actor.set_delivery_agent("alice-key", "worker")

        def forbidden(*args):
            raise AssertionError("read projection opened profile storage")

        monkeypatch.setattr(actor._store, "list", forbidden)
        assert actor.resolve("alice").delivery_agent == "worker"
        assert actor.get("alice-key").owner == profile.owner
    finally:
        assert actor.close()


def test_profile_timeout_keeps_accepted_write_and_old_projection(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = UserProfileStore._save

    def stalled(self, profiles):
        entered.set()
        assert release.wait(3)
        return original(self, profiles)

    monkeypatch.setattr(UserProfileStore, "_save", stalled)
    actor = UserProfileAuthority(tmp_path / "users.json", timeout=0.01)
    try:
        with pytest.raises(TimeoutError, match="remains accepted"):
            actor.ensure(
                owner="alice",
                owner_key="alice-key",
                login_name="alice",
                machine="node",
                machine_key="node-key",
            )
        assert entered.is_set()
        assert actor.list() == ()
        assert not actor.close(0.01)
    finally:
        release.set()
        assert actor.close(2)
    assert actor.get("alice-key").owner == "alice"


def test_explicit_refresh_adopts_offline_profile_changes(tmp_path):
    path = tmp_path / "users.json"
    actor = UserProfileAuthority(path)
    try:
        UserProfileStore(path).ensure(
            owner="alice",
            owner_key="alice-key",
            login_name="alice",
            machine="node",
            machine_key="node-key",
        )
        assert actor.list() == ()
        actor.refresh(wait=True)
        assert actor.resolve("alice").owner_key == "alice-key"
    finally:
        assert actor.close()


def test_routing_resolution_refreshes_an_offline_changed_binding(tmp_path):
    path = tmp_path / "users.json"
    store = UserProfileStore(path)
    store.ensure(
        owner="alice",
        owner_key="alice-key",
        login_name="alice",
        machine="node",
        machine_key="node-key",
    )
    store.set_delivery_agent("alice-key", "old")
    actor = UserProfileAuthority(path)
    try:
        store.set_delivery_agent("alice-key", "new")
        assert actor.resolve("alice").delivery_agent == "old"
        assert actor.resolve_current("alice").delivery_agent == "new"
    finally:
        assert actor.close()
