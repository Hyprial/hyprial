# Decisions:
# - Package F6 (R3-1): ``local_member`` answers whether the host's own author
#   (``user:<node without "node-">``, the author ``make_node`` gives the sync)
#   is a current member; ``left`` makes it false, as it does every other gate.
"""In-memory SyncHost: one pycrdt Doc per document, one client id per host.

It plays the facade coordinator of design §2.5 / §3.6 closely enough for the
sync contract: local commits bump ``__orgfs__.c`` in the same transaction
(§3.3), remote updates are staged on a throwaway copy first and reported as
``Pending("dependency")`` when the staged vector does not cover the frame's
vector, and content without a tree row is ``Pending("unknown-doc")``.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping, Sequence
import json
import random
import threading

from pycrdt import Doc, Map, Text

from hyprial.daemon.impl.orgfs.sync import covers, decode_vector, encode_vector

from tests.daemon.orgfs.sync.support.outcomes import Accepted, Pending, Rejected

EMPTY = b"\x00"
Decider = Callable[[str, bytes, str], object | None]


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


class MemoryHost:
    def __init__(
        self,
        node: str,
        *,
        members: Sequence[str] = ("user:a", "user:b", "user:c", "user:d"),
        state: Mapping[str, bytes] | None = None,
        client_id: int | None = None,
    ) -> None:
        self.node = node
        self.client_id = client_id or random.getrandbits(53) or 1
        self.lock = threading.RLock()
        self.docs: dict[str, Doc] = {}
        self.members = set(members)
        self.removed: set[str] = set()
        self.denied_suppliers: set[str] = set()
        self.left = False
        self.decide: Decider | None = None
        self.sync = None
        self.calls: list[tuple[str, str]] = []
        self.applied: list[tuple[str, str, str]] = []
        self.visible: list[tuple[str, str | None]] = []
        self.block: threading.Event | None = None
        self.callbacks_after_close = 0
        self.closed = False
        self.read_only = False
        self.repair_after_tree = True
        self.repair_union = True
        self.frontier_repairs = 0
        self.generation = 0
        for doc_id, update in (state or {}).items():
            self._doc(doc_id).apply_update(update)

    def _doc(self, doc_id: str) -> Doc:
        doc = self.docs.get(doc_id)
        if doc is None:
            doc = Doc(client_id=self.client_id)
            self.docs[doc_id] = doc
        return doc

    def _call(self, name: str, doc_id: str = "") -> None:
        if self.closed:
            self.callbacks_after_close += 1
        self.calls.append((name, doc_id))

    def export(self) -> dict[str, bytes]:
        with self.lock:
            return {doc_id: doc.get_update() for doc_id, doc in self.docs.items()}

    def commit(self, doc_id: str, mutate: Callable[[Doc], None], *, publish: bool = True) -> tuple[bytes, bytes]:
        with self.lock:
            doc = self._doc(doc_id)
            before = doc.get_state()
            with doc.transaction():
                mutate(doc)
                orgfs = doc.get("__orgfs__", type=Map)
                orgfs["c"] = int(orgfs.get("c", 0)) + 1
            update = doc.get_update(before)
            vector = doc.get_state()
            self.generation += 1
        if publish and self.sync is not None:
            self.sync.publish(doc_id, update, vector)
        return update, vector

    def write_text(self, doc_id: str, text: str, *, publish: bool = True) -> None:
        def mutate(doc: Doc) -> None:
            body = doc.get("body", type=Text)
            del body[0 : len(body)]
            body += text
            doc.get("__orgfs__", type=Map)["authors"] = Map({"user:" + self.node: True})

        content_update, vector = self.commit(doc_id, mutate, publish=False)
        tree_update, tree_vector = self.commit("tree", lambda doc: doc.get("nodes", type=Map).__setitem__(
            doc_id, json.dumps({"docId": doc_id, "frontier": _b64(vector)})
        ), publish=False)
        if publish and self.sync is not None:
            self.sync.publish("tree", tree_update, tree_vector)
            self.sync.publish(doc_id, content_update, vector)

    def clear_text(self, doc_id: str, *, publish: bool = True, clock: bool = True) -> None:
        """Delete the whole body: a pure deletion apart from the commit clock."""

        with self.lock:
            doc = self._doc(doc_id)
            before = doc.get_state()
            with doc.transaction():
                body = doc.get("body", type=Text)
                del body[0 : len(body)]
                if clock:
                    orgfs = doc.get("__orgfs__", type=Map)
                    orgfs["c"] = int(orgfs.get("c", 0)) + 1
            update = doc.get_update(before)
            vector = doc.get_state()
            self.generation += 1
        tree_update, tree_vector = self.commit("tree", lambda tree: tree.get("nodes", type=Map).__setitem__(
            doc_id, json.dumps({"docId": doc_id, "frontier": _b64(vector)})
        ), publish=False)
        if publish and self.sync is not None:
            self.sync.publish("tree", tree_update, tree_vector)
            self.sync.publish(doc_id, update, vector)

    def set_meta(self, key: str, value: object | None, *, publish: bool = True) -> None:
        def mutate(doc: Doc) -> None:
            members = doc.get("members", type=Map)
            if value is None:
                if key in members:
                    del members[key]
            else:
                members[key] = value

        self.commit("meta", mutate, publish=publish)

    def text(self, doc_id: str) -> str | None:
        with self.lock:
            row = self._tree_row(doc_id)
            if row is None:
                return None
            doc = self.docs.get(doc_id)
            local = doc.get_state() if doc is not None else EMPTY
            if not covers(local, base64.b64decode(row["frontier"])):
                return None
            return str(doc.get("body", type=Text))

    def meta(self) -> dict:
        with self.lock:
            meta = self.docs.get("meta")
            return {} if meta is None else (meta.get("members", type=Map).to_py() or {})

    def _tree_row(self, doc_id: str) -> dict | None:
        tree = self.docs.get("tree")
        if tree is None:
            return None
        raw = tree.get("nodes", type=Map).get(doc_id)
        return None if raw is None else json.loads(raw)

    def rows(self) -> list[dict]:
        tree = self.docs.get("tree")
        if tree is None:
            return []
        nodes = tree.get("nodes", type=Map)
        return [json.loads(nodes[key]) for key in sorted(nodes.keys())]

    def vectors(self) -> dict[str, dict[int, int]]:
        from hyprial.daemon.impl.orgfs.sync import decode_vector

        with self.lock:
            return {
                doc_id: decode_vector(doc.get_state())
                for doc_id, doc in self.docs.items()
                if decode_vector(doc.get_state())
            }

    def texts(self) -> dict[str, str | None]:
        with self.lock:
            return {row["docId"]: self.text(row["docId"]) for row in self.rows()}

    def read_vectors(self, doc_ids: Sequence[str]) -> Mapping[str, bytes]:
        with self.lock:
            self._call("read_vectors")
            return {
                doc_id: (self.docs[doc_id].get_state() if doc_id in self.docs else EMPTY)
                for doc_id in doc_ids
            }

    def read_update(self, doc_id: str, since: bytes) -> tuple[bytes, bytes] | None:
        with self.lock:
            self._call("read_update", doc_id)
            doc = self.docs.get(doc_id)
            if doc is None:
                return None
            return doc.get_update(since), doc.get_state()

    def pending_content(self, limit: int) -> tuple[str, ...]:
        with self.lock:
            self._call("pending_content")
            pending: list[str] = []
            for row in self.rows():
                if len(pending) >= limit:
                    break
                doc = self.docs.get(row["docId"])
                local = doc.get_state() if doc is not None else EMPTY
                if not covers(local, base64.b64decode(row["frontier"])):
                    pending.append(row["docId"])
            return tuple(pending)

    def apply_remote(self, doc_id: str, update: bytes, expected_vector: bytes, peer: str) -> object:
        if self.block is not None:
            self.block.wait(10)
        with self.lock:
            self._call("apply_remote", doc_id)
            outcome = self.decide(doc_id, update, peer) if self.decide is not None else None
            if outcome is None:
                outcome = self._admit(doc_id, update, expected_vector)
            kind = type(outcome).__name__
            self.applied.append((doc_id, kind, peer))
            if isinstance(outcome, Accepted):
                self._doc(doc_id).apply_update(update)
                watched = [row["docId"] for row in self.rows()] if doc_id == "tree" else [doc_id]
                for watched_id in watched:
                    self.visible.append((watched_id, self.text(watched_id)))
        if isinstance(outcome, Accepted) and doc_id not in ("meta", "tree"):
            self._repair_frontier(doc_id)
        if isinstance(outcome, Accepted) and doc_id == "tree" and self.repair_after_tree:
            for row in self.rows():
                self._repair_frontier(row["docId"])
        return outcome

    def _repair_frontier(self, doc_id: str) -> bool:
        """§4.2: after a merge, a local content vector beyond the row is written back."""

        if self.read_only:
            return False
        with self.lock:
            row = self._tree_row(doc_id)
            doc = self.docs.get(doc_id)
            if row is None or doc is None:
                return False
            local = doc.get_state()
            frontier = base64.b64decode(row["frontier"])
            if covers(frontier, local):
                return False
            value = local
            if self.repair_union:
                merged = decode_vector(frontier)
                for client, clock in decode_vector(local).items():
                    merged[client] = max(clock, merged.get(client, 0))
                value = encode_vector(merged)
            self.frontier_repairs += 1
            update, vector = self.commit("tree", lambda tree: tree.get("nodes", type=Map).__setitem__(
                doc_id, json.dumps({"docId": doc_id, "frontier": _b64(value)})
            ), publish=False)
        if self.sync is not None:
            self.sync.publish("tree", update, vector)
        return True

    def _admit(self, doc_id: str, update: bytes, expected_vector: bytes) -> object:
        if doc_id not in ("meta", "tree") and self._tree_row(doc_id) is None:
            return Pending("unknown-doc")
        staged = Doc()
        current = self.docs.get(doc_id)
        try:
            if current is not None:
                staged.apply_update(current.get_update())
            staged.apply_update(update)
        except ValueError:
            return Rejected("invalid-argument", {})
        if not covers(staged.get_state(), expected_vector):
            return Pending("dependency")
        return Accepted()

    def requester_allowed(self, author: str) -> bool:
        with self.lock:
            self._call("requester_allowed")
            return not self.left and author in self.members

    def requester_removed(self, author: str) -> bool:
        with self.lock:
            self._call("requester_removed")
            return not self.left and author in self.removed and author not in self.members

    def supplier_allowed(self, node: str) -> bool:
        with self.lock:
            self._call("supplier_allowed")
            return not self.left and node not in self.denied_suppliers

    def local_member(self) -> bool:
        with self.lock:
            self._call("local_member")
            return not self.left and "user:" + self.node.removeprefix("node-") in self.members

    def local_generation(self) -> int:
        with self.lock:
            self._call("local_generation")
            return self.generation
