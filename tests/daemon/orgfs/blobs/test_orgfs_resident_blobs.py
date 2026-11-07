"""Production-entry regressions for resident replica blob reconciliation."""

from __future__ import annotations

import socket
import time

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.storage.blobs import BlobPurged
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.transport import ZenohConfig, ZenohTransport


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(autouse=True)
def isolated_orgfs_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


def _eventually(action, *, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return action()
        except (AssertionError, OrgFsError) as error:
            last_error = error
            time.sleep(0.05)
    if last_error is not None:
        raise last_error
    raise AssertionError("condition did not become true")


def _assert_equal(actual, expected):
    assert actual == expected
    return actual


def _close_and_drain(transport: ZenohTransport) -> None:
    """Close ``transport`` and wait until its callback lanes have run dry."""

    transport.close()
    for worker in transport._dispatcher._threads:  # noqa: SLF001
        if worker is not None:
            worker.join(timeout=10.0)
            assert not worker.is_alive(), "zenoh callback lane did not drain"


def _loopback_pair(tmp_path):
    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    peer_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    # Exercise resident blobs after private sidecar admission (§7), not admission itself.
    owner = OrgFsRuntime(
        tmp_path / "owner", node_id="node-owner", author="user:owner",
        trust_tailnet_members=True,
    )
    peer = OrgFsRuntime(
        tmp_path / "peer", node_id="node-peer", author="user:peer",
        trust_tailnet_members=True,
    )
    time.sleep(0.3)
    owner.bind_transport(owner_transport, supplier_online=lambda _node: True)
    peer.bind_transport(
        peer_transport,
        supplier_online=lambda _node: True,
        holder_candidates=lambda: ("node-owner",),
    )
    return owner, peer, owner_transport, peer_transport


def test_import_after_serve_is_fetchable_from_resident_replica(tmp_path) -> None:
    owner, peer, owner_transport, peer_transport = _loopback_pair(tmp_path)
    try:
        space = owner.facade.create_space("resident blobs")
        owner.facade.invite(space.space_id, "user:peer", "rw")
        owner.serve(space.space_id, "memory")
        owner._announce_all()
        _eventually(lambda: peer.facade.join(space.space_id))

        payload = b"post-serve production import"
        source = tmp_path / "artifact.bin"
        source.write_bytes(payload)
        imported = owner.facade.write_bytes(space.space_id, "artifact.bin", source.read_bytes())
        assert imported.blob_hash is not None

        _eventually(
            lambda: _assert_equal(
                peer.fetch_blob(space.space_id, imported.blob_hash), payload
            )
        )
        assert (
            imported.blob_hash in owner.serve(space.space_id, "memory")["pinnedBlobs"]
        )
    finally:
        peer.close()
        owner.close()
        peer_transport.close()
        owner_transport.close()


def test_repeat_serve_reconciles_retained_blobs_without_restoring_purged(
    tmp_path,
) -> None:
    owner, peer, owner_transport, peer_transport = _loopback_pair(tmp_path)
    try:
        space = owner.facade.create_space("resident retention")
        owner.facade.invite(space.space_id, "user:peer", "rw")
        owner.serve(space.space_id, "memory")
        owner._announce_all()
        _eventually(lambda: peer.facade.join(space.space_id))

        source = tmp_path / "retained.bin"
        source.write_bytes(b"history retained")
        historical = owner.facade.write_bytes(space.space_id, "retained.bin", source.read_bytes())
        source.write_bytes(b"trash retained")
        trashed = owner.facade.write_bytes(
            space.space_id, f"id:{historical.node_id}", source.read_bytes()
        )
        owner.facade.remove(space.space_id, f"id:{historical.node_id}")
        assert historical.blob_hash is not None
        assert trashed.blob_hash is not None

        purge_source = tmp_path / "purged.bin"
        purge_source.write_bytes(b"must never return")
        purged = owner.facade.write_bytes(space.space_id, "purged.bin", purge_source.read_bytes())
        assert purged.blob_hash is not None
        plan = owner.purge_plan(
            space.space_id, [{"kind": "blob", "sha": purged.blob_hash}]
        )
        owner.purge(space.space_id, plan.plan_id)

        replica = owner._replicas[space.space_id]
        replica.apply_blob_purge((historical.blob_hash, trashed.blob_hash))
        assert replica.pinned_blobs() == ()

        repeated = owner.serve(space.space_id, "memory")
        assert set(repeated["pinnedBlobs"]) == {
            historical.blob_hash,
            trashed.blob_hash,
        }
        assert replica.backend_get(f"blob/{purged.blob_hash}") is None
        with pytest.raises(BlobPurged):
            owner.blobs.pin(space.space_id, purged.blob_hash, "replica")
        with pytest.raises(OrgFsError) as unavailable:
            peer.fetch_blob(space.space_id, purged.blob_hash)
        assert unavailable.value.code == "blob-unavailable"
    finally:
        # Drain callback lanes before closing the runtimes: a log callback
        # still queued at runtime close re-opens the peer's space store and
        # leaks its journal fds.  That product defect is fixed on #976 and
        # pinned there by its late-callback-after-close regression test;
        # tracked by 80a6e231.
        _close_and_drain(peer_transport)
        _close_and_drain(owner_transport)
        peer.close()
        owner.close()


def test_inbound_tree_blob_is_materialized_in_serving_replica(tmp_path) -> None:
    owner, resident, owner_transport, resident_transport = _loopback_pair(tmp_path)
    try:
        space = owner.facade.create_space("inbound resident blob")
        owner.facade.invite(space.space_id, "user:peer", "rw")
        owner._announce_all()
        _eventually(lambda: resident.facade.join(space.space_id))
        resident.serve(space.space_id, "memory")

        source = tmp_path / "inbound.bin"
        source.write_bytes(b"inbound tree reference")
        imported = owner.facade.write_bytes(space.space_id, "inbound.bin", source.read_bytes())
        assert imported.blob_hash is not None

        _eventually(
            lambda: _assert_equal(
                imported.blob_hash
                in resident.serve(space.space_id, "memory")["pinnedBlobs"],
                True,
            )
        )
        assert (
            resident._replicas[space.space_id].backend_get(f"blob/{imported.blob_hash}")
            == source.read_bytes()
        )
    finally:
        resident.close()
        owner.close()
        resident_transport.close()
        owner_transport.close()

def test_serving_node_fetch_stores_and_pins_blob_in_its_replica(tmp_path) -> None:
    owner, resident, owner_transport, resident_transport = _loopback_pair(tmp_path)
    try:
        space = owner.facade.create_space("serving fetch store-back")
        owner.facade.invite(space.space_id, "user:peer", "rw")
        owner._announce_all()
        _eventually(lambda: resident.facade.join(space.space_id))
        resident.serve(space.space_id, "memory")

        payload = b"peer-only fetch payload"
        digest = owner.blobs.put(space.space_id, payload, reason="own-write")
        replica = resident._replicas[space.space_id]
        assert replica.backend_exists(f"blob/{digest}") is False
        assert replica.pinned_reason(digest) is None

        assert resident.fetch_blob(space.space_id, digest) == payload

        assert replica.backend_get(f"blob/{digest}") == payload
        assert replica.pinned_reason(digest) == "replica"
        assert (space.space_id, digest, "replica") in resident.blobs.references(
            space.space_id, digest
        )
    finally:
        resident.close()
        owner.close()
        resident_transport.close()
        owner_transport.close()
