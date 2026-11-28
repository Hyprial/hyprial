"""Wire MemoryHost + fake session + StateSpaceSync into test nodes."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
import time

from hyprial.daemon.impl.orgfs.network.supplier_health import SupplierSyncHealth
from hyprial.daemon.impl.orgfs.storage.blobs import BlobStore
from hyprial.daemon.impl.orgfs.sync.space import StateSpaceSync
from hyprial.daemon.impl.transport import KeySpace

from tests.daemon.orgfs.sync.support.bus import Bus, Session
from tests.daemon.orgfs.sync.support.host import MemoryHost

SPACE = "space-1"
OPEN_NODES: list["Node"] = []


@dataclass
class Node:
    name: str
    host: MemoryHost
    session: Session
    sync: StateSpaceSync
    blobs: BlobStore | None
    online: set[str]
    events: list[tuple[str, str, dict]] = field(default_factory=list)

    def close(self) -> None:
        try:
            self.sync.close()
        finally:
            if self.blobs is not None:
                self.blobs.close()

    def restart(self, bus: Bus) -> "Node":
        """Close sync, reopen the host from its exported state, new client id."""

        self.sync.close()
        self.host.closed = True
        host = MemoryHost(self.name, state=self.host.export())
        return make_node(bus, self.name, host=host, blobs=self.blobs, online=self.online)


def make_node(
    bus: Bus,
    name: str,
    *,
    host: MemoryHost | None = None,
    blobs: BlobStore | None = None,
    online: set[str] | None = None,
    health: SupplierSyncHealth | None = None,
    presence: Callable[[str], bool] | None = None,
    holders: set[str] | Callable[[], Iterable[str]] | None = None,
    announcement_source: Callable[[], Iterable[dict]] | None = None,
) -> Node:
    host = host or MemoryHost(name)
    session = bus.session(name)
    online = online if online is not None else {f"node-{x}" for x in "abcd"}
    events: list[tuple[str, str, dict]] = []
    sync = StateSpaceSync(
        session,
        KeySpace(),
        SPACE,
        name,
        "user:" + name.removeprefix("node-"),
        host,
        blobs,
        health or SupplierSyncHealth(),
        presence or (lambda peer: peer in online),
        lambda level, event, **fields: events.append((level, event, fields)),
        announcement_source=announcement_source,
        holder_discovery=holders,
    )
    host.sync = sync
    node = Node(name, host, session, sync, blobs, online, events)
    OPEN_NODES.append(node)
    return node


def converge(nodes: Iterable[Node], *, rounds: int = 50) -> int:
    """Pull every node from every other until a full pass applies nothing."""

    nodes = list(nodes)
    for count in range(1, rounds + 1):
        applied = 0
        for node in nodes:
            for other in nodes:
                if other is not node:
                    applied += node.sync.sync_from(other.name)
        settle(*nodes)
        if applied == 0:
            return count
    raise AssertionError("nodes did not converge")


def settle(*nodes: Node, timeout: float = 10.0) -> None:
    """Wait until every node's ingress and worker queues are idle."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(node.sync.idle() for node in nodes):
            time.sleep(0.02)
            if all(node.sync.idle() for node in nodes):
                return
        time.sleep(0.005)
    raise AssertionError("sync queues did not settle")


def same_state(*nodes: Node) -> bool:
    first = nodes[0].host
    return all(
        node.host.vectors() == first.vectors() and node.host.texts() == first.texts()
        and node.host.meta() == first.meta()
        for node in nodes[1:]
    )


def wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()
