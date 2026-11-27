# Decisions:
# - Phase D, package T2: the two seam tests run on ``OrgFsRuntime``.
#   Delivery stays hand-driven: the in-process network drops every broadcast
#   and the test moves state only with explicit pull rounds
#   (``StatePeers.pull_all``), so the order of arrival is fixed and no
#   journal envelope is read or imported.  The property is unchanged: the
#   creator, the editor and a third writer all read the merged text, and the
#   creator's ``await_content`` succeeds.
# - Package S deleted ``test_committed_update_refuses_a_retired_document``
#   (journal-only, ``snapshot-barrier``).
"""D4 x D1 seam: the creating node can read after a peer edits its text.

Regression for docs/notes/orgfs-fix-round/d4-d1-seam.md, actor-line adaptation
(brief: docs/notes/orgfs-fix-round/d4-d1-seam-actor-brief.md).  Delivery is
hand-driven (no transport, no last-writer-wins race), so the failure is
deterministic: the peer's edit reaches the creator's live facade
incrementally, exactly as an inbound envelope does, which strands the
creator's own coverage-clock op without the absorb step.

On this tree the store sits behind OrgSpaceAuthority; the absorb delta
comes back inside the typed authority commit result and the facade folds
it into its content doc.  The kill-mutation disables
LocalOrgFs._absorb_committed_deltas (or OrgSpaceAuthority._absorb) and
both seam tests go red with content-pending.
"""

from __future__ import annotations

from collections.abc import Iterator
import time

import pytest

from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from tests.daemon.orgfs.mesh.replication.state_support import (
    StatePeers,
    open_state_peers,
    shared_space,
)
from tests.daemon.orgfs.runtime_state.support import Peer, text


@pytest.fixture
def state_peers(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[StatePeers]:
    yield from open_state_peers(tmp_path, monkeypatch)


def _runtime(tmp_path, name, author):
    return OrgFsRuntime(tmp_path / name, node_id=name, author=author)


def _hand_driven(state_peers: StatePeers, *names: str) -> tuple[list[Peer], str]:
    state_peers.network.drop_updates = True
    nodes = [state_peers.add(f"node-{name}", f"user:{name}") for name in names]
    return nodes, shared_space(state_peers, nodes[0], *nodes[1:], name="seam")


def _deliver(state_peers: StatePeers, space_id: str) -> None:
    for _ in range(3):
        state_peers.pull_all(space_id)


def test_creator_reads_after_peer_edit_two_writers(state_peers: StatePeers) -> None:
    (b, c), sid = _hand_driven(state_peers, "b", "c")
    created = b.fs.write_text(sid, "shared.txt", "from B 1")
    ref = f"id:{created.node_id}"
    _deliver(state_peers, sid)
    assert text(c.fs, sid, ref) == "from B 1"

    c.fs.write_text(sid, ref, "from B 1\nline-C")
    expected = "from B 1\nline-C"
    assert text(b.fs, sid, ref) == "from B 1"
    _deliver(state_peers, sid)

    assert text(b.fs, sid, ref) == expected
    assert b.runtime is not None
    assert b.runtime.await_content(sid, created.node_id, deadline_monotonic=time.monotonic() + 5.0)
    assert text(b.fs, sid, ref) == expected
    assert text(c.fs, sid, ref) == expected


def test_creator_and_editor_read_after_third_writer(state_peers: StatePeers) -> None:
    (b, c, d), sid = _hand_driven(state_peers, "b", "c", "d")
    created = b.fs.write_text(sid, "shared.txt", "from B 1")
    ref = f"id:{created.node_id}"
    _deliver(state_peers, sid)

    c.fs.write_text(sid, ref, "from B 1\nline-C")
    _deliver(state_peers, sid)
    d.fs.write_text(sid, ref, text(d.fs, sid, ref) + "\nline-D")
    _deliver(state_peers, sid)

    texts = {peer.node_id: text(peer.fs, sid, ref) for peer in (b, c, d)}
    assert len(set(texts.values())) == 1, texts
    only = next(iter(texts.values()))
    assert only.startswith("from B 1")
    assert "line-C" in only and "line-D" in only
