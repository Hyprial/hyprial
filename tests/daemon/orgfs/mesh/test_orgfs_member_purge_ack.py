"""Pin for the member purge-ack rejection (80a6e231 group).

Seen in CI next to the resident_blobs fd leak as a logged
``not-owner: metadata update is owner-only`` from a zenoh log callback: a
member's first purge ack is rejected, and only a retry after a later owner
meta update lands it.
"""

from __future__ import annotations

import socket
import time

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.storage.store import LocalSpaceStore
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


def _assert_true(value) -> bool:
    assert value
    return value


def _joined_pair(tmp_path):
    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    peer_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    # Purge ACK authorship remains enforced after private sidecar admission (§7).
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-owner", author="user:owner", trust_tailnet_members=True)
    peer = OrgFsRuntime(tmp_path / "peer", node_id="node-peer", author="user:peer", trust_tailnet_members=True)
    time.sleep(0.3)
    owner.bind_transport(owner_transport, supplier_online=lambda _node: True)
    peer.bind_transport(
        peer_transport,
        supplier_online=lambda _node: True,
        holder_candidates=lambda: ("node-owner",),
    )
    space = owner.facade.create_space("pins")
    owner.facade.invite(space.space_id, "user:peer", "rw")

    def announce_and_join():
        owner._announce_all()
        return peer.facade.join(space.space_id)

    _eventually(announce_and_join)
    return owner, peer, owner_transport, peer_transport, space.space_id


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "80a6e231: a member's first purge ack touches the absent purgeAcks "
        "root, is classified as ('purgeAcks', None) and rejected as owner-only"
    ),
)
def test_member_acknowledges_purge_when_no_ack_exists_yet(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The peer's first ack attempt must be accepted, not retried into place.

    Today the first attempt (no ``purgeAcks`` root yet) is rejected inside the
    log callback and only a later owner meta update lets a retry succeed; with
    no further owner update the ack would never land.
    """

    # Count rejections at the store's own permission check, not in logs: how
    # the callback failure is reported differs between runtimes.
    rejected: list[str] = []
    allowed = LocalSpaceStore._local_meta_exception_allowed

    def recording(self, **kwargs):
        verdict = allowed(self, **kwargs)
        if not verdict:
            rejected.append(f"{self.node_id}:{kwargs.get('author')}")
        return verdict

    monkeypatch.setattr(LocalSpaceStore, "_local_meta_exception_allowed", recording)
    owner, peer, owner_transport, peer_transport, space_id = _joined_pair(tmp_path)
    try:
        source = tmp_path / "purged.bin"
        source.write_bytes(b"purged before any ack exists")
        purged = owner.facade.write_bytes(space_id, "purged.bin", source.read_bytes())
        assert purged.blob_hash is not None
        plan = owner.purge_plan(space_id, [{"kind": "blob", "sha": purged.blob_hash}])
        owner.purge(space_id, plan.plan_id)
        _eventually(
            lambda: _assert_true(
                "node-peer" in owner.purge_status(space_id, plan.plan_id).acknowledged
            )
        )
        assert rejected == []
    finally:
        peer.close()
        owner.close()
        peer_transport.close()
        owner_transport.close()
