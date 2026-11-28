# Decisions (package O, 2026-11-25):
# - The org cell tests used to join a member's facade to the owner's spaces by
#   sharing the journal facade's in-memory space objects (``_spaces[sid]``)
#   and by sending the member's reads, and sometimes its writes, to the
#   owner's facade.  On the state host a facade's spaces are its own
#   registries and stores, so a shared space object is not a replica, and a
#   write forwarded to the owner's facade is authored by the owner (the
#   local write check then refuses the member's own device record with
#   ``protected-path``).  Those stubs no longer hold.
# - Instead each node keeps a real replica.  A join goes through the facade's
#   own join path: ``mesh.join`` opens the temporary space (``begin_join``),
#   pulls meta and tree from the peer through the two ``SyncHost``s, and
#   ``complete_join`` admits it only when this node's author is a member
#   (design section 9 R5, without the network).
# - Two always-online peers converge by anti-entropy.  Before any facade call
#   that names a linked space, both directions are pulled until nothing
#   changes (design section 4.3: meta, tree, then the requester's pending
#   content), so every read sees the peer's committed writes and every
#   write is authored by the node that makes it.  The pull is skipped inside
#   a facade command (the facade's own reentrant call of ``space_meta``) and
#   while another pull is running on the thread.
"""Linked in-memory OrgFS replicas for multi-node org cell tests."""

from __future__ import annotations

import threading
from functools import wraps
from typing import Any, Callable

from hyprial.daemon.impl.orgfs.api import OrgFsError, SpaceInfo
from hyprial.daemon.impl.orgfs.membership import Accepted, vector_covers

_PULL_ROUNDS = 32
_PENDING_BATCH = 256
_SPACE_METHODS = (
    "document_authors",
    "invite",
    "listdir",
    "members",
    "mkdir",
    "move",
    "read_bytes",
    "read_text",
    "read_text_snapshot",
    "remove",
    "remove_member",
    "resolve",
    "restore",
    "space_meta",
    "stat",
    "status",
    "trash",
    "update_space_meta",
    "write_bytes",
    "write_text",
)


def pull(requester: Any, supplier: Any, space_id: str, supplier_node: str) -> int:
    """Pull ``space_id`` from ``supplier`` into ``requester`` until it is current."""

    mine = requester.sync_host(space_id)
    theirs = supplier.sync_host(space_id)
    total = 0
    for _ in range(_PULL_ROUNDS):
        ids = ("meta", "tree", *mine.pending_content(_PENDING_BATCH))
        local = mine.read_vectors(ids)
        applied = 0
        for doc_id in ids:
            found = theirs.read_update(doc_id, local[doc_id])
            if found is None:
                continue
            update, vector = found
            if vector_covers(local[doc_id], vector):
                continue
            if isinstance(mine.apply_remote(doc_id, update, vector, supplier_node), Accepted):
                applied += 1
        if not applied:
            return total
        total += applied
    raise AssertionError(f"space {space_id} did not converge")


class _ReplicaMesh:
    """The ``mesh`` a facade joins through: one pull from the linked peer."""

    def __init__(self, link: "LinkedReplicas", fs: Any) -> None:
        self._link = link
        self._fs = fs

    def join(self, space_id: str, timeout: float | None = None) -> SpaceInfo:
        return self._link.join(self._fs, space_id)


class LinkedReplicas:
    """Two facades that replicate the spaces one of them joins from the other."""

    def __init__(self, first: tuple[Any, str], second: tuple[Any, str]) -> None:
        self._nodes = {id(first[0]): first, id(second[0]): second}
        self._spaces: set[str] = set()
        self._local = threading.local()
        for fs, _node in (first, second):
            fs.mesh = _ReplicaMesh(self, fs)
            for name in _SPACE_METHODS:
                original = getattr(fs, name)
                setattr(fs, name, self._settling(fs, original))

    def _peer(self, fs: Any) -> tuple[Any, str]:
        return next(node for key, node in self._nodes.items() if key != id(fs))

    def _node(self, fs: Any) -> str:
        return self._nodes[id(fs)][1]

    def join(self, fs: Any, space_id: str) -> SpaceInfo:
        peer, peer_node = self._peer(fs)
        if not _holds(peer, space_id):
            raise OrgFsError("no-holder-online", {"spaceId": space_id})
        fs.begin_join(space_id)
        try:
            self._guarded(lambda: pull(fs, peer, space_id, peer_node))
            info = fs.complete_join(space_id)
        except BaseException:
            fs.abandon_join(space_id)
            raise
        self._spaces.add(space_id)
        return info

    def settle(self, space_id: str) -> None:
        """Pull ``space_id`` both ways until neither side changes."""

        if space_id not in self._spaces:
            return
        (first, first_node), (second, second_node) = self._nodes.values()
        if not (_holds(first, space_id) and _holds(second, space_id)):
            return
        for _ in range(_PULL_ROUNDS):
            changed = pull(first, second, space_id, second_node)
            changed += pull(second, first, space_id, first_node)
            if not changed:
                return
        raise AssertionError(f"space {space_id} did not settle")

    def _guarded(self, action: Callable[[], Any]) -> Any:
        self._local.active = True
        try:
            return action()
        finally:
            self._local.active = False

    def _settling(self, fs: Any, original: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(original)
        def call(*args: Any, **kwargs: Any) -> Any:
            space_id = args[0] if args else kwargs.get("space_id")
            nested = getattr(self._local, "active", False) or getattr(
                fs._space_state_context, "running", False
            )
            if isinstance(space_id, str) and not nested:
                self._guarded(lambda: self.settle(space_id))
            return original(*args, **kwargs)

        return call


def _holds(fs: Any, space_id: str) -> bool:
    return any(info.space_id == space_id for info in fs.spaces())


def link_replicas(owner_app: Any, member_app: Any) -> LinkedReplicas:
    """Link the OrgFS facades of two org test applications."""

    return LinkedReplicas(
        (owner_app._require_orgfs_runtime().facade, owner_app.node_id),
        (member_app._require_orgfs_runtime().facade, member_app.node_id),
    )
