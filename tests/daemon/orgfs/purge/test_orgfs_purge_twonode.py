"""P2R cross-node purge/reclone acceptance over isolated loopback Zenoh."""

from __future__ import annotations

import hashlib
import json
import socket
from threading import Event, Thread
import time
import tracemalloc

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.storage.blobs import encode_request
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.storage.store import (
    ORGFS_INLINE_UPDATE_BYTES,
    StoreError,
    assert_client_ids_registered)
from hyprial.daemon.impl.transport import KeySpace, ZenohConfig, ZenohTransport


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _eventually(action, *, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return action()
        except (AssertionError, OrgFsError, StoreError) as exc:
            last_error = exc
            time.sleep(0.05)
    if last_error is not None:
        raise last_error
    raise AssertionError("condition did not become true")


def _doc_target(node_id: str) -> dict[str, str]:
    return {
        "kind": "doc-range",
        "node": f"id:{node_id}",
        "from": "all",
        "to": "all",
    }


def _scan_replica_backend(
    runtime: OrgFsRuntime, space_id: str, *needles: bytes
) -> None:
    backend = runtime._replicas[space_id].backend
    for prefix in ("log/", "snapshot/", "blob/"):
        for key in backend.keys(prefix):
            value = backend.get(key)
            assert value is not None
            assert all(needle not in value for needle in needles), key


@pytest.fixture(autouse=True)
def isolated_orgfs_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


def test_cross_node_reclone_offline_cleanup_ack_and_fresh_joiner(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = f"tcp/127.0.0.1:{_port()}"
    online = {"node-a", "node-b"}
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    member_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    # Cross-node purge runs after private sidecar admission (§7); purge rules stay real.
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-a", author="user:owner", trust_tailnet_members=True)
    member = OrgFsRuntime(tmp_path / "member", node_id="node-b", author="user:member", trust_tailnet_members=True)
    returned: OrgFsRuntime | None = None
    returned_transport: ZenohTransport | None = None
    newcomer: OrgFsRuntime | None = None
    newcomer_transport: ZenohTransport | None = None
    allow_install = Event()
    try:
        time.sleep(0.3)
        owner.bind_transport(owner_transport, supplier_online=online.__contains__)
        member.bind_transport(
            member_transport,
            supplier_online=online.__contains__,
            holder_candidates=lambda: ("node-a",),
        )
        space = owner.facade.create_space("team")
        owner.facade.invite(space.space_id, "user:member", "rw")
        owner._mesh(space.space_id).announce()
        _eventually(lambda: member.facade.join(space.space_id))
        assert_client_ids_registered(owner.stores[space.space_id])
        assert_client_ids_registered(member.stores[space.space_id])

        marker = b"P2R-FORBIDDEN-HISTORY-9d31"
        historical = (marker + b"x" * (ORGFS_INLINE_UPDATE_BYTES + 64_000)).decode()
        node = owner.facade.write_text(space.space_id, "history.md", historical)
        owner_rows = tuple(
            (bytes(row[0]), json.loads(bytes(row[0])))
            for row in owner.stores[space.space_id]._db.execute(
                "SELECT envelope_bytes FROM commits WHERE doc_id = ? ORDER BY rowid",
                (node.doc_id,),
            )
        )
        large_envelope, large_value = next(
            row for row in owner_rows if "updateBlob" in row[1]
        )
        large_digest = str(large_value["updateBlob"])
        fetched = owner.blobs.get(space.space_id, large_digest)
        assert (
            member.blobs.put(space.space_id, fetched, reason="replica") == large_digest
        )
        # The large content envelope's metaFrontier covers the owner's meta
        # commit from the same write, which reaches the member by broadcast.
        # If the broadcast has not landed yet the store parks the envelope as
        # pending-meta and drains it when the meta arrives (CI tasks 37841 and
        # 38738); wait for that instead of racing it.
        first = member._meshes[space.space_id].receive(
            large_envelope, supplier="node-a"
        )
        assert (first.status, first.code) in {
            ("applied", None),
            ("applied", "duplicate"),
            ("pending", "pending-meta"),
        }, first
        _eventually(
            lambda: (
                member._meshes[space.space_id]
                .receive(large_envelope, supplier="node-a")
                .status
                == "applied"
                or (_ for _ in ()).throw(AssertionError())
            )
        )
        # The envelope above carries only the document content; the tree entry
        # that makes id:<node> resolvable arrives by mesh broadcast.  Wait for
        # it instead of racing it (CI task 34368: unknown-doc).
        _eventually(
            lambda: (
                member.facade.read_text(space.space_id, f"id:{node.node_id}")[0]
                == historical
                or (_ for _ in ()).throw(AssertionError())
            )
        )
        member.facade.write_text(
            space.space_id, f"id:{node.node_id}", "sanitized and non-empty"
        )
        owner.await_content(
            space.space_id,
            node.node_id,
            deadline_monotonic=time.monotonic() + 8,
        )

        def owner_text() -> str:
            try:
                return owner.facade.read_text(
                    space.space_id, f"id:{node.node_id}"
                )[0]
            except OrgFsError as exc:
                if exc.code != "content-pending":
                    raise
                owner.await_content(
                    space.space_id,
                    node.node_id,
                    deadline_monotonic=time.monotonic() + 1,
                )
                return owner.facade.read_text(
                    space.space_id, f"id:{node.node_id}"
                )[0]

        _eventually(
            lambda: (
                owner_text() == "sanitized and non-empty"
                or (_ for _ in ()).throw(AssertionError())
            )
        )

        banned_bytes = b"P2R-BANNED-BLOB-731a"
        blob_node = owner.facade.write_bytes(space.space_id, "banned.bin", banned_bytes)
        member._meshes[space.space_id].sync_from("node-a")
        banned_digest = hashlib.sha256(banned_bytes).hexdigest()
        assert (
            member.blobs.put(space.space_id, banned_bytes, reason="replica")
            == banned_digest
        )
        _eventually(
            lambda: (
                member.facade.read_bytes(space.space_id, f"id:{blob_node.node_id}")
                == banned_bytes
                or (_ for _ in ()).throw(AssertionError())
            )
        )
        old_doc_id = node.doc_id
        assert any("updateBlob" in envelope for _raw, envelope in owner_rows)

        member.serve(space.space_id, "fs")
        assert_client_ids_registered(owner.stores[space.space_id])
        assert_client_ids_registered(member.stores[space.space_id])
        member.close()
        member_transport.close()
        online.remove("node-b")

        plan = owner.purge_plan(
            space.space_id,
            [_doc_target(node.node_id), {"kind": "blob", "sha": banned_digest}],
        )
        purged = owner.purge(space.space_id, plan.plan_id)
        assert "node-b" in purged.pending
        assert "user:member" not in purged.pending
        retirement = owner.stores[space.space_id].retired(old_doc_id)
        assert retirement is not None
        assert (
            owner.stores[space.space_id]
            ._db.execute(
                "SELECT COUNT(*) FROM commits WHERE doc_id = ?",
                (retirement.replacement_doc_id,),
            )
            .fetchone()[0]
            > 0
        )

        allowed_suppliers: set[str] = set()
        returned = OrgFsRuntime(
            tmp_path / "member", node_id="node-b", author="user:member",
            trust_tailnet_members=True,  # Same private-path fixture after restart (§7).
        )
        returned.serve(space.space_id, "fs")
        returned_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
        time.sleep(0.2)
        returned.bind_transport(
            returned_transport,
            supplier_online=allowed_suppliers.__contains__,
            holder_candidates=lambda: ("node-a",),
        )
        time.sleep(0.3)
        mesh = returned._meshes[space.space_id]
        # This process is the requester in this leg; remove its own wildcard
        # blob queryable so the content-addressed snapshot request is answered
        # by the remote holder rather than the local empty replica backend.
        mesh._registrations.pop().close()
        snapshot_seen = Event()
        original_install = mesh._install_sync_snapshot

        def gated_install(supplier, manifest, *, timeout):
            snapshot_id = str(manifest["snapshotId"])
            replacement_doc_id = str(manifest["docId"])
            # G2 debt 3, cross-node ordering: B can only observe publication
            # after A has made both the snapshot row and its blob body available.
            assert owner.blobs.has(snapshot_id)
            assert (
                owner.stores[space.space_id]
                .snapshot(replacement_doc_id, shallow_since=None)
                .snapshot_id
                == snapshot_id
            )
            snapshot_seen.set()
            assert allow_install.wait(5.0)
            return original_install(supplier, manifest, timeout=timeout)

        monkeypatch.setattr(mesh, "_install_sync_snapshot", gated_install)
        allowed_suppliers.add("node-a")
        online.add("node-b")
        result: list[int] = []
        failure: list[BaseException] = []

        def sync() -> None:
            try:
                result.append(mesh.sync_from("node-a"))
            except BaseException as exc:  # surfaced in the test thread below
                failure.append(exc)

        worker = Thread(target=sync, daemon=True)
        tracemalloc.start()
        reclone_started = time.perf_counter()
        worker.start()
        if not snapshot_seen.wait(5.0):
            if failure:
                raise failure[0]
            raise AssertionError("replacement snapshot was not offered")
        assert returned.stores[space.space_id].retired(old_doc_id) is not None
        with pytest.raises(OrgFsError) as fenced:
            returned.facade.write_text(
                space.space_id, f"id:{node.node_id}", "must stay fenced"
            )
        assert fenced.value.code == "snapshot-barrier"
        allow_install.set()
        worker.join(8.0)
        assert not worker.is_alive()
        assert not failure
        assert result and result[0] > 0
        reclone_elapsed = time.perf_counter() - reclone_started
        _reclone_current, reclone_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print(
            "G2 two-node reclone "
            f"elapsed_s={reclone_elapsed:.6f} peak_bytes={reclone_peak}"
        )
        assert (
            returned.facade.read_text(space.space_id, f"id:{node.node_id}")[0]
            == "sanitized and non-empty"
        )
        assert returned.stores[space.space_id].retired_residue(old_doc_id) == ()
        _eventually(
            lambda: (
                owner.purge_status(space.space_id, plan.plan_id).pending == ()
                or (_ for _ in ()).throw(AssertionError())
            )
        )

        # Exercise the handlers themselves, not ReplicaStore methods.
        sync_request = json.dumps(
            {
                "schemaVersion": 1,
                "type": "orgfs-sync",
                "spaceId": space.space_id,
                "docIds": [old_doc_id],
                "versionVectors": {},
                "requester": {"node": "node-a", "author": "user:owner"},
                "cursor": None,
                "maxBytes": 1_048_576,
            },
            separators=(",", ":"),
        ).encode()
        sync_reply = json.loads(tuple(mesh._handle_sync("", sync_request))[0][1])
        assert sync_reply["code"] == "snapshot-barrier"
        log_request = json.dumps(
            {
                "schemaVersion": 1,
                "type": "orgfs-log-range",
                "spaceId": space.space_id,
                "docId": old_doc_id,
                "writer": "node-a.writer.lane",
                "after": None,
                "limit": 1,
            },
            separators=(",", ":"),
        ).encode()
        log_reply = json.loads(tuple(mesh._handle_log_range("", log_request))[0][1])
        assert log_reply["code"] == "snapshot-barrier"
        blob_reply = json.loads(
            tuple(
                mesh._handle_blob(
                    KeySpace().orgfs_blob(banned_digest),
                    encode_request(space.space_id, banned_digest, 0, None),
                )
            )[0][1]
        )
        assert blob_reply["code"] == "purged"

        newcomer = OrgFsRuntime(
            tmp_path / "newcomer", node_id="node-c", author="user:member",
            trust_tailnet_members=True,  # Fresh clone on the admitted private path (§7).
        )
        newcomer_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
        time.sleep(0.2)
        newcomer.bind_transport(
            newcomer_transport,
            supplier_online=lambda supplier: supplier == "node-a",
            holder_candidates=lambda: ("node-a",),
        )
        newcomer_mesh = newcomer._mesh(space.space_id)
        assert newcomer_mesh is not None
        newcomer_mesh._registrations.pop().close()
        newcomer.join(space.space_id)
        assert (
            newcomer.facade.read_text(space.space_id, f"id:{node.node_id}")[0]
            == "sanitized and non-empty"
        )
        for runtime in (owner, returned, newcomer):
            assert_client_ids_registered(runtime.stores[space.space_id])

        needles = (marker, banned_bytes)
        for runtime in (owner, returned, newcomer):
            scans = runtime.stores[space.space_id].scan_forbidden_bytes(needles)
            assert scans == {"journal": (), "documents": (), "blobs": ()}
        _scan_replica_backend(returned, space.space_id, *needles)
    finally:
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        allow_install.set()
        if newcomer is not None:
            newcomer.close()
        if newcomer_transport is not None:
            newcomer_transport.close()
        if returned is not None:
            returned.close()
        if returned_transport is not None:
            returned_transport.close()
        if member._session is not None:
            member.close()
        if owner._session is not None:
            owner.close()
        owner_transport.close()
