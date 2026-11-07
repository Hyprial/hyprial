"""Authority-correction contracts for the space writer lane and its fences.

测试契约:purge 与 writer 投影操作必须经 space authority 的 writer lane
执行(线程名 orgfs-space-<space>-writer-*)。

CI red(2026-11-03,runs 44167/44177/44123 shard 4):`unbroadcast` 断言把
broadcast-drain probe 也算进 writer-lane-only——aae66d11ff(fix(orgfs):
decouple commit broadcast effects)起,`FacadeEffects._space_has_unbroadcast`
会在 orgfs-post-commit-network-* 与 effect completion actor(_RuntimeActor-*)
线程上直接只读调用 `store.unbroadcast()` 判断 outbox 是否仍有积压;store
自有 RLock,该探针线程安全且故意不串行到 authority lane(避免广播排空
被 writer 队头阻塞)。本地稳定绿只是探针通常落在测试 wrap 之前的窗口;
CI 慢调度下落进 wrap 之后即红。因此:其余九个方法保持 writer-lane 严格
断言,`unbroadcast` 只要求至少一次 writer-lane 调用(purge 发布前沿的
authority read),容忍 drain-probe 的额外条目。产品行为不改。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading
import time

import pytest

from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.docs import LocalOrgFs
from hyprial.daemon.impl.orgfs.storage.replica import MemoryReplicaBackend, ReplicaStore
from hyprial.daemon.impl.orgfs.storage.replica_authority import ReplicaAuthority
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.composition.vocabulary import _CheckoutSpace
from hyprial.daemon.impl.orgfs.storage.space_authority import OrgSpaceAuthority
from hyprial.daemon.impl.orgfs.storage.store import StoreError


def _wait_for(predicate) -> None:
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.005)


def _restart(runtime, actor) -> int:
    generation = runtime.snapshot(actor).generation
    assert runtime.tell(actor, object()) is AdmissionResult.ACCEPTED
    _wait_for(lambda: runtime.snapshot(actor).generation > generation)
    return runtime.snapshot(actor).generation


def test_space_total_custody_bounds_deferred_and_rebases_restart(monkeypatch) -> None:
    monkeypatch.setattr('hyprial.daemon.impl.orgfs.storage.space_authority._SPACE_AUTHORITY_CAPACITY', 2)
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    class Store:
        def commit_with_outbox(self, doc_id, _mutate, **_metadata):
            calls.append(doc_id)
            if doc_id == "first":
                entered.set()
                assert release.wait(3)
            return doc_id, ()

    authority = OrgSpaceAuthority("space", Store())
    authority._effects._capacity = 1
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                authority.commit, "first", b"one", author="user:a", actor=None
            )
            assert entered.wait(1)
            second = pool.submit(
                authority.commit, "second", b"two", author="user:a", actor=None
            )
            _wait_for(lambda: len(authority._deferred) == 1)
            with pytest.raises(StoreError) as rejected:
                authority.commit("third", b"three", author="user:a", actor=None)
            assert rejected.value.code == "resource-exhausted"
            generation = _restart(authority._runtime, authority._actor)
            assert len(authority._commands) == 2
            _wait_for(
                lambda: [item.generation for item in authority._deferred]
                == [generation]
            )
            assert [item.generation for item in authority._deferred] == [generation]
            release.set()
            assert first.result(timeout=2) == ("first", (), ())
            assert second.result(timeout=2) == ("second", (), ())
        assert calls == ["first", "second"]
    finally:
        release.set()
        assert authority.close(timeout=2)


def test_facade_total_custody_bounds_deferred_and_rebases_restart(monkeypatch) -> None:
    monkeypatch.setattr('hyprial.daemon.impl.orgfs.document.space_state._SPACE_STATE_CAPACITY', 2)
    facade = LocalOrgFs()
    space = facade.create_space("bounded facade")
    facade.mkdir(space.space_id, "seed")
    owner = facade._space_state_owners[space.space_id]
    owner.effects._capacity = 1
    entered = threading.Event()
    release = threading.Event()
    original = facade._execute_space_state

    def execute(space_id, command):
        if not entered.is_set():
            entered.set()
            assert release.wait(3)
        return original(space_id, command)

    facade._execute_space_state = execute
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(facade.mkdir, space.space_id, "first")
            assert entered.wait(1)
            second = pool.submit(facade.mkdir, space.space_id, "second")
            _wait_for(lambda: len(owner.deferred) == 1)
            with pytest.raises(OrgFsError) as rejected:
                facade.mkdir(space.space_id, "third")
            assert rejected.value.code == "resource-exhausted"
            generation = _restart(owner.runtime, owner.actor)
            assert len(owner.commands) == 2
            _wait_for(
                lambda: [item.generation for item in owner.deferred] == [generation]
            )
            assert [item.generation for item in owner.deferred] == [generation]
            release.set()
            first.result(timeout=2)
            second.result(timeout=2)
        assert facade.resolve(space.space_id, "first")
        assert facade.resolve(space.space_id, "second")
        with pytest.raises(OrgFsError, match="unknown-doc"):
            facade.resolve(space.space_id, "third")
    finally:
        release.set()
        assert facade.close_effects(timeout=2)


def test_replica_total_custody_bounds_deferred_and_rebases_restart(monkeypatch) -> None:
    monkeypatch.setattr('hyprial.daemon.impl.orgfs.storage.replica_authority._REPLICA_CAPACITY', 2)
    backend = MemoryReplicaBackend("space")
    store = ReplicaStore(
        "space",
        backend,
        SimpleNamespace(purge_listed=lambda _digest: False),
    )
    authority = ReplicaAuthority(store, SimpleNamespace(), None)
    authority._effects._capacity = 1
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    original = backend.put

    def put(key: str, data: bytes):
        calls.append(key)
        if key == "log/space/doc/writer/1":
            entered.set()
            assert release.wait(3)
        return original(key, data)

    backend.put = put
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(authority.backend.put, "log/space/doc/writer/1", b"one")
            assert entered.wait(1)
            second = pool.submit(authority.backend.put, "log/space/doc/writer/2", b"two")
            _wait_for(lambda: len(authority._deferred) == 1)
            with pytest.raises(OrgFsError) as rejected:
                authority.backend.put("log/space/doc/writer/3", b"three")
            assert rejected.value.code == "resource-exhausted"
            generation = _restart(authority._runtime, authority._actor)
            assert len(authority._commands) == 2
            _wait_for(
                lambda: [item.generation for item in authority._deferred]
                == [generation]
            )
            assert [item.generation for item in authority._deferred] == [generation]
            release.set()
            first.result(timeout=2)
            second.result(timeout=2)
        assert calls == ["log/space/doc/writer/1", "log/space/doc/writer/2"]
    finally:
        release.set()
        assert authority.close(timeout=2)


def test_concurrent_serve_constructs_one_replica_per_space(monkeypatch, tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path, node_id="node-serve", author="user:owner")
    same = runtime.facade.create_space("same")
    other = runtime.facade.create_space("other")
    entered = threading.Event()
    release = threading.Event()
    duplicate_blocked = threading.Event()

    class ObservedCreationLock:
        def __init__(self):
            self.lock = threading.Lock()

        def __enter__(self):
            if not self.lock.acquire(blocking=False):
                duplicate_blocked.set()
                self.lock.acquire()
            return self

        def __exit__(self, *_args):
            self.lock.release()

    runtime._replica_creation_locks[same.space_id] = ObservedCreationLock()
    constructed: list[str] = []
    primed: list[str] = []
    original_init = ReplicaAuthority.__init__
    original_prime = ReplicaAuthority.prime_from

    def counted_init(self, store, source_store, blob_store):
        constructed.append(store.space_id)
        original_init(self, store, source_store, blob_store)

    def blocked_prime(self, store, blob_store=None):
        primed.append(self.space_id)
        if self.space_id == same.space_id:
            entered.set()
            assert release.wait(3)
        return original_prime(self, store, blob_store)

    monkeypatch.setattr(ReplicaAuthority, "__init__", counted_init)
    monkeypatch.setattr(ReplicaAuthority, "prime_from", blocked_prime)
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(runtime.serve, same.space_id, "memory")
            assert entered.wait(1)
            duplicate = pool.submit(runtime.serve, same.space_id, "memory")
            independent = pool.submit(runtime.serve, other.space_id, "memory")
            assert independent.result(timeout=2)["spaceId"] == other.space_id
            # A submitted future is not evidence that the duplicate reached
            # this space's creation fence while its first replica was held.
            assert duplicate_blocked.wait(1)
            assert constructed.count(same.space_id) == 1
            assert primed.count(same.space_id) == 1
            release.set()
            assert first.result(timeout=2)["spaceId"] == same.space_id
            assert duplicate.result(timeout=2)["spaceId"] == same.space_id
        assert constructed.count(same.space_id) == 1
        assert primed.count(same.space_id) == 2
        assert constructed.count(other.space_id) == 1
    finally:
        release.set()
        runtime.close()


def test_purge_and_writer_projection_use_the_space_authority(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path, node_id="node-purge", author="user:owner")
    space = runtime.facade.create_space("typed purge")
    node = runtime.facade.write_text(space.space_id, "note.md", "secret")
    store = runtime.stores.get(space.space_id)
    store.snapshot(node.doc_id, shallow_since=None)
    methods = (
        "purge_inventory",
        "frontier",
        "save_purge_plan",
        "load_purge_plan",
        "install_replacement",
        "writer_seq_watermarks",
        "unbroadcast",
        "delete_retired_objects",
        "retired_residue",
        "writer_attributions",
    )
    observed: dict[str, list[str]] = {name: [] for name in methods}

    for name in methods:
        original = getattr(store, name)

        def wrapped(*args, _name=name, _original=original, **kwargs):
            observed[_name].append(threading.current_thread().name)
            return _original(*args, **kwargs)

        setattr(store, name, wrapped)

    try:
        plan = runtime.purge_plan(
            space.space_id,
            [
                {
                    "kind": "doc-range",
                    "node": f"id:{node.node_id}",
                    "from": node.version,
                    "to": node.version,
                }
            ],
        )
        runtime.facade._purge_plans.clear()
        assert runtime.purge(space.space_id, plan.plan_id).executed_locally
        assert runtime.writer_attributions(space.space_id)
        writer_prefix = f"orgfs-space-{space.space_id[:12]}-writer-"
        assert all(observed[name] for name in methods)
        assert all(
            all(thread.startswith(writer_prefix) for thread in thread_names)
            for name, thread_names in observed.items()
            if name != "unbroadcast"
        )
        assert any(
            thread.startswith(writer_prefix) for thread in observed["unbroadcast"]
        )
        # Off-lane unbroadcast reads are tolerated only from the known
        # broadcast-drain probe threads, never from an arbitrary caller.
        assert all(
            thread.startswith(
                (writer_prefix, "orgfs-post-commit-network-", "_RuntimeActor-")
            )
            for thread in observed["unbroadcast"]
        )
    finally:
        runtime.close()


def test_public_checkout_uses_lifecycle_command(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path, node_id="node-checkout", author="user:owner")
    observed: list[object] = []
    original = runtime._ask_lifecycle
    runtime._ask_lifecycle = lambda operation: observed.append(operation) or {
        "spaceId": "space",
        "enabled": True,
    }
    try:
        assert runtime.checkout("space", True)["enabled"] is True
        assert observed == [_CheckoutSpace("space", True)]
    finally:
        runtime._ask_lifecycle = original
        runtime.close()


def test_purge_plan_authority_preserves_json_container_shapes(tmp_path):
    from hyprial.daemon.impl.orgfs.storage.space_authority import _ReadStore

    runtime = OrgFsRuntime(tmp_path, node_id="node-json", author="user:owner")
    space = runtime.facade.create_space("shape")
    store = runtime.stores.get(space.space_id)
    authority = runtime.facade.space_authority(space.space_id, store)
    plan = {"emptyList": [], "emptyObject": {}, "pairs": [["key", "value"]]}
    targets = ({"kind": "blob", "sha256": "synthetic"},)
    try:
        authority.save_purge_plan("shape-plan", plan, targets)
        loaded = LocalOrgFs._thaw_space_value(
            authority.read(_ReadStore("load_purge_plan", plan_id="shape-plan"))
        )
        assert loaded[0] == plan
        assert loaded[1] == list(targets)
        loaded[0]["emptyList"].append("caller edit")
        fresh = LocalOrgFs._thaw_space_value(
            authority.read(_ReadStore("load_purge_plan", plan_id="shape-plan"))
        )
        assert fresh[0] == plan
    finally:
        runtime.close()
