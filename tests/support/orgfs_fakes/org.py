"""Doubles: an in-memory OrgFs subset, a settable clock and a log recorder.

``FakeOrgFs`` implements the ``OrgFs`` subset ``LinkStore`` uses — ``spaces``,
``listdir``, ``read_text_snapshot``, ``write_text``, ``document_authors``,
``mkdir`` and ``watch`` — with injectable failures, blocking points and an
optional call guard.

Decision: T0 zero-behaviour move 2026-11-19 (send-link/thread removal plan).
``BlockPoint``, ``LogRecorder``, ``FakeClock`` and ``FakeOrgFs`` moved
verbatim from ``tests/support/links/org.py``; the private ``_FakeNode``,
``_FakeSpace`` and ``_FakeRegistration`` helpers moved with them.
``tests/support/links/org.py`` re-imports them for its own fixtures and for
its existing importers until it is deleted with the links package (plan T1c).

Nothing here touches a production home, socket or database.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from hyprial.daemon.impl.orgfs.api import (
    NodeInfo,
    OrgFsError,
    SpaceInfo,
    TextReadSnapshot,
)

__all__ = [
    "BlockPoint",
    "FakeClock",
    "FakeOrgFs",
    "LogRecorder",
]


class BlockPoint:
    """A blocking point: announces entry, waits for the test to release it."""

    def __init__(self, timeout: float = 5.0) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self._timeout = timeout

    def wait(self) -> None:
        self.entered.set()
        self.release.wait(self._timeout)

    def reset(self) -> None:
        self.entered.clear()
        self.release.clear()

    def go(self) -> None:
        self.release.set()

    def wait_entered(self, timeout: float = 5.0) -> bool:
        return self.entered.wait(timeout)


class LogRecorder:
    """Records ``logger(level, domain, event, **fields)`` calls."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, level: str, domain: str, event: str, **fields: Any) -> None:
        self.events.append((event, {"level": level, "domain": domain, **fields}))

    def names(self) -> list[str]:
        return [event for event, _ in self.events]

    def count(self, name: str) -> int:
        return sum(1 for event, _ in self.events if event == name)

    def fields_for(self, name: str) -> list[dict[str, Any]]:
        return [fields for event, fields in self.events if event == name]


class FakeClock:
    """A settable wall clock in milliseconds."""

    def __init__(self, start: int = 1_000_000) -> None:
        self._now = start
        self._lock = threading.Lock()

    def __call__(self) -> int:
        with self._lock:
            return self._now

    def now_ms(self) -> int:
        return self()

    def set(self, value: int) -> None:
        with self._lock:
            self._now = value

    def advance(self, ms: int) -> int:
        with self._lock:
            self._now += ms
            return self._now


@dataclass
class _FakeNode:
    kind: str
    name: str
    path: str
    content: str = ""
    version: int = 1
    authors: list[str] = field(default_factory=list)


@dataclass
class _FakeSpace:
    space_id: str
    name: str
    owner: str
    writable: bool = True
    nodes: dict[str, _FakeNode] = field(default_factory=dict)
    watches: list[tuple[str, Callable[[Any], None]]] = field(default_factory=list)


class _FakeRegistration:
    def __init__(self, owner: FakeOrgFs, space_id: str, callback: Any) -> None:
        self._owner = owner
        self._space_id = space_id
        self._callback = callback
        self.closed = False

    def close(self) -> None:
        self.closed = True
        space = self._owner._spaces.get(self._space_id)
        if space is None:
            return
        space.watches = [
            item for item in space.watches if item[1] is not self._callback
        ]


class FakeOrgFs:
    """An in-memory OrgFs subset with injectable failure and blocking."""

    def __init__(
        self,
        *,
        read_block: BlockPoint | None = None,
        write_block: BlockPoint | None = None,
        guard: Callable[[str], None] | None = None,
    ) -> None:
        self._spaces: dict[str, _FakeSpace] = {}
        self.read_block = read_block
        self.write_block = write_block
        self.guard = guard
        #: Operations that must fail with an OrgFsError code.
        self.fail_reads: set[str] = set()
        self.fail_writes: set[str] = set()
        #: Records
        self.calls: list[tuple[str, str, str]] = []
        self.write_versions: list[tuple[str, str, str | None]] = []
        self.watch_reads = 0
        self._in_watch = False
        self.watch_threads: list[int] = []

    # -- test helpers ---------------------------------------------------

    def add_space(
        self,
        space_id: str,
        *,
        name: str | None = None,
        owner: str = "user:o",
        writable: bool = True,
    ) -> None:
        space = _FakeSpace(space_id=space_id, name=name or space_id, owner=owner)
        space.writable = writable
        self._spaces[space_id] = space
        self._mkdir(space, "link")

    def drop_space(self, space_id: str) -> None:
        self._spaces.pop(space_id, None)

    def set_writable(self, space_id: str, writable: bool) -> None:
        self._spaces[space_id].writable = writable

    def put_text(
        self,
        space_id: str,
        path: str,
        content: str,
        *,
        author: str = "user:o",
        authors: Iterable[str] | None = None,
    ) -> str:
        space = self._spaces[space_id]
        node = space.nodes.get(path)
        if node is None:
            parent, name = self._parent(space, path)
            if parent is not None:
                node = _FakeNode(kind="doc", name=name, path=path)
                space.nodes[path] = node
        assert node is not None
        node.content = content
        node.version += 1
        node.authors = list(authors) if authors is not None else [author]
        self._notify(space, path, "modified")
        return str(node.version)

    def put_document(
        self, space_id: str, path: str, document: Mapping[str, Any], **kwargs: Any
    ) -> str:
        return self.put_text(
            space_id, path, json.dumps(document, sort_keys=True), **kwargs
        )

    def remove(self, space_id: str, path: str) -> None:
        space = self._spaces[space_id]
        space.nodes.pop(path, None)
        self._notify(space, path, "removed")

    def read_count(self) -> int:
        return sum(1 for call in self.calls if call[0].startswith("read"))

    def fire_change(self, space_id: str, path: str = "link/x.json") -> None:
        self._notify(self._spaces[space_id], path, "modified")

    # -- OrgFs subset ---------------------------------------------------

    def spaces(self) -> tuple[SpaceInfo, ...]:
        return tuple(
            SpaceInfo(
                space_id=space.space_id,
                name=space.name,
                owner=space.owner,
                created_at="2026-01-01T00:00:00Z",
            )
            for space in self._spaces.values()
        )

    def listdir(self, space_id: str, node: str) -> tuple[NodeInfo, ...]:
        self._guard("listdir")
        self.calls.append(("listdir", space_id, node))
        if self.read_block is not None:
            self.read_block.wait()
        if space_id in self.fail_reads:
            raise OrgFsError("unavailable", {"message": "fake read failure"})
        space = self._space(space_id)
        parent = self._path_ref(space_id, node)
        prefix = f"{parent}/" if parent else ""
        children = [
            item
            for path, item in sorted(space.nodes.items())
            if path.startswith(prefix)
            and "/" not in path[len(prefix) :]
            and path != parent
        ]
        return tuple(self._info(space, item) for item in children)

    def read_text_snapshot(self, space_id: str, node: str) -> TextReadSnapshot:
        self._guard("read_text_snapshot")
        self.calls.append(("read_text_snapshot", space_id, node))
        if self._in_watch:
            self.watch_reads += 1
        if self.read_block is not None:
            self.read_block.wait()
        if space_id in self.fail_reads:
            raise OrgFsError("unavailable", {"message": "fake read failure"})
        item = self._node(space_id, node)
        return TextReadSnapshot(
            content=item.content,
            version=str(item.version),
            node=self._info(self._space(space_id), item),
        )

    def document_authors(self, space_id: str, node: str) -> tuple[str, ...]:
        self._guard("document_authors")
        return tuple(self._node(space_id, node).authors)

    def write_text(
        self,
        space_id: str,
        node: str,
        content: str,
        *,
        base_version: str | None = None,
        expect_version: str | None = None,
        create_only: bool = False,
    ) -> NodeInfo:
        del base_version
        self._guard("write_text")
        self.calls.append(("write_text", space_id, node))
        self.write_versions.append((space_id, node, expect_version))
        if self.write_block is not None:
            self.write_block.wait()
        if space_id in self.fail_writes:
            raise OrgFsError("unavailable", {"message": "fake write failure"})
        space = self._space(space_id)
        if not space.writable:
            raise OrgFsError("not-a-member", {"message": "read-only space"})
        path = self._path_ref(space_id, node)
        item = space.nodes.get(path)
        if item is None and create_only is False:
            parent, name = self._parent(space, path)
            if parent is None:
                raise OrgFsError("unknown-doc", {"message": "no parent"})
            item = _FakeNode(kind="doc", name=name, path=path)
            space.nodes[path] = item
        elif create_only and item is not None:
            raise OrgFsError("invalid-argument", {"message": "already exists"})
        assert item is not None
        if expect_version is not None and str(item.version) != str(expect_version):
            raise OrgFsError("stale-write", {"message": "version moved"})
        item.content = content
        item.version += 1
        item.authors = [space.owner]
        self._notify(space, node, "modified")
        return self._info(space, item)

    def mkdir(self, space_id: str, path: str) -> NodeInfo:
        self._guard("mkdir")
        self.calls.append(("mkdir", space_id, path))
        space = self._space(space_id)
        if not space.writable:
            raise OrgFsError("not-a-member", {"message": "read-only space"})
        item = self._mkdir(space, path)
        return self._info(space, item)

    def watch(
        self, space_id: str, glob: str, callback: Callable[[Any], None]
    ) -> _FakeRegistration:
        self._guard("watch")
        self._spaces[space_id].watches.append((glob, callback))
        return _FakeRegistration(self, space_id, callback)

    # -- internals ------------------------------------------------------

    def _space(self, space_id: str) -> _FakeSpace:
        space = self._spaces.get(space_id)
        if space is None:
            raise OrgFsError("unknown-space", {"message": space_id})
        return space

    def _node(self, space_id: str, path: str) -> _FakeNode:
        item = self._space(space_id).nodes.get(self._path_ref(space_id, path))
        if item is None:
            raise OrgFsError("unknown-doc", {"message": path})
        return item

    @staticmethod
    def _path_ref(space_id: str, node: str) -> str:
        clean = node.strip("/")
        if not clean.startswith("id:"):
            return clean
        value = clean[3:]
        prefix = f"{space_id}:"
        if not value.startswith(prefix):
            raise OrgFsError("unknown-doc", {"message": node})
        return value[len(prefix) :]

    def _parent(self, space: _FakeSpace, path: str) -> tuple[_FakeNode | None, str]:
        clean = path.strip("/")
        parent_path, _, name = clean.rpartition("/")
        if not parent_path:
            return _FakeNode(kind="dir", name="", path=""), name
        return space.nodes.get(parent_path), name

    def _mkdir(self, space: _FakeSpace, path: str) -> _FakeNode:
        clean = path.strip("/")
        existing = space.nodes.get(clean)
        if existing is not None:
            raise OrgFsError("invalid-argument", {"message": "node already exists"})
        parent, name = self._parent(space, path)
        if parent is None:
            raise OrgFsError("unknown-doc", {"message": "no parent"})
        item = _FakeNode(kind="dir", name=name, path=clean)
        space.nodes[clean] = item
        return item

    def _info(self, space: _FakeSpace, item: _FakeNode) -> NodeInfo:
        return NodeInfo(
            space_id=space.space_id,
            node_id=f"{space.space_id}:{item.path or '/'}",
            kind=item.kind,  # type: ignore[arg-type]
            name=item.name,
            path=item.path,
            size=None,
            blob_hash=None,
            doc_id=None,
            version=str(item.version),
            modified_by=space.owner,
            modified_via=None,
            name_conflict=False,
            deleted=False,
            content_state="arrived",
            uri=f"orgfs:{space.owner}:{space.space_id}:{item.path or '/'}",
        )

    def _notify(self, space: _FakeSpace, path: str, kind: str) -> None:
        if not space.watches:
            return
        event = type(
            "ChangeEvent",
            (),
            {"space_id": space.space_id, "kind": kind, "node": None, "old_path": None},
        )()
        self._in_watch = True
        try:
            for _glob, callback in tuple(space.watches):
                self.watch_threads.append(threading.get_ident())
                callback(event)
        finally:
            self._in_watch = False

    def _guard(self, op: str) -> None:
        if self.guard is not None:
            self.guard(op)
