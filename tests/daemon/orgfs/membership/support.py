"""Shared test tools for OrgFS membership and admission (design §3.6).

``StaticView`` is a minimal ``AdmissionView``.  ``Replica`` is an honest
node: every write goes through the C2 helpers (``local_commit``, which
advances the clock only when the edit did not advance the state vector, and
the member-row helpers that mark removal with ``removedAt``) and is signed
with the caller-supplied author, but it performs no local write check, so it
can produce *honestly signed* overreaching writes.  ``forge_update`` and the
``forge_*`` helpers build updates an honest node never produces (missing
author, no clock, edited member rows, deleted keys, missing predecessor).
C3, C4 and phase D reuse these tools to drive the admission entry point.
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from pycrdt import Array, Doc, Map, Text

from hyprial.daemon.impl.orgfs.membership import (
    Accepted,
    AdmissionResult,
    StagedDoc,
    TreeValue,
    doc_kind,
    local_commit,
    mark_member_removed,
    put_member,
    read_members,
    read_space,
    tree_row_wire,
    with_row_deleted_batch,
    write_space,
)
from hyprial.identity import (
    org_from_space_name,
    protected_directory_doc_id,
    protected_directory_node_id,
)

SPACE_ID = "6d0c3c39-8f8b-4f36-9a51-0a8f4a5c1e01"
ORG_NAME = "group-acme"
OWNER = "user:owner"
ADDED_AT = "2026-11-25T00:00:00.000000Z"


class StaticView:
    """An ``AdmissionView`` over fixed members and an optional tree value."""

    def __init__(
        self,
        owner: str,
        members: Mapping[str, str],
        tree: TreeValue | None = None,
        *,
        space_id: str = SPACE_ID,
        space_name: str = ORG_NAME,
    ) -> None:
        self.space_id = space_id
        self.space_name = space_name
        self.owner = owner
        self._members = dict(members)
        self._tree = tree

    def member_mode(self, user: str) -> str | None:
        return self._members.get(user)

    def tree_row(self, node_id: str) -> Mapping[str, object] | None:
        if self._tree is None:
            return None
        return self._tree.placed.get(node_id)

    def node_for_doc(self, doc_id: str) -> Mapping[str, object] | None:
        if self._tree is None:
            return None
        for row in self._tree.placed.values():
            if row.get("kind") == "doc" and row.get("doc_id") == doc_id:
                return row
        return None

    def path_of(self, node_id: str) -> str | None:
        if self._tree is None:
            return None
        return self._tree.paths.get(node_id)


@dataclass(frozen=True)
class Frame:
    """One ``(doc_id, update, sender vector after the update)`` triple."""

    doc_id: str
    update: bytes
    vector: bytes


def _root_row(author: str) -> str:
    return tree_row_wire(
        {"node_id": "root", "parent": None, "name": "", "kind": "dir",
         "doc_id": None, "blob_hash": None, "deleted": False},
        author=author,
    )


class Replica:
    """An honest node's meta, tree and content documents.

    As a minimal coordinator it follows design §3.6 "export swaps the
    instance": every export of an authoritative document (to build or
    rebuild a staged copy) replaces that document with one loaded from the
    export.  ``swap_on_export=False`` reproduces the pre-revision behaviour
    for comparison tests only.
    """

    def __init__(
        self,
        *,
        client_id: int,
        name: str = ORG_NAME,
        space_id: str = SPACE_ID,
        states: Mapping[str, bytes] | None = None,
        swap_on_export: bool = True,
    ) -> None:
        self.client_id = client_id
        self.swap_on_export = swap_on_export
        self.name = name
        self.space_id = space_id
        self.docs: dict[str, Doc] = {}
        for doc_id, state in (states or {}).items():
            self._doc(doc_id).apply_update(state)
        self._doc("meta")
        self._doc("tree")

    @classmethod
    def create(
        cls,
        *,
        owner: str = OWNER,
        members: Mapping[str, str] | None = None,
        client_id: int = 1,
        name: str = ORG_NAME,
    ) -> "Replica":
        """A fresh space created by ``owner``, then the given members invited."""

        replica = cls(client_id=client_id, name=name)
        meta = replica.docs["meta"]
        with local_commit(meta, "meta", owner, space_root=True):
            write_space(meta, space_id=replica.space_id, name=name, owner=owner, created_at=ADDED_AT)
            put_member(meta, owner, mode="rw", added_by=owner, added_at=ADDED_AT)
        tree = replica.docs["tree"]
        with local_commit(tree, "tree", owner, tree_rows=("root",)):
            tree.get("nodes", type=Map)["root"] = _root_row(owner)
        for user, mode in (members or {}).items():
            replica.invite(user, mode, by=owner)
        return replica

    def _doc(self, doc_id: str) -> Doc:
        if doc_id not in self.docs:
            self.docs[doc_id] = Doc(client_id=self.client_id)
        return self.docs[doc_id]

    def clone(self, client_id: int, *, swap_on_export: bool = True) -> "Replica":
        """Another node holding this node's full state under ``client_id``."""

        return Replica(
            client_id=client_id,
            name=self.name,
            space_id=self.space_id,
            states={doc_id: doc.get_update() for doc_id, doc in self.docs.items()},
            swap_on_export=swap_on_export,
        )

    def state(self, doc_id: str) -> bytes:
        return self._doc(doc_id).get_update()

    def vector(self, doc_id: str) -> bytes:
        return self._doc(doc_id).get_state()

    @property
    def owner(self) -> str:
        return str(read_space(self.docs["meta"]).get("owner", ""))

    def tree_value(self) -> TreeValue:
        value = StagedDoc.from_state("tree", self.state("tree")).materialize()
        assert isinstance(value, TreeValue)
        return value

    def view(self) -> StaticView:
        members = {user: row.mode for user, row in read_members(self.docs["meta"]).items()}
        return StaticView(
            self.owner, members, self.tree_value(), space_id=self.space_id, space_name=self.name
        )

    def staged(self, doc_id: str) -> StagedDoc:
        """A staged copy built from an export of the authoritative document."""

        staged = StagedDoc(doc_kind(doc_id))
        if doc_id in self.docs:
            self.rebuild(doc_id, staged)
        return staged

    def rebuild(self, doc_id: str, staged: StagedDoc) -> None:
        """Rebuild ``staged`` from an export, swapping the authority (§3.6)."""

        if self.swap_on_export:
            self.docs[doc_id] = staged.reset_from_authority(self.docs[doc_id])
        else:
            staged.reset(self.docs[doc_id].get_update())

    def apply(self, doc_id: str, update: bytes) -> None:
        self._doc(doc_id).apply_update(update)

    def _edit(self, doc_id: str, edit: Callable[[Doc], None]) -> Frame:
        doc = self._doc(doc_id)
        before = doc.get_state()
        with doc.transaction():
            edit(doc)
        return Frame(doc_id, doc.get_update(before), doc.get_state())

    def invite(self, user: str, mode: str, *, by: str, added_at: str = ADDED_AT) -> Frame:
        def edit(doc: Doc) -> None:
            with local_commit(doc, "meta", by):
                put_member(doc, user, mode=mode, added_by=by, added_at=added_at)

        return self._edit("meta", edit)

    def remove_member(self, user: str, *, by: str, removed_at: str = ADDED_AT) -> Frame:
        """Mark ``user`` removed: the row stays with ``removedAt`` (design §9 R8)."""

        def edit(doc: Doc) -> None:
            with local_commit(doc, "meta", by):
                mark_member_removed(doc, user, removed_at=removed_at)

        return self._edit("meta", edit)

    def set_space(self, key: str, value: object, *, by: str) -> Frame:
        def edit(doc: Doc) -> None:
            with local_commit(doc, "meta", by, space_root=True):
                doc.get("space", type=Map)[key] = value

        return self._edit("meta", edit)

    def resolve(self, path: str, *, include_deleted: bool = True) -> str | None:
        tree = self.tree_value()
        for node_id, node_path in tree.paths.items():
            row = tree.placed[node_id]
            if node_path == path and (include_deleted or not row.get("deleted")):
                return node_id
        return None

    def _protected(self) -> bool:
        return org_from_space_name(self.name) is not None

    def _new_ids(self, path: str, kind: str) -> tuple[str, str | None]:
        owner = self.owner
        node_id = (
            protected_directory_node_id(self.space_id, path, space_owner=owner)
            if self._protected()
            else None
        ) or uuid.uuid4().hex
        doc_id = None
        if kind == "doc":
            doc_id = (
                protected_directory_doc_id(self.space_id, path, space_owner=owner)
                if self._protected()
                else None
            ) or f"doc-{uuid.uuid4()}"
        return node_id, doc_id

    def _parent(self, path: str) -> tuple[str, str]:
        parent_path, _, name = path.rpartition("/")
        parent = self.resolve(parent_path) if parent_path else "root"
        if parent is None:
            raise KeyError(parent_path)
        return parent, name

    def _row_edit(self, rows: Mapping[str, Mapping[str, Any]], author: str) -> Frame:
        def edit(doc: Doc) -> None:
            with local_commit(doc, "tree", author, tree_rows=tuple(rows)):
                nodes = doc.get("nodes", type=Map)
                for node_id, row in rows.items():
                    nodes[node_id] = tree_row_wire(row)

        return self._edit("tree", edit)

    def mkdir(self, path: str, *, author: str) -> Frame:
        parent, name = self._parent(path)
        node_id, _ = self._new_ids(path, "dir")
        row = {"node_id": node_id, "parent": parent, "name": name, "kind": "dir",
               "doc_id": None, "blob_hash": None, "deleted": False}
        return self._row_edit({node_id: row}, author)

    def makedirs(self, path: str, *, author: str) -> list[Frame]:
        frames: list[Frame] = []
        parts = path.split("/")
        for index in range(1, len(parts) + 1):
            prefix = "/".join(parts[:index])
            if self.resolve(prefix) is None:
                frames.append(self.mkdir(prefix, author=author))
        return frames

    def write_text(self, path: str, text: str, *, author: str) -> list[Frame]:
        """Write a document; returns the tree frame (when created or moved) then content."""

        node_id = self.resolve(path)
        frames: list[Frame] = []
        if node_id is None:
            parent, name = self._parent(path)
            node_id, doc_id = self._new_ids(path, "doc")
            row: dict[str, Any] = {"node_id": node_id, "parent": parent, "name": name,
                                   "kind": "doc", "doc_id": doc_id, "blob_hash": None,
                                   "deleted": False}
        else:
            row = dict(self.tree_value().rows[node_id])
            doc_id = str(row["doc_id"])

        def edit(doc: Doc) -> None:
            with local_commit(doc, "content", author):
                body = doc.get("text", type=Text)
                body.clear()
                body.insert(0, text)

        content = self._edit(str(doc_id), edit)
        row["contentFrontier"] = base64.b64encode(content.vector).decode("ascii")
        row["deleted"] = False
        frames.append(self._row_edit({node_id: row}, author))
        frames.append(content)
        return frames

    def move(self, path: str, new_parent_path: str, *, author: str) -> Frame:
        node_id = self.resolve(path)
        assert node_id is not None
        row = dict(self.tree_value().rows[node_id])
        new_parent = self.resolve(new_parent_path) if new_parent_path else "root"
        old_parent = row.get("parent")
        row["parent"] = new_parent

        def edit(doc: Doc) -> None:
            with local_commit(doc, "tree", author, tree_rows=(node_id,)):
                doc.get("nodes", type=Map)[node_id] = tree_row_wire(row)
                doc.get("moves", type=Array).append(json.dumps(
                    {"ts": 1, "peer": str(self.client_id), "node": node_id,
                     "oldParent": old_parent, "newParent": new_parent, "name": row["name"]},
                    sort_keys=True, separators=(",", ":"),
                ))

        return self._edit("tree", edit)

    def remove(self, path: str, *, author: str, batch: str = "batch-1") -> Frame:
        """Soft delete ``path`` and its descendants with one ``deletedBatch``."""

        tree = self.tree_value()
        prefix = f"{path}/"
        targets = {
            node_id: dict(tree.rows[node_id])
            for node_id, node_path in tree.paths.items()
            if node_path == path or node_path.startswith(prefix)
        }

        def edit(doc: Doc) -> None:
            with local_commit(doc, "tree", author, tree_rows=tuple(targets)):
                nodes = doc.get("nodes", type=Map)
                for node_id, row in targets.items():
                    row["deleted"] = True
                    nodes[node_id] = with_row_deleted_batch(tree_row_wire(row), batch)

        return self._edit("tree", edit)


def deliver(admission: Any, receiver: Replica, frame: Frame) -> AdmissionResult:
    """Admit ``frame`` on ``receiver``; apply it when accepted, else rebuild the staged copy."""

    staged = receiver.staged(frame.doc_id)
    result = admission.admit(frame.doc_id, frame.update, frame.vector, staged, receiver.view())
    if isinstance(result, Accepted):
        receiver.apply(frame.doc_id, frame.update)
    elif frame.doc_id in receiver.docs:
        receiver.rebuild(frame.doc_id, staged)
    return result


def forge_update(
    kind: str,
    mutate: Callable[[Doc, str], None],
    *,
    author: str,
    client_id: int,
    base: bytes | None = None,
) -> Frame:
    """Run ``mutate(doc, author)`` on a copy of ``base`` without any C2 helper.

    ``kind`` names the document (``"meta"``, ``"tree"`` or a content doc id)
    and becomes the frame's ``doc_id``.  The result is the raw update the
    mutation produced, and the forger's vector after it.
    """

    doc = Doc(client_id=client_id)
    if base:
        doc.apply_update(base)
    before = doc.get_state()
    with doc.transaction():
        mutate(doc, author)
    return Frame(kind, doc.get_update(before), doc.get_state())


def forge_tree_row(
    base: bytes, node_id: str, row: Mapping[str, Any], *, author: str | None, client_id: int
) -> Frame:
    """Write one tree row; ``author=None`` omits the author field."""

    def mutate(doc: Doc, signer: str) -> None:
        doc.get("nodes", type=Map)[node_id] = tree_row_wire(row, author=author)

    return forge_update("tree", mutate, author=author or "", client_id=client_id, base=base)


def forge_raw_tree_value(base: bytes, node_id: str, raw: object, *, client_id: int) -> Frame:
    """Store an arbitrary value under a tree key (malformed rows)."""

    def mutate(doc: Doc, signer: str) -> None:
        doc.get("nodes", type=Map)[node_id] = raw

    return forge_update("tree", mutate, author="", client_id=client_id, base=base)


def forge_delete_tree_key(base: bytes, node_id: str, *, client_id: int) -> Frame:
    """Delete a tree key outright (a permanent delete no honest remote may send)."""

    def mutate(doc: Doc, signer: str) -> None:
        del doc.get("nodes", type=Map)[node_id]

    return forge_update("tree", mutate, author="", client_id=client_id, base=base)


def forge_move(
    base: bytes, node_id: str, new_parent: str, name: str, *, client_id: int
) -> Frame:
    """Append a move record without rewriting the moved row."""

    def mutate(doc: Doc, signer: str) -> None:
        doc.get("moves", type=Array).append(json.dumps(
            {"ts": 9, "peer": "forger", "node": node_id, "oldParent": None,
             "newParent": new_parent, "name": name},
            sort_keys=True, separators=(",", ":"),
        ))

    return forge_update("tree", mutate, author="", client_id=client_id, base=base)


def forge_member_row(
    base: bytes, user: str, row: Mapping[str, Any], *, client_id: int
) -> Frame:
    """Write a member row verbatim (any ``addedBy``)."""

    def mutate(doc: Doc, signer: str) -> None:
        doc.get("members", type=Map)[user] = Map(dict(row))

    return forge_update("meta", mutate, author="", client_id=client_id, base=base)


def forge_delete_member(base: bytes, user: str, *, client_id: int) -> Frame:
    """Delete a member row's key (no honest node does; removal marks ``removedAt``)."""

    def mutate(doc: Doc, signer: str) -> None:
        del doc.get("members", type=Map)[user]

    return forge_update("meta", mutate, author="", client_id=client_id, base=base)


def forge_space_value(base: bytes, key: str, value: object, *, client_id: int) -> Frame:
    """Set one key of the ``space`` root (owner, kind, ...)."""

    def mutate(doc: Doc, signer: str) -> None:
        doc.get("space", type=Map)[key] = value

    return forge_update("meta", mutate, author="", client_id=client_id, base=base)


def forge_content(
    doc_id: str,
    base: bytes | None,
    text: str,
    *,
    author: str | None,
    clock: bool,
    client_id: int,
) -> Frame:
    """Replace a content document's text.

    ``author=None`` leaves ``__orgfs_authors__`` untouched; ``clock=False``
    leaves ``__orgfs__.c`` untouched, so a pure-delete edit does not advance
    the state vector.
    """

    def mutate(doc: Doc, signer: str) -> None:
        body = doc.get("text", type=Text)
        body.clear()
        body.insert(0, text)
        orgfs = doc.get("__orgfs__", type=Map)
        if author is not None:
            doc.get("__orgfs_authors__", type=Map)[author] = True
        if clock:
            current = orgfs.get("c") or 0
            orgfs["c"] = int(current) + 1

    return forge_update(doc_id, mutate, author=author or "", client_id=client_id, base=base)


def forge_text_delete(
    doc_id: str, base: bytes, start: int, stop: int, *, client_id: int
) -> Frame:
    """Delete ``text[start:stop]`` and nothing else: no author, no clock.

    A pure deletion carries only a delete set, so it does not advance the
    state vector; an honest node never sends one without a clock write.
    """

    def mutate(doc: Doc, signer: str) -> None:
        del doc.get("text", type=Text)[start:stop]

    return forge_update(doc_id, mutate, author="", client_id=client_id, base=base)


def forge_missing_predecessor(
    doc_id: str,
    base: bytes,
    first: Callable[[Doc], None],
    second: Callable[[Doc], None],
    *,
    client_id: int,
) -> tuple[Frame, Frame]:
    """Two successive edits; deliver the second alone to miss its predecessor.

    Returns ``(predecessor, successor)``; each frame's vector is the
    sender's vector after that edit.
    """

    doc = Doc(client_id=client_id)
    doc.apply_update(base)
    start = doc.get_state()
    with doc.transaction():
        first(doc)
    middle = doc.get_state()
    predecessor = Frame(doc_id, doc.get_update(start), middle)
    with doc.transaction():
        second(doc)
    return predecessor, Frame(doc_id, doc.get_update(middle), doc.get_state())
