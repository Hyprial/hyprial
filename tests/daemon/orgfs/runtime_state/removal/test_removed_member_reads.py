# Decisions (package F7, ruling 5, 2026-11-25):
# - A removed member's node holds meta only and no member serves it
#   content, so reading content it does not hold fails at once with
#   ``not-a-member`` instead of waiting and ending in ``content-pending``
#   with holder suggestions.  This covers ``await_content`` (blob and text)
#   and the facade reads that ``orgfs.read`` and ``orgfs.export`` call.
#   Content already held locally stays readable; nothing is taken back.
# - The member misses the text content because the writer's content
#   broadcast is dropped (tree and meta still go out); the blob is fetched
#   lazily, so its row arrives without its bytes.
"""A removed member reading content it does not hold gets ``not-a-member``."""

from __future__ import annotations

import json
import time

import pytest

from hyprial.daemon.impl.orgfs import OrgFsError

from ..convergence.team import members, settle, team
from ..support import Peer, Peers, eventually, text

BINARY = bytes(range(256)) * 8


def _check(condition: bool) -> None:
    assert condition


def _drop_content_broadcasts(peer: Peer) -> None:
    assert peer.runtime is not None
    session = peer.runtime._session  # noqa: SLF001 - the node's transport
    original = session.put

    def put(key: str, payload: bytes) -> None:
        if "/org/fs3/update/" in key and json.loads(payload).get("docId") not in ("meta", "tree"):
            return
        original(key, payload)

    session.put = put


def test_a_removed_member_reading_content_it_lacks_gets_not_a_member(peers: Peers) -> None:
    alice, bob, carol, space_id = team(peers)
    group = (alice, bob, carol)
    carol.fs.write_bytes(space_id, "c.bin", BINARY)
    _drop_content_broadcasts(alice)
    alice.fs.write_text(space_id, "t.md", "content bob never received")
    eventually(lambda: _check("c.bin" in {n.path for n in bob.fs.listdir(space_id, "", recursive=True)}))
    eventually(lambda: _check("t.md" in {n.path for n in bob.fs.listdir(space_id, "", recursive=True)}))
    settle(group, space_id)
    blob_node = bob.fs.stat(space_id, "c.bin").node_id
    text_node = bob.fs.stat(space_id, "t.md").node_id
    assert bob.fs.stat(space_id, "c.bin").content_state != "arrived"

    alice.fs.remove_member(space_id, bob.author)
    eventually(lambda: _check(bob.author not in members(bob, space_id)))
    assert bob.runtime is not None

    started = time.monotonic()
    for node_id in (blob_node, text_node):
        with pytest.raises(OrgFsError) as raised:
            bob.runtime.await_content(space_id, node_id, deadline_monotonic=time.monotonic() + 5.0)
        assert raised.value.code == "not-a-member"
    for read in (lambda: bob.fs.read_bytes(space_id, "c.bin"),
                 lambda: bob.fs.read_text(space_id, "t.md"),
                 lambda: bob.fs.read_bytes(space_id, "t.md")):
        with pytest.raises(OrgFsError) as raised:
            read()
        assert raised.value.code == "not-a-member"
        assert "suggestedHolders" not in raised.value.details
    assert time.monotonic() - started < 2.0

    assert text(bob.fs, space_id, "node-alice.md") == "from node-alice"
    assert bob.runtime.await_content(
        space_id, bob.fs.stat(space_id, "node-alice.md").node_id,
        deadline_monotonic=time.monotonic() + 5.0,
    )
