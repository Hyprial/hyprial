# Decisions (phase F4, item 5, real-node finding R2-10, 2026-11-25):
# - A binary file born on another node reaches a watcher as one ``created``
#   event whose node reports ``content_state == "pending"``: blobs are fetched
#   on demand (design §4.2, the runtime's ``await_content`` / ``fetch_blob``),
#   so the tree row is ahead of the bytes.  The legacy runtime emitted
#   nothing more either.  To tell a subscriber when the bytes are readable,
#   the state runtime reports a stored fetch to the facade, which emits one
#   ``modified`` event (an existing kind; no new public value) for every live
#   node of that space naming the digest, once per node and digest.
# - The watcher reads inside the callback: on the ``modified`` event the
#   bytes are there.  A second provisioning of the same blob emits nothing.
"""A remote blob's arrival is one ``modified`` event."""

from __future__ import annotations

import time

from hyprial.daemon.impl.orgfs.api import OrgFsError

from ..support import Peer, Peers, eventually, holds

OWNER = "user:owner@example"
MEMBER = "user:member@example"
PAYLOAD = b"\x00binary\xff" * 512


class _Seen:
    def __init__(self, peer: Peer, space_id: str) -> None:
        self.peer = peer
        self.space_id = space_id
        self.rows: list[tuple[str, str, str]] = []
        peer.fs.watch(space_id, "**", self)

    def __call__(self, event) -> None:
        try:
            value = "bytes" if self.peer.fs.read_bytes(self.space_id, f"id:{event.node.node_id}") == PAYLOAD else "other"
        except OrgFsError as error:
            value = f"<{error.code}>"
        self.rows.append((event.kind, event.node.content_state, value))


def _equal(actual: object, expected: object) -> None:
    assert actual == expected


def test_a_remote_blob_reports_its_arrival_once(peers: Peers) -> None:
    owner = peers.add("node-owner", OWNER)
    member = peers.add("node-member", MEMBER)
    space_id = owner.fs.create_space("team").space_id
    owner.fs.invite(space_id, MEMBER, "rw")
    member.fs.join(space_id)
    seen = _Seen(member, space_id)

    written = owner.fs.write_bytes(space_id, "image.bin", PAYLOAD)
    eventually(lambda: _equal(seen.rows, [("created", "pending", "<content-pending>")]))
    assert member.runtime is not None

    assert member.runtime.await_content(space_id, written.node_id, deadline_monotonic=time.monotonic() + 5)

    expected = [("created", "pending", "<content-pending>"), ("modified", "arrived", "bytes")]
    eventually(lambda: _equal(seen.rows, expected))
    assert member.runtime.await_content(space_id, written.node_id, deadline_monotonic=time.monotonic() + 5)
    member.runtime.fetch_blob(space_id, str(written.blob_hash))
    holds(lambda: _equal(seen.rows, expected), duration=0.3)
