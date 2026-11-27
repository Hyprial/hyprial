# Decisions:
# - Copied from tests/daemon/orgfs/mesh/test_orgfs_mesh.py:45-86 (_Bus) and
#   presence/test_orgfs_rebuild_catchup.py (_Presence), so the contract suite
#   does not import another test module that phase E will delete.
# - The fake implements only the transport session protocol
#   (put/subscribe/declare_query_handler/query) plus a presence edge source;
#   it carries no journal, envelope or key knowledge, so the same network can
#   serve the phase D sync unchanged.
# - A partition is a set of blocked node names: a blocked node neither sends
#   nor receives puts and its query handlers do not answer.  Healing emits a
#   presence online edge in both directions, which is the only catch-up
#   trigger the contracts rely on.
"""In-process transport network shared by multi-node OrgFS contracts."""

from __future__ import annotations

from collections.abc import Callable
from fnmatch import fnmatch
import threading

from hyprial.daemon.impl.transport import TransportSample
from hyprial.daemon.impl.transport.presence_actor import ActorOnlineTransition


class _Registration:
    def __init__(self, close: Callable[[], None]) -> None:
        self._close = close

    def close(self) -> None:
        self._close()


class FakeNetwork:
    """A synchronous bus with per-node partitions and presence edges."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.subscribers: list[tuple[str, str, Callable[[TransportSample], None]]] = []
        self.handlers: list[tuple[str, str, Callable[..., object]]] = []
        self.nodes: list[str] = []
        self.blocked: set[str] = set()
        self._observers: dict[str, list[Callable[[ActorOnlineTransition], object]]] = {}
        self._sequence = 0

    def add_node(self, node: str) -> "FakeSession":
        with self._lock:
            if node not in self.nodes:
                self.nodes.append(node)
            self._observers.setdefault(node, [])
        return FakeSession(self, node)

    def peers_of(self, node: str) -> tuple[str, ...]:
        with self._lock:
            return tuple(name for name in self.nodes if name != node)

    def reachable(self, source: str, target: str) -> bool:
        with self._lock:
            return source not in self.blocked and target not in self.blocked

    def online_for(self, node: str) -> Callable[[str], bool]:
        def supplier_online(peer: str) -> bool:
            with self._lock:
                known = peer in self.nodes
            return known and self.reachable(node, peer)

        return supplier_online

    def observe_for(
        self, node: str
    ) -> Callable[[Callable[[ActorOnlineTransition], object]], Callable[[], None]]:
        def observe(callback: Callable[[ActorOnlineTransition], object]) -> Callable[[], None]:
            with self._lock:
                self._observers.setdefault(node, []).append(callback)

            def stop() -> None:
                with self._lock:
                    rows = self._observers.get(node, [])
                    if callback in rows:
                        rows.remove(callback)

            return stop

        return observe

    def partition(self, node: str) -> None:
        with self._lock:
            self.blocked.add(node)

    def heal(self, node: str) -> None:
        with self._lock:
            self.blocked.discard(node)
            others = tuple(name for name in self.nodes if name != node)
        for other in others:
            self.emit_online(observer=other, peer=node)
            self.emit_online(observer=node, peer=other)

    def emit_online(self, *, observer: str, peer: str) -> None:
        with self._lock:
            self._sequence += 1
            transition = ActorOnlineTransition(1, peer, self._sequence)
            callbacks = tuple(self._observers.get(observer, ()))
        for callback in callbacks:
            callback(transition)

    def forget(self, node: str) -> None:
        with self._lock:
            self.subscribers = [row for row in self.subscribers if row[0] != node]
            self.handlers = [row for row in self.handlers if row[0] != node]
            self._observers[node] = []


class FakeSession:
    """One node's view of :class:`FakeNetwork` (transport session protocol)."""

    def __init__(self, network: FakeNetwork, node: str) -> None:
        self.network = network
        self.node = node

    def put(self, key: str, payload: bytes) -> None:
        network = self.network
        for target, expression, callback in tuple(network.subscribers):
            if not network.reachable(self.node, target):
                continue
            if fnmatch(key, expression.replace("**", "*")):
                callback(TransportSample(key, payload))

    def subscribe(self, key_expr: str, callback: Callable[[TransportSample], None]) -> _Registration:
        row = (self.node, key_expr, callback)
        self.network.subscribers.append(row)
        return _Registration(
            lambda: self.network.subscribers.remove(row)
            if row in self.network.subscribers
            else None
        )

    def declare_query_handler(self, key_expr: str, handler: Callable[..., object]) -> _Registration:
        row = (self.node, key_expr, handler)
        self.network.handlers.append(row)
        return _Registration(
            lambda: self.network.handlers.remove(row)
            if row in self.network.handlers
            else None
        )

    def query(self, key_expr: str, payload: bytes, **_kwargs: object) -> list[TransportSample]:
        replies: list[TransportSample] = []
        for target, expression, handler in tuple(self.network.handlers):
            if not self.network.reachable(self.node, target):
                continue
            if fnmatch(key_expr, expression.replace("**", "*")):
                replies.extend(
                    TransportSample(key, body) for key, body in handler(key_expr, payload)
                )
        return replies


__all__ = ["FakeNetwork", "FakeSession"]
