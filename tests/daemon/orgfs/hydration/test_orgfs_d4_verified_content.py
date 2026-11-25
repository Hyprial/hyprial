# Decisions:
# - Phase D, package T2: ``test_text_required_frontier_blocks_stale_read_then_converges``
#   runs on ``StateOrgFsRuntime`` (plan §5: the text ``content-pending`` and
#   its convergence had no test on the new runtime).  Broadcasts are dropped;
#   the reader gets the owner's newer tree row through the sync host (the
#   seam every pulled update crosses) while the owner is partitioned, so the
#   row names a content frontier the reader's document does not cover.  The
#   read is refused with the typed error and both frontiers, and healing the
#   partition (an online edge, then a pull) delivers the text.
# - ``test_legacy_entries_are_honestly_unverifiable_or_unknown_size`` is
#   journal-only (triage) and left for package S.
# - ``test_content_frontier_tree_entry_carries_reference_metadata`` (triage
#   "pending") is kept unchanged: ``contentFrontier``, ``refSize`` and
#   ``refSha256`` stay in protocol 3 tree rows (they are what makes the read
#   above pending), and it passes on both runtimes.
from __future__ import annotations

from collections.abc import Iterator

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
from hyprial.daemon.impl.orgfs.membership.contract import Accepted
from hyprial.daemon.impl.transport import KeySpace, ZenohConfig, ZenohTransport
from tests.daemon.orgfs.mesh.replication.state_support import (
    StatePeers,
    open_state_peers,
    shared_space,
)
from tests.daemon.orgfs.runtime_state.support import eventually, text


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


@pytest.fixture
def state_peers(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[StatePeers]:
    yield from open_state_peers(tmp_path, monkeypatch)


def test_text_required_frontier_blocks_stale_read_then_converges(state_peers: StatePeers) -> None:
    network = state_peers.network
    network.drop_updates = True
    owner = state_peers.add("node-owner", "user:owner")
    peer = state_peers.add("node-peer", "user:peer")
    space_id = shared_space(state_peers, owner, peer, name="text-frontier")
    node = owner.fs.write_text(space_id, "note.md", "A")
    state_peers.pull_all(space_id)
    state_peers.pull_all(space_id)
    assert text(peer.fs, space_id, f"id:{node.node_id}") == "A"

    owner.fs.write_text(space_id, f"id:{node.node_id}", "AB")
    owner_host = owner.fs.sync_host(space_id)
    peer_host = peer.fs.sync_host(space_id)
    found = owner_host.read_update("tree", peer_host.read_vectors(("tree",))["tree"])
    assert found is not None
    network.partition("node-owner")
    assert isinstance(peer_host.apply_remote("tree", found[0], found[1], "node-owner"), Accepted)

    pending = peer.fs.stat(space_id, f"id:{node.node_id}")
    assert pending.content_state == "pending"
    with pytest.raises(OrgFsError) as rejected:
        peer.fs.read_text(space_id, f"id:{node.node_id}")
    assert rejected.value.code == ipc_errors.ORGFS_CONTENT_PENDING
    assert rejected.value.details["requiredFrontier"]
    assert rejected.value.details["localFrontier"]
    assert rejected.value.details["requiredFrontier"] != rejected.value.details["localFrontier"]
    assert "node-owner" in rejected.value.details["suggestedHolders"]

    network.heal("node-owner")
    eventually(lambda: _equal(text(peer.fs, space_id, f"id:{node.node_id}"), "AB"))
    assert peer.fs.stat(space_id, f"id:{node.node_id}").content_state == "arrived"


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
