from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import threading
import time

import pytest

from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.daemon.impl.orgfs.api import ChangeEvent, OrgFsError
from hyprial.daemon.impl.orgfs.storage.blob_authority import BlobAuthority
from hyprial.daemon.impl.orgfs.storage.blobs import BlobError
from hyprial.daemon.impl.orgfs.docs import LocalOrgFs
from hyprial.daemon.impl.orgfs.runtime import (
    OrgFsRuntime,
)
from hyprial.daemon.impl.orgfs.composition.vocabulary import (
    _DirectoryCommand,
    _DirectoryEffect)
from hyprial.daemon.impl.transport import TransportSample


def test_watchers_run_after_space_authority_lock_is_released() -> None:
    facade = LocalOrgFs()
    space = facade.create_space("actor boundary")
    observed: list[bool] = []
    delivered = threading.Event()

    def watcher(_event: object) -> None:
        acquired = facade._lock.acquire(blocking=False)
        observed.append(acquired)
        if acquired:
            facade._lock.release()
        delivered.set()

    facade.watch(space.space_id, "**", watcher)
    facade.mkdir(space.space_id, "child")

    assert delivered.wait(2)
    assert observed == [True]


def test_membership_meta_watch_runs_after_commit_and_can_be_closed() -> None:
    facade = LocalOrgFs(author="user:owner")
    space = facade.create_space("membership watch")
    delivered = threading.Event()
    observed: list[tuple[str, ...]] = []

    def observe_members() -> None:
        observed.append(tuple(row.user for row in facade.members(space.space_id)))
        delivered.set()

    watcher = facade.watch_meta(space.space_id, observe_members)
    facade.invite(space.space_id, "user:member")

    assert delivered.wait(2)
    assert observed == [("user:owner", "user:member")]
    watcher.close()
    facade.remove_member(space.space_id, "user:member")
    time.sleep(0.05)
    assert len(observed) == 1


def test_committed_watcher_effects_keep_space_commit_order() -> None:
    facade = LocalOrgFs()
    space = facade.create_space("ordered effects")
    first_started = threading.Event()
    release_first = threading.Event()
    observed: list[str] = []
    second_delivered = threading.Event()

    def watcher(event: object) -> None:
        path = event.node.path  # type: ignore[attr-defined]
        if path.endswith("first"):
            first_started.set()
            assert release_first.wait(2)
        observed.append(path)
        if path.endswith("second"):
            second_delivered.set()

    facade.watch(space.space_id, "**", watcher)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(facade.mkdir, space.space_id, "first")
        assert first_started.wait(2)
        second = pool.submit(facade.mkdir, space.space_id, "second")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with facade._lock:
                if facade._effect_sequence >= 2:
                    break
            time.sleep(0.005)
        with facade._lock:
            assert facade._effect_sequence == 2
        release_first.set()
        first.result(timeout=2)
        second.result(timeout=2)

    assert second_delivered.wait(2)
    assert observed == ["first", "second"]


def test_effect_lane_overload_rejects_mutation_before_commit(monkeypatch) -> None:
    monkeypatch.setattr('hyprial.daemon.impl.orgfs.document.effects._FACADE_EFFECT_CAPACITY', 1)
    facade = LocalOrgFs()
    space = facade.create_space("bounded effects")
    callback_started = threading.Event()
    release_callback = threading.Event()

    def watcher(event: object) -> None:
        if event.node.path.endswith("first"):  # type: ignore[attr-defined]
            callback_started.set()
            assert release_callback.wait(5)

    facade.watch(space.space_id, "**", watcher)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(facade.mkdir, space.space_id, "first")
        assert callback_started.wait(2)
        try:
            with pytest.raises(OrgFsError, match="effect lane is overloaded"):
                facade.mkdir(space.space_id, "rejected")
            with pytest.raises(OrgFsError, match="unknown-doc"):
                facade.resolve(space.space_id, "rejected")
        finally:
            release_callback.set()
            first.result(timeout=2)
            assert facade.close_effects(timeout=5)


def test_one_blob_authority_serializes_cross_space_reference_lifecycle(tmp_path) -> None:
    blobs = BlobAuthority(tmp_path)
    payload = b"shared immutable payload"
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            digests = tuple(
                pool.map(
                    lambda space: blobs.put(space, payload, reason="replica"),
                    ("space-a", "space-b"),
                )
            )
        digest = digests[0]
        assert digests == (digest, digest)
        assert blobs.references(digest=digest) == (
            ("space-a", digest, "replica"),
            ("space-b", digest, "replica"),
        )

        blobs.release("space-a", digest)
        assert not blobs.delete_if_unreferenced(digest)
        assert blobs.get("space-b", digest) == payload
        blobs.release("space-b", digest)
        assert blobs.delete_if_unreferenced(digest)
    finally:
        blobs.close()


def test_blob_close_waits_for_accepted_cross_space_effect(tmp_path) -> None:
    blobs = BlobAuthority(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    effect_finished = threading.Event()
    store_closed_after_effect: list[bool] = []
    original_put = blobs._store.put
    original_close = blobs._store.close

    def blocked_put(space_id: str, data: bytes, *, reason: str) -> str:
        entered.set()
        assert release.wait(3)
        digest = original_put(space_id, data, reason=reason)
        effect_finished.set()
        return digest

    def checked_close() -> None:
        store_closed_after_effect.append(effect_finished.is_set())
        original_close()

    blobs._store.put = blocked_put
    blobs._store.close = checked_close
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            admitted = pool.submit(blobs.put, "space-a", b"accepted", reason="replica")
            assert entered.wait(2)
            assert not blobs.close(timeout=0.02)
            assert blobs._closing
            assert not blobs._closed
            assert store_closed_after_effect == []
            with pytest.raises(BlobError, match="authority is closed"):
                blobs.put("space-b", b"rejected", reason="replica")
            release.set()
            assert admitted.result(timeout=2) == hashlib.sha256(b"accepted").hexdigest()
            assert blobs.close(timeout=2)
        assert store_closed_after_effect == [True]
    finally:
        release.set()
        blobs.close()


def test_blob_authority_does_not_repeat_an_effect_after_actor_restart(tmp_path) -> None:
    blobs = BlobAuthority(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original_put = blobs._store.put
    calls = 0

    def blocked_put(space_id: str, data: bytes, *, reason: str) -> str:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(3)
        return original_put(space_id, data, reason=reason)

    blobs._store.put = blocked_put
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            accepted = pool.submit(blobs.put, "space", b"custodied", reason="test")
            assert entered.wait(2)
            assert blobs._runtime.tell(blobs._handle, object()) is AdmissionResult.ACCEPTED
            deadline = time.monotonic() + 3
            while (
                blobs._runtime.snapshot(blobs._handle).generation == 1
                and time.monotonic() < deadline
            ):
                time.sleep(0.005)
            assert blobs._runtime.snapshot(blobs._handle).generation > 1
            release.set()
            digest = accepted.result(timeout=3)
        assert digest == hashlib.sha256(b"custodied").hexdigest()
        assert calls == 1
    finally:
        release.set()
        blobs.close()


def test_checkout_io_is_per_space_and_close_drains_accepted_reconcile(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path, node_id="node-checkout", author="user:owner")
    first = runtime.facade.create_space("checkout one")
    second = runtime.facade.create_space("checkout two")
    runtime.checkout(first.space_id, True)
    runtime.checkout(second.space_id, True)
    first_owner = runtime._checkouts[first.space_id]
    second_owner = runtime._checkouts[second.space_id]
    entered = threading.Event()
    release = threading.Event()
    original_scan = first_owner.manager._scan_tree

    def blocked_scan():
        entered.set()
        assert release.wait(3)
        return original_scan()

    first_owner.manager._scan_tree = blocked_scan
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            first_reconcile = pool.submit(first_owner.reconcile)
            assert entered.wait(2)
            independent = pool.submit(second_owner.reconcile)
            independent.result(timeout=2)
            assert not first_owner.close(timeout=0.05)
            release.set()
            first_reconcile.result(timeout=2)
            assert first_owner.close(timeout=2)
    finally:
        release.set()
        runtime.close()


def test_checkout_watch_burst_coalesces_while_reconcile_is_blocked(tmp_path, monkeypatch) -> None:
    runtime = OrgFsRuntime(tmp_path, node_id="node-checkout", author="user:owner")
    space = runtime.facade.create_space("checkout burst")
    runtime.facade.write_text(space.space_id, "note.txt", "value")
    runtime.checkout(space.space_id, True)
    owner = runtime._checkouts[space.space_id]
    node = runtime.facade.stat(space.space_id, "note.txt")
    entered = threading.Event()
    release = threading.Event()
    count_lock = threading.Lock()
    calls = 0
    original_scan = owner.manager._scan_tree
    coalesced_admitted = threading.Event()
    original_tell = owner._runtime.tell
    reconcile_admissions = 0

    def tracked_tell(handle, command):
        nonlocal reconcile_admissions
        result = original_tell(handle, command)
        if getattr(command, "action", None) == "reconcile" and result is AdmissionResult.ACCEPTED:
            with count_lock:
                reconcile_admissions += 1
                if reconcile_admissions == 2:
                    coalesced_admitted.set()
        return result

    monkeypatch.setattr(owner._runtime, "tell", tracked_tell)

    def blocked_first_scan():
        nonlocal calls
        with count_lock:
            calls += 1
            current = calls
        if current == 1:
            entered.set()
            assert release.wait(3)
        return original_scan()

    owner.manager._scan_tree = blocked_first_scan
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(owner.reconcile)
            assert entered.wait(2)
            event = ChangeEvent(space.space_id, "modified", node, None)
            for _ in range(12):
                owner.notify_change(event)
            coalesced = pool.submit(owner.reconcile)
            assert coalesced_admitted.wait(2)
            release.set()
            first.result(timeout=2)
            coalesced.result(timeout=2)
        assert calls == 2
    finally:
        release.set()
        runtime.close()


def test_checkout_request_admitted_during_followup_scan_gets_its_own_scan(
    tmp_path, monkeypatch,
) -> None:
    runtime = OrgFsRuntime(tmp_path, node_id="node-checkout", author="user:owner")
    space = runtime.facade.create_space("checkout late request")
    runtime.facade.write_text(space.space_id, "note.txt", "value")
    runtime.checkout(space.space_id, True)
    owner = runtime._checkouts[space.space_id]
    node = runtime.facade.stat(space.space_id, "note.txt")
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    admitted = threading.Event()
    lock = threading.Lock()
    calls = 0
    admissions = 0
    original_scan = owner.manager._scan_tree
    original_tell = owner._runtime.tell

    def scan():
        nonlocal calls
        with lock:
            calls += 1
            index = calls - 1
        if index < 2:
            entered[index].set()
            assert release[index].wait(3)
        return original_scan()

    def tell(handle, command):
        nonlocal admissions
        result = original_tell(handle, command)
        if getattr(command, "action", None) == "reconcile" and result is AdmissionResult.ACCEPTED:
            with lock:
                admissions += 1
                if admissions == 2:
                    admitted.set()
        return result

    owner.manager._scan_tree = scan
    monkeypatch.setattr(owner._runtime, "tell", tell)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(owner.reconcile)
            assert entered[0].wait(2)
            event = ChangeEvent(space.space_id, "modified", node, None)
            for _ in range(12):
                owner.notify_change(event)
            release[0].set()
            assert entered[1].wait(2)
            late = pool.submit(owner.reconcile)
            assert admitted.wait(2)
            release[1].set()
            first.result(timeout=2)
            late.result(timeout=2)
        assert calls == 3
    finally:
        for gate in release:
            gate.set()
        runtime.close()


def test_space_authority_restarts_without_repeating_an_admitted_commit(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path, node_id="node-space", author="user:owner")
    space = runtime.facade.create_space("space authority")
    original_node = runtime.facade.write_text(space.space_id, "seed.md", "before")
    store = runtime.stores[space.space_id]
    authority = runtime.facade._space_authorities[space.space_id]
    entered = threading.Event()
    release = threading.Event()
    original_commit = store.commit_many_with_outbox
    calls = 0

    def blocked_commit(*args, **kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(3)
        return original_commit(*args, **kwargs)

    store.commit_many_with_outbox = blocked_commit  # type: ignore[method-assign]
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            accepted = pool.submit(
                    runtime.facade.write_text,
                    space.space_id,
                    f"id:{original_node.node_id}",
                "committed once",
            )
            assert entered.wait(2)
            assert (
                authority._runtime.tell(authority._actor, object())
                is AdmissionResult.ACCEPTED
            )
            deadline = time.monotonic() + 3
            while (
                authority._runtime.snapshot(authority._actor).generation == 1
                and time.monotonic() < deadline
            ):
                time.sleep(0.005)
            assert authority._runtime.snapshot(authority._actor).generation > 1
            release.set()
            node = accepted.result(timeout=3)
        assert runtime.facade.read_text(space.space_id, f"id:{node.node_id}")[0] == (
            "committed once"
        )
        assert calls == 1
    finally:
        release.set()
        runtime.close()


def test_slow_space_commit_does_not_hold_other_space_facade_lock(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path, node_id="node-sharded", author="user:owner")
    first_space = runtime.facade.create_space("first")
    second_space = runtime.facade.create_space("second")
    first_node = runtime.facade.write_text(first_space.space_id, "a.md", "before")
    second_node = runtime.facade.write_text(second_space.space_id, "b.md", "before")
    store = runtime.stores[first_space.space_id]
    original_commit = store.commit_many_with_outbox
    entered = threading.Event()
    release = threading.Event()

    def blocked_commit(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original_commit(*args, **kwargs)

    store.commit_many_with_outbox = blocked_commit  # type: ignore[method-assign]
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            blocked = pool.submit(
                runtime.facade.write_text,
                first_space.space_id,
                f"id:{first_node.node_id}",
                "blocked durable write",
            )
            assert entered.wait(2)
            started = time.monotonic()
            updated = runtime.facade.write_text(
                second_space.space_id,
                f"id:{second_node.node_id}",
                "independent write",
            )
            assert time.monotonic() - started < 1.0
            assert runtime.facade.read_text(
                second_space.space_id, f"id:{updated.node_id}"
            )[0] == "independent write"
            release.set()
            blocked.result(timeout=3)
    finally:
        release.set()
        runtime.close()


def test_runtime_lazy_store_registry_is_singleton_under_cross_space_admission(
    tmp_path,
) -> None:
    runtime = OrgFsRuntime(tmp_path, node_id="node-registry", author="user:owner")
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            stores = tuple(pool.map(lambda _index: runtime.stores.get("same-space"), range(32)))
        assert {id(store) for store in stores} == {id(stores[0])}
        assert runtime.stores.snapshot_ids() == ("same-space",)
    finally:
        runtime.close()


def test_runtime_close_retries_after_a_partial_drain_timeout(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path, node_id="node-close", author="user:owner")
    space = runtime.facade.create_space("close retry")
    runtime.checkout(space.space_id, True)
    checkout = runtime._checkouts[space.space_id]
    original_close = checkout.close
    release = threading.Event()
    calls = 0

    def fail_until_released(*, timeout: float = 5.0, remove_tree: bool = False) -> bool:
        nonlocal calls
        calls += 1
        if not release.is_set():
            return False
        return original_close(timeout=timeout, remove_tree=remove_tree)

    checkout.close = fail_until_released  # type: ignore[method-assign]
    try:
        with pytest.raises(TimeoutError):
            runtime.close()
        with pytest.raises(TimeoutError):
            runtime.close()
        assert calls == 2
        assert not runtime._closed
        release.set()
        checkout.close = original_close  # type: ignore[method-assign]
        runtime.close()
        assert runtime._closed
    finally:
        release.set()
        checkout.close = original_close  # type: ignore[method-assign]
        runtime.close()


def test_mesh_creation_custody_coalesces_same_space_but_not_other_spaces(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path, node_id="node-mesh-open", author="user:owner")
    entered = {"same": threading.Event(), "other": threading.Event()}
    release = threading.Event()
    calls: list[str] = []
    calls_lock = threading.Lock()

    def create(space_id: str):
        with calls_lock:
            calls.append(space_id)
        entered[space_id].set()
        assert release.wait(3)
        return runtime_module._SpaceResourceHandle(space_id, 1)

    import hyprial.daemon.impl.orgfs.runtime as runtime_module

    runtime._create_mesh_sync = create  # type: ignore[method-assign]
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(runtime._ensure_mesh_sync, "same")
            assert entered["same"].wait(2)
            duplicate = pool.submit(runtime._ensure_mesh_sync, "same")
            other = pool.submit(runtime._ensure_mesh_sync, "other")
            assert entered["other"].wait(2)
            assert not duplicate.done()
            release.set()
            assert first.result(timeout=2).space_id == "same"
            assert duplicate.result(timeout=2).space_id == "same"
            assert other.result(timeout=2).space_id == "other"
        assert sorted(calls) == ["other", "same"]
    finally:
        release.set()
        runtime.close()


def test_directory_effect_overload_defers_without_blocking_completion(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path, node_id="node-directory", author="user:owner")
    runtime._start_directory_ingress()
    lane = runtime._directory_effects
    assert lane is not None
    lane._capacity = 1  # type: ignore[attr-defined]
    entered = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    calls = 0

    def execute(_batch: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(3)
        else:
            second_started.set()

    runtime._execute_directory_effect = execute  # type: ignore[method-assign]
    runtime._apply_announcement_command = lambda _sample: [  # type: ignore[method-assign]
        _DirectoryEffect("announce")
    ]
    first = _DirectoryCommand(
        "directory-first", 1, "announce", TransportSample("key", b"payload")
    )
    second = _DirectoryCommand(
        "directory-second", 1, "announce", TransportSample("key", b"payload")
    )
    try:
        runtime._on_directory_command(first)
        assert entered.wait(2)
        started = time.monotonic()
        runtime._on_directory_command(second)
        assert time.monotonic() - started < 0.5
        assert len(runtime._directory_deferred) == 1
        release.set()
        assert second_started.wait(2)
        assert calls == 2
    finally:
        release.set()
        runtime.close()
        assert runtime._closed


def test_broadcast_runs_after_space_authority_lock_is_released() -> None:
    class Store:
        def commit(self, _doc_id: str, mutate: object, **_metadata: object) -> None:
            mutate(None)  # type: ignore[operator]

    facade = LocalOrgFs(stores=Store())
    space = facade.create_space("broadcast boundary")
    observed: list[bool] = []
    delivered = threading.Event()

    class Mesh:
        def broadcast_pending(self, _space_id: str) -> None:
            acquired = facade._lock.acquire(blocking=False)
            observed.append(acquired)
            if acquired:
                facade._lock.release()
            delivered.set()

    facade.mesh = Mesh()
    facade.mkdir(space.space_id, "child")

    assert delivered.wait(2)
    assert observed == [True]
