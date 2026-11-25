# Decisions:
# - Phase D, package T2: the P1 acceptance scenarios that remain run on
#   ``OrgFsRuntime`` over isolated loopback Zenoh (the cold-cache blob
#   fetch, which is the only place ``await_content`` on a blob crosses the real
#   transport) or over the in-process network (the restarted owner).
# - Deleted, each covered elsewhere on the new runtime:
#   * ``test_revocation_offline_window_write_restore_and_reconverge`` and
#     ``test_revoked_writer_rejected_across_all_three_mesh_injection_paths``:
#     protocol 3 frames carry no author, so "a removed writer's forged
#     envelope" has no counterpart; the receiver's defence is the node gate.
#     Covered by contract ``multinode/test_remote_admission.py::
#     test_writes_of_a_member_removed_while_apart_do_not_change_the_owner``
#     (its ``state`` parameter fails on the gap recorded in design §9.2,
#     first row), ``runtime_state/test_node_gate.py`` (a removed member's
#     node is refused and its partition-time writes do not land), and
#     ``sync/test_broadcast.py::test_node_gate_drops_frames_from_offline_or_non_member_nodes``
#     with ``sync/test_pull.py::test_requester_refuses_a_supplier_that_fails_the_node_gate``
#     (the two injection paths protocol 3 has: broadcast and pull).
#   * ``test_delete_only_commit_catches_up_after_offline_live_broadcast``:
#     contract ``multinode/test_convergence.py::test_a_pure_deletion_made_while_disconnected_catches_up``
#     (C4 report §5) and ``runtime_state/test_multinode.py::
#     test_a_partitioned_node_catches_up_including_a_pure_deletion``.
#   * ``test_restore_to_empty_catches_up_after_offline_live_broadcast``:
#     restore by version is removed (design §6); the remaining case, content
#     cleared to empty while the peer is away, is the pure-deletion contract.
"""Cross-slice P1 acceptance scenarios on the state runtime."""

from __future__ import annotations

from collections.abc import Iterator
import socket
import time

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.composition.state import OrgFsRuntime
from hyprial.daemon.impl.transport import ZenohConfig, ZenohTransport
from tests.daemon.orgfs.mesh.replication.state_support import StatePeers, open_state_peers
from tests.daemon.orgfs.runtime_state.support import Peer, eventually, text


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(autouse=True)
def isolated_orgfs_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


@pytest.fixture
def state_peers(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[StatePeers]:
    yield from open_state_peers(tmp_path, monkeypatch)


def test_cold_cache_blob_unavailable_then_fetches_when_holder_returns(tmp_path) -> None:
    """review-p1 15h: §3.5 cold-cache reads fail fast, then the wait fetches the blob."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    online = {"node-owner", "node-reader"}
    is_online = online.__contains__
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    reader_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-owner", author="user:owner", trust_tailnet_members=True)
    reader = OrgFsRuntime(tmp_path / "reader", node_id="node-reader", author="user:reader", trust_tailnet_members=True)
    returned_owner: OrgFsRuntime | None = None
    try:
        time.sleep(0.3)
        owner.bind_transport(owner_transport, supplier_online=is_online)
        reader.bind_transport(
            reader_transport,
            supplier_online=is_online,
            holder_candidates=lambda: tuple(online - {"node-reader"}),
        )
        space = owner.facade.create_space("team")
        owner.facade.invite(space.space_id, "user:reader", "rw")
        payload = b"only-the-offline-holder-has-this" * 40_000
        created = owner.facade.write_bytes(space.space_id, "archive.bin", payload)
        eventually(lambda: reader.facade.join(space.space_id))
        assert created.blob_hash is not None

        owner.close()
        online.remove("node-owner")
        started = time.monotonic()
        with pytest.raises(OrgFsError) as unavailable:
            reader.facade.read_bytes(space.space_id, f"id:{created.node_id}")
        assert unavailable.value.code == "content-pending"
        assert unavailable.value.details["expectedSha256"] == created.blob_hash
        assert "node-owner" in unavailable.value.details["suggestedHolders"]
        assert time.monotonic() - started < 1.0
        assert not reader.await_content(
            space.space_id, created.node_id, deadline_monotonic=time.monotonic() + 0.3
        )

        online.add("node-owner")
        returned_owner = OrgFsRuntime(
            tmp_path / "owner", node_id="node-owner", author="user:owner", trust_tailnet_members=True
        )
        time.sleep(0.2)
        returned_owner.bind_transport(owner_transport, supplier_online=is_online)
        assert reader.await_content(
            space.space_id, created.node_id, deadline_monotonic=time.monotonic() + 8
        )
        assert reader.facade.read_bytes(space.space_id, f"id:{created.node_id}") == payload
    finally:
        if returned_owner is not None:
            returned_owner.close()
        else:
            owner.close()
        reader.close()
        reader_transport.close()
        owner_transport.close()


def test_restart_file_first_then_invite_preserves_member_writes_everywhere(
    state_peers: StatePeers,
) -> None:
    """Round-3 D.2(a/b): a restarted owner's first write is a file; the next member's writes reach all."""

    owner = state_peers.add("node-owner", "user:owner")
    member = state_peers.add("node-b", "user:b")
    third = state_peers.add("node-third", "user:third")
    space = owner.fs.create_space("restart-attribution")
    owner.fs.invite(space.space_id, "user:third", "rw")
    assert third.fs.join(space.space_id) == space

    owner.reopen()
    owner.fs.write_text(space.space_id, "owner-after-restart.md", "owner")
    owner.fs.invite(space.space_id, "user:b", "rw")
    assert "user:b" in {item.user for item in owner.fs.members(space.space_id)}
    eventually(lambda: member.fs.join(space.space_id))
    eventually(lambda: _assert_member(third, space.space_id, "user:b"))

    by_member = member.fs.write_text(space.space_id, "member-after-invite.md", "from-b")

    for peer in (owner, member, third):
        eventually(lambda peer=peer: _assert_text(peer, space.space_id, f"id:{by_member.node_id}", "from-b"))
        assert peer.fs.stat(space.space_id, f"id:{by_member.node_id}").modified_by == "user:b"
        eventually(lambda peer=peer: _assert_text(peer, space.space_id, "owner-after-restart.md", "owner"))


def _assert_member(peer: Peer, space_id: str, user: str) -> None:
    assert user in {item.user for item in peer.fs.members(space_id)}


def _assert_text(peer: Peer, space_id: str, ref: str, expected: str) -> None:
    assert text(peer.fs, space_id, ref) == expected
