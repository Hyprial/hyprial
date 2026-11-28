# Decisions:
# - Phase D2: runtime-level tests build ``OrgFsRuntime`` nodes on the
#   contract suite's in-process ``FakeNetwork`` (presence ``online_for``,
#   online edges ``observe_for``), the same seam ``StateImpl.connect`` uses.
# - ``Peers`` keeps every node of one test, closes them in reverse order and
#   can close and reopen one node on the same network.
# - Nodes trust the tailnet unless a test asks otherwise; the node gate test
#   builds a real org directory space with device rows and then turns trust
#   off (the runtime reads the flag at every gate call, as the mesh did).
# - Phase F1: ``Peer``/``Peers.add`` take an optional ``runtime_class`` (default
#   ``OrgFsRuntime``) so one test can run the legacy ``OrgFsRuntime`` on
#   the same network seam.
# - ``pull_all`` runs one design §4.3 round between every ordered pair of
#   online nodes, which is what anti-entropy would do eventually.
"""In-process network helpers for the state runtime tests."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import time
from typing import Any, TypeVar

from hyprial.daemon.impl.orgfs import OrgFsError
from hyprial.daemon.impl.orgfs.composition.state import OrgFsRuntime
from hyprial.daemon.impl.orgfs.state.layout import document_paths
from hyprial.daemon.impl.orgfs.sync import SyncError

from tests.daemon.orgfs.contracts.support.network import FakeNetwork

T = TypeVar("T")


class Events:
    """A logger that records ``(event, fields)``."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, dict[str, object]]] = []

    def __call__(self, level: str, event: str, **fields: object) -> None:
        self.seen.append((event, fields))

    def names(self) -> list[str]:
        return [event for event, _fields in self.seen]


class Peer:
    """One ``OrgFsRuntime`` bound to a ``FakeNetwork``."""

    def __init__(
        self, network: FakeNetwork, root: Path, node_id: str, author: str, *, trust: bool,
        runtime_class: Any = OrgFsRuntime,
    ) -> None:
        self.network = network
        self.runtime_class = runtime_class
        self.state_dir = root / node_id
        self.node_id = node_id
        self.author = author
        self.trust = trust
        self.events = Events()
        self.runtime: OrgFsRuntime | None = None
        self.open()

    def open(self) -> OrgFsRuntime:
        runtime = self.runtime_class(
            self.state_dir,
            node_id=self.node_id,
            author=self.author,
            trust_tailnet_members=self.trust,
            logger=self.events,
        )
        session = self.network.add_node(self.node_id)
        runtime.bind_transport(
            session,
            supplier_online=self.network.online_for(self.node_id),
            holder_candidates=lambda: self.network.peers_of(self.node_id),
            observe_supplier_online=self.network.observe_for(self.node_id),
        )
        self.runtime = runtime
        return runtime

    @property
    def fs(self) -> Any:
        assert self.runtime is not None
        return self.runtime.facade

    def close(self) -> None:
        runtime, self.runtime = self.runtime, None
        if runtime is not None:
            self.network.forget(self.node_id)
            runtime.close()

    def reopen(self) -> OrgFsRuntime:
        self.close()
        return self.open()

    @property
    def spaces_root(self) -> Path:
        return self.state_dir / "orgfs" / "spaces"

    def content_files(self, space_id: str, doc_id: str) -> list[Path]:
        return [path for path in document_paths(self.spaces_root / space_id, doc_id) if path.exists()]

    def sync(self, space_id: str) -> Any:
        assert self.runtime is not None
        sync = self.runtime._mesh(space_id)  # noqa: SLF001 - the space's SpaceSync
        assert sync is not None
        return sync


class Peers:
    """Every node of one test on one network."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.network = FakeNetwork()
        self.nodes: list[Peer] = []

    def add(
        self, node_id: str, author: str, *, trust: bool = True, runtime_class: Any = OrgFsRuntime,
    ) -> Peer:
        self.network.add_node(node_id)
        peer = Peer(self.network, self.root, node_id, author, trust=trust, runtime_class=runtime_class)
        self.nodes.append(peer)
        return peer

    def disconnect(self, peer: Peer) -> None:
        self.network.partition(peer.node_id)

    def reconnect(self, peer: Peer) -> None:
        self.network.heal(peer.node_id)

    def pull_all(self, space_id: str) -> int:
        """One pull round between every ordered pair of reachable nodes."""

        accepted = 0
        for requester in self.nodes:
            if requester.runtime is None:
                continue
            for supplier in self.nodes:
                if supplier is requester or supplier.runtime is None:
                    continue
                if not self.network.reachable(requester.node_id, supplier.node_id):
                    continue
                try:
                    accepted += requester.sync(space_id).sync_from(supplier.node_id)
                except SyncError:
                    continue
        return accepted

    def close(self) -> None:
        for peer in reversed(self.nodes):
            peer.close()


def eventually(action: Callable[[], T], *, timeout: float = 10.0) -> T:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return action()
        except (AssertionError, OrgFsError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def holds(action: Callable[[], object], *, duration: float = 1.0) -> None:
    deadline = time.monotonic() + duration
    while True:
        action()
        if time.monotonic() >= deadline:
            return
        time.sleep(0.02)


def text(fs: Any, space_id: str, ref: str) -> str:
    return fs.read_text(space_id, ref)[0]


def tree(fs: Any, space_id: str) -> tuple[tuple[str, str, str], ...]:
    return tuple((node.node_id, node.path, node.kind) for node in fs.listdir(space_id, "", recursive=True))


def snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(root)): (path.read_bytes() if path.is_file() else None)
        for path in sorted(root.rglob("*"))
    }


def json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


__all__ = [
    "Events",
    "Peer",
    "Peers",
    "eventually",
    "holds",
    "json_bytes",
    "snapshot",
    "text",
    "tree",
]
