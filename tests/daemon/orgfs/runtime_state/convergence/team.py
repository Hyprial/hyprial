# Decisions (package F7, real-node finding R4-3, 2026-11-25):
# - Three members on the in-process network with device owners from a fixed
#   ``node -> author`` map and trust off, so the node gate is the production
#   gate (as in ``removal/test_removed_member_connected.py``).
# - Each member writes once while all are connected and every ordered pair
#   pulls once, the steady state of a running space: every node has received
#   broadcasts from and holds an observation of every other.
# - Due anti-entropy ticks are driven by calling ``_maintain_spaces(now)`` on
#   every node with ``now`` advancing by more than the longest catalog
#   backoff, so a test counts ticks, not seconds.
# - ``Traffic`` counts pull queries and broadcast or announce puts each node
#   sends, wrapping the node's transport session.
"""Shared three-member setup and probes for the convergence tests."""

from __future__ import annotations

from collections import Counter
import time
from typing import Any

from hyprial.daemon.impl.orgfs import OrgFsError
from hyprial.daemon.impl.orgfs.composition.state import OrgFsRuntime
from hyprial.daemon.impl.orgfs.composition.state.sync import CATALOG_RETRY_MAX_SECONDS

from ..support import Peer, Peers, eventually

OWNERS = {
    "node-alice": "user:alice@example",
    "node-bob": "user:bob@example",
    "node-carol": "user:carol@example",
}
TICK_SECONDS = CATALOG_RETRY_MAX_SECONDS + 1.0


class FixedOwnerRuntime(OrgFsRuntime):
    def _device_owner_resolver(self, space_id: str) -> Any:
        return OWNERS.get


def add(peers: Peers, name: str) -> Peer:
    return peers.add(name, OWNERS[name], trust=False, runtime_class=FixedOwnerRuntime)


def team(peers: Peers) -> tuple[Peer, Peer, Peer, str]:
    alice, bob, carol = (add(peers, name) for name in OWNERS)
    space_id = alice.fs.create_space("team").space_id
    alice.fs.invite(space_id, bob.author, "rw")
    alice.fs.invite(space_id, carol.author, "rw")
    bob.fs.join(space_id)
    carol.fs.join(space_id)
    for peer in (alice, bob, carol):
        peer.fs.write_text(space_id, f"{peer.node_id}.md", f"from {peer.node_id}")
    group = (alice, bob, carol)
    eventually(lambda: _same(group, space_id))
    peers.pull_all(space_id)
    settle(group, space_id)
    return alice, bob, carol, space_id


def contents(peer: Peer, space_id: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for node in peer.fs.listdir(space_id, "", recursive=True):
        if node.kind == "doc":
            try:
                found[node.path] = peer.fs.read_text(space_id, f"id:{node.node_id}")[0]
            except OrgFsError as error:
                found[node.path] = f"<{error.code}>"
    return found


def members(peer: Peer, space_id: str) -> set[str]:
    return {row.user for row in peer.fs.members(space_id)}


def settle(group: tuple[Peer, ...], space_id: str, *, timeout: float = 10.0) -> None:
    def idle() -> None:
        assert all(peer.sync(space_id).idle() for peer in group)
        time.sleep(0.05)
        assert all(peer.sync(space_id).idle() for peer in group)

    eventually(idle, timeout=timeout)


def tick(group: tuple[Peer, ...], space_id: str, now: float) -> None:
    for peer in group:
        assert peer.runtime is not None
        peer.runtime._maintain_spaces(now)  # noqa: SLF001 - one due anti-entropy tick
    settle(group, space_id)


def synced(peer: Peer, space_id: str) -> bool | None:
    assert peer.runtime is not None
    return peer.runtime.status(space_id).synced


def _same(group: tuple[Peer, ...], space_id: str) -> None:
    first = contents(group[0], space_id)
    assert len(first) == len(group)
    assert all(contents(peer, space_id) == first for peer in group[1:])


class Traffic:
    """Pull queries and broadcast or announce puts sent by each node."""

    def __init__(self, group: tuple[Peer, ...]) -> None:
        self.counts: Counter[tuple[str, str]] = Counter()
        for peer in group:
            assert peer.runtime is not None
            self._wrap(peer.node_id, peer.runtime._session)  # noqa: SLF001 - the node's transport

    def _wrap(self, node: str, session: Any) -> None:
        query, put = session.query, session.put

        def counted_query(key_expr: str, payload: bytes, **kwargs: Any) -> Any:
            if "/org/fs3/sync/" in key_expr:
                self.counts[(node, "pull")] += 1
            return query(key_expr, payload, **kwargs)

        def counted_put(key: str, payload: bytes) -> None:
            if "/org/fs3/update/" in key:
                self.counts[(node, "broadcast")] += 1
            elif "/org/fs/announce/" in key:
                self.counts[(node, "announce")] += 1
            put(key, payload)

        session.query, session.put = counted_query, counted_put

    def snapshot(self) -> dict[tuple[str, str], int]:
        return dict(self.counts)


__all__ = [
    "OWNERS",
    "TICK_SECONDS",
    "FixedOwnerRuntime",
    "Traffic",
    "add",
    "contents",
    "members",
    "settle",
    "team",
    "tick",
    "synced",
]
