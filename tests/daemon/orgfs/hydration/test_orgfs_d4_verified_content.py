from __future__ import annotations

import base64
import json
import socket
import threading
import time

import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from hyprial.kernel.impl.contracts.daemon import ipc_errors
from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.storage.blobs import ORGFS_BLOB_CHUNK_BYTES
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.mcp import DaemonRequestRejected, create_mcp_server
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
        except (AssertionError, OrgFsError) as exc:
            last_error = exc
            time.sleep(0.05)
    if last_error is not None:
        raise last_error
    raise AssertionError("condition did not become true")


def _equal(actual, expected) -> None:
    assert actual == expected


def _daemon_for(runtime: OrgFsRuntime) -> DaemonApplication:
    application = object.__new__(DaemonApplication)
    application._orgfs_runtime = runtime
    return application


def _pair(tmp_path):
    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    peer_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    # Verified hydration after private sidecar admission (§7); hashes stay enforced.
    owner = OrgFsRuntime(
        tmp_path / "owner", node_id="node-owner", author="user:owner", trust_tailnet_members=True
    )
    peer = OrgFsRuntime(
        tmp_path / "peer", node_id="node-peer", author="user:peer", trust_tailnet_members=True
    )
    time.sleep(0.3)
    online = {"node-owner", "node-peer"}.__contains__
    owner.bind_transport(owner_transport, supplier_online=online)
    peer.bind_transport(
        peer_transport,
        supplier_online=online,
        holder_candidates=lambda: ("node-owner",),
    )
    space = owner.facade.create_space("verified")
    owner.facade.invite(space.space_id, "user:peer", "rw")
    owner._mesh(space.space_id).announce()
    _eventually(lambda: peer.facade.join(space.space_id))
    return owner, peer, owner_transport, peer_transport, space


def _stop_blob_handler(runtime: OrgFsRuntime, space_id: str) -> None:
    runtime._meshes[space_id]._registrations[-1].close()


def _start_blob_handler(runtime: OrgFsRuntime, space_id: str) -> None:
    mesh = runtime._meshes[space_id]
    registration = mesh.session.declare_query_handler(
        f"{KeySpace().prefix}/org/fs/blob/*", mesh._handle_blob
    )
    mesh._registrations.append(registration)


def test_blob_pending_reports_expected_size_and_typed_daemon_error(tmp_path) -> None:
    owner, peer, owner_transport, peer_transport, space = _pair(tmp_path)
    try:
        _stop_blob_handler(owner, space.space_id)
        content = b"expected" * (ORGFS_BLOB_CHUNK_BYTES // 2)
        node = owner.facade.write_bytes(space.space_id, "archive.bin", content)
        info = _eventually(
            lambda: peer.facade.stat(space.space_id, f"id:{node.node_id}")
        )
        assert info.size == len(content)
        assert info.content_state == "pending"
        listed = peer.facade.listdir(space.space_id, "")
        assert [(item.size, item.content_state) for item in listed] == [
            (len(content), "pending")
        ]

        with pytest.raises(DaemonRequestError) as rejected:
            _daemon_for(peer)._handle_orgfs(
                "orgfs.read",
                {
                    "spaceId": space.space_id,
                    "node": f"id:{node.node_id}",
                    "waitSeconds": 0.25,
                },
            )
        assert rejected.value.code == ipc_errors.ORGFS_CONTENT_PENDING
        assert rejected.value.data["expectedSize"] == len(content)
        assert rejected.value.data["expectedSha256"] == node.blob_hash
        assert rejected.value.data["localState"] == "absent"
        assert "node-owner" in rejected.value.data["suggestedHolders"]
        assert rejected.value.data["waitedSeconds"] >= 0.25
    finally:
        peer.close()
        owner.close()
        peer_transport.close()
        owner_transport.close()


def test_blob_wait_actively_fetches_when_bytes_arrive(tmp_path) -> None:
    owner, peer, owner_transport, peer_transport, space = _pair(tmp_path)
    timer: threading.Timer | None = None
    try:
        _stop_blob_handler(owner, space.space_id)
        content = b"active-fetch" * 100_000
        node = owner.facade.write_bytes(space.space_id, "later.bin", content)
        _eventually(
            lambda: _equal(
                peer.facade.stat(space.space_id, f"id:{node.node_id}").content_state,
                "pending",
            )
        )
        timer = threading.Timer(
            0.4, _start_blob_handler, args=(owner, space.space_id)
        )
        timer.start()
        result = _daemon_for(peer)._handle_orgfs(
            "orgfs.read",
            {
                "spaceId": space.space_id,
                "node": f"id:{node.node_id}",
                "waitSeconds": 3,
            },
        )
        assert base64.b64decode(result["contentB64"]) == content
        assert result["node"]["contentState"] == "arrived"
    finally:
        if timer is not None:
            timer.cancel()
        peer.close()
        owner.close()
        peer_transport.close()
        owner_transport.close()


def test_corrupt_supplier_and_local_copy_are_never_returned(tmp_path) -> None:
    owner, peer, owner_transport, peer_transport, space = _pair(tmp_path)
    try:
        content = b"verified-content" * 100_000
        node = owner.facade.write_bytes(space.space_id, "verified.bin", content)
        assert node.blob_hash is not None
        _eventually(
            lambda: peer.facade.stat(space.space_id, f"id:{node.node_id}")
        )
        owner_path = owner.blobs._path(node.blob_hash)
        owner_path.write_bytes(b"x" * len(content))

        with pytest.raises(DaemonRequestError) as supplier_rejected:
            _daemon_for(peer)._handle_orgfs(
                "orgfs.read",
                {
                    "spaceId": space.space_id,
                    "node": f"id:{node.node_id}",
                    "waitSeconds": 0.5,
                },
            )
        assert supplier_rejected.value.code == ipc_errors.ORGFS_CONTENT_PENDING
        assert supplier_rejected.value.data["localState"] == "absent"

        # Supplier corruption has no kill-mutation: BlobStore.put derives the
        # destination from the received bytes, so corrupt data cannot occupy
        # the entry's expected content address even if fetch checks disappear.
        owner_path.write_bytes(content)
        fetched = _daemon_for(peer)._handle_orgfs(
            "orgfs.read",
            {
                "spaceId": space.space_id,
                "node": f"id:{node.node_id}",
                "waitSeconds": 3,
            },
        )
        assert base64.b64decode(fetched["contentB64"]) == content

        peer.blobs._path(node.blob_hash).write_bytes(b"z" * len(content))
        with pytest.raises(DaemonRequestError) as local_rejected:
            _daemon_for(peer)._handle_orgfs(
                "orgfs.read",
                {
                    "spaceId": space.space_id,
                    "node": f"id:{node.node_id}",
                    "waitSeconds": 0,
                },
            )
        assert local_rejected.value.code == ipc_errors.ORGFS_CONTENT_PENDING
        assert local_rejected.value.data["localState"] == "corrupt"
    finally:
        peer.close()
        owner.close()
        peer_transport.close()
        owner_transport.close()


def test_text_required_frontier_blocks_stale_read_then_converges(tmp_path) -> None:
    owner = OrgFsRuntime(
        tmp_path / "owner", node_id="node-owner", author="user:owner"
    )
    peer = OrgFsRuntime(
        tmp_path / "peer", node_id="node-peer", author="user:peer"
    )
    try:
        space = owner.facade.create_space("text-frontier")
        owner.facade.invite(space.space_id, "user:peer", "rw")
        node = owner.facade.write_text(space.space_id, "note.md", "A")
        owner.facade.write_text(space.space_id, f"id:{node.node_id}", "AB")
        rows = [
            (str(row[0]), bytes(row[1]))
            for row in owner.stores[space.space_id]._db.execute(
                "SELECT doc_id, envelope_bytes FROM commits ORDER BY rowid"
            )
        ]
        content_rows = [row for row in rows if row[0] == node.doc_id]
        assert len(content_rows) == 2
        withheld = content_rows[-1][1]
        peer_store = peer.stores.get(space.space_id)
        for _doc_id, envelope in rows:
            if envelope == withheld:
                continue
            result = peer_store.import_envelope(envelope, supplier="node-owner")
            assert result.status in {"applied", "duplicate"}, result
        peer.facade.load_space(space.space_id)

        pending = peer.facade.stat(space.space_id, f"id:{node.node_id}")
        assert pending.content_state == "pending"
        with pytest.raises(OrgFsError) as rejected:
            peer.facade.read_text(space.space_id, f"id:{node.node_id}")
        assert rejected.value.code == ipc_errors.ORGFS_CONTENT_PENDING
        assert rejected.value.details["requiredFrontier"]
        assert rejected.value.details["localFrontier"]

        result = peer_store.import_envelope(withheld, supplier="node-owner")
        assert result.status == "applied"
        peer.facade.apply_envelope(space.space_id, withheld)
        assert peer.facade.read_text(space.space_id, f"id:{node.node_id}")[0] == "AB"
        assert (
            peer.facade.stat(space.space_id, f"id:{node.node_id}").content_state
            == "arrived"
        )
    finally:
        peer.close()
        owner.close()


def test_legacy_entries_are_honestly_unverifiable_or_unknown_size(tmp_path) -> None:
    runtime = OrgFsRuntime(
        tmp_path / "runtime", node_id="node-owner", author="user:owner"
    )
    try:
        space = runtime.facade.create_space("legacy")
        blob = runtime.facade.write_bytes(space.space_id, "old.bin", b"legacy")
        doc = runtime.facade.write_text(space.space_id, "old.md", "legacy text")
        live = runtime.facade._space(space.space_id)

        def write_legacy_entries() -> None:
            for item in (live.nodes[blob.node_id], live.nodes[doc.node_id]):
                item.size = None
                item.required_content_frontier = None
                item.ref_size = None
                item.ref_sha256 = None
                live.tree._write_node(
                    {
                        "node_id": item.node_id,
                        "parent": item.parent,
                        "name": item.name,
                        "kind": item.kind,
                        "doc_id": item.doc_id,
                        "blob_hash": item.blob_hash,
                        "deleted": item.deleted,
                    }
                )

        runtime.facade._commit(live, live.tree_doc_id, write_legacy_entries)
        blob_info = runtime.facade.stat(space.space_id, f"id:{blob.node_id}")
        assert blob_info.size is None
        assert blob_info.content_state == "arrived"
        assert runtime.facade.read_bytes(space.space_id, f"id:{blob.node_id}") == b"legacy"
        doc_info = runtime.facade.stat(space.space_id, f"id:{doc.node_id}")
        assert doc_info.content_state == "unverifiable"
        assert runtime.facade.read_text(space.space_id, f"id:{doc.node_id}")[0] == "legacy text"
        healed = runtime.facade.write_text(
            space.space_id, f"id:{doc.node_id}", "legacy text"
        )
        assert healed.content_state == "arrived"
    finally:
        runtime.close()


def test_daemon_orgfs_json_uses_content_state_wire_key() -> None:
    from hyprial.daemon.impl.orgfs.api import NodeInfo

    value = DaemonApplication._orgfs_json(
        NodeInfo(
            "space",
            "node",
            "blob",
            "x",
            "x",
            1,
            "0" * 64,
            None,
            "v",
            "user:owner",
            None,
            False,
            False,
            "pending",
            uri="orgfs:owner:space:node",
        )
    )
    assert value["contentState"] == "pending"
    assert "content_state" not in value


def test_content_frontier_tree_entry_carries_reference_metadata(tmp_path) -> None:
    runtime = OrgFsRuntime(
        tmp_path / "runtime", node_id="node-owner", author="user:owner"
    )
    try:
        space = runtime.facade.create_space("wire")
        node = runtime.facade.write_text(space.space_id, "note.md", "hello")
        raw = runtime.facade._space(space.space_id).tree.materialize()[node.node_id]
        assert base64.b64decode(raw["contentFrontier"], validate=True)
        assert raw["refSize"] == len(b"hello")
        assert raw["refSha256"]
        assert json.loads(json.dumps(raw))["contentFrontier"] == raw["contentFrontier"]
    finally:
        runtime.close()


@pytest.mark.anyio
async def test_mcp_read_preserves_content_pending_code_and_data() -> None:
    details = {
        "kind": "blob",
        "spaceId": "space",
        "node": "id:node",
        "path": "x.bin",
        "expectedSize": 7,
        "expectedSha256": "0" * 64,
        "localState": "absent",
        "suggestedHolders": ["node-owner"],
        "waitedSeconds": 0,
    }

    class RejectingProxy:
        async def call(self, **kwargs):
            assert kwargs["params"]["waitSeconds"] == 0
            raise DaemonRequestRejected(
                ipc_errors.ORGFS_CONTENT_PENDING, "pending", details
            )

    server = create_mcp_server(
        RejectingProxy(),  # type: ignore[arg-type]
        actor="agent:owner:node:worker",
        session_ref="session",
    )
    async with (
        InMemoryTransport(server) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        result = await client.call_tool(
            "orgfs_read", {"spaceId": "space", "node": "id:node", "waitSeconds": 0}
        )
    assert result.is_error is True
    payload = json.loads(result.content[0].text.partition(": ")[2])
    assert payload == {"code": ipc_errors.ORGFS_CONTENT_PENDING, "data": details}
