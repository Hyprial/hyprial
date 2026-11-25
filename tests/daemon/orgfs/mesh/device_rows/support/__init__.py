# Decisions:
# - Phase D T1: the device-row and protected-directory tests run on the state
#   host (``StateOrgFs``) only.  An org directory space ``group-acme`` with the
#   shared roots is created by the owner, the members are invited, and every
#   member gets a node cloned from the owner's durable state (the D1
#   coordinator support's ``clone_node``), joined by its in-process ``Cluster``.
#   The node gate sits in front of ``SyncHost.apply_remote`` and is not part
#   of these tests; the old fixtures trusted the tailnet for the same reason.
# - A forged update is C2's ``forge_update`` over one node's current tree
#   (``read_update(doc, EMPTY)``) and is delivered only through
#   ``SyncHost.apply_remote``.  Rows are written in the facade's wire format
#   with the forger's ``author``, which is the evidence the receivers judge
#   (design §3.3, §3.6).
# - ``capture`` returns the frames a node published during one action without
#   delivering them, so a test can choose the arrival order.
"""An in-memory org directory space on the state host, and forging helpers."""

from __future__ import annotations

import itertools
import json
from typing import Any, Callable, Iterable, Mapping

from pycrdt import Array, Doc, Map, Text

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.membership import Accepted, Pending, Rejected, tree_row_wire
from hyprial.identity import DirectoryDevice

from tests.daemon.orgfs.coordinator.support import EMPTY, Cluster, Node, clone_node
from tests.daemon.orgfs.membership.support import Frame, forge_update

OWNER = "owner@example"
ALICE = "alice@example"
BOB = "bob@example"
CAROL = "carol@example"
ORG = "group-acme"
DIRECTORY_ROOTS = (
    "directory",
    "directory/devices",
    "directory/people",
    "directory/invites",
    "directory/leaves",
)
Outcome = Accepted | Pending | Rejected

_CLIENT_IDS = itertools.count(9001)


def principal(user: str) -> str:
    return user if user.startswith("user:") else f"user:{user}"


def device(owner: str, device_id: str, *, address: str = "tcp://10.0.0.7:7447#psk") -> DirectoryDevice:
    return DirectoryDevice(
        device_id=device_id,
        owner=owner,
        server_public=f"nodekey:server-{owner}-{device_id}",
        client_public=f"nodekey:client-{owner}-{device_id}",
        address=address,
        key_generation=1,
        updated_at="2026-10-07T12:00:00+00:00",
    )


def record_text(row: DirectoryDevice) -> str:
    return json.dumps(row.as_record(), sort_keys=True, separators=(",", ":"))


def new_row(
    node_id: str,
    *,
    parent: str,
    name: str,
    kind: str = "doc",
    doc_id: str | None = None,
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "parent": parent,
        "name": name,
        "kind": kind,
        "doc_id": doc_id,
        "blob_hash": None,
        "deleted": False,
    }


class Org:
    """The owner and member nodes of one org directory space."""

    def __init__(self, members: Iterable[str] = (ALICE, BOB, CAROL), *, name: str = ORG) -> None:
        self.nodes: dict[str, Node] = {}
        self.events: dict[str, list[tuple[str, dict[str, object]]]] = {}
        owner = Node(principal(OWNER), "node-owner", logger=self._logger(OWNER))
        self.nodes[OWNER] = owner
        try:
            self.space_id = owner.fs.create_space(name).space_id
            for path in DIRECTORY_ROOTS:
                owner.fs.mkdir(self.space_id, path)
            for user in members:
                owner.fs.invite(self.space_id, principal(user), "rw")
            for user in members:
                self.nodes[user] = clone_node(
                    owner,
                    self.space_id,
                    principal(user),
                    f"node-{user.split('@', 1)[0]}",
                    logger=self._logger(user),
                )
            self.cluster = Cluster(self.space_id, list(self.nodes.values()))
        except BaseException:
            self.close()
            raise

    def _logger(self, user: str) -> Callable[..., None]:
        seen = self.events.setdefault(user, [])

        def log(_level: str, event: str, **fields: object) -> None:
            seen.append((event, fields))

        return log

    def __getitem__(self, user: str) -> Node:
        return self.nodes[user]

    def fs(self, user: str) -> Any:
        return self.nodes[user].fs

    def directory(self, user: str) -> OrgFsDirectoryStore:
        return OrgFsDirectoryStore(self.fs(user))

    def deliver(self) -> int:
        return self.cluster.deliver()

    def capture(self, user: str, action: Callable[[], object]) -> list[Frame]:
        """Run ``action`` and return what ``user``'s node published, undelivered."""

        self.deliver()
        action()
        return [
            Frame(doc_id, update, vector)
            for sender, doc_id, update, vector in self.cluster.take()
            if sender == self.nodes[user].node_id
        ]

    def apply(self, user: str, frame: Frame, *, peer: str = "node-forger") -> Outcome:
        return self.fs(user).sync_host(self.space_id).apply_remote(
            frame.doc_id, frame.update, frame.vector, peer
        )

    def state(self, user: str, doc_id: str) -> bytes:
        found = self.fs(user).sync_host(self.space_id).read_update(doc_id, EMPTY)
        assert found is not None
        return found[0]

    def rows(self, user: str) -> dict[str, dict[str, Any]]:
        doc = Doc()
        doc.apply_update(self.state(user, "tree"))
        nodes = doc.get("nodes", type=Map)
        return {key: json.loads(str(value)) for key, value in nodes.items()}

    def node_id(self, user: str, path: str) -> str:
        return "root" if not path else self.fs(user).stat(self.space_id, path).node_id

    def forge_tree(
        self,
        base_user: str,
        mutate: Callable[[Map], None],
        *,
        author: str,
    ) -> Frame:
        """A tree update made on ``base_user``'s tree, outside any local check."""

        def edit(doc: Doc, _signer: str) -> None:
            mutate(doc.get("nodes", type=Map))

        return forge_update(
            "tree",
            edit,
            author=principal(author),
            client_id=next(_CLIENT_IDS),
            base=self.state(base_user, "tree"),
        )

    def forge_move(
        self, base_user: str, node_id: str, *, old_parent: str, new_parent: str, name: str
    ) -> Frame:
        """Append one move record and leave every row untouched."""

        record = json.dumps(
            {"name": name, "newParent": new_parent, "node": node_id,
             "oldParent": old_parent, "peer": base_user, "ts": 959},
            sort_keys=True,
            separators=(",", ":"),
        )

        def edit(doc: Doc, _signer: str) -> None:
            doc.get("moves", type=Array).append(record)

        return forge_update(
            "tree", edit, author="", client_id=next(_CLIENT_IDS), base=self.state(base_user, "tree")
        )

    def forge_rows(
        self, base_user: str, rows: Iterable[Mapping[str, Any]], *, author: str
    ) -> Frame:
        """Write whole rows (new or replaced) signed by ``author``."""

        values = [dict(row) for row in rows]

        def mutate(nodes: Map) -> None:
            for row in values:
                nodes[str(row["node_id"])] = tree_row_wire(row, author=principal(author))

        return self.forge_tree(base_user, mutate, author=author)

    def forge_rewrite(
        self, base_user: str, node_id: str, *, author: str, **changes: object
    ) -> Frame:
        """Rewrite one existing row with ``changes``, signed by ``author``."""

        row = dict(self.rows(base_user)[node_id])
        row.update(changes)
        return self.forge_rows(base_user, [row], author=author)

    def forge_content(
        self,
        base_user: str | None,
        doc_id: str,
        text: str,
        *,
        author: str,
    ) -> Frame:
        """A content update adding ``author`` and replacing the text."""

        base = None if base_user is None else self._content_state(base_user, doc_id)

        def edit(doc: Doc, signer: str) -> None:
            body = doc.get("text", type=Text)
            body.clear()
            body.insert(0, text)
            doc.get("__orgfs_authors__", type=Map)[signer] = True
            clock = doc.get("__orgfs__", type=Map)
            clock["c"] = int(clock.get("c") or 0) + 1

        return forge_update(
            doc_id, edit, author=principal(author), client_id=next(_CLIENT_IDS), base=base
        )

    def _content_state(self, user: str, doc_id: str) -> bytes | None:
        found = self.fs(user).sync_host(self.space_id).read_update(doc_id, EMPTY)
        return None if found is None else found[0]

    def text(self, user: str, path: str) -> str:
        return self.fs(user).read_text(self.space_id, path)[0]

    def missing(self, user: str, path: str) -> bool:
        try:
            self.fs(user).stat(self.space_id, path)
        except OrgFsError as error:
            assert error.code == "unknown-doc", error.code
            return True
        return False

    def listing(self, user: str) -> tuple[tuple[str, str, str, bool], ...]:
        return tuple(
            (node.node_id, node.path, node.kind, node.deleted)
            for node in self.fs(user).listdir(self.space_id, "", recursive=True)
        )

    def rejected(self, user: str) -> int:
        return self.fs(user).status(self.space_id).rejected_updates

    def close(self) -> None:
        for node in reversed(list(self.nodes.values())):
            node.close()


def refused(action: Callable[[], object]) -> str:
    """Run a local write that must fail; return its error code."""

    try:
        action()
    except OrgFsError as error:
        return error.code
    raise AssertionError("the local write was not refused")


__all__ = [
    "ALICE",
    "BOB",
    "CAROL",
    "DIRECTORY_ROOTS",
    "ORG",
    "OWNER",
    "Frame",
    "Org",
    "device",
    "new_row",
    "principal",
    "record_text",
    "refused",
]
