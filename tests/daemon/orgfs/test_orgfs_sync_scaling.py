"""Large-space sync must stay bounded and expose timeout diagnostics.

决策记录 · 2026-11-03 · R2：supplier 页预算改按线程 CPU 计量

CI run 44192/44293（shard 4，两个不同 PR，均 dev 基）上
``test_large_space_sync_answers_each_page_within_transport_timeout`` 红：
``_MeasuredBus.query`` 用 wall clock 计量 supplier handler 耗时，超过请求
timeout（0.1s）即丢弃回复，sync 端按产品契约报 ``no-holder-online``。
CI 作业 cgroup cpu.max=3 CPU，日志显示 nr_throttled=180、throttled_usec≈25s：
CFS 节流窗口可把 handler 线程冻结整整一个 100ms 周期，恰好吞掉全部预算。
handler 实际 CPU 开销约 10–20ms（注入 4ms 忙等 ×2 + 导出），预算 100ms 本来
就有 5–10× 余量；冻结是宿主调度问题，不是 supplier 页构造失控。修法：
``query`` 改用 ``time.thread_time()`` 计量（调用线程的 CPU 时间）——调度冻结
不计入，supplier 真实失控（每页 CPU 超预算）仍然丢弃回复并按原契约报错。
注入的忙等发生在同一线程，照常计入。判定依据：同断言下注入 0.12s 调度冻结
（wall 超、CPU 不变）修复前必红、修复后必绿；契约断言（页字节上限、每页
supplier 计量 ≤ timeout、目标文档集一致）逐字保留。
"""

from __future__ import annotations

from fnmatch import fnmatch
import json
from pathlib import Path
from threading import Lock
import time
from types import SimpleNamespace

from pycrdt import Map, Text
import pytest

from hyprial.daemon.impl.orgfs.mesh import OrgFsMesh
from hyprial.daemon.impl.orgfs.network import queries as queries_module
from hyprial.daemon.impl.orgfs.network import sync as sync_module
from hyprial.daemon.impl.orgfs.network.protocol import (
    ORGFS_SCHEDULED_SYNC_ATTEMPT_SECONDS,
    ORGFS_SCHEDULED_SYNC_BUDGET_SECONDS,
)
from hyprial.daemon.impl.orgfs.network.supplier_health import SupplierSyncHealth
from hyprial.daemon.impl.orgfs.storage.store import LocalSpaceStore, StoreError
from hyprial.daemon.impl.transport import TransportSample


TREE_DOC_ID = "tree-00000000-0000-0000-0000-000000000001"


class _Registration:
    def __init__(self, close) -> None:
        self._close = close

    def close(self) -> None:
        self._close()


class _MeasuredBus:
    def __init__(self, *, export_delay: float = 0.0) -> None:
        self.handlers = []
        self.subscribers = []
        self.export_delay = export_delay
        self.request_bytes: list[int] = []
        self.supplier_seconds: list[float] = []

    def session(self):
        bus = self

        class Session:
            def put(self, key, payload) -> None:
                for expression, callback in tuple(bus.subscribers):
                    if fnmatch(key, expression.replace("**", "*")):
                        callback(TransportSample(key, payload))

            def subscribe(self, key_expr, callback):
                row = (key_expr, callback)
                bus.subscribers.append(row)
                return _Registration(lambda: bus.subscribers.remove(row))

            def declare_query_handler(self, key_expr, handler):
                row = (key_expr, handler)
                bus.handlers.append(row)
                return _Registration(lambda: bus.handlers.remove(row))

            def query(self, key_expr, payload, *, timeout=3.0, **_kwargs):
                bus.request_bytes.append(len(payload))
                started = time.thread_time()
                replies = []
                for expression, handler in tuple(bus.handlers):
                    if fnmatch(key_expr, expression.replace("**", "*")):
                        replies.extend(
                            TransportSample(key, body)
                            for key, body in handler(key_expr, payload)
                        )
                elapsed = time.thread_time() - started
                bus.supplier_seconds.append(elapsed)
                return [] if elapsed > timeout else replies

        return Session()


def _seed(store: LocalSpaceStore) -> None:
    store.commit(
        "meta",
        lambda doc: (
            doc["space"].update(
                {"owner": "user:owner", "spaceId": "space-1", "name": "team"}
            ),
            doc["members"].__setitem__(
                "user:owner", Map({"mode": "rw", "removedAt": None})
            ),
            doc["docs"].__setitem__(
                "tree", Map({"active": TREE_DOC_ID, "planId": None})
            ),
        ),
        author="user:owner",
        actor=None,
    )


def _add_documents(store: LocalSpaceStore, start: int, count: int) -> None:
    for index in range(start, start + count):
        doc_id = f"doc-00000000-0000-4000-8000-{index:012x}"
        for revision in range(2):
            store.commit(
                doc_id,
                lambda doc, index=index, revision=revision: doc.__setitem__(
                    f"revision-{revision}", Text(f"{index}:" + "x" * 1_500)
                ),
                author="user:owner",
                actor="agent:owner:source:writer",
            )


def _mesh_for_error(session, deadline: float | None) -> StoreError:
    mesh = OrgFsMesh.__new__(OrgFsMesh)
    mesh.store = type("Store", (), {"space_id": "space-1"})()
    mesh.node_id = "node-requester"
    mesh.author = "user:owner"
    mesh.session = session
    mesh._keys = type(
        "Keys", (), {"orgfs_sync": lambda _self, space, supplier: f"{space}/{supplier}"}
    )()
    mesh._repair_projections = lambda **_kwargs: None
    mesh._supplier_allowed = lambda _supplier: True
    mesh._supplier_member_allowed = lambda _supplier: True
    mesh._document_ids = lambda: ("meta",)
    mesh._frontier = lambda _doc_id: b""
    mesh._pending_replacements = lambda: ()
    mesh._receive = lambda *_args, **_kwargs: type("Result", (), {"status": "duplicate"})()
    with pytest.raises(StoreError) as raised:
        mesh._sync_from_once(
            "node-supplier", timeout=0.1, deadline_monotonic=deadline
        )
    return raised.value


def test_sync_timeout_reasons_are_distinct_and_measured() -> None:
    class NoReply:
        def query(self, *_args, **_kwargs):
            return []

    class LateReply:
        def query(self, *_args, **_kwargs):
            time.sleep(0.01)
            return [
                TransportSample(
                    "reply",
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "type": "orgfs-sync-page",
                            "supplier": "node-supplier",
                            "envelopes": [],
                            "next": None,
                        }
                    ).encode(),
                )
            ]

    expired = _mesh_for_error(NoReply(), time.monotonic() - 1)
    clipped = _mesh_for_error(NoReply(), time.monotonic() + 0.001)
    no_reply = _mesh_for_error(NoReply(), time.monotonic() + 1)
    late = _mesh_for_error(LateReply(), time.monotonic() + 0.001)

    assert [error.code for error in (expired, clipped, no_reply, late)] == [
        "no-holder-online",
        "no-holder-online",
        "no-holder-online",
        "no-holder-online",
    ]
    assert [
        error.details["reason"] for error in (expired, clipped, no_reply, late)
    ] == [
        "deadline-expired-mid-sync",
        "deadline-expired-mid-sync",
        "no-reply",
        "reply-after-deadline",
    ]
    for error in (expired, clipped, no_reply, late):
        assert set(error.details) >= {
            "pages",
            "requestBytes",
            "responseBytes",
            "elapsedMs",
        }


def test_sync_events_include_wire_progress_and_distinct_failure_reason() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    mesh = OrgFsMesh.__new__(OrgFsMesh)
    mesh.store = type("Store", (), {"space_id": "space-1"})()
    mesh._worker_lock = Lock()
    mesh._sync_active = set()
    mesh._sync_pending = {"node-supplier"}
    mesh._sync_rerun = set()
    mesh._closed = False
    mesh._supplier_sync_health = SupplierSyncHealth()
    mesh._supplier_sync_local_health = SupplierSyncHealth()
    mesh._log = lambda _level, event, **fields: events.append((event, fields))
    mesh._sync_from_once = lambda *_args, **_kwargs: 0
    mesh.schedule_sync_from = lambda _supplier: True
    mesh._supplier_allowed = lambda _supplier: True

    assert mesh.sync_from("node-supplier") == 0
    completed = [fields for event, fields in events if event == "orgfs.sync.completed"]
    assert set(completed[-1]) >= {
        "pages",
        "requestBytes",
        "responseBytes",
        "elapsedMs",
    }

    def fail(*_args, **_kwargs):
        raise StoreError(
            "no-holder-online",
            "supplier reply arrived after sync deadline",
            reason="reply-after-deadline",
            pages=2,
            requestBytes=300,
            responseBytes=400,
            elapsedMs=500,
        )

    mesh.sync_from = fail
    mesh._sync_pending = {"node-supplier"}
    events.clear()
    mesh._run_scheduled_sync("node-supplier")
    failed = [fields for event, fields in events if event == "orgfs.sync.failed"]
    assert {
        "reason": "reply-after-deadline",
        "code": "no-holder-online",
        "pages": 2,
        "requestBytes": 300,
        "responseBytes": 400,
        "elapsedMs": 500,
    }.items() <= failed[-1].items()
    assert "reply arrived after sync deadline" in str(failed[-1]["message"])


def test_large_space_sync_answers_each_page_within_transport_timeout(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page_bytes = 16_384
    monkeypatch.setattr(sync_module, "ORGFS_PAGE_BYTES", page_bytes)
    monkeypatch.setattr(queries_module, "ORGFS_PAGE_BYTES", page_bytes)
    source = LocalSpaceStore(tmp_path / "source", "space-1", node_id="node-source")
    target = LocalSpaceStore(tmp_path / "target", "space-1", node_id="node-target")
    small = LocalSpaceStore(tmp_path / "small", "space-1", node_id="node-small")
    bus = _MeasuredBus(export_delay=0.004)
    source_mesh = None
    target_mesh = None
    small_mesh = None
    try:
        _seed(source)
        _add_documents(source, 0, 1)
        source_session = bus.session()
        source_mesh = OrgFsMesh(
            source_session,
            source,
            "node-source",
            author="user:owner",
            trust_tailnet_members=True,  # Cold-sync page budget after private admission (§7).
            supplier_online=lambda _node: True,
        )
        original_export = source_mesh._export_since

        def delayed_export(*args, **kwargs):
            ready_at = time.monotonic() + bus.export_delay
            while time.monotonic() < ready_at:
                pass
            return original_export(*args, **kwargs)

        source_mesh._export_since = delayed_export
        target_mesh = OrgFsMesh(
            bus.session(),
            target,
            "node-target",
            author="user:owner",
            trust_tailnet_members=True,  # Cold-sync page budget after private admission (§7).
            supplier_online=lambda _node: True,
        )
        small_mesh = OrgFsMesh(
            bus.session(),
            small,
            "node-small",
            author="user:owner",
            trust_tailnet_members=True,  # Cold-sync page budget after private admission (§7).
            supplier_online=lambda _node: True,
        )

        assert small_mesh.sync_from(
            "node-source", timeout=0.1, deadline_monotonic=time.monotonic() + 2
        ) == 4
        _add_documents(source, 1, 47)
        bus.request_bytes.clear()
        bus.supplier_seconds.clear()
        imported = target_mesh.sync_from(
            "node-source", timeout=0.1, deadline_monotonic=time.monotonic() + 2
        )

        assert imported == 98
        assert max(bus.request_bytes) <= page_bytes
        assert max(bus.supplier_seconds) <= 0.1
        assert target.document_ids() == source.document_ids()
    finally:
        for mesh in (small_mesh, target_mesh, source_mesh):
            if mesh is not None:
                mesh.close()
        small.close()
        target.close()
        source.close()


class _StreamingSession:
    def __init__(self, supplier: OrgFsMesh) -> None:
        self.supplier = supplier
        self.requests: list[dict[str, object]] = []
        self.reply_sizes: list[int] = []
        self.handler_seconds: list[float] = []

    def query(self, key, payload, *, timeout=3.0, **_kwargs):
        request = json.loads(payload)
        self.requests.append(request)
        started = time.monotonic()
        replies = [
            TransportSample(reply_key, body)
            for reply_key, body in self.supplier._handle_sync(key, payload)
        ]
        elapsed = time.monotonic() - started
        self.handler_seconds.append(elapsed)
        if elapsed > timeout:
            return []
        self.reply_sizes.extend(len(reply.payload) for reply in replies)
        return replies


def _streaming_supplier(document_count: int, *, delay: float) -> OrgFsMesh:
    document_ids = tuple(
        f"doc-00000000-0000-4000-8000-{index:012x}"
        for index in range(document_count)
    )
    mesh = OrgFsMesh.__new__(OrgFsMesh)
    mesh.store = SimpleNamespace(space_id="space-1")
    mesh.node_id = "node-supplier"
    mesh._keys = SimpleNamespace(orgfs_sync=lambda space, supplier: f"{space}/{supplier}")
    mesh._log = lambda *_args, **_kwargs: None
    mesh.replica_store = None
    mesh._requester_allowed = lambda _value: True
    mesh._document_ids = lambda: document_ids
    mesh._retirement_records = lambda: ()
    mesh._frontier = lambda _doc_id: b""

    def export(doc_id, _vv, *, cursor, max_bytes):
        assert cursor is None
        assert max_bytes > 0
        ready_at = time.monotonic() + delay
        while time.monotonic() < ready_at:
            pass
        return SimpleNamespace(envelopes=(doc_id.encode(),), next=None, snapshots=())

    mesh._export_since = export
    return mesh


def _streaming_requester(root: Path, session: _StreamingSession, received: set[str]):
    mesh = OrgFsMesh.__new__(OrgFsMesh)
    mesh.store = SimpleNamespace(space_id="space-1", root=root)
    mesh.node_id = "node-requester"
    mesh.author = "user:owner"
    mesh.session = session
    mesh._keys = SimpleNamespace(orgfs_sync=lambda space, supplier: f"{space}/{supplier}")
    mesh._repair_projections = lambda **_kwargs: None
    mesh._supplier_allowed = lambda _supplier: True
    mesh._supplier_member_allowed = lambda _supplier: True
    mesh._document_ids = lambda: tuple(received)
    mesh._frontier = lambda _doc_id: b""
    mesh._pending_replacements = lambda: ()
    mesh._sync_envelope_known = lambda _envelope: False
    mesh._worker_lock = Lock()
    mesh._sync_active = set()
    mesh._sync_pending = set()
    mesh._sync_rerun = set()
    mesh._closed = False
    mesh._log = lambda *_args, **_kwargs: None
    mesh.schedule_sync_from = lambda _supplier: True

    def receive(envelope, **_kwargs):
        doc_id = envelope.decode()
        duplicate = doc_id in received
        received.add(doc_id)
        return SimpleNamespace(status="duplicate" if duplicate else "applied", code=None)

    mesh._receive = receive
    return mesh


@pytest.mark.parametrize("document_count,max_passes", [(1_500, 3), (4_000, 6)])
def test_scheduled_large_space_sync_resumes_durable_cursor_and_converges(
    tmp_path, document_count: int, max_passes: int
) -> None:
    supplier = _streaming_supplier(document_count, delay=0.004)
    session = _StreamingSession(supplier)
    received: set[str] = set()
    root = tmp_path / "requester"
    root.mkdir()
    progress = [0]
    expected = document_count + 1  # supplier stream always includes meta

    for pass_index in range(max_passes):
        requester = _streaming_requester(root, session, received)
        try:
            requester.sync_from(
                "node-supplier",
                timeout=ORGFS_SCHEDULED_SYNC_ATTEMPT_SECONDS,
                deadline_monotonic=(
                    time.monotonic() + ORGFS_SCHEDULED_SYNC_BUDGET_SECONDS
                ),
            )
        except StoreError as exc:
            assert exc.details["reason"] == "deadline-expired-mid-sync"
        progress.append(len(received))
        assert progress[-1] >= progress[-2]
        if pass_index == 0:
            assert 0 < progress[-1] < expected
            requester = _streaming_requester(root, session, received)
        if progress[-1] == expected:
            break

    assert progress[-1] == expected
    assert all(after > before for before, after in zip(progress, progress[1:]))
    stream_requests = [
        request
        for request in session.requests
        if request.get("type") == "orgfs-sync-stream"
    ]
    assert len(stream_requests) < document_count // 100
    assert all(
        "docIds" not in request and "versionVectors" not in request
        for request in stream_requests[1:]
    )
    assert any(request.get("cursor") is not None for request in stream_requests)
    assert max(session.handler_seconds) <= ORGFS_SCHEDULED_SYNC_ATTEMPT_SECONDS


def test_sync_page_builders_reject_budget_below_empty_response() -> None:
    def mesh(*, replica: bool) -> OrgFsMesh:
        supplier = OrgFsMesh.__new__(OrgFsMesh)
        supplier.store = SimpleNamespace(space_id="space-1")
        supplier.node_id = "node-supplier"
        supplier._replica_ready = replica
        supplier.replica_store = (
            SimpleNamespace(serve_sync_page=lambda *_args, **_kwargs: ())
            if replica
            else None
        )
        supplier._requester_allowed = lambda _value: True
        supplier._document_ids = lambda: ()
        supplier._retirement_records = lambda: ()
        supplier._retired = lambda _doc_id: None
        supplier._export_since = lambda *_args, **_kwargs: SimpleNamespace(
            envelopes=(), next=None, snapshots=()
        )
        return supplier

    requester = {"node": "node-requester", "author": "user:owner"}
    legacy_request = {
        "schemaVersion": 1,
        "type": "orgfs-sync",
        "spaceId": "space-1",
        "docIds": None,
        "versionVectors": {},
        "requester": requester,
        "cursor": None,
        "maxBytes": 1_048_576,
    }
    stream_request = {
        "schemaVersion": 1,
        "type": "orgfs-sync-stream",
        "spaceId": "space-1",
        "requester": requester,
        "cursor": None,
        "maxBytes": 1_048_576,
    }
    document_request = {
        "spaceId": "space-1",
        "requester": requester,
        "cursor": 1,
        "maxBytes": 1_048_576,
    }
    local = mesh(replica=False)
    replica = mesh(replica=True)
    builders = (
        (local._sync_page, legacy_request),
        (local._stream_sync_page, stream_request),
        (local._document_page, document_request),
        (replica._sync_page, legacy_request),
        (replica._stream_sync_page, stream_request),
    )

    for build, request in builders:
        minimum_size = len(build(request))
        undersized = {**request, "maxBytes": minimum_size - 1}
        with pytest.raises(StoreError) as raised:
            build(undersized)
        assert raised.value.code == "too-large"


def test_document_page_budgets_the_final_nullable_cursor() -> None:
    mesh = OrgFsMesh.__new__(OrgFsMesh)
    mesh.store = SimpleNamespace(space_id="space-1")
    mesh.node_id = "node-supplier"
    mesh._requester_allowed = lambda _value: True
    mesh._document_ids = lambda: ("doc-a",)
    request = {
        "spaceId": "space-1",
        "requester": {"node": "node-requester", "author": "user:owner"},
        "cursor": None,
        "maxBytes": 1_048_576,
    }
    full_size = len(mesh._document_page(request))

    for reduction in (1, 2, 3):
        request["maxBytes"] = full_size - reduction
        page = mesh._document_page(request)
        assert json.loads(page)["type"] == "orgfs-sync-documents-page"
        assert len(page) <= request["maxBytes"]


def test_replica_sync_pages_budget_the_final_wire_type() -> None:
    def supplier(row_count: int) -> OrgFsMesh:
        rows = []
        for sequence in range(row_count):
            envelope = json.dumps(
                {
                    "schemaVersion": 1,
                    "type": "orgfs-update",
                    "spaceId": "space-1",
                    "docId": "meta",
                    "seq": sequence,
                    "origin": {
                        "writer": "writer",
                        "node": "node-supplier",
                        "author": "user:owner",
                        "actor": None,
                        "committedAt": "2026-10-01T00:00:00Z",
                        "metaFrontier": "",
                    },
                    "update": "x" * (300 + sequence),
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            rows.append((f"log/space-1/meta/writer/{sequence}", envelope))

        mesh = OrgFsMesh.__new__(OrgFsMesh)
        mesh.store = SimpleNamespace(space_id="space-1")
        mesh.node_id = "node-supplier"
        mesh._replica_ready = True
        mesh.replica_store = SimpleNamespace(
            serve_sync_page=lambda _doc_ids, *, after_key=None, limit: tuple(
                row for row in rows if after_key is None or row[0] > after_key
            )[:limit]
        )
        mesh._requester_allowed = lambda _value: True
        mesh._document_ids = lambda: ("meta",)
        mesh._retired = lambda _doc_id: None
        mesh._commit_version = lambda _doc_id, _writer, _sequence: None
        return mesh

    observed_budgets = []
    for row_count in (2, 3, 5, 8):
        mesh = supplier(row_count)
        request = {
            "schemaVersion": 1,
            "type": "orgfs-sync",
            "spaceId": "space-1",
            "docIds": None,
            "versionVectors": {},
            "requester": {"node": "node-requester", "author": "user:owner"},
            "cursor": None,
            "maxBytes": 1_048_576,
        }
        exact_legacy = mesh._sync_page(request)
        max_bytes = len(exact_legacy)
        observed_budgets.append(max_bytes)
        request["maxBytes"] = max_bytes
        legacy_page = mesh._sync_page(request)
        stream_page = mesh._stream_sync_page(
            {
                "schemaVersion": 1,
                "type": "orgfs-sync-stream",
                "spaceId": "space-1",
                "requester": request["requester"],
                "cursor": None,
                "maxBytes": max_bytes,
            }
        )

        assert json.loads(legacy_page)["type"] == "orgfs-sync-page"
        assert len(legacy_page) == max_bytes
        assert json.loads(stream_page)["type"] == "orgfs-sync-stream-page"
        assert len(stream_page) <= max_bytes

    assert observed_budgets == sorted(set(observed_budgets))


def test_new_requester_bounds_old_peer_fallback_and_new_supplier_accepts_old_request(
    tmp_path,
) -> None:
    class OldSession:
        def __init__(self) -> None:
            self.requests = []

        def query(self, _key, payload, **_kwargs):
            value = json.loads(payload)
            self.requests.append(value)
            if value["type"] == "orgfs-sync-stream":
                body = {
                    "schemaVersion": 1,
                    "type": "orgfs-error",
                    "code": "invalid-argument",
                    "message": "unknown sync query type",
                }
            else:
                body = {
                    "schemaVersion": 1,
                    "type": "orgfs-sync-page",
                    "supplier": "node-supplier",
                    "envelopes": [],
                    "next": None,
                }
            return [TransportSample("reply", json.dumps(body).encode())]

    old = OldSession()
    received: set[str] = set()
    requester = _streaming_requester(tmp_path, old, received)
    requester._document_ids = lambda: tuple(f"doc-{index}" for index in range(1_500))
    with pytest.raises(StoreError) as skipped:
        requester.sync_from("node-supplier")
    assert skipped.value.details["reason"] == "legacy-peer-large-space"
    assert [request["type"] for request in old.requests] == ["orgfs-sync-stream"]

    old.requests.clear()
    requester._document_ids = lambda: ("meta", "doc-small")
    assert requester.sync_from("node-supplier") == 0
    assert [request["type"] for request in old.requests] == [
        "orgfs-sync-stream",
        "orgfs-sync",
    ]
    assert old.requests[-1]["docIds"] == ["meta", "doc-small"]

    supplier = _streaming_supplier(2, delay=0)
    legacy_request = json.dumps(
        {
            "schemaVersion": 1,
            "type": "orgfs-sync",
            "spaceId": "space-1",
            "docIds": None,
            "versionVectors": {},
            "requester": {"node": "old", "author": "user:owner"},
            "cursor": None,
            "maxBytes": 16_384,
        }
    ).encode()
    response = json.loads(next(iter(supplier._handle_sync("key", legacy_request)))[1])
    assert response["type"] == "orgfs-sync-page"
