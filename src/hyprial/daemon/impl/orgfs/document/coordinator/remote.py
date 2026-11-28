# Decision record (phase D, package D1, 2026-11-25):
# - Design §3.6 / §3.7, C2 report §9: ``apply_remote`` runs on the space's
#   command loop: policy generation (meta vector digest) and the rejection
#   memory first (a remembered rejection returns ``Rejected`` without counting
#   or logging), content without a tree row is ``Pending("unknown-doc")``
#   before anything is loaded, then admission on the staged copy.  meta and
#   tree use the resident staged copies; a content document is staged from an
#   export of its authoritative instance, which swaps the instance (§3.6).
#   ``Rejected`` and ``Pending("dependency")`` rebuild the staged copy by an
#   export that swaps the authoritative instance too.
# - An accepted update is applied to the authoritative instance and what it
#   actually integrated (``get_update`` against the vector from before) is
#   committed, so structs left in the in-memory pending set never reach disk.
#   An update that integrates nothing is not committed.  A failed commit
#   reloads the document from disk and answers ``Pending("dependency")``.
# - Design §6: deleting content files is triggered only by an observed
#   transition, a tree row present before and absent after an accepted tree
#   update; those content documents go into the same batch's ``deletes`` and
#   blob references no remaining row retains are released afterwards (§9 R13).
# - Design §4.2 (revised): after accepting content the document is checked,
#   after accepting a tree update the rows whose frontier changed are checked;
#   when the local content vector is not covered by the row's frontier the row
#   is rewritten, keeping its author, to the per-client maximum of both, with
#   ``refSize`` / ``refSha256`` of the local content only when that maximum
#   equals the local vector.  A read-only member never repairs.  Only
#   documents whose local vector is known (loaded) are checked at once; the
#   others become unverified and are checked by ``pending_content``.
# - Remote changes produce watch events through the shared
#   ``_tree_change_events`` and the legacy birth-pending rule; versions and
#   attribution come from the rows.
# - D2, design §9.1 (D1 report §6-4): ``_remote_birth_content_pending`` never
#   loads a content document.  A new document row waits for its birth content
#   unless the document is already in memory and covers the row's frontier, so
#   accepting a tree update with N new document rows reads no content file.
# Decision record (phase D, package F3 follow-up, 2026-11-25):
# - An accepted content update finds the nodes of its doc id through the
#   registry's ``NodeIndex`` (``doc_nodes``) instead of scanning
#   ``space.nodes``: a join that receives N documents one by one was
#   O(N^2) in that scan alone.
# Decision record (phase F4, real-node finding R2-10, 2026-11-25):
# - A remote binary file is announced (``created``, or ``modified`` for a new
#   digest) when its tree row arrives, while its blob is still absent: blobs
#   are fetched on demand (§4.2).  ``blob_arrived`` is the runtime's report
#   that a fetched blob is now stored; it emits one ``modified`` event, an
#   existing kind, for every live node of the space naming that digest, at
#   most once per node and digest (``DocRegistry.announced_blobs``, in
#   memory).  It does nothing for a digest the store does not hold.
"""Remote update path of the state host (design §3.5-§3.7, §4.2, §6)."""

from __future__ import annotations

import hashlib
from typing import Mapping

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.document.coordinator.host import AdmissionViewOf, state_command
from hyprial.daemon.impl.orgfs.document.coordinator.registry import EMPTY_VECTOR, DocRegistry
from hyprial.daemon.impl.orgfs.document.model import _Node, _Space
from hyprial.daemon.impl.orgfs.membership import (
    Accepted,
    Pending,
    Rejected,
    ResultStateAdmission,
    StagedDoc,
    decode_vector,
    doc_kind,
    encode_vector,
    policy_generation,
    vector_covers,
)
from hyprial.daemon.impl.orgfs.state import Batch


def union_vector(left: bytes, right: bytes) -> bytes:
    """The per-client maximum of two state vectors, canonically encoded."""

    merged = decode_vector(left)
    for client, clock in decode_vector(right).items():
        merged[client] = max(clock, merged.get(client, 0))
    return encode_vector(merged)


class StateRemote:
    """``apply_remote`` and its tree, content and frontier consequences."""

    _admission: ResultStateAdmission

    @state_command
    def apply_remote(
        self,
        space_id: str,
        doc_id: str,
        update: bytes,
        expected_vector: bytes,
        peer: str,
    ) -> Accepted | Pending | Rejected:
        """Admit, apply and commit one remote update (design §3.6)."""

        space = self._space(space_id)
        registry = self._registry(space_id)
        update = bytes(update)
        generation = policy_generation(space.meta.get_state())
        if registry.rejections.seen(doc_id, update, generation):
            return Rejected("invalid-argument", {"docId": doc_id, "reason": "remembered"})
        kind = doc_kind(doc_id)
        if kind == "content" and self._doc_node(space, doc_id) is None:
            return Pending("unknown-doc")
        if kind == "meta":
            staged: StagedDoc = registry.staged_meta
        elif kind == "tree":
            staged = registry.staged_tree
        else:
            staged = registry.stage_content(doc_id)
        outcome = self._admission.admit(
            doc_id, update, bytes(expected_vector), staged, AdmissionViewOf(self, space)
        )
        if isinstance(outcome, Rejected):
            registry.rejections.remember(doc_id, update, generation)
            registry.rejected_updates += 1
            self._log(
                "warn",
                "orgfs.update.rejected",
                spaceId=space_id,
                docId=doc_id,
                code=outcome.code,
                peer=peer,
            )
            self._restage(registry, doc_id)
            return outcome
        if isinstance(outcome, Pending):
            if outcome.reason == "dependency":
                self._restage(registry, doc_id)
            return outcome
        return self._accept_remote(space, registry, doc_id, update)

    @staticmethod
    def _restage(registry: DocRegistry, doc_id: str) -> None:
        if doc_id in ("meta", "tree"):
            registry.export_and_swap(doc_id)

    def _accept_remote(
        self, space: _Space, registry: DocRegistry, doc_id: str, update: bytes
    ) -> Accepted | Pending:
        kind = doc_kind(doc_id)
        document = registry.document(doc_id)
        before = document.get_state()
        before_probe = document.get_update(before)
        previous: dict[str, tuple] = {}
        old_paths: dict[str, str | None] = {}
        rows_before: dict[str, dict] = {}
        if kind == "tree":
            previous = {
                node_id: (node.parent, node.name, node.kind, node.blob_hash, node.deleted)
                for node_id, node in space.nodes.items()
            }
            old_paths = {node_id: self._path(space, node_id) for node_id in space.nodes}
            rows_before = space.tree.rows()
        document.apply_update(update)
        integrated = document.get_update(before)
        if integrated == before_probe:
            return Accepted()
        deletes: tuple[str, ...] = ()
        if kind == "tree":
            rows_after = space.tree.nodes.to_py() or {}
            deletes = tuple(
                sorted(
                    {
                        str(row.get("doc_id"))
                        for node_id, row in rows_before.items()
                        if node_id not in rows_after
                        and row.get("kind") == "doc"
                        and isinstance(row.get("doc_id"), str)
                        and row.get("doc_id") not in ("meta", "tree", "")
                    }
                )
            )
        try:
            registry.store.commit(Batch(appends=((doc_id, integrated),), deletes=deletes))
        except Exception as exc:
            self._log(
                "warn",
                "orgfs.update.commit-failed",
                spaceId=space.info.space_id,
                docId=doc_id,
                error=str(exc),
            )
            self._recover_after_failure(space, registry, (doc_id,))
            return Pending("dependency")
        registry.refresh_vectors([doc_id])
        for deleted in deletes:
            registry.drop(deleted)
        registry.forget_vectors(list(deletes))
        if kind == "meta":
            self._refresh_members(space)
            self._adopt_space_root(space)
            self._notify_meta_watchers(space)
        elif kind == "tree":
            self._project_remote_tree(space, registry, previous, old_paths, rows_before)
        else:
            self._project_remote_content(space, registry, doc_id)
        self._maybe_compact(registry, [doc_id])
        return Accepted()

    def _project_remote_tree(
        self,
        space: _Space,
        registry: DocRegistry,
        previous: Mapping[str, tuple],
        old_paths: Mapping[str, str | None],
        rows_before: Mapping[str, dict],
    ) -> None:
        _added, vanished = self._sync_nodes(space, registry)
        digests = {space.nodes[node_id].blob_hash for node_id in vanished}
        live = [node_id for node_id in vanished if not space.nodes[node_id].deleted]
        for node_id in vanished:
            space.nodes[node_id].deleted = True
        if live:
            self._finish(space, live, "removed", old_paths={node_id: old_paths.get(node_id) for node_id in live})
        for node_id in vanished:
            space.nodes.pop(node_id, None)
            space.birth_pending.pop(node_id, None)
        registry.doc_index = None
        self._tree_change_events(space, previous, old_paths, ("", None))
        rows_after = space.tree.rows()
        changed = [
            node_id
            for node_id, row in rows_after.items()
            if rows_before.get(node_id) != row and node_id in space.nodes
        ]
        self._refresh_versions(space, changed)
        self._release_blobs(space.info.space_id, digests)
        frontier_docs = [
            space.nodes[node_id].doc_id
            for node_id in changed
            if space.nodes[node_id].kind == "doc"
            and space.nodes[node_id].doc_id
            and (rows_before.get(node_id) or {}).get("contentFrontier")
            != rows_after[node_id].get("contentFrontier")
        ]
        repairs = self._recheck(space, registry, [doc_id for doc_id in frontier_docs if doc_id])
        self._repair_frontiers(space, registry, repairs)

    def _project_remote_content(self, space: _Space, registry: DocRegistry, doc_id: str) -> None:
        repairs = self._recheck(space, registry, [doc_id])
        self._repair_frontiers(space, registry, repairs)
        affected = [node.node_id for node in self._bound_index(space, registry).doc_nodes(doc_id)]
        born: list[str] = []
        progressed: list[str] = []
        for node_id in affected:
            if node_id not in space.birth_pending:
                progressed.append(node_id)
            elif not self._remote_birth_content_pending(space, space.nodes[node_id]):
                space.birth_pending.pop(node_id, None)
                born.append(node_id)
        if born:
            self._finish(space, born, "created")
        if progressed:
            self._finish(space, progressed, "content")

    @state_command
    def blob_arrived(self, space_id: str, digest: str) -> int:
        """Emit ``modified`` for the live nodes naming a newly stored blob (R2-10)."""

        space = self._space(space_id)
        registry = self._registry(space_id)
        if self.blobs is None or not self.blobs.contains(digest):
            return 0
        arrived = [
            node_id
            for node_id, node in space.nodes.items()
            if node.kind == "blob"
            and not node.deleted
            and node.blob_hash == digest
            and registry.announced_blobs.get(node_id) != digest
        ]
        for node_id in arrived:
            registry.announced_blobs[node_id] = digest
        if arrived:
            self._finish(space, arrived, "content")
        return len(arrived)

    def _remote_birth_content_pending(self, space: _Space, node: _Node) -> bool:
        """Hook: whether a remote birth still waits for content; never loads."""

        if node.kind != "doc" or not node.doc_id or node.required_content_frontier is None:
            return False
        document = self._registry(space.info.space_id).loaded(node.doc_id)
        if document is None:
            return True
        return not vector_covers(document.doc.get_state(), node.required_content_frontier)

    def _recheck(self, space: _Space, registry: DocRegistry, doc_ids: list[str]) -> list[str]:
        """Record coverage of loaded documents; return those ahead of their row."""

        repairs: list[str] = []
        for doc_id in dict.fromkeys(doc_ids):
            node = self._doc_node(space, doc_id)
            if node is None:
                registry.drop(doc_id)
                continue
            document = registry.loaded(doc_id)
            if document is None:
                registry.mark(doc_id, None)
                continue
            local = document.doc.get_state()
            frontier = node.required_content_frontier
            registry.mark(doc_id, frontier is None or vector_covers(local, frontier))
            if frontier is not None and not vector_covers(frontier, local):
                repairs.append(doc_id)
        return repairs

    def _repair_frontiers(self, space: _Space, registry: DocRegistry, doc_ids: list[str]) -> None:
        member = space.members.get(self.author)
        if not doc_ids or member is None or member.mode != "rw":
            return
        targets = []
        for doc_id in doc_ids:
            node = self._doc_node(space, doc_id)
            document = registry.loaded(doc_id)
            if node is None or document is None or node.required_content_frontier is None:
                continue
            local = document.doc.get_state()
            if vector_covers(node.required_content_frontier, local):
                continue
            targets.append((node, document, union_vector(node.required_content_frontier, local), local))
        if not targets:
            return

        def operation() -> None:
            for node, document, value, local in targets:
                node.required_content_frontier = value
                if decode_vector(value) == decode_vector(local):
                    raw = document.value().encode()
                    node.ref_size = len(raw)
                    node.ref_sha256 = hashlib.sha256(raw).hexdigest()
                else:
                    node.ref_size = None
                    node.ref_sha256 = None
                self._record_row(space, node, stamp=False)

        try:
            self._commit(space, "tree", operation)
        except OrgFsError as exc:
            self._log(
                "warn", "orgfs.frontier.repair-failed", spaceId=space.info.space_id, code=exc.code
            )
            return
        registry.frontier_repairs += 1
        node_ids = [node.node_id for node, _document, _value, _local in targets]
        self._refresh_versions(space, node_ids)
        self._recheck(space, registry, [node.doc_id for node, *_rest in targets if node.doc_id])

    @state_command
    def repair_frontiers(self, space_id: str, doc_ids: tuple[str, ...]) -> None:
        """Repair the rows of ``doc_ids`` found ahead of their frontier (§4.2)."""

        space = self._space(space_id)
        registry = self._registry(space_id)
        for doc_id in doc_ids:
            if self._doc_node(space, doc_id) is not None:
                registry.content(doc_id)
        self._repair_frontiers(space, registry, self._recheck(space, registry, list(doc_ids)))

    def _verify_content(self, space: _Space, registry: DocRegistry, limit: int) -> list[str]:
        """Check up to ``limit`` unverified documents (design §9 R4)."""

        batch = list(registry.unverified)[: max(0, limit)]
        for doc_id in batch:
            if self._doc_node(space, doc_id) is None:
                registry.drop(doc_id)
                continue
            registry.content(doc_id)
        return self._recheck(space, registry, batch)

    def _pending_content_locked(self, space_id: str, limit: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
        with self._space_lock(space_id):
            space = self._space(space_id)
            registry = self._registry(space_id)
            repairs = self._verify_content(space, registry, limit)
            order = [
                node.doc_id
                for node in space.nodes.values()
                if node.doc_id and node.doc_id in registry.pending
            ]
            return tuple(dict.fromkeys(order))[: max(0, limit)], tuple(repairs)

    def _read_vectors_locked(self, space_id: str, doc_ids: tuple[str, ...]) -> dict[str, bytes]:
        with self._space_lock(space_id):
            space = self._space(space_id)
            registry = self._registry(space_id)
            result: dict[str, bytes] = {}
            for doc_id in doc_ids:
                if doc_id in ("meta", "tree"):
                    result[doc_id] = registry.document(doc_id).get_state()
                elif self._doc_node(space, doc_id) is not None:
                    vector = registry.content(doc_id).doc.get_state()
                    registry.remember_vector(doc_id, vector)
                    result[doc_id] = vector
                else:
                    result[doc_id] = EMPTY_VECTOR
            return result

    def _read_update_locked(
        self, space_id: str, doc_id: str, since: bytes
    ) -> tuple[bytes, bytes] | None:
        with self._space_lock(space_id):
            space = self._space(space_id)
            registry = self._registry(space_id)
            if doc_id not in ("meta", "tree") and self._doc_node(space, doc_id) is None:
                return None
            document = registry.document(doc_id)
            return document.get_update(since or None), document.get_state()


__all__ = ["StateRemote", "union_vector"]
