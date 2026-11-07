"""The peer side of a reclaim barrier must not revive a stale reference.

The mirror-side exit of ``_supersede_reclaims`` clears a marker when the
peer's file holds an entry for this actor newer than the peer revision
recorded in the barrier.  That record must be the highest peer revision the
store has observed, not only the revision of a currently committed copy: a
peer file that is conflicted (same revision, different bodies) or absent
(unreadable) at the moment of the reclaim is not in ``index.files``, and a
recorded 0 let the stale pre-drop entry (revision 2) clear the marker as
soon as the file came back, undoing the drop (#1585 kept these reclaimed).

The peer side compares the *entry* revision, not the file revision, against
the *peer* revision of the barrier: a peer file that advances for an
unrelated edit keeps the old entry revision, and a stale entry that lies
between the two recorded revisions is still stale.

A repeated reclaim keeps the per-side maximum of the barrier.  On the peer
side that maximum is not observable: the recorded value is
``index.revisions``, which the store only ever raises, so a later reclaim
never records a lower peer revision and dropping the max there is an
equivalent mutation.  The own side is covered in ``test_reclaimed_mirror``.

Real ``LinkService`` / ``LinkStore`` / SQLite ``link_ledger``; only the
orgfs is the in-memory double.
"""

from __future__ import annotations

from pathlib import Path

import hyprial.daemon.impl.links.model as model
from hyprial.daemon.impl.links.model import LinkEntry
from tests.daemon.links.remint.test_reclaimed_mirror import _reclaimed
from tests.daemon.links.remint.test_reclaimed_remint import (
    A,
    REMOTE_PEER,
    _boot,
    _org,
    _state,
)
from tests.support.links.org import FakeOrgFs, write_reference

OTHER = "agent:o:n2:c"


def _own(fs: FakeOrgFs, now: int, revision: int) -> None:
    write_reference(
        fs,
        A,
        {REMOTE_PEER: LinkEntry(peer=REMOTE_PEER, minted_at_ms=now, revision=revision)},
        revision=revision,
    )


def _peer(
    fs: FakeOrgFs,
    now: int,
    *,
    revision: int,
    entry_revision: int,
    other: bool = False,
    space: str | None = None,
) -> None:
    peers = {A: LinkEntry(peer=A, minted_at_ms=now, revision=entry_revision)}
    if other:
        peers[OTHER] = LinkEntry(peer=OTHER, minted_at_ms=now, revision=revision)
    write_reference(fs, REMOTE_PEER, peers, revision=revision, space=space)


def _drop(node) -> None:
    node.service.drop(actor=A, peer=REMOTE_PEER, now_ms=node.now)
    node.tick(2)
    assert REMOTE_PEER in _reclaimed(node, A)


def test_a_peer_file_conflicted_at_the_drop_does_not_revive_the_stale_entry(
    tmp_path: Path,
) -> None:
    fs = _org()
    fs.add_space("s2")
    node = _boot(tmp_path, fs)
    try:
        _own(fs, node.now, 5)
        _peer(fs, node.now, revision=2, entry_revision=2)
        node.tick()
        assert _state(node, A, REMOTE_PEER) == "established"
        _peer(fs, node.now, revision=3, entry_revision=2, other=True, space="s1")
        _peer(fs, node.now, revision=3, entry_revision=2, space="s2")
        node.tick()
        assert REMOTE_PEER in node.service._store.index().conflicted
        _drop(node)

        _peer(fs, node.now, revision=4, entry_revision=2, other=True)
        node.tick(4)

        assert REMOTE_PEER in _reclaimed(node, A)
        assert _state(node, A, REMOTE_PEER) == "reclaimed"
    finally:
        node.close()


def test_a_peer_file_absent_at_the_drop_does_not_revive_the_stale_entry(
    tmp_path: Path,
) -> None:
    fs = _org()
    node = _boot(tmp_path, fs)
    try:
        _own(fs, node.now, 5)
        _peer(fs, node.now, revision=2, entry_revision=2)
        node.tick()
        assert _state(node, A, REMOTE_PEER) == "established"
        fs.remove("s1", model.link_file_path(REMOTE_PEER))
        node.tick()
        assert REMOTE_PEER in node.service._store.index().absent
        _drop(node)

        _peer(fs, node.now, revision=3, entry_revision=2, other=True)
        node.tick(4)

        assert REMOTE_PEER in _reclaimed(node, A)
        assert _state(node, A, REMOTE_PEER) == "reclaimed"
    finally:
        node.close()


def test_a_stale_peer_entry_between_the_recorded_revisions_stays_reclaimed(
    tmp_path: Path,
) -> None:
    fs = _org()
    node = _boot(tmp_path, fs)
    try:
        _own(fs, node.now, 2)
        _peer(fs, node.now, revision=5, entry_revision=4)
        node.tick()
        assert _state(node, A, REMOTE_PEER) == "established"
        _drop(node)
        node.tick(3)

        assert REMOTE_PEER in _reclaimed(node, A)
        assert _state(node, A, REMOTE_PEER) == "reclaimed"
    finally:
        node.close()


def test_an_unrelated_peer_edit_keeps_the_stale_entry_reclaimed(
    tmp_path: Path,
) -> None:
    fs = _org()
    node = _boot(tmp_path, fs)
    try:
        _own(fs, node.now, 5)
        _peer(fs, node.now, revision=2, entry_revision=2)
        node.tick()
        assert _state(node, A, REMOTE_PEER) == "established"
        _drop(node)

        _peer(fs, node.now, revision=3, entry_revision=2, other=True)
        node.tick(4)

        assert REMOTE_PEER in _reclaimed(node, A)
        assert _state(node, A, REMOTE_PEER) == "reclaimed"
    finally:
        node.close()
