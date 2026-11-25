# Decisions:
# - Phase D, package T2 (plan §5 "跨节点新建文档不先以空内容出现", marked
#   missing and assigned to T2 as a watch event sequence).  On
#   ``StateOrgFsRuntime`` a document created on another node reaches a
#   watcher as exactly one ``created`` event, and a read made from inside
#   that callback already returns the text: it never shows up empty first.
# - Both arrival orders are driven by hand through the receiver's sync host
#   with broadcasts dropped: tree row before content (the row is known, the
#   read says ``content-pending``, no event yet), and content before tree (the
#   content is ``Pending("unknown-doc")``, so the later pull brings it again).
#   The holder is partitioned while the test looks for "no event yet": a read
#   of pending content asks the holder at once (read-first), which would
#   otherwise deliver the content inside that window.
# - ``stat`` of the remote-born document before any read reports
#   ``arrived`` (design §9.1 §6-3: an unverified document reports
#   ``arrived``); the test does not pin that, it is raised in the T2 report.
"""A document born on another node is seen once, with its content."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.membership.contract import Accepted, Pending
from tests.daemon.orgfs.mesh.replication.state_support import (
    StatePeers,
    open_state_peers,
    shared_space,
)
from tests.daemon.orgfs.runtime_state.support import Peer, eventually, holds


@pytest.fixture
def state_peers(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[StatePeers]:
    yield from open_state_peers(tmp_path, monkeypatch)


class _Seen:
    """Watch events of one peer with the text read inside each callback."""

    def __init__(self, peer: Peer, space_id: str) -> None:
        self.peer = peer
        self.space_id = space_id
        self.rows: list[tuple[str, str, str]] = []
        peer.fs.watch(space_id, "**", self)

    def __call__(self, event) -> None:
        try:
            value = self.peer.fs.read_text(self.space_id, f"id:{event.node.node_id}")[0]
        except OrgFsError as error:
            value = f"<{error.code}>"
        self.rows.append((event.kind, event.node.path, value))

    def of(self, path: str) -> list[tuple[str, str]]:
        return [(kind, value) for kind, seen_path, value in self.rows if seen_path == path]


def _assert_rows(seen: _Seen, path: str, expected: list[tuple[str, str]]) -> None:
    assert seen.of(path) == expected


def test_a_live_remote_birth_is_one_created_event_with_its_text(state_peers: StatePeers) -> None:
    owner = state_peers.add("node-owner", "user:owner")
    peer = state_peers.add("node-peer", "user:peer")
    space_id = shared_space(state_peers, owner, peer)
    seen = _Seen(peer, space_id)

    owner.fs.write_text(space_id, "new.md", "hello")

    eventually(lambda: _assert_rows(seen, "new.md", [("created", "hello")]))
    holds(lambda: _assert_rows(seen, "new.md", [("created", "hello")]), duration=0.3)


def _deliver(owner: Peer, peer: Peer, space_id: str, doc_id: str) -> object:
    receiver = peer.fs.sync_host(space_id)
    since = receiver.read_vectors((doc_id,)).get(doc_id) or b"\x00"
    found = owner.fs.sync_host(space_id).read_update(doc_id, since)
    assert found is not None
    return receiver.apply_remote(doc_id, found[0], found[1], owner.node_id)


def test_a_tree_row_ahead_of_its_content_is_not_an_empty_document(state_peers: StatePeers) -> None:
    state_peers.network.drop_updates = True
    owner = state_peers.add("node-owner", "user:owner")
    peer = state_peers.add("node-peer", "user:peer")
    space_id = shared_space(state_peers, owner, peer)
    seen = _Seen(peer, space_id)
    created = owner.fs.write_text(space_id, "new.md", "hello")
    assert created.doc_id is not None

    assert isinstance(_deliver(owner, peer, space_id, "tree"), Accepted)
    state_peers.network.partition("node-owner")
    with pytest.raises(OrgFsError) as pending:
        peer.fs.read_text(space_id, "new.md")
    assert pending.value.code == "content-pending"
    holds(lambda: _assert_rows(seen, "new.md", []), duration=0.2)

    state_peers.network.heal("node-owner")
    assert isinstance(_deliver(owner, peer, space_id, created.doc_id), (Accepted, Pending))
    eventually(lambda: _assert_rows(seen, "new.md", [("created", "hello")]))


def test_content_ahead_of_its_tree_row_waits_for_the_pull(state_peers: StatePeers) -> None:
    state_peers.network.drop_updates = True
    owner = state_peers.add("node-owner", "user:owner")
    peer = state_peers.add("node-peer", "user:peer")
    space_id = shared_space(state_peers, owner, peer)
    seen = _Seen(peer, space_id)
    created = owner.fs.write_text(space_id, "second.md", "world")
    assert created.doc_id is not None

    assert _deliver(owner, peer, space_id, created.doc_id) == Pending("unknown-doc")
    state_peers.network.partition("node-owner")
    assert isinstance(_deliver(owner, peer, space_id, "tree"), Accepted)
    holds(lambda: _assert_rows(seen, "second.md", []), duration=0.2)
    state_peers.network.heal("node-owner")

    state_peers.pull_all(space_id)
    eventually(lambda: _assert_rows(seen, "second.md", [("created", "world")]))
