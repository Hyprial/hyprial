# Decisions:
# - Phase D, package T2: rewritten onto ``OrgFsRuntime``.  The join no
#   longer runs through the space's mesh (design §9 R5: a temporary space and
#   a temporary sync), so the interruption is placed on the reader's
#   ``apply_remote`` (the seam every pulled update crosses), as D2's
#   ``runtime_state/test_join.py`` does, and the holder disappears either from
#   the reader's presence (``supplier-offline``) or because the reader's
#   transport starts a rebuild (``callbacks-incomplete``).
# - Completeness is asserted through the facade (spaces, meta, members, tree,
#   every text) instead of comparing journal frontiers; "not joined" also
#   means no space directory is left on disk.
# - A join pulls only the catalog (``meta`` then ``tree``) before it
#   confirms; content follows lazily.  The legacy cuts "after the first and
#   the third applied commit" become "after ``meta``" and "after ``tree``",
#   and the joined texts are read with ``eventually``.
# - The plain interrupted-join case without the org service overlaps D2's
#   ``test_a_holder_lost_in_the_middle_of_the_join_leaves_no_directory``; it is
#   kept here because it adds the rebuild cause and the later-page cut.
"""A join must not report a partially pulled space as joined."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from hyprial.daemon.impl.org.network.directory import InvalidDeviceQuarantine
from hyprial.daemon.impl.org.network.service import OrgNetworkService
from hyprial.daemon.impl.orgfs.api import OrgFsError
from tests.daemon.orgfs.mesh.replication.state_support import StatePeers, open_state_peers
from tests.daemon.orgfs.runtime_state.support import Peer, eventually, text, tree


@pytest.fixture
def state_peers(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[StatePeers]:
    yield from open_state_peers(tmp_path, monkeypatch)


class _Interrupted:
    """Owner, reader and a reader whose holder vanishes after ``after`` pulled updates."""

    def __init__(self, peers: StatePeers, *, after: int, cause: str) -> None:
        self.peers = peers
        self.network = peers.network
        self.owner = peers.add("node-owner", "user:owner")
        self.reader = peers.add("node-reader", "user:reader")
        self.cause = cause
        self.after = after
        self.calls = 0
        self.after_cut = 0
        self.cut = False
        space = self.owner.fs.create_space("join interrupted within a page")
        self.owner.fs.invite(space.space_id, "user:reader", "rw")
        for index in range(3):
            self.owner.fs.write_text(space.space_id, f"file-{index}.txt", f"body-{index}")
        self.space = space
        self.original = self.reader.fs.apply_remote
        self.reader.fs.apply_remote = self._apply

    def _apply(self, space_id, doc_id, update, vector, peer):
        if self.cut:
            self.after_cut += 1
        outcome = self.original(space_id, doc_id, update, vector, peer)
        self.calls += 1
        if self.calls == self.after and not self.cut:
            if self.cause == "callbacks-incomplete":
                self.network.begin_rebuild("node-reader")
            else:
                self.network.hide("node-reader", "node-owner")
            self.cut = True
        return outcome

    def recover(self) -> None:
        if self.cause == "callbacks-incomplete":
            self.network.finish_rebuild("node-reader")
        else:
            self.network.show("node-reader", "node-owner", edge=False)


def _assert_not_joined(reader: Peer, space_id: str) -> None:
    assert reader.fs.spaces() == ()
    with pytest.raises(OrgFsError) as error:
        reader.fs.stat(space_id, "id:root")
    assert error.value.code == "unknown-space"
    assert not (reader.spaces_root / space_id).exists()


def _assert_complete(owner: Peer, reader: Peer, space) -> None:
    space_id = space.space_id
    assert reader.fs.spaces() == (space,)
    assert reader.fs.space_meta(space_id) == owner.fs.space_meta(space_id)
    assert reader.fs.members(space_id) == owner.fs.members(space_id)
    assert {member.user: member.mode for member in reader.fs.members(space_id)}["user:reader"] == "rw"
    assert tree(reader.fs, space_id) == tree(owner.fs, space_id)
    for index in range(3):
        eventually(lambda index=index: _assert_text(reader, space_id, f"file-{index}.txt", f"body-{index}"))


def _assert_text(reader: Peer, space_id: str, ref: str, expected: str) -> None:
    assert text(reader.fs, space_id, ref) == expected


@pytest.mark.parametrize("interrupt_after", [1, 2])
@pytest.mark.parametrize("cause", ["supplier-offline", "callbacks-incomplete"])
def test_sync_fails_immediately_when_supplier_disappears_mid_page(
    state_peers: StatePeers, interrupt_after: int, cause: str
) -> None:
    pair = _Interrupted(state_peers, after=interrupt_after, cause=cause)

    with pytest.raises(OrgFsError) as error:
        pair.reader.fs.join(pair.space.space_id)

    assert error.value.code == "not-a-member"
    assert error.value.details["reason"] == "supplier-not-online"
    assert pair.cut
    _assert_not_joined(pair.reader, pair.space.space_id)

    pair.recover()
    assert pair.reader.fs.join(pair.space.space_id) == pair.space
    _assert_complete(pair.owner, pair.reader, pair.space)


@pytest.mark.parametrize("interrupt_after", [1, 2])
@pytest.mark.parametrize("cause", ["supplier-offline", "callbacks-incomplete"])
def test_org_join_retries_mid_page_presence_loss_without_half_joined_state(
    state_peers: StatePeers, tmp_path, interrupt_after: int, cause: str
) -> None:
    pair = _Interrupted(state_peers, after=interrupt_after, cause=cause)
    retries: list[float] = []

    def presence_recovers(seconds: float) -> None:
        retries.append(seconds)
        assert pair.cut
        _assert_not_joined(pair.reader, pair.space.space_id)
        pair.recover()

    service = OrgNetworkService(
        fs=pair.reader.fs, home=tmp_path / "reader-home", node_id="node-reader",
        owner="reader", invalid_devices=InvalidDeviceQuarantine(),
        sleep=presence_recovers, join_timeout_seconds=1.0,
    )
    service._join_with_retry(pair.space.space_id)  # noqa: SLF001 - the org join's retry loop

    assert len(retries) == 1
    _assert_complete(pair.owner, pair.reader, pair.space)
    assert pair.reader.fs.join(pair.space.space_id) == pair.space
    _assert_complete(pair.owner, pair.reader, pair.space)
