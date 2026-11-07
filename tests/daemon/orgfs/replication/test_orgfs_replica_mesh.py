from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from fnmatch import fnmatch
import hashlib
import json
import threading
import time

from pycrdt import Map
import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.storage.blobs import encode_request
from hyprial.daemon.impl.orgfs.storage.replica import encode_snapshot_frontier
from hyprial.daemon.impl.orgfs.storage.replica_authority import ReplicaAuthority
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.storage.store import ORGFS_INLINE_UPDATE_BYTES, assert_client_ids_registered
from hyprial.daemon.impl.transport import KeySpace, TransportSample


class _Registration:
    def __init__(self, close) -> None:
        self._close = close

    def close(self) -> None:
        self._close()


class _Bus:
    def __init__(self) -> None:
        self.subscribers = []
        self.handlers = []

    def session(self):
        bus = self

        class Session:
            def put(self, key, payload):
                for expression, callback in tuple(bus.subscribers):
                    if fnmatch(key, expression.replace("**", "*")):
                        callback(TransportSample(key, payload))

            def subscribe(self, key_expr, callback):
                row = (key_expr, callback)
                bus.subscribers.append(row)
                return _Registration(
                    lambda: (
                        bus.subscribers.remove(row) if row in bus.subscribers else None
                    )
                )

            def declare_query_handler(self, key_expr, handler):
                row = (key_expr, handler)
                bus.handlers.append(row)
                return _Registration(
                    lambda: bus.handlers.remove(row) if row in bus.handlers else None
                )

            def query(self, key_expr, payload, **_kwargs):
                replies = []
                for expression, handler in tuple(bus.handlers):
                    if fnmatch(key_expr, expression.replace("**", "*")):
                        replies.extend(
                            TransportSample(key, body)
                            for key, body in handler(key_expr, payload)
                        )
                return replies

        return Session()


@pytest.fixture(autouse=True)
def isolated_orgfs_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


def _assert_runtime_client_ids(*runtimes: OrgFsRuntime) -> None:
    for runtime in runtimes:
        for store in runtime.stores.values():
            assert_client_ids_registered(store)


def _eventually(action, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return action()
        except (AssertionError, OrgFsError) as error:
            last_error = error
            time.sleep(0.005)
    if last_error is not None:
        raise last_error
    raise AssertionError("condition did not become true")


def _assert_text(runtime: OrgFsRuntime, space_id: str, node_id: str, text: str) -> None:
    assert runtime.facade.read_text(space_id, f"id:{node_id}")[0] == text


def _assert_broadcast_drained(runtime: OrgFsRuntime, space_id: str) -> None:
    assert not tuple(runtime.stores[space_id].unbroadcast())


def _store_seqs(runtime: OrgFsRuntime, space_id: str, doc_id: str) -> tuple[int, ...]:
    try:
        page = runtime.stores[space_id].export_since(
            doc_id, b"", cursor=None, max_bytes=1_048_576
        )
    except Exception:
        return ()
    return tuple(
        int(json.loads(envelope)["seq"])
        for envelope in page.envelopes
        if json.loads(envelope).get("docId") == doc_id
    )


def _page_seqs(page: dict) -> tuple[int, ...]:
    return tuple(
        int(json.loads(base64.b64decode(row))["seq"])
        for row in page["envelopes"]
        if json.loads(base64.b64decode(row)).get("docId") != "meta"
    )


def _await_broadcast_drained(runtime: OrgFsRuntime, space_id: str) -> None:
    def check():
        assert not tuple(runtime.stores[space_id].unbroadcast())

    _eventually(check, timeout=30)


def _await_resident_journal(
    runtime: OrgFsRuntime, space_id: str, doc_id: str, expected: tuple[int, ...]
) -> None:
    def check():
        assert _store_seqs(runtime, space_id, doc_id) == expected

    _eventually(check, timeout=30)


def _await_replica_serving(runtime: OrgFsRuntime, space_id: str) -> None:
    def check():
        assert runtime._meshes[space_id]._replica_serving is True

    _eventually(check)


def _log_key(space_id: str, envelope: bytes) -> str:
    value = json.loads(envelope)
    origin = value["origin"]
    return (
        f"log/{space_id}/{value['docId']}/{origin['writer']}/{value['seq']}"
    )


def _gate_second_prime(
    monkeypatch: pytest.MonkeyPatch,
    prime_started: threading.Event,
    release: threading.Event,
) -> None:
    """Hold the second prime of a replica authority until the test releases it.

    The first prime (lifecycle create path, or the gated attach prime) runs
    through; the second call is where the attach/completion scan has finished
    exporting the journal, so writes arriving during the hold exercise the
    attach-window handling deterministically.
    """

    original = ReplicaAuthority.prime_from
    counts: dict[int, int] = {}

    def gated(authority: ReplicaAuthority, store, blob_store=None):
        index = counts.get(id(authority), 0)
        counts[id(authority)] = index + 1
        original(authority, store, blob_store)
        if index == 1:
            prime_started.set()
            if not release.wait(60):
                raise TimeoutError("gated replica prime was not released")

    monkeypatch.setattr(ReplicaAuthority, "prime_from", gated)


def _sync_all_pages(mesh, request: dict) -> tuple[int, ...]:
    seen: list[int] = []
    cursor = None
    for _page in range(64):
        page = json.loads(mesh._sync_page({**request, "cursor": cursor}))
        seen.extend(_page_seqs(page))
        cursor = page["next"]
        if cursor is None:
            break
    else:
        raise AssertionError("sync pagination did not terminate")
    return tuple(sorted(seen))


def _window_request(space_id: str, doc_id: str) -> dict:
    return {
        "schemaVersion": 1,
        "type": "orgfs-sync",
        "spaceId": space_id,
        "docIds": [doc_id],
        "versionVectors": {},
        "requester": {"node": "node-reader", "author": "user:reader"},
        "cursor": None,
        "maxBytes": 1_048_576,
    }


def _joined_topology(tmp_path):
    bus = _Bus()

    def online(_node):
        return True

    # Replica topology models private sidecar admission (§7), including cold joins.
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-owner", author="user:owner", trust_tailnet_members=True)
    resident = OrgFsRuntime(
        tmp_path / "resident", node_id="node-resident", author="user:resident", trust_tailnet_members=True
    )
    reader = OrgFsRuntime(
        tmp_path / "reader", node_id="node-reader", author="user:reader", trust_tailnet_members=True
    )
    owner.bind_transport(bus.session(), supplier_online=online)
    resident.bind_transport(
        bus.session(), supplier_online=online, holder_candidates=lambda: ("node-owner",)
    )
    reader.bind_transport(
        bus.session(),
        supplier_online=online,
        holder_candidates=lambda: ("node-owner", "node-resident"),
    )
    space = owner.facade.create_space("team")
    owner.facade.invite(space.space_id, "user:resident", "ro")
    owner.facade.invite(space.space_id, "user:reader", "rw")
    owner._mesh(space.space_id).announce()
    resident.facade.join(space.space_id)
    reader.facade.join(space.space_id)
    _assert_runtime_client_ids(owner, resident, reader)
    return owner, resident, reader, space


def test_all_members_offline_replica_serves_new_member_clone_and_catchup(
    tmp_path,
) -> None:
    owner, resident, reader, space = _joined_topology(tmp_path)
    newcomer = None
    restarted_owner = None
    try:
        node = owner.facade.write_text(space.space_id, "shared.md", "resident copy")
        resident.serve(space.space_id, "memory")
        owner.close()
        reader.close()
        newcomer = OrgFsRuntime(
            tmp_path / "newcomer", node_id="node-new", author="user:reader",
            trust_tailnet_members=True,  # Cold clone on the admitted private path (§7).
        )
        newcomer.bind_transport(
            resident._session,
            supplier_online=lambda _node: True,
            holder_candidates=lambda: ("node-resident",),
        )
        newcomer.facade.join(space.space_id)
        assert newcomer.facade.read_text(space.space_id, f"id:{node.node_id}")[0] == (
            "resident copy"
        )
        newcomer.facade.write_text(
            space.space_id, f"id:{node.node_id}", "newcomer catch-up"
        )
        restarted_owner = OrgFsRuntime(
            tmp_path / "owner", node_id="node-owner", author="user:owner",
            trust_tailnet_members=True,  # Same topology after restart (§7).
        )
        restarted_owner.bind_transport(
            resident._session,
            supplier_online=lambda _node: True,
            holder_candidates=lambda: ("node-resident", "node-new"),
        )
        assert restarted_owner._meshes[space.space_id].sync_from("node-resident") > 0
        assert (
            restarted_owner.facade.read_text(space.space_id, f"id:{node.node_id}")[0]
            == "newcomer catch-up"
        )
        assert_client_ids_registered(newcomer.stores[space.space_id])
        assert_client_ids_registered(restarted_owner.stores[space.space_id])
    finally:
        if restarted_owner is not None:
            restarted_owner.close()
        if newcomer is not None:
            newcomer.close()
        if reader._session is not None:
            reader.close()
        resident.close()
        if owner._session is not None:
            owner.close()


def test_attach_window_broadcast_is_backfilled_before_replica_serving(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, resident, reader, space = _joined_topology(tmp_path)
    newcomer = None
    release = threading.Event()
    prime_started = threading.Event()
    _gate_second_prime(monkeypatch, prime_started, release)
    try:
        node = owner.facade.write_text(space.space_id, "window.md", "attach v0")
        _await_broadcast_drained(owner, space.space_id)
        _await_resident_journal(resident, space.space_id, node.doc_id, (0,))
        with ThreadPoolExecutor(max_workers=1) as pool:
            serving = pool.submit(resident.serve, space.space_id, "memory")
            assert prime_started.wait(60), "gated prime never started"
            owner.facade.write_text(
                space.space_id, f"id:{node.node_id}", "attach v1"
            )
            _await_broadcast_drained(owner, space.space_id)
            _await_resident_journal(resident, space.space_id, node.doc_id, (0, 1))
            release.set()
            serving.result(timeout=120)
        if owner._session is not None:
            owner.close()
        if reader._session is not None:
            reader.close()
        newcomer = OrgFsRuntime(
            tmp_path / "newcomer-window",
            node_id="node-new-window",
            author="user:reader",
            trust_tailnet_members=True,
        )
        newcomer.bind_transport(
            resident._session,
            supplier_online=lambda _node: True,
            holder_candidates=lambda: ("node-resident",),
        )
        newcomer.facade.join(space.space_id)
        assert newcomer.facade.read_text(
            space.space_id, f"id:{node.node_id}"
        )[0] == "attach v1"
        assert_client_ids_registered(newcomer.stores[space.space_id])
    finally:
        if newcomer is not None:
            newcomer.close()
        resident.close()
        if owner._session is not None:
            owner.close()


def test_attach_window_mirror_put_stays_fast_and_is_not_lost(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, resident, reader, space = _joined_topology(tmp_path)
    release = threading.Event()
    prime_started = threading.Event()
    _gate_second_prime(monkeypatch, prime_started, release)
    try:
        node = owner.facade.write_text(space.space_id, "mirror.md", "mirror v0")
        _await_broadcast_drained(owner, space.space_id)
        _await_resident_journal(resident, space.space_id, node.doc_id, (0,))
        with ThreadPoolExecutor(max_workers=1) as pool:
            serving = pool.submit(resident.serve, space.space_id, "memory")
            assert prime_started.wait(60), "gated prime never started"
            owner.facade.write_text(
                space.space_id, f"id:{node.node_id}", "mirror v1"
            )
            _await_broadcast_drained(owner, space.space_id)
            _await_resident_journal(resident, space.space_id, node.doc_id, (0, 1))
            page = resident.stores[space.space_id].export_since(
                node.doc_id, b"", cursor=None, max_bytes=1_048_576
            )
            envelope = bytes(page.envelopes[-1])
            key = _log_key(space.space_id, envelope)
            mesh = resident._meshes[space.space_id]
            started = time.perf_counter()
            mesh._store_replica_envelope(envelope)
            elapsed = time.perf_counter() - started
            assert elapsed < 1.0, (
                f"broadcaster mirror put blocked {elapsed:.3f}s during attach"
            )
            release.set()
            serving.result(timeout=120)
        assert mesh.replica_store is not None
        assert mesh.replica_store.backend_get(key) == envelope
    finally:
        if reader._session is not None:
            reader.close()
        resident.close()
        if owner._session is not None:
            owner.close()


def test_sync_pages_stay_complete_across_the_attach_boundary(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, resident, reader, space = _joined_topology(tmp_path)
    release = threading.Event()
    prime_started = threading.Event()
    _gate_second_prime(monkeypatch, prime_started, release)
    try:
        node = owner.facade.write_text(space.space_id, "pages.md", "pages v0")
        _await_broadcast_drained(owner, space.space_id)
        _await_resident_journal(resident, space.space_id, node.doc_id, (0,))
        with ThreadPoolExecutor(max_workers=1) as pool:
            serving = pool.submit(resident.serve, space.space_id, "memory")
            assert prime_started.wait(60), "gated prime never started"
            owner.facade.write_text(
                space.space_id, f"id:{node.node_id}", "pages v1"
            )
            _await_broadcast_drained(owner, space.space_id)
            _await_resident_journal(resident, space.space_id, node.doc_id, (0, 1))
            mesh = resident._meshes[space.space_id]
            request = _window_request(space.space_id, node.doc_id)
            assert _sync_all_pages(mesh, request) == (0, 1), (
                "attach-window sync skipped a journal envelope"
            )
            release.set()
            serving.result(timeout=120)
        assert _sync_all_pages(mesh, request) == (0, 1), (
            "post-attach replica sync skipped a journal envelope"
        )
    finally:
        if reader._session is not None:
            reader.close()
        resident.close()
        if owner._session is not None:
            owner.close()


def test_re_serve_reused_replica_backfills_journal_delta(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, resident, reader, space = _joined_topology(tmp_path)
    newcomer = None
    try:
        node = owner.facade.write_text(space.space_id, "delta.md", "delta v0")
        _await_broadcast_drained(owner, space.space_id)
        _await_resident_journal(resident, space.space_id, node.doc_id, (0,))
        resident.serve(space.space_id, "memory")
        _await_replica_serving(resident, space.space_id)
        mesh = resident._meshes[space.space_id]

        def drop_mirror(envelope: bytes) -> None:
            return None

        mesh._store_replica_envelope = drop_mirror
        owner.facade.write_text(space.space_id, f"id:{node.node_id}", "delta v1")
        _await_broadcast_drained(owner, space.space_id)
        _await_resident_journal(resident, space.space_id, node.doc_id, (0, 1))
        del mesh._store_replica_envelope
        page = resident.stores[space.space_id].export_since(
            node.doc_id, b"", cursor=None, max_bytes=1_048_576
        )
        envelope = bytes(page.envelopes[-1])
        key = _log_key(space.space_id, envelope)
        resident.serve(space.space_id, "memory")
        replica = resident._replicas[space.space_id]
        assert replica.backend_get(key) == envelope
        if owner._session is not None:
            owner.close()
        if reader._session is not None:
            reader.close()
        newcomer = OrgFsRuntime(
            tmp_path / "newcomer-delta",
            node_id="node-new-delta",
            author="user:reader",
            trust_tailnet_members=True,
        )
        newcomer.bind_transport(
            resident._session,
            supplier_online=lambda _node: True,
            holder_candidates=lambda: ("node-resident",),
        )
        newcomer.facade.join(space.space_id)
        assert newcomer.facade.read_text(
            space.space_id, f"id:{node.node_id}"
        )[0] == "delta v1"
        assert_client_ids_registered(newcomer.stores[space.space_id])
    finally:
        if newcomer is not None:
            newcomer.close()
        if reader._session is not None:
            reader.close()
        resident.close()
        if owner._session is not None:
            owner.close()


def test_close_drains_lagging_commit_effect_before_replica_clone(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, resident, reader, space = _joined_topology(tmp_path)
    newcomer = None
    release = threading.Event()
    try:
        resident.serve(space.space_id, "memory")
        _eventually(
            lambda: (
                not tuple(owner.stores[space.space_id].unbroadcast())
                and not owner.facade._network_active_spaces
            )
        )
        effect_entered = threading.Event()
        original_effect = owner.facade._execute_network_effect

        def delayed_effect(space_id: str):
            effect_entered.set()
            if not release.wait(5):
                raise TimeoutError("test commit effect remained delayed")
            return original_effect(space_id)

        monkeypatch.setattr(
            owner.facade, "_execute_network_effect", delayed_effect
        )
        node = owner.facade.write_text(
            space.space_id, "close-drain.md", "resident copy"
        )
        assert effect_entered.wait(2)
        with ThreadPoolExecutor(max_workers=1) as pool:
            closing = pool.submit(owner.close)
            with pytest.raises(FutureTimeout):
                closing.result(timeout=0.1)
            release.set()
            closing.result(timeout=5)

        reader.close()
        newcomer = OrgFsRuntime(
            tmp_path / "newcomer-close-drain",
            node_id="node-new-close-drain",
            author="user:reader",
            trust_tailnet_members=True,  # Clone/drain after private admission (§7).
        )
        newcomer.bind_transport(
            resident._session,
            supplier_online=lambda _node: True,
            holder_candidates=lambda: ("node-resident",),
        )
        newcomer.facade.join(space.space_id)
        assert newcomer.facade.read_text(
            space.space_id, f"id:{node.node_id}"
        )[0] == "resident copy"
    finally:
        release.set()
        if newcomer is not None:
            newcomer.close()
        if reader._session is not None:
            reader.close()
        resident.close()
        if owner._session is not None:
            owner.close()


def test_replica_down_members_still_converge(tmp_path) -> None:
    owner, resident, reader, space = _joined_topology(tmp_path)
    try:
        resident.serve(space.space_id, "memory")
        resident.close()
        node = owner.facade.write_text(space.space_id, "live.md", "member path")
        _eventually(
            lambda: _assert_text(
                reader, space.space_id, node.node_id, "member path"
            )
        )
        assert_client_ids_registered(owner.stores[space.space_id])
        assert_client_ids_registered(reader.stores[space.space_id])
    finally:
        reader.close()
        if resident._session is not None:
            resident.close()
        owner.close()


def test_backend_conflict_key_e2e_event_and_owner_notice(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str, dict[str, object]]] = []
    notices: list[tuple[str, str, dict[str, object]]] = []
    bus = _Bus()
    # Backend conflict handling after private sidecar admission (§7).
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-owner", author="user:owner", trust_tailnet_members=True)
    resident = OrgFsRuntime(
        tmp_path / "resident",
        node_id="node-resident",
        author="user:resident",
        trust_tailnet_members=True,
        logger=lambda level, event, **fields: events.append((level, event, fields)),
        owner_notifier=lambda owner, event, details: notices.append(
            (owner, event, details)
        ),
    )
    try:
        owner.bind_transport(bus.session(), supplier_online=lambda _node: True)
        resident.bind_transport(
            bus.session(),
            supplier_online=lambda _node: True,
            holder_candidates=lambda: ("node-owner",),
        )
        space = owner.facade.create_space("team")
        owner.facade.invite(space.space_id, "user:resident", "ro")
        owner._mesh(space.space_id).announce()
        resident.facade.join(space.space_id)
        resident.serve(space.space_id, "memory")
        node = owner.facade.write_text(space.space_id, "conflict.md", "before")

        mesh = owner._meshes[space.space_id]
        original_broadcast = mesh.broadcast_pending
        monkeypatch.setattr(mesh, "broadcast_pending", lambda _space_id=None: 0)
        owner.facade.write_text(space.space_id, f"id:{node.node_id}", "conflict")
        pending = tuple(owner.stores[space.space_id].unbroadcast())
        envelope = pending[-1].envelope_bytes
        value = json.loads(envelope)
        key = (
            f"log/{space.space_id}/{value['docId']}/"
            f"{value['origin']['writer']}/{value['seq']}"
        )
        replica = resident._replicas[space.space_id]
        assert (
            replica.backend.put(key, b"preexisting-different-bytes").value == "created"
        )
        monkeypatch.setattr(mesh, "broadcast_pending", original_broadcast)

        assert mesh.broadcast_pending() >= 1
        assert resident.facade.read_text(space.space_id, f"id:{node.node_id}")[0] == (
            "conflict"
        )
        assert replica.backend.get(key) == b"preexisting-different-bytes"
        assert {event for _level, event, _fields in events} >= {
            "orgfs.log-key-conflict",
            "orgfs.owner-notice",
        }
        assert notices and notices[-1][0:2] == (
            "user:owner",
            "log-key-conflict",
        )
        owner.facade.write_text(space.space_id, f"id:{node.node_id}", "after")
        _eventually(
            lambda: _assert_text(resident, space.space_id, node.node_id, "after")
        )
        _assert_runtime_client_ids(owner, resident)
    finally:
        resident.close()
        owner.close()


def test_memory_backend_replica_does_not_confirm_durability(tmp_path) -> None:
    owner, resident, reader, space = _joined_topology(tmp_path)
    try:
        result = resident.serve(space.space_id, "memory")
        assert result["durable"] is False
        owner.facade.write_text(space.space_id, "memory.md", "not durable")
        owner._meshes[space.space_id].probe("node-resident")
        assert owner.facade.status(space.space_id).unconfirmed_commits > 0
        assert (
            "node-resident"
            not in owner.facade.status(space.space_id).durable_holders_seen
        )
    finally:
        reader.close()
        resident.close()
        owner.close()


def test_fs_backend_replica_confirms_durability(tmp_path) -> None:
    owner, resident, reader, space = _joined_topology(tmp_path)
    try:
        result = resident.serve(space.space_id, "fs")
        assert result["durable"] is True
        owner.facade.write_text(space.space_id, "durable.md", "saved")
        _eventually(lambda: _assert_broadcast_drained(owner, space.space_id))
        owner._meshes[space.space_id].probe("node-resident")
        status = owner.facade.status(space.space_id)
        assert status.unconfirmed_commits == 0
        assert "node-resident" in status.durable_holders_seen
    finally:
        reader.close()
        resident.close()
        owner.close()


def test_last_holder_loss_reported_honestly(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path / "runtime", node_id="node-a", author="user:owner")
    try:
        space = runtime.facade.create_space("team")
        old_payload = b"only holder"
        current_payload = b"current"
        node = runtime.facade.write_bytes(space.space_id, "asset.bin", old_payload)
        old_version = node.version
        runtime.facade.write_bytes(
            space.space_id, f"id:{node.node_id}", current_payload
        )
        digest = hashlib.sha256(old_payload).hexdigest()
        runtime.blobs._path(digest).unlink()
        with pytest.raises(OrgFsError) as unavailable:
            runtime.facade.restore(space.space_id, f"id:{node.node_id}", old_version)
        assert unavailable.value.code == "blob-unavailable"
        assert unavailable.value.details["digest"] == digest
        assert runtime.facade.status(space.space_id).durable_holders_seen == ()
        assert (
            runtime.blobs.put(space.space_id, old_payload, reason="replica") == digest
        )
        restored = runtime.facade.restore(
            space.space_id, f"id:{node.node_id}", old_version
        )
        assert restored.blob_hash == digest
        assert runtime.facade.read_bytes(space.space_id, f"id:{node.node_id}") == (
            old_payload
        )
    finally:
        runtime.close()


def test_replica_sync_honors_version_vectors_and_orders_numeric_sequences(
    tmp_path,
) -> None:
    bus = _Bus()
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-owner", author="user:owner")
    try:
        owner.bind_transport(bus.session(), supplier_online=lambda _node: True)
        space = owner.facade.create_space("team")
        node = owner.facade.write_text(space.space_id, "ordered.md", "0")
        for value in range(1, 6):
            owner.facade.write_text(space.space_id, f"id:{node.node_id}", str(value))
        frontier = owner.stores[space.space_id].frontier(node.doc_id)
        for value in range(6, 13):
            owner.facade.write_text(space.space_id, f"id:{node.node_id}", str(value))
        owner.serve(space.space_id, "memory")
        request = {
            "schemaVersion": 1,
            "type": "orgfs-sync",
            "spaceId": space.space_id,
            "docIds": [node.doc_id],
            "versionVectors": {
                "meta": base64.b64encode(
                    owner.stores[space.space_id].frontier("meta")
                ).decode(),
                node.doc_id: base64.b64encode(frontier).decode(),
            },
            "requester": {"node": "node-owner", "author": "user:owner"},
            "cursor": None,
            "maxBytes": 1_048_576,
        }
        page = json.loads(owner._meshes[space.space_id]._sync_page(request))
        envelopes = [json.loads(base64.b64decode(row)) for row in page["envelopes"]]
        sequences = [
            int(row["seq"]) for row in envelopes if row["docId"] == node.doc_id
        ]
        assert sequences == sorted(sequences)
        assert sequences and min(sequences) >= 6
    finally:
        owner.close()


def test_m8_replica_mesh_pages_with_bounded_key_cursors(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = _Bus()
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-owner", author="user:owner")
    try:
        owner.bind_transport(bus.session(), supplier_online=lambda _node: True)
        space = owner.facade.create_space("team")
        node = owner.facade.write_text(space.space_id, "paged.md", "0")
        for value in range(1, 40):
            owner.facade.write_text(space.space_id, f"id:{node.node_id}", str(value))
        owner.serve(space.space_id, "memory")
        replica = owner._replicas[space.space_id]
        observed: list[tuple[str | None, int]] = []
        original = replica.serve_sync_page

        def observe(*args, **kwargs):
            observed.append((kwargs.get("after_key"), kwargs["limit"]))
            return original(*args, **kwargs)

        monkeypatch.setattr(replica, "serve_sync_page", observe)
        request = {
            "schemaVersion": 1,
            "type": "orgfs-sync",
            "spaceId": space.space_id,
            "docIds": None,
            "versionVectors": {},
            "requester": {"node": "node-owner", "author": "user:owner"},
            "cursor": None,
            "maxBytes": 16_384,
        }
        seen: list[str] = []
        while True:
            assert len(observed) < 100, "M8 replica cursor re-read the same batch"
            page = json.loads(owner._meshes[space.space_id]._sync_page(request))
            seen.extend(page["envelopes"])
            request["cursor"] = page["next"]
            if page["next"] is None:
                break

        assert len(observed) > 1
        assert observed[0][0] is None
        assert all(after_key is not None for after_key, _limit in observed[1:])
        assert {limit for _after_key, limit in observed} == {257}
        assert len(seen) == len(set(seen))
    finally:
        owner.close()


def test_m8_blob_handler_reports_disappearing_replica_blob(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = _Bus()
    runtime = OrgFsRuntime(tmp_path / "runtime", node_id="node-a", author="user:owner")
    try:
        runtime.bind_transport(bus.session(), supplier_online=lambda _node: True)
        space = runtime.facade.create_space("team")
        payload = b"racing replica blob"
        node = runtime.facade.write_bytes(space.space_id, "asset.bin", payload)
        assert node.blob_hash is not None
        runtime.serve(space.space_id, "memory")
        mesh = runtime._meshes[space.space_id]
        backend = runtime._replicas[space.space_id].backend
        original_get = backend.get
        blob_key = f"blob/{node.blob_hash}"
        reads = 0

        def disappearing_get(key: str):
            nonlocal reads
            if key == blob_key:
                reads += 1
                if reads > 1:
                    return None
            return original_get(key)

        monkeypatch.setattr(backend, "get", disappearing_get)
        reply = json.loads(
            tuple(
                mesh._handle_blob(
                    KeySpace().orgfs_blob(node.blob_hash),
                    encode_request(space.space_id, node.blob_hash, 0, None),
                )
            )[0][1]
        )

        assert reply["type"] == "orgfs-error"
        assert reply["code"] == "unknown-blob"
    finally:
        runtime.close()


def test_serve_pins_blobs_is_idempotent_and_wires_retention(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path / "runtime", node_id="node-a", author="user:owner")
    try:
        space = runtime.facade.create_space("team")
        payload = b"replica pinned"
        blob = runtime.facade.write_bytes(space.space_id, "asset.bin", payload)
        digest = hashlib.sha256(payload).hexdigest()
        text = runtime.facade.write_text(space.space_id, "retained.md", "one")
        runtime.facade.write_text(space.space_id, f"id:{text.node_id}", "two")
        first = runtime.serve(space.space_id, "memory")
        assert first["pinnedBlobs"] == [digest]
        assert (space.space_id, digest, "replica") in runtime.blobs.references()
        replica = runtime._replicas[space.space_id]
        assert runtime.serve(space.space_id, "memory")["pinnedBlobs"] == [digest]
        assert runtime._replicas[space.space_id] is replica
        with pytest.raises(OrgFsError) as replacement:
            runtime.serve(space.space_id, "fs")
        assert replacement.value.code == "invalid-argument"

        store = runtime.stores[space.space_id]
        manifest = store.snapshot(text.doc_id, shallow_since=None)

        def publish_retention_point() -> None:
            points = runtime.facade._space(space.space_id).meta.get(
                "snapshotPoints", type=Map
            )
            points[text.doc_id] = Map(
                {
                    "frontier": base64.b64encode(
                        encode_snapshot_frontier(
                            store.writer_seq_watermarks(text.doc_id)
                        )
                    ).decode(),
                    "snapshotId": manifest.snapshot_id,
                    "planId": None,
                }
            )

        runtime.facade._commit(
            runtime.facade._space(space.space_id),
            "meta",
            publish_retention_point,
        )
        assert tuple(replica.backend.keys(f"log/{space.space_id}/{text.doc_id}/"))
        runtime._reconcile_retirements(space.space_id)
        assert not tuple(replica.backend.keys(f"log/{space.space_id}/{text.doc_id}/"))

        plan = runtime.purge_plan(
            space.space_id,
            [
                {
                    "kind": "doc-range",
                    "node": f"id:{text.node_id}",
                    "from": "all",
                    "to": "all",
                }
            ],
        )
        runtime.purge(space.space_id, plan.plan_id)
        replacement_doc = runtime.facade.stat(
            space.space_id, f"id:{text.node_id}"
        ).doc_id
        assert replacement_doc is not None
        point = runtime.stores[space.space_id].snapshot_point(replacement_doc)
        assert point is not None
        assert json.loads(point)["writers"]
        assert not tuple(
            replica.backend.keys(f"log/{space.space_id}/{replacement_doc}/")
        )
        assert (
            runtime.facade.read_bytes(space.space_id, f"id:{blob.node_id}") == payload
        )
    finally:
        runtime.close()


def test_r4_retention_then_purge_deletes_real_update_blob_residue(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path / "runtime", node_id="node-a", author="user:owner")
    try:
        space = runtime.facade.create_space("team")
        marker = "R4-RETENTION-PURGE-UPDATE-BLOB"
        content = marker + "x" * (ORGFS_INLINE_UPDATE_BYTES + 64_000)
        node = runtime.facade.write_text(space.space_id, "history.md", content)
        store = runtime.stores[space.space_id]
        envelope = next(
            json.loads(bytes(row[0]))
            for row in store._db.execute(
                "SELECT envelope_bytes FROM commits WHERE doc_id = ? ORDER BY rowid",
                (node.doc_id,),
            )
            if "updateBlob" in json.loads(bytes(row[0]))
        )
        digest = str(envelope["updateBlob"])
        runtime.serve(space.space_id, "memory")
        replica = runtime._replicas[space.space_id]
        assert replica.backend.get(f"blob/{digest}") is not None

        manifest = store.snapshot(node.doc_id, shallow_since=None)

        def publish_retention_point() -> None:
            points = runtime.facade._space(space.space_id).meta.get(
                "snapshotPoints", type=Map
            )
            points[node.doc_id] = Map(
                {
                    "frontier": base64.b64encode(
                        encode_snapshot_frontier(
                            store.writer_seq_watermarks(node.doc_id)
                        )
                    ).decode(),
                    "snapshotId": manifest.snapshot_id,
                    "planId": None,
                }
            )

        runtime.facade._commit(
            runtime.facade._space(space.space_id),
            "meta",
            publish_retention_point,
        )
        runtime._reconcile_retirements(space.space_id)
        assert not tuple(replica.backend.keys(f"log/{space.space_id}/{node.doc_id}/"))
        assert replica.backend.get(f"blob/{digest}") is not None

        plan = runtime.purge_plan(
            space.space_id,
            [
                {
                    "kind": "doc-range",
                    "node": f"id:{node.node_id}",
                    "from": "all",
                    "to": "all",
                }
            ],
        )
        runtime.purge(space.space_id, plan.plan_id)

        assert replica.backend.get(f"blob/{digest}") is None
        assert replica.pinned_reason(digest) is None
    finally:
        runtime.close()
