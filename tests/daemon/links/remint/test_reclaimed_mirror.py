"""The peer's reclaim marker must yield to the actor's fresh mint (2026-10-19).

Production: after ``emergent`` dropped ``be-dev-1`` / ``be-dev-3`` its own
marker cleared once both withdrawals were visible, but each peer kept
``reclaimed[emergent]``: the peer's barrier waited for emergent's file to
advance *without* the peer, and the next advance was emergent re-minting
the peer.  Every send from emergent stayed ``pending`` / ``LINK_MINTING``
until the peer sent first or the daemon restarted.

The marker must also end when the peer's file holds an entry for the actor
whose revision is strictly newer than the peer file revision recorded at the
reclaim.  A repeated reclaim of the same peer must not overwrite the recorded
withdrawal with 0, and a copy whose revision equals the recorded one is the
stale copy and keeps the marker.

Every case drives the real ``LinkService`` over the real ``LinkStore``, the
real inbox custody facade and its SQLite ``link_ledger`` (window switch
``observe``, the production default); only the orgfs is the in-memory
double.
"""

from __future__ import annotations

from pathlib import Path

from hyprial.daemon.impl.links.model import LinkEntry
from hyprial.daemon.impl.links.ports import Allow, Mint
from tests.daemon.links.remint.test_reclaimed_remint import (
    A,
    LOCAL_PEER,
    REMOTE_PEER,
    _boot,
    _held_ids,
    _ledger_state,
    _org,
    _send,
    _state,
)
from tests.support.links.org import establish, write_reference


def _reclaimed(node, actor: str) -> set[str]:
    with node.service._mem:
        return set(node.service._state(actor).reclaimed)


def test_re_send_after_own_drop_re_establishes_without_peer_first(
    tmp_path: Path,
) -> None:
    fs = _org()
    node = _boot(tmp_path, fs)
    try:
        establish(fs, A, LOCAL_PEER, now_ms=node.now)
        node.tick()
        assert _state(node, A, LOCAL_PEER) == "established"
        assert _state(node, LOCAL_PEER, A) == "established"
        node.service.drop(actor=A, peer=LOCAL_PEER, now_ms=node.now)
        node.tick(4)
        assert LOCAL_PEER not in _reclaimed(node, A)
        assert A in _reclaimed(node, LOCAL_PEER)

        assert isinstance(_send(node, "m-mirror", LOCAL_PEER), Mint)
        node.tick(4)

        assert _state(node, LOCAL_PEER, A) == "established"
        assert _state(node, A, LOCAL_PEER) == "established"
        assert _held_ids(node) == []
        assert _ledger_state(node.database, "m-mirror") == "open"
    finally:
        node.close()


def test_remote_peer_re_mint_after_its_withdrawal_is_accepted(
    tmp_path: Path,
) -> None:
    fs = _org()
    node = _boot(tmp_path, fs)
    try:
        establish(fs, A, REMOTE_PEER, now_ms=node.now)
        node.tick()
        assert _state(node, A, REMOTE_PEER) == "established"
        write_reference(fs, REMOTE_PEER, {}, revision=2)
        node.tick(4)
        assert REMOTE_PEER in _reclaimed(node, A)
        assert _state(node, A, REMOTE_PEER) is None

        write_reference(
            fs,
            REMOTE_PEER,
            {A: LinkEntry(peer=A, minted_at_ms=node.now, revision=3)},
            revision=3,
        )
        node.tick(4)

        assert _state(node, A, REMOTE_PEER) == "established"
        assert isinstance(_send(node, "m-after-peer-remint", REMOTE_PEER), Allow)
        assert _held_ids(node) == []
    finally:
        node.close()


def test_a_repeated_reclaim_keeps_the_recorded_withdrawal(
    tmp_path: Path,
) -> None:
    fs = _org()
    node = _boot(tmp_path, fs)
    try:
        write_reference(
            fs,
            A,
            {
                REMOTE_PEER: LinkEntry(
                    peer=REMOTE_PEER, minted_at_ms=node.now, revision=5
                )
            },
            revision=5,
        )
        write_reference(
            fs,
            REMOTE_PEER,
            {A: LinkEntry(peer=A, minted_at_ms=node.now, revision=2)},
            revision=2,
        )
        node.tick()
        assert _state(node, A, REMOTE_PEER) == "established"
        node.service._reclaim(A, REMOTE_PEER, "evicted", node.now)
        node.service._reclaim(A, REMOTE_PEER, "dropped", node.now)
        with node.service._mem:
            node.service._state(A).pending_file = None
        assert _state(node, A, REMOTE_PEER) == "reclaimed"
        node.tick()
        assert _state(node, A, REMOTE_PEER) == "reclaimed"
        with node.service._mem:
            assert node.service._state(A).barrier[REMOTE_PEER] == (5, 2)
    finally:
        node.close()
