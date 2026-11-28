"""In-process transport for protocol 3 sync tests.

Copied from the protocol-agnostic fakes in ``tests/daemon/orgfs/mesh/
test_orgfs_mesh.py`` (``_Bus``) and ``tests/daemon/orgfs/presence/
test_orgfs_rebuild_catchup.py`` (``_Session`` rebuild hooks), extended with
the faults the convergence tests need: held broadcasts (reorder, duplicate,
drop), partitions, a rebuild that loses declarations, and byte counters.
"""

from __future__ import annotations

from collections.abc import Callable
from fnmatch import fnmatch
import threading

from hyprial.daemon.impl.transport import TransportSample


class Registration:
    def __init__(self, close: Callable[[], None]) -> None:
        self._close = close

    def close(self) -> None:
        self._close()


def _matches(key: str, expression: str) -> bool:
    return fnmatch(key, expression.replace("**", "*"))


class Bus:
    """Delivers puts to subscribers and queries to handlers, by node."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.subscribers: list[tuple[str, str, Callable]] = []
        self.handlers: list[tuple[str, str, Callable]] = []
        self.partitions: set[frozenset[str]] = set()
        self.hold_puts = False
        self.held: list[tuple[str, str, bytes]] = []
        self.dropped_puts = 0
        self.queries: list[tuple[str, str, int, int]] = []
        self.payloads: list[tuple[str, bytes, list[bytes]]] = []
        self.reply_filter: Callable[[str, bytes], bytes | None] | None = None
        self.sessions: dict[str, Session] = {}

    def session(self, node: str) -> "Session":
        session = Session(self, node)
        self.sessions[node] = session
        return session

    def blocked(self, left: str, right: str) -> bool:
        return frozenset({left, right}) in self.partitions

    def partition(self, left: str, right: str) -> None:
        self.partitions.add(frozenset({left, right}))

    def heal(self) -> None:
        self.partitions.clear()

    def deliver(self, source: str, key: str, payload: bytes) -> int:
        delivered = 0
        with self.lock:
            rows = tuple(self.subscribers)
        for target, expression, callback in rows:
            if target == source or self.blocked(source, target):
                continue
            if _matches(key, expression):
                callback(TransportSample(key, payload))
                delivered += 1
        return delivered

    def take_held(self) -> list[tuple[str, str, bytes]]:
        with self.lock:
            held, self.held = self.held, []
        return held

    def requests(self, needle: str) -> list[tuple[bytes, list[bytes]]]:
        with self.lock:
            return [(sent, replies) for key, sent, replies in self.payloads if needle in key]

    def query_bytes(self, needle: str) -> list[tuple[int, int]]:
        with self.lock:
            return [(sent, received) for key, _node, sent, received in self.queries if needle in key]


class Session:
    def __init__(self, bus: Bus, node: str) -> None:
        self.bus = bus
        self.node = node
        self.closed = False
        self._rebuild_hooks: list[Callable[[], None]] = []

    def put(self, key: str, payload: bytes) -> None:
        if self.bus.hold_puts and "/org/fs3/update/" in key:
            with self.bus.lock:
                self.bus.held.append((self.node, key, payload))
            return
        self.bus.deliver(self.node, key, payload)

    def subscribe(self, key_expr: str, callback: Callable) -> Registration:
        row = (self.node, key_expr, callback)
        with self.bus.lock:
            self.bus.subscribers.append(row)
        return Registration(lambda: self._remove(self.bus.subscribers, row))

    def declare_query_handler(self, key_expr: str, handler: Callable) -> Registration:
        row = (self.node, key_expr, handler)
        with self.bus.lock:
            self.bus.handlers.append(row)
        return Registration(lambda: self._remove(self.bus.handlers, row))

    def _remove(self, rows: list, row: tuple) -> None:
        with self.bus.lock:
            if row in rows:
                rows.remove(row)

    def query(self, key_expr: str, payload: bytes | None, **_kwargs) -> list[TransportSample]:
        with self.bus.lock:
            rows = tuple(self.bus.handlers)
        replies: list[TransportSample] = []
        for owner, expression, handler in rows:
            if self.bus.blocked(self.node, owner):
                continue
            if _matches(key_expr, expression):
                for key, body in handler(key_expr, payload):
                    if self.bus.reply_filter is not None:
                        body = self.bus.reply_filter(key, body)
                        if body is None:
                            continue
                    replies.append(TransportSample(key, body))
        with self.bus.lock:
            self.bus.queries.append(
                (key_expr, self.node, len(payload or b""), sum(len(r.payload) for r in replies))
            )
            self.bus.payloads.append((key_expr, payload or b"", [r.payload for r in replies]))
        return replies

    def on_rebuild_complete(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._rebuild_hooks.append(callback)
        return lambda: self._rebuild_hooks.remove(callback) if callback in self._rebuild_hooks else None

    def lose_declarations(self) -> None:
        with self.bus.lock:
            self.bus.subscribers[:] = [row for row in self.bus.subscribers if row[0] != self.node]
            self.bus.handlers[:] = [row for row in self.bus.handlers if row[0] != self.node]

    def finish_rebuild(self) -> None:
        for hook in tuple(self._rebuild_hooks):
            hook()
