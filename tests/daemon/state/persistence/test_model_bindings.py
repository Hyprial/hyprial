"""The model port shares serialized mutation and original settlement custody."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from hyprial.daemon.impl.desired_state.bindings.store import ModelBindingStore
from hyprial.kernel import DesiredStateError
from .test_state_persistence_authority import _authority


def candidate():
    return {
        "authKind": "connection",
        "connectionRef": "connection-1",
        "configRevision": 1,
        "model": "hy4-flash",
        "harness": "pi",
        "mode": "headless",
        "intentDigest": "a" * 64,
    }


def test_binding_lifecycle_publishes_and_survives_authority_restart(
    tmp_path, actor_host
):
    authority = _authority(tmp_path, actor_host)
    try:
        port = authority.desired.model_binding_store
        first = port.prepare("agent", "member", "first", 0, candidate())
        assert authority.desired.load().model_binding_operations == (first,)
        port.cancel("agent", "member", "first", first.request_digest, 0)
        second = port.prepare("agent", "member", "second", 0, candidate())
        port.finalize("agent", "member", "second", second.request_digest, 0)
        binding = port.status("agent", "member").binding
        assert binding.binding_revision == 1
    finally:
        assert authority.close()
    restarted = _authority(tmp_path, actor_host)
    try:
        status = restarted.desired.model_binding_store.status("agent", "member")
        assert status.binding == binding
        assert {row.phase for row in status.operations} == {"cancelled", "finalized"}
        restarted.desired.model_binding_store.mark_cleanup("agent", "entity", "destroy")
        restarted.desired.model_binding_store.purge_destroyed_agent(
            "agent", "entity", "destroy"
        )
        assert restarted.desired.load().model_binding_operations == ()
    finally:
        assert restarted.close()


def test_binding_and_existing_mutation_share_owner_thread(
    tmp_path, actor_host, monkeypatch
):
    authority = _authority(tmp_path, actor_host)
    threads = []
    original_binding = ModelBindingStore.mark_cleanup
    original_existing = authority._desired_store.set_mailbox_role

    def mark(self, *args, **kwargs):
        threads.append(threading.get_ident())
        return original_binding(self, *args, **kwargs)

    def mailbox(value):
        threads.append(threading.get_ident())
        return original_existing(value)

    monkeypatch.setattr(ModelBindingStore, "mark_cleanup", mark)
    monkeypatch.setattr(authority._desired_store, "set_mailbox_role", mailbox)
    try:
        authority.desired.model_binding_store.mark_cleanup("agent", "entity", "destroy")
        authority.desired.set_mailbox_role(True)
        assert len(threads) == 2 and len(set(threads)) == 1
        assert threads[0] != threading.get_ident()
    finally:
        assert authority.close()


def test_cleanup_waits_for_accepted_write_after_observational_deadline(
    tmp_path, actor_host, monkeypatch
):
    authority = _authority(tmp_path, actor_host, call_timeout=0.01)
    entered, release = threading.Event(), threading.Event()
    original = ModelBindingStore.mark_cleanup
    calls = []

    def delayed(self, *args, **kwargs):
        calls.append(args)
        entered.set()
        assert release.wait(3)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ModelBindingStore, "mark_cleanup", delayed)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            authority.desired.model_binding_store.mark_cleanup,
            "agent",
            "entity",
            "destroy",
        )
        try:
            assert entered.wait(1)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.05)
            assert not next(iter(authority._pending.values())).abandoned
            release.set()
            assert future.result(timeout=2).destroy_attempt == "destroy"
            assert len(calls) == 1
            assert authority.desired.load().model_agent_lifecycle
            assert authority._pending == {}
        finally:
            release.set()
            assert authority.close()


def test_concurrent_prepare_has_one_winner_without_stale_projection(
    tmp_path, actor_host
):
    authority = _authority(tmp_path, actor_host)
    port = authority.desired.model_binding_store
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [
                executor.submit(port.prepare, "agent", "member", op, 0, candidate())
                for op in ("one", "two")
            ]
        successes, failures = [], []
        for future in results:
            try:
                successes.append(future.result())
            except DesiredStateError as error:
                failures.append(error)
        assert len(successes) == len(failures) == 1
        assert port.status("agent", "member").pending == successes[0]
        assert authority.desired.load().model_binding_operations == tuple(successes)
    finally:
        assert authority.close()
