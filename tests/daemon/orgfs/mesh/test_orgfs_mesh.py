from __future__ import annotations

from fnmatch import fnmatch
import hashlib
import json
import time

from pycrdt import Map, Text
import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.mesh import ORGFS_ENVELOPE_BYTES, ORGFS_PAGE_BYTES, OrgFsMesh
from hyprial.daemon.impl.orgfs.storage.blobs import BlobStore
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.storage.store import (
    LocalSpaceStore,
    StoreError,
    assert_client_ids_registered)
from hyprial.daemon.impl.transport import KeySpace, TransportSample

TREE_DOC_ID = "tree-00000000-0000-0000-0000-000000000001"


def _eventually_text(
    runtime: OrgFsRuntime, space_id: str, node_id: str, expected: str
) -> None:
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            assert runtime.facade.read_text(space_id, f"id:{node_id}")[0] == expected
            return
        except (AssertionError, OrgFsError):
            time.sleep(0.005)
    assert runtime.facade.read_text(space_id, f"id:{node_id}")[0] == expected


class _Registration:
    def __init__(self, close):
        self._close = close

    def close(self):
        self._close()


class _Bus:
    def __init__(self):
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


def _seed(store: LocalSpaceStore):
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


def _assert_registered(*stores: LocalSpaceStore) -> None:
    for store in stores:
        assert_client_ids_registered(store)


def test_three_replica_reorder_duplicate_drop_and_partition_heal(tmp_path):
    source = LocalSpaceStore(tmp_path / "source", "space-1", node_id="node-a")
    replicas = [
        LocalSpaceStore(tmp_path / name, "space-1", node_id=f"node-{name}")
        for name in ("b", "c", "d")
    ]
    try:
        _seed(source)
        for index in range(8):
            source.commit(
                TREE_DOC_ID,
                lambda doc, index=index: doc.__setitem__(f"k{index}", Text(str(index))),
                author="user:owner",
                actor="agent:owner:a",
            )
        envelopes = [
            bytes(row[0])
            for row in source._db.execute(
                "SELECT envelope_bytes FROM commits ORDER BY rowid"
            )
        ]
        schedules = (
            envelopes,
            list(reversed(envelopes)) + [envelopes[-1]],
            envelopes[::2],
        )
        for replica, schedule in zip(replicas, schedules, strict=True):
            for envelope in schedule:
                replica.import_envelope(envelope, supplier="node-a")
        # Heal the partition by replaying the immutable source journal.
        for envelope in envelopes:
            replicas[2].import_envelope(envelope, supplier="node-a")
        for replica in replicas:
            assert replica.frontier("meta") == source.frontier("meta")
            assert replica.frontier(TREE_DOC_ID) == source.frontier(TREE_DOC_ID)
        _assert_registered(source, *replicas)
    finally:
        for replica in replicas:
            replica.close()
        source.close()


def test_ro_supplier_replays_three_original_authors_without_rewriting_origin(tmp_path):
    source = LocalSpaceStore(tmp_path / "source", "space-1", node_id="node-a")
    replica = LocalSpaceStore(tmp_path / "replica", "space-1", node_id="node-r")
    target = LocalSpaceStore(tmp_path / "target", "space-1", node_id="node-b")
    try:
        _seed(source)
        source.commit(
            "meta",
            lambda doc: tuple(
                doc["members"].__setitem__(user, Map({"mode": mode}))
                for user, mode in (
                    ("user:a", "rw"),
                    ("user:b", "rw"),
                    ("user:c", "rw"),
                    ("user:replica", "ro"),
                )
            ),
            author="user:owner",
            actor=None,
        )
        for index, author in enumerate(("user:a", "user:b", "user:c")):
            source.commit(
                TREE_DOC_ID,
                lambda doc, index=index: doc.__setitem__(
                    f"author-{index}", Text(author)
                ),
                author=author,
                actor=f"agent:{author[5:]}:node-a:writer",
            )
        envelopes = [
            bytes(row[0])
            for row in source._db.execute(
                "SELECT envelope_bytes FROM commits ORDER BY rowid"
            )
        ]
        for envelope in envelopes:
            replica.import_envelope(envelope, supplier="node-a")

        bus = _Bus()
        # Cold replica sync models the private sidecar-admitted path (§7).
        replica_mesh = OrgFsMesh(
            bus.session(), replica, "node-r", author="user:replica",
            trust_tailnet_members=True,
        )
        target_mesh = OrgFsMesh(
            bus.session(), target, "node-b", author="user:owner",
            trust_tailnet_members=True,
        )
        try:
            target_mesh.sync_from("node-r")
            authors = [
                json.loads(row[0])["origin"]["author"]
                for row in target._db.execute(
                    "SELECT envelope_bytes FROM commits WHERE doc_id = ? ORDER BY rowid",
                    (TREE_DOC_ID,),
                )
            ]
            assert authors == ["user:a", "user:b", "user:c"]
            _assert_registered(source, replica, target)
        finally:
            target_mesh.close()
            replica_mesh.close()
    finally:
        target.close()
        replica.close()
        source.close()


def test_sync_pages_are_bounded_meta_first_and_converge(tmp_path):
    source = LocalSpaceStore(tmp_path / "a", "space-1", node_id="node-a")
    target = LocalSpaceStore(tmp_path / "b", "space-1", node_id="node-b")
    try:
        _seed(source)
        for index in range(90):
            source.commit(
                TREE_DOC_ID,
                lambda doc, index=index: doc.__setitem__(
                    f"item-{index}", Text("x" * 20_000)
                ),
                author="user:owner",
                actor="agent:owner:a",
            )
        bus = _Bus()
        source_mesh = OrgFsMesh(
            bus.session(),
            source,
            "node-a",
            author="user:owner",
            supplier_online=lambda node: node in {"node-a", "node-b"},
            trust_tailnet_members=True,  # Cold sync after private sidecar admission (§7).
        )
        target_mesh = OrgFsMesh(
            bus.session(),
            target,
            "node-b",
            author="user:owner",
            supplier_online=lambda node: node in {"node-a", "node-b"},
            trust_tailnet_members=True,  # Cold sync after private sidecar admission (§7).
        )
        try:
            request = {
                "schemaVersion": 1,
                "type": "orgfs-sync",
                "spaceId": "space-1",
                "docIds": None,
                "versionVectors": {"meta": "", TREE_DOC_ID: ""},
                "requester": {"node": "node-b", "author": "user:owner"},
                "cursor": None,
                "maxBytes": ORGFS_PAGE_BYTES,
            }
            pages = []
            seen = []
            while True:
                response = tuple(
                    source_mesh._handle_sync("", json.dumps(request).encode())
                )[0][1]
                decoded = json.loads(response)
                pages.append(response)
                seen.extend(decoded["envelopes"])
                request["cursor"] = decoded["next"]
                if decoded["next"] is None:
                    break
            assert len(pages) >= 3
            assert all(len(page) <= ORGFS_PAGE_BYTES for page in pages)
            assert len(seen) == len(set(seen))
            assert (
                json.loads(__import__("base64").b64decode(seen[0]))["docId"] == "meta"
            )
            assert target_mesh.sync_from("node-a") > 0
            assert target.frontier("meta") == source.frontier("meta")
            assert target.frontier(TREE_DOC_ID) == source.frontier(TREE_DOC_ID)
            _assert_registered(source, target)
        finally:
            target_mesh.close()
            source_mesh.close()
    finally:
        target.close()
        source.close()


def test_r3_two_node_sync_from_supplier_missing_replacement_is_bounded(tmp_path):
    supplier = LocalSpaceStore(
        tmp_path / "supplier", "space-1", node_id="node-supplier"
    )
    requester = LocalSpaceStore(
        tmp_path / "requester", "space-1", node_id="node-requester"
    )
    replacement_doc_id = "tree-00000000-0000-0000-0000-000000000002"
    snapshot_id = hashlib.sha256(b"replacement supplier never received").hexdigest()
    try:
        _seed(supplier)

        def retire_without_snapshot(doc):
            retirements = doc.get("retirements", type=Map)
            retirements[TREE_DOC_ID] = Map(
                {
                    "replacementDocId": replacement_doc_id,
                    "planId": "plan-missed-by-supplier",
                    "snapshotId": snapshot_id,
                    "retiredAt": "2026-09-27T00:00:00Z",
                }
            )

        supplier.commit(
            "meta",
            retire_without_snapshot,
            author="user:owner",
            actor=None,
        )
        for row in supplier._db.execute(
            "SELECT envelope_bytes FROM commits ORDER BY rowid"
        ):
            assert (
                requester.import_envelope(
                    bytes(row[0]), supplier="node-supplier"
                ).status
                == "applied"
            )
        assert supplier.pending_replacements()
        assert requester.pending_replacements()

        bus = _Bus()
        supplier_mesh = OrgFsMesh(
            bus.session(),
            supplier,
            "node-supplier",
            author="user:owner",
            supplier_owner={"node-requester": "user:owner"}.get,
            supplier_online=lambda _node: True,
        )
        requester_session = bus.session()
        query = requester_session.query
        query_calls = 0

        def bounded_query(*args, **kwargs):
            nonlocal query_calls
            query_calls += 1
            if query_calls > 4:
                raise AssertionError("R3 sync repeated an exhausted no-progress round")
            return query(*args, **kwargs)

        requester_session.query = bounded_query
        requester_mesh = OrgFsMesh(
            requester_session,
            requester,
            "node-requester",
            author="user:owner",
            supplier_owner={"node-supplier": "user:owner"}.get,
            supplier_online=lambda _node: True,
        )
        try:
            with pytest.raises(StoreError) as unavailable:
                requester_mesh.sync_from("node-supplier", timeout=0.1)

            assert unavailable.value.code == "snapshot-unavailable"
            assert unavailable.value.details["supplier"] == "node-supplier"
            assert unavailable.value.details["pendingReplacementDocIds"] == (
                replacement_doc_id,
            )
            assert query_calls == 2
            _assert_registered(supplier, requester)
        finally:
            requester_mesh.close()
            supplier_mesh.close()
    finally:
        requester.close()
        supplier.close()


def test_supplier_gate_precedes_store_admission(tmp_path):
    """Review P1 fix 13: offline suppliers get a truthful rejection code."""

    source = LocalSpaceStore(tmp_path / "a", "space-1", node_id="node-a")
    target = LocalSpaceStore(tmp_path / "b", "space-1", node_id="node-b")
    try:
        _seed(source)
        envelope = bytes(
            source._db.execute(
                "SELECT envelope_bytes FROM commits ORDER BY rowid LIMIT 1"
            ).fetchone()[0]
        )
        mesh = OrgFsMesh(
            _Bus().session(),
            target,
            "node-b",
            author="user:owner",
            supplier_online=lambda _node: False,
        )
        try:
            result = mesh.receive(envelope, supplier="node-a")
            assert result.status == "rejected"
            assert result.code == "supplier-offline"
            assert target._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 0
            _assert_registered(source, target)
        finally:
            mesh.close()
    finally:
        target.close()
        source.close()


def test_review_fix_13_schema_gate_runs_before_supplier_gate(tmp_path):
    """Review P1 fix 13: gate 1 schema validation runs before gate 2."""

    store = LocalSpaceStore(tmp_path / "target", "space-1", node_id="node-b")
    supplier_checks = 0

    def supplier_online(_supplier):
        nonlocal supplier_checks
        supplier_checks += 1
        return True

    try:
        mesh = OrgFsMesh(
            _Bus().session(),
            store,
            "node-b",
            author="user:owner",
            supplier_online=supplier_online,
        )
        try:
            result = mesh.receive(b"{}", supplier="node-a")
            assert result.status == "rejected"
            assert result.code == "invalid-argument"
            assert supplier_checks == 0
        finally:
            mesh.close()
    finally:
        store.close()


def test_round3_d3_live_broadcast_uses_liveliness_not_space_announce(tmp_path):
    """D.3: gate 2 accepts a live host without consulting holder announces."""

    source = LocalSpaceStore(tmp_path / "source", "space-1", node_id="node-a")
    target = LocalSpaceStore(tmp_path / "target", "space-1", node_id="node-b")
    try:
        _seed(source)
        envelope = bytes(
            source._db.execute(
                "SELECT envelope_bytes FROM commits ORDER BY rowid LIMIT 1"
            ).fetchone()[0]
        )
        decoded = json.loads(envelope)
        key = KeySpace().orgfs_log(
            "space-1", "meta", decoded["origin"]["writer"], decoded["seq"]
        )
        bus = _Bus()
        source_mesh = OrgFsMesh(
            bus.session(),
            source,
            "node-a",
            author="user:owner",
            # Live genesis broadcast models private sidecar admission (§7).
            trust_tailnet_members=True,
            supplier_online=lambda _node: True,
        )
        target_mesh = OrgFsMesh(
            bus.session(),
            target,
            "node-b",
            author="user:owner",
            trust_tailnet_members=True,  # Receive genesis before local meta exists (§7).
            supplier_online=lambda _node: True,
        )
        try:
            # No announce is published. The daemon-provided online-host verdict
            # is the sole gate, matching org.fetch.
            bus.session().put(key, envelope)
            assert target._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 1
            _assert_registered(source, target)
        finally:
            target_mesh.close()
            source_mesh.close()
    finally:
        target.close()
        source.close()


def test_oversized_envelope_is_rejected_before_json_decode(tmp_path):
    store = LocalSpaceStore(tmp_path / "target", "space-1", node_id="node-b")
    try:
        mesh = OrgFsMesh(_Bus().session(), store, "node-b", author="user:owner")
        try:
            result = mesh.receive(b"{" + b"x" * ORGFS_ENVELOPE_BYTES, supplier="node-a")
            assert result.status == "rejected"
            assert result.code == "too-large"
            assert store._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 0
        finally:
            mesh.close()
    finally:
        store.close()


def test_bad_cursor_and_handler_exception_return_wire_errors(tmp_path):
    store = LocalSpaceStore(tmp_path / "a", "space-1", node_id="node-a")
    try:
        _seed(store)
        mesh = OrgFsMesh(_Bus().session(), store, "node-a", author="user:owner")
        try:
            request = {
                "schemaVersion": 1,
                "type": "orgfs-sync",
                "spaceId": "space-1",
                "docIds": None,
                "versionVectors": {},
                "requester": {"node": "node-a", "author": "user:owner"},
                "cursor": "not-a-cursor",
                "maxBytes": ORGFS_PAGE_BYTES,
            }
            response = json.loads(
                tuple(mesh._handle_sync("", json.dumps(request).encode()))[0][1]
            )
            assert response["type"] == "orgfs-error"
            assert response["code"] == "bad-cursor"
            missing = json.loads(tuple(mesh._handle_sync("", None))[0][1])
            assert missing["type"] == "orgfs-error"
            assert missing["code"] == "invalid-argument"
            request["cursor"] = None
            request["requester"] = {"node": "stranger", "author": "user:stranger"}
            denied = json.loads(
                tuple(mesh._handle_sync("", json.dumps(request).encode()))[0][1]
            )
            assert denied["type"] == "orgfs-error"
            assert denied["code"] == "not-a-member"
        finally:
            mesh.close()
    finally:
        store.close()


def test_log_range_blob_fetch_and_frontier_confirmation(tmp_path):
    source_blobs = BlobStore(tmp_path / "a")
    target_blobs = BlobStore(tmp_path / "b")
    source = LocalSpaceStore(
        tmp_path / "a", "space-1", node_id="node-a", blob_store=source_blobs
    )
    target = LocalSpaceStore(
        tmp_path / "b", "space-1", node_id="node-b", blob_store=target_blobs
    )
    try:
        _seed(source)
        change = source.commit(
            TREE_DOC_ID,
            lambda doc: doc.__setitem__("note", Text("hello")),
            author="user:owner",
            actor="agent:owner:a",
        )
        content = b"blob" * 400_000
        digest = source_blobs.put("space-1", content, reason="own-write")
        bus = _Bus()
        source_mesh = OrgFsMesh(
            bus.session(),
            source,
            "node-a",
            author="user:owner",
            # Exercise blob/frontier replication after private sidecar admission (§7).
            trust_tailnet_members=True,
            blob_store=source_blobs,
        )
        target_mesh = OrgFsMesh(
            bus.session(),
            target,
            "node-b",
            author="user:owner",
            trust_tailnet_members=True,  # Cold target on the admitted private path (§7).
            blob_store=target_blobs,
        )
        try:
            envelope = json.loads(change.envelope_bytes)
            request = json.dumps(
                {
                    "schemaVersion": 1,
                    "type": "orgfs-log-range",
                    "spaceId": "space-1",
                    "docId": TREE_DOC_ID,
                    "writer": envelope["origin"]["writer"],
                    "after": None,
                    "limit": 256,
                }
            ).encode()
            replies = tuple(source_mesh._handle_log_range("", request))
            assert replies == ((replies[0][0], change.envelope_bytes),)
            assert target_mesh.fetch_blob("node-a", digest) == content
            assert hashlib.sha256(content).hexdigest() == digest
            assert source.unconfirmed_commits() > 0
            target_mesh.sync_from("node-a")
            source_mesh.probe("node-b")
            assert source.unconfirmed_commits() == 0
            _assert_registered(source, target)
        finally:
            target_mesh.close()
            source_mesh.close()
    finally:
        target.close()
        source.close()
        target_blobs.close()
        source_blobs.close()


def test_review_fix_14_log_range_errors_use_concrete_record_keys(tmp_path):
    """Review P1 fix 14: log-range error replies never use a wildcard key."""

    store = LocalSpaceStore(tmp_path / "source", "space-1", node_id="node-a")
    try:
        _seed(store)
        envelope = json.loads(
            bytes(
                store._db.execute(
                    "SELECT envelope_bytes FROM commits ORDER BY rowid LIMIT 1"
                ).fetchone()[0]
            )
        )
        bus = _Bus()
        mesh = OrgFsMesh(bus.session(), store, "node-a", author="user:owner")
        try:
            request = json.dumps(
                {
                    "schemaVersion": 1,
                    "type": "orgfs-log-range",
                    "spaceId": "space-1",
                    "docId": "meta",
                    "writer": envelope["origin"]["writer"],
                    "after": 0,
                    "limit": 0,
                }
            ).encode()
            replies = bus.session().query(
                KeySpace().orgfs_log_any("space-1"), request, all_replies=True
            )
            assert len(replies) == 1
            assert replies[0].key == KeySpace().orgfs_log(
                "space-1", "meta", envelope["origin"]["writer"], 1
            )
            assert "*" not in replies[0].key
            assert json.loads(replies[0].payload)["code"] == "invalid-argument"
        finally:
            mesh.close()
    finally:
        store.close()


def test_online_replication_refreshes_open_facade_and_watch(tmp_path):
    bus = _Bus()
    # Watch refresh tests replication after private sidecar admission (§7).
    owner = OrgFsRuntime(
        tmp_path / "owner", node_id="node-a", author="user:owner",
        trust_tailnet_members=True,
    )
    reader = OrgFsRuntime(
        tmp_path / "reader", node_id="node-b", author="user:reader",
        trust_tailnet_members=True,
    )
    try:
        owner.bind_transport(
            bus.session(), supplier_online=lambda node: node in {"node-a", "node-b"}
        )
        reader.bind_transport(
            bus.session(), supplier_online=lambda node: node in {"node-a", "node-b"}
        )
        space = owner.facade.create_space("team")
        owner.facade.invite(space.space_id, "user:reader", "rw")
        owner._mesh(space.space_id).announce()
        joined = reader.facade.join(space.space_id)
        assert joined.space_id == space.space_id

        created = owner.facade.write_text(space.space_id, "shared.md", "hello")
        _eventually_text(reader, space.space_id, created.node_id, "hello")
        owner.facade.write_text(space.space_id, f"id:{created.node_id}", "hello again")
        _eventually_text(reader, space.space_id, created.node_id, "hello again")
        changes = reader.facade.changes_since(space.space_id, "*.md")
        assert [(event.kind, event.node.path) for event in changes] == [
            ("created", "shared.md"),
            ("modified", "shared.md"),
        ]
        _assert_registered(
            owner.stores.get(space.space_id), reader.stores.get(space.space_id)
        )
    finally:
        reader.close()
        owner.close()


@pytest.mark.parametrize("owner_case", ["nonmember", "missing", "unknown", "error"])
def test_import_supplier_gate_refuses_non_member_device_owner(tmp_path, owner_case):
    """R1 supplier gate: import only from a current member's device row.

    The resolver maps a supplier node to the owner of its directory device
    row.  A resolved owner that is not a current member refuses the supplier
    outright, before any envelope is read and never as a parked row.  A node
    with no device-row evidence is refused in strict mode; only the explicitly
    selected private sidecar policy bypasses this gate (§7).
    """

    source = LocalSpaceStore(tmp_path / "source", "space-1", node_id="node-a")
    target = LocalSpaceStore(tmp_path / "target", "space-1", node_id="node-b")
    try:
        _seed(source)
        source.commit(
            TREE_DOC_ID,
            lambda doc: doc.__setitem__("gated", Text("gated")),
            author="user:owner",
            actor="agent:owner:a",
        )
        # Share only the meta genesis so the target knows the owner/member
        # rows before the gate runs; the tree commit is what sync must fetch.
        for row in source._db.execute(
            "SELECT envelope_bytes FROM commits WHERE doc_id = 'meta' ORDER BY rowid"
        ):
            target.import_envelope(bytes(row[0]), supplier="node-a")
        bus = _Bus()
        source_mesh = OrgFsMesh(bus.session(), source, "node-a", author="user:owner")
        owners = {"node-a": "user:ghost"}

        def broken_resolver(_supplier):
            raise RuntimeError("fixture owner lookup failed")

        resolver = {"nonmember": owners.get, "missing": None,
                    "unknown": {}.get, "error": broken_resolver}[owner_case]
        target_mesh = OrgFsMesh(
            bus.session(),
            target,
            "node-b",
            author="user:owner",
            supplier_owner=resolver,
        )
        try:
            before = target._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
            with pytest.raises(StoreError) as refused:
                target_mesh.sync_from("node-a")
            assert refused.value.code == "not-a-member"
            assert refused.value.details.get("reason") == "supplier-not-member"
            assert (
                target._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
                == before
            )
            # Only a resolved current member is admitted in strict mode.
            owners["node-a"] = "user:owner"
            target_mesh._supplier_owner = owners.get
            assert target_mesh.sync_from("node-a") > 0
            _assert_registered(source, target)
        finally:
            target_mesh.close()
            source_mesh.close()
    finally:
        target.close()
        source.close()
