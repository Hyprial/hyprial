# Decisions:
# - Phase D1: coordinator tests drive ``LocalOrgFs`` only through its public
#   facade, its ``SyncHost`` (``sync_host``) and the in-memory store backings.
#   A second node is made by copying the first node's backing (a clone) and
#   ``load_space``; join is D2.
# - ``Cluster`` is an in-process direct sync in the spirit of C3's test host
#   (tests/daemon/orgfs/sync/support/host.py): publications are queued and
#   delivered only when a test calls ``deliver`` (so a test can drop them),
#   and ``pull`` follows design §4.3 (meta, tree, then the requester's
#   pending content, each answered only when the supplier is ahead).
"""In-process helpers for the commit coordinator tests."""

from __future__ import annotations

import copy
import hashlib
import threading
import time
from typing import Any, Callable

from hyprial.daemon.impl.orgfs.document.coordinator import MemoryStores, LocalOrgFs
from hyprial.daemon.impl.orgfs.membership import Accepted, decode_vector, vector_covers
from hyprial.daemon.impl.orgfs.state import MemoryBacking
from hyprial.daemon.impl.orgfs.sync import PeerObservation

OWNER = "user:owner"
EMPTY = b"\x00"


class MemoryBlobs:
    """Content-addressed blobs that remember releases."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.released: list[tuple[str, str]] = []

    def put(self, space_id: str, data: bytes, *, reason: str) -> str:
        digest = hashlib.sha256(data).hexdigest()
        self.values[digest] = bytes(data)
        return digest

    def get(self, space_id: str, digest: str) -> bytes:
        return self.values[digest]

    def has(self, digest: str) -> bool:
        return digest in self.values

    def release(self, space_id: str, digest: str) -> None:
        self.released.append((space_id, digest))


class RecordingSync:
    """A ``SpaceSync`` stand-in that records publications."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, bytes]] = []
        self.requested: list[str] = []
        self.observed: tuple[PeerObservation, ...] = ()
        self.lock = threading.Lock()

    def publish(self, doc_id: str, update: bytes, vector: bytes) -> None:
        with self.lock:
            self.published.append((doc_id, update, vector))

    def observations(self) -> tuple[PeerObservation, ...]:
        return self.observed

    def request_content(self, doc_id: str) -> None:
        self.requested.append(doc_id)


class Node:
    """One ``LocalOrgFs`` with its own store backings."""

    def __init__(self, author: str, node_id: str, **options: Any) -> None:
        self.author = author
        self.node_id = node_id
        self.stores = MemoryStores()
        self.blobs = MemoryBlobs()
        self.fs = LocalOrgFs(
            author=author,
            node_id=node_id,
            blobs=self.blobs,
            store_factory=self.stores,
            store_discard=self.stores.discard,
            **options,
        )

    def backing(self, space_id: str) -> MemoryBacking:
        return self.stores.backings[space_id]

    def close(self) -> None:
        self.fs.close_effects(timeout=5.0)


def copy_backing(source: MemoryBacking) -> MemoryBacking:
    clone = MemoryBacking(source.space_id)
    clone.documents = copy.deepcopy(source.documents)
    return clone


def clone_node(origin: Node, space_id: str, author: str, node_id: str, **options: Any) -> Node:
    """A second node holding ``origin``'s durable state of ``space_id``."""

    node = Node(author, node_id, **options)
    node.stores.backings[space_id] = copy_backing(origin.backing(space_id))
    node.fs.load_space(space_id)
    return node


class _ClusterSync:
    def __init__(self, cluster: "Cluster", node: Node) -> None:
        self._cluster = cluster
        self._node = node

    def publish(self, doc_id: str, update: bytes, vector: bytes) -> None:
        with self._cluster.lock:
            self._cluster.outbox.append((self._node.node_id, doc_id, update, vector))

    def observations(self) -> tuple[PeerObservation, ...]:
        return ()

    def request_content(self, doc_id: str) -> None:
        return None


class Cluster:
    """Nodes of one space joined by an in-process direct sync."""

    def __init__(self, space_id: str, nodes: list[Node]) -> None:
        self.space_id = space_id
        self.nodes = {node.node_id: node for node in nodes}
        self.lock = threading.Lock()
        self.outbox: list[tuple[str, str, bytes, bytes]] = []
        self.dropping: set[str] = set()
        for node in nodes:
            node.fs.attach_sync(space_id, _ClusterSync(self, node))

    def host(self, node: Node) -> Any:
        return node.fs.sync_host(self.space_id)

    def drain(self) -> None:
        for node in self.nodes.values():
            assert node.fs.drain_broadcasts((self.space_id,), timeout=5.0)

    def take(self) -> list[tuple[str, str, bytes, bytes]]:
        self.drain()
        with self.lock:
            frames, self.outbox = self.outbox, []
        return frames

    def deliver(self, *, rounds: int = 20) -> int:
        """Deliver queued publications (and the ones they cause) to every peer."""

        delivered = 0
        for _ in range(rounds):
            frames = self.take()
            if not frames:
                return delivered
            for sender, doc_id, update, vector in frames:
                if sender in self.dropping:
                    continue
                for node in self.nodes.values():
                    if node.node_id == sender or node.node_id in self.dropping:
                        continue
                    self.host(node).apply_remote(doc_id, update, vector, sender)
                    delivered += 1
        return delivered

    def drop_all(self) -> None:
        self.take()

    def pull(self, requester: Node, supplier: Node) -> int:
        """One design §4.3 pull; returns how many updates changed the requester."""

        mine = self.host(requester)
        theirs = self.host(supplier)
        ids = ("meta", "tree", *mine.pending_content(128))
        local = mine.read_vectors(ids)
        applied = 0
        for doc_id in ids:
            found = theirs.read_update(doc_id, local[doc_id])
            if found is None:
                continue
            update, vector = found
            if vector_covers(local[doc_id], vector):
                continue
            outcome = mine.apply_remote(doc_id, update, vector, supplier.node_id)
            if isinstance(outcome, Accepted):
                applied += 1
        return applied

    def settle(self, *, rounds: int = 20) -> int:
        """All-pairs pulls (dropping publications) until a round changes nothing."""

        for count in range(1, rounds + 1):
            changed = 0
            for requester in self.nodes.values():
                for supplier in self.nodes.values():
                    if requester is not supplier:
                        changed += self.pull(requester, supplier)
            self.drop_all()
            if not changed:
                return count
        raise AssertionError("cluster did not settle")


def eventually(check: Callable[[], Any], *, timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return check()
        except AssertionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def vector(fs: LocalOrgFs, space_id: str, doc_id: str) -> dict[int, int]:
    return decode_vector(fs.sync_host(space_id).read_vectors((doc_id,))[doc_id])


def doc_id_of(fs: LocalOrgFs, space_id: str, ref: str) -> str:
    doc_id = fs.stat(space_id, ref).doc_id
    assert doc_id is not None
    return doc_id
