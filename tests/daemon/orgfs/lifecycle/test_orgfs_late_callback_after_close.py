# Decisions:
# - Phase D, package T2: rewritten onto ``StateOrgFsRuntime``.  The late work
#   is no longer a journal log callback reaching the mesh's on-applied hook:
#   every inbound update reaches the coordinator through the space's sync
#   host (``SyncHost.apply_remote``), so the test keeps a reference to the
#   closed node's sync host and delivers a real update from the owner after
#   ``close``.  Refusing that work is allowed (``unavailable``); opening a
#   store again is not: the runtime's store registry must stay empty.
# - The in-process network replaces the two Zenoh sessions; the property is
#   about close custody, not transport.
"""Work arriving after runtime close must not re-open space stores.

On dev (pre-#976) the on-applied hook reached the lazy store registry after
close() cleared it and re-opened the space's files, which nothing closed
again (the resident_blobs CI fd leak, 80a6e231 group).  #976 refuses late
work with ``unavailable: orgfs is closing``; this keeps that pinned.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.membership.contract import Accepted
from tests.daemon.orgfs.mesh.replication.state_support import (
    StatePeers,
    open_state_peers,
    shared_space,
)
from tests.daemon.orgfs.runtime_state.support import eventually, text


@pytest.fixture
def state_peers(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[StatePeers]:
    yield from open_state_peers(tmp_path, monkeypatch)


def test_late_log_callback_after_close_does_not_reopen_store(state_peers: StatePeers) -> None:
    owner = state_peers.add("node-owner", "user:owner")
    peer = state_peers.add("node-peer", "user:peer")
    space_id = shared_space(state_peers, owner, peer, name="late callback")
    owner.fs.write_text(space_id, "before.txt", "seen")
    eventually(lambda: _assert_text(peer, space_id, "before.txt", "seen"))
    runtime = peer.runtime
    assert runtime is not None
    host = peer.fs.sync_host(space_id)
    vector = host.read_vectors(("tree",))["tree"]
    owner.fs.write_text(space_id, "late.txt", "delivered after close")
    found = owner.fs.sync_host(space_id).read_update("tree", vector)
    assert found is not None

    peer.close()
    if runtime.stores.snapshot_ids():
        pytest.fail("precondition: close() left space stores registered")
    try:
        outcome = host.apply_remote("tree", found[0], found[1], "node-owner")
    except OrgFsError as error:
        assert error.code == "unavailable"
    else:
        assert not isinstance(outcome, Accepted)

    assert runtime.stores.snapshot_ids() == ()


def _assert_text(peer, space_id: str, ref: str, expected: str) -> None:
    assert text(peer.fs, space_id, ref) == expected
