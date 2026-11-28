"""Two OrgFS nodes of one owner on the in-process synchronous bus (probe GC0).

Decision notes (slice GC0, 2026-11-18):

- The bus, session and presence doubles are the ones the OrgFS catch-up
  tests use (``tests/daemon/orgfs/presence/test_orgfs_rebuild_catchup.py``):
  every put reaches every matching subscriber synchronously, no Zenoh and
  no real daemon, so a negative assertion does not depend on timing.
- Both nodes write as the same owner principal, which is what two daemons
  of one owner do (the daemon's OrgFS author is ``user:<owner>``).  Each
  node sees the other as an online host through its presence double and as
  the only holder candidate, the most permissive wiring the daemon has.
- Record segments are written with the real ``SegmentStore`` under the
  writer id of the node that writes them, into the space chosen by the real
  ``CellSpaces`` rule.

Decision notes (OrgFS package S, 2026-11-25):

- The helpers read the runtime only through its public surface: holders
  through ``known_holders``, held spaces through ``facade.spaces()``.  The
  journal store's ``unbroadcast`` and the mesh's ingress counter are gone;
  "a's update was published and lost" is the bus's ``dropped`` count, and
  "b has taken in what reached it" is ``status(space).synced`` or a read.
- After a join the catalog is local and content arrives on demand (design
  Q7); ``content_arrived`` waits through the runtime's ``await_content`` the
  way a consumer has to before reading.
- An announce is published when a space is created or joined; the helper
  only waits for the target to list the source as a holder.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.composition.cells.spaces import CellSpaces
from hyprial.daemon.impl.conversations import ConversationDocuments, RecordEntry, SegmentStore
from hyprial.daemon.impl.delivery.ports import SOURCE_SENDER_COPY
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from tests.daemon.orgfs.presence.test_orgfs_rebuild_catchup import _Bus, _Presence

OWNER = "user:op"
NODE_A = "node-a"
NODE_B = "node-b"


@dataclass
class Node:
    """One node: its runtime, the session it is bound on and its presence view."""

    runtime: OrgFsRuntime
    session: Any
    presence: _Presence

    @property
    def fs(self) -> Any:
        return self.runtime.facade

    @property
    def node_id(self) -> str:
        return self.runtime.node_id

    def homes(self) -> tuple[str, ...]:
        return tuple(ref.space_id for ref in CellSpaces(self.fs).home_spaces(OWNER))

    def write_space(self) -> str | None:
        home = CellSpaces(self.fs).private_space(OWNER)
        return None if home is None else home.space_id

    def holders_of(self, space_id: str) -> frozenset[str]:
        return frozenset(self.runtime.known_holders(space_id))

    def held_spaces(self) -> frozenset[str]:
        return frozenset(info.space_id for info in self.fs.spaces())


def start_node(bus: _Bus, root: Path, node_id: str, peer: str) -> Node:
    """Bind one runtime of ``OWNER`` on *bus*, seeing *peer* as online."""

    session = bus.session(node_id)
    presence = _Presence(session)
    presence.online = {peer}
    runtime = OrgFsRuntime(root / node_id, node_id=node_id, author=OWNER, trust_tailnet_members=True)
    runtime.bind_transport(
        session,
        supplier_online=presence.supplier_online,
        holder_candidates=lambda: (peer,),
        observe_supplier_online=presence.observe,
    )
    return Node(runtime, session, presence)


def start_pair(root: Path) -> tuple[_Bus, Node, Node]:
    bus = _Bus()
    return bus, start_node(bus, root, NODE_A, NODE_B), start_node(bus, root, NODE_B, NODE_A)


def announce_until_held(source: Node, space_id: str, target: Node) -> None:
    """Announce *space_id* from *source* until *target* records it as a holder."""

    def seen() -> None:
        assert source.node_id in target.holders_of(space_id)

    eventually(seen)


def content_arrived(node: Node, space_id: str, *, timeout: float = 3.0) -> None:
    """Wait, as a consumer does after a join, until every document's content is local."""

    deadline = time.monotonic() + timeout
    for info in node.fs.listdir(space_id, "", recursive=True):
        if info.kind == "dir":
            continue
        assert node.runtime.await_content(space_id, info.node_id, deadline_monotonic=deadline)


def updates_dropped(bus: Any, at_least: int) -> None:
    """The bus has swallowed at least *at_least* update puts since it was built."""

    assert bus.dropped >= at_least


def drained(node: Node) -> None:
    assert node.runtime.directory_ingress_pending == 0


def eventually(action: Callable[[], Any], *, timeout: float = 3.0) -> Any:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return action()
        except (AssertionError, OrgFsError) as error:
            last_error = error
            time.sleep(0.005)
    if last_error is not None:
        raise last_error
    raise AssertionError("condition did not become true")


def entry(cell_id: str, message_id: str, at_ms: int, *, sender: str, recipient: str,
          conversation_id: str = "conv-probe", thread_id: str = "t.probe") -> RecordEntry:
    """One sender-copy record line for *message_id*."""

    body = f"body {message_id}".encode()
    return RecordEntry(
        key=f"{SOURCE_SENDER_COPY}:{at_ms}:{message_id}", source=SOURCE_SENDER_COPY,
        position=at_ms, message_id=message_id, conversation_id=conversation_id,
        thread_id=thread_id, cell_id=cell_id, sender=sender, recipient=recipient,
        intent="request", created_at_ms=at_ms, recorded_at_ms=at_ms,
        payload_sha256=hashlib.sha256(body).hexdigest(), body=body,
    )


def record(node: Node, cell_id: str, entries: list[RecordEntry], seq: int = 0) -> str:
    """Append *entries* to *node*'s own segment in its write space; returns the space."""

    space = node.write_space()
    assert space is not None
    store = SegmentStore(fs=lambda: node.fs, space_for=lambda _owner: space, writer=node.node_id)
    store.append(OWNER, cell_id, entries, seq)
    return space


def read_message_ids(node: Node, cell_id: str) -> list[str]:
    """Every message id *node* reads for *cell_id* over the union of its homes."""

    documents = ConversationDocuments(fs=lambda: node.fs, spaces=lambda _fs: node.homes())
    return sorted(item.message_id for item in documents.cell_entries(cell_id))


def expect_message_ids(node: Node, cell_id: str, expected: list[str]) -> None:
    assert read_message_ids(node, cell_id) == expected


def close(*nodes: Node | None) -> None:
    for node in nodes:
        if node is not None:
            node.runtime.close()


__all__ = [
    "NODE_A",
    "NODE_B",
    "OWNER",
    "Node",
    "announce_until_held",
    "close",
    "content_arrived",
    "drained",
    "entry",
    "eventually",
    "expect_message_ids",
    "read_message_ids",
    "record",
    "start_node",
    "start_pair",
    "updates_dropped",
]
