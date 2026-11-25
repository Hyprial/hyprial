# Decision record (phase D, package D1, 2026-11-25):
# - Design §2.5 / §3.3: every local write of the state host goes through
#   ``_commit_many``: each ``(doc_id, operation)`` runs inside
#   ``membership.local_commit`` (one Yjs transaction, author fields, clock only
#   when the vector did not move), then one ``StateStore.commit(Batch)`` carries
#   every touched document and the permanent deletions.  ``_commit`` is the
#   one-document form; the legacy mixins call both unchanged.
# - The ``space`` root of meta is compared before and after the operation, and
#   only a changed root is stamped with ``modifiedBy`` (``space_root``), so
#   member-table commits do not rewrite the root.
# - Tree rows written by a local commit carry the local author and actor
#   directly and are also touched on the stamp, which then finds nothing to
#   rewrite; a frontier repair writes the row with its previous author
#   (design §4.2) and does not touch it.
# - Design §9 R14: no rollback snapshot.  A failed commit reloads the touched
#   documents from the store (disk is the pre-commit state when nothing was
#   applied) and rematerializes; a reload that raises (``StateStoreBroken``,
#   or any other failure) closes and reopens the store and rebuilds the
#   space from ``load()`` (design §2.5).  The error is mapped like the legacy
#   ``projection.py`` commit (``OrgFsError`` with the exception's ``code`` or
#   ``invalid-argument``).
# - Design §2.5 step 3 / plan §2.4: publication happens only after the batch
#   is durable, as a ``_Publish`` effect carrying ``(doc_id, update, vector)``
#   in the order meta, tree, content (a new file's tree row before its
#   content, design §4.2).  The effect goes to a bounded per-space queue (256,
#   oldest dropped and counted) drained by the existing network lane, one task
#   per space at a time, into the attached ``SpaceSync.publish``.
# - Design §2.5: compaction is triggered here after a commit whose document
#   ``needs_compaction``; its state comes from an export that swaps the
#   instance.  A compaction failure is logged and retried after a later commit.
# - D2: when the facade belongs to a runtime (``mesh`` is set) but the space
#   has no sync attached yet (a space just created, or opened before the
#   transport was bound), publications are still queued and the network lane
#   asks the runtime to open the space's sync (``_mesh``), as the legacy lane
#   opened the mesh.  It runs on the lane worker, never under a space lock.
# Decision record (phase D, package F3, 2026-11-25):
# - A recorded row registers its node in the registry's ``NodeIndex``
#   (``_note_node``) instead of dropping the index, and a tree commit no
#   longer drops it: every local tree change passes through ``_record_row``,
#   and a failed commit rematerializes, which drops it (F3 profile).
"""Local commit path of the state host (design §2.5, §3.3)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.document.model import (
    _FacadeEffectResult,
    _Node,
    _Space,
    _encode_content_frontier,
)
from hyprial.daemon.impl.orgfs.document.coordinator.registry import (
    PUBLISH_QUEUE_LIMIT,
    DocRegistry,
)
from hyprial.daemon.impl.orgfs.membership import (
    doc_kind,
    local_commit,
    read_space,
    row_author,
    row_via,
    tree_row_wire,
)
from hyprial.daemon.impl.orgfs.state import Batch


@dataclass(frozen=True, slots=True)
class _Publish:
    space_id: str
    items: tuple[tuple[str, bytes, bytes], ...]


def row_value(node: _Node, batch: str | None) -> dict[str, Any]:
    """The tree row of ``node`` without author fields (``_wire_node`` shape)."""

    value: dict[str, Any] = {
        "node_id": node.node_id,
        "parent": node.parent,
        "name": node.name,
        "kind": node.kind,
        "doc_id": node.doc_id,
        "blob_hash": node.blob_hash,
        "deleted": node.deleted,
    }
    if node.kind == "blob" and node.size is not None:
        value["size"] = node.size
    if node.kind == "doc":
        if node.required_content_frontier is not None:
            value["contentFrontier"] = _encode_content_frontier(node.required_content_frontier)
        if node.ref_size is not None:
            value["refSize"] = node.ref_size
        if node.ref_sha256 is not None:
            value["refSha256"] = node.ref_sha256
    if node.deleted and batch:
        value["deletedBatch"] = batch
    return value


class StateCommit:
    """Commit coordinator mixin: the single local write path of ``StateOrgFs``."""

    def _registry(self, space_id: str) -> DocRegistry:
        with self._lock:
            registry = self._registries.get(space_id)
        if registry is None:
            raise OrgFsError("unknown-space", {"spaceId": space_id})
        if registry.broken:
            raise OrgFsError("unavailable", {"spaceId": space_id, "message": "state store is broken"})
        return registry

    def _commit(
        self,
        space: _Space,
        doc_id: str,
        operation: Callable[[], None],
        *,
        broadcast: bool = True,
    ) -> None:
        self._commit_many(space, ((doc_id, operation),))

    def _commit_many(
        self,
        space: _Space,
        operations: Iterable[tuple[str, Callable[[], None]]],
        *,
        deletes: tuple[str, ...] = (),
    ) -> None:
        if not hasattr(self._effect_context, "pending"):
            with self._post_commit_scope(space.info.space_id):
                return self._commit_many(space, operations, deletes=deletes)
        items = tuple(operations)
        if not items and not deletes:
            return None
        registry = self._registry(space.info.space_id)
        order = tuple(dict.fromkeys(doc_id for doc_id, _operation in items))
        try:
            documents = {doc_id: registry.document(doc_id) for doc_id in order}
            befores = {doc_id: documents[doc_id].get_state() for doc_id in order}
            for doc_id, operation in items:
                self._run_stamped(registry, doc_id, documents[doc_id], operation)
            appends = tuple(
                (doc_id, documents[doc_id].get_update(befores[doc_id])) for doc_id in order
            )
            registry.store.commit(Batch(appends=appends, deletes=tuple(deletes)))
        except Exception as exc:
            registry.stamps.clear()
            self._recover_after_failure(space, registry, order)
            raise self._commit_error(exc) from exc
        self._after_local_commit(space, registry, appends, tuple(deletes))
        return None

    def _run_stamped(
        self, registry: DocRegistry, doc_id: str, document: Any, operation: Callable[[], None]
    ) -> None:
        kind = doc_kind(doc_id)
        root_before = read_space(document) if kind == "meta" else None
        with local_commit(document, kind, self.author, via=self.actor) as stamp:
            registry.stamps[doc_id] = stamp
            try:
                operation()
            finally:
                registry.stamps.pop(doc_id, None)
            if kind == "meta" and read_space(document) != root_before:
                stamp.space_root = True

    @staticmethod
    def _commit_error(exc: Exception) -> OrgFsError:
        if isinstance(exc, OrgFsError):
            return exc
        code = getattr(exc, "code", "invalid-argument")
        details = dict(getattr(exc, "details", {}) or {})
        details.setdefault("message", str(exc))
        return OrgFsError(str(code), details)

    def _record_row(
        self,
        space: _Space,
        node: _Node,
        *,
        stamp: bool = True,
        extra_paths: tuple[str, ...] = (),
    ) -> None:
        """Hook: write ``node``'s row; a stamped write carries the local author."""

        registry = self._registry(space.info.space_id)
        batch = registry.batches.get(node.node_id) if node.deleted else None
        value = row_value(node, batch)
        if stamp:
            self._check_row_write(space, node, extra_paths)
            wire = tree_row_wire(value, author=self.author, via=self.actor or None)
            current = registry.stamps.get("tree")
            if current is None:
                raise RuntimeError("tree rows are written only inside a tree commit")
            current.touch(node.node_id)
            node.modified_by = self.author
            node.modified_via = self.actor
        else:
            existing = space.tree.row(node.node_id) or {}
            wire = tree_row_wire(value, author=row_author(existing), via=row_via(existing))
        space.tree.nodes[node.node_id] = wire
        self._note_node(space, node)

    def _write_tree(
        self,
        space: _Space,
        operation: Callable[[], None],
        affected: Iterable[str],
        changed: str,
        *,
        old_paths: Mapping[str, str | None] | None = None,
        move: tuple[_Node, str, str | None] | None = None,
        broadcast: bool = True,
    ) -> None:
        affected = tuple(affected)

        def apply() -> None:
            operation()
            if move is not None:
                node, name, old_parent = move
                with self._lock:
                    self._clock += 1
                    clock = self._clock
                space.tree.record_move(
                    node,
                    timestamp=clock,
                    peer=self.node_id,
                    old_parent=old_parent,
                    new_parent=node.parent,
                    name=name,
                )
                previous = tuple(path for path in (old_paths or {}).values() if path)
                self._record_row(space, node, extra_paths=previous)
            else:
                for node_id in affected:
                    self._record_row(space, space.nodes[node_id])

        self._commit(space, "tree", apply)
        self._finish(space, affected, changed, old_paths=old_paths)

    def _recover_after_failure(
        self, space: _Space, registry: DocRegistry, doc_ids: tuple[str, ...]
    ) -> None:
        try:
            registry.reload(doc_ids)
        except Exception:
            self._reopen_after_broken(space)
            return
        self._rematerialize(space)

    def _reopen_after_broken(self, space: _Space) -> None:
        """Design §2.5: drop every instance, reopen the store, rebuild from ``load()``."""

        space_id = space.info.space_id
        with self._lock:
            registry = self._registries.get(space_id)
        if registry is None:
            return
        try:
            registry.store.close()
        except Exception as exc:
            self._log("warn", "orgfs.state.close-failed", spaceId=space_id, error=str(exc))
        try:
            registry.reset(self._store_factory(space_id, False))
        except Exception as exc:
            registry.broken = True
            self._log("error", "orgfs.state.reopen-failed", spaceId=space_id, error=str(exc))
            return
        self._log("warn", "orgfs.state.reopened", spaceId=space_id)
        self._rematerialize(space)
        for node in space.nodes.values():
            if node.kind == "doc" and node.doc_id:
                registry.mark(node.doc_id, None)

    def _after_local_commit(
        self,
        space: _Space,
        registry: DocRegistry,
        appends: tuple[tuple[str, bytes], ...],
        deletes: tuple[str, ...],
    ) -> None:
        registry.generation += 1
        doc_ids = [doc_id for doc_id, _update in appends]
        for doc_id, update in appends:
            if doc_id == "meta":
                registry.staged_meta.apply(update)
            elif doc_id == "tree":
                registry.staged_tree.apply(update)
        registry.refresh_vectors(doc_ids)
        for doc_id in deletes:
            registry.drop(doc_id)
        registry.forget_vectors(list(deletes))
        content = [doc_id for doc_id in doc_ids if doc_id not in ("meta", "tree")]
        if content:
            self._recheck(space, registry, content)
        self._defer_publication(space, registry, appends)
        if "meta" in doc_ids:
            self._refresh_members(space)
            self._notify_meta_watchers(space)
        self._maybe_compact(registry, doc_ids)

    def _defer_publication(
        self, space: _Space, registry: DocRegistry, appends: tuple[tuple[str, bytes], ...]
    ) -> None:
        with self._lock:
            sync = self._syncs.get(space.info.space_id)
        if sync is None and self.mesh is None:
            return
        rank = {"meta": 0, "tree": 1}
        ordered = sorted(appends, key=lambda item: rank.get(item[0], 2))
        items = tuple(
            (doc_id, update, registry.vectors.get(doc_id, b"\x00")) for doc_id, update in ordered
        )
        self._defer_effect(_Publish(space.info.space_id, items))

    def _maybe_compact(self, registry: DocRegistry, doc_ids: list[str]) -> None:
        for doc_id in doc_ids:
            try:
                if registry.store.needs_compaction(doc_id):
                    state = registry.export_and_swap(doc_id)
                    registry.store.compact(doc_id, state)
            except Exception as exc:
                self._log(
                    "warn",
                    "orgfs.state.compact-failed",
                    spaceId=registry.space_id,
                    docId=doc_id,
                    error=str(exc),
                )

    def _log(self, level: str, event: str, **fields: object) -> None:
        try:
            self._logger(level, event, **fields)
        except Exception:
            pass

    def _needs_post_commit_effects(self) -> bool:
        with self._lock:
            attached = bool(self._syncs)
        return attached or self.mesh is not None or super()._needs_post_commit_effects()

    def _submit_post_commit_effects(
        self, operation_id: str, generation: int, effects: tuple[object, ...]
    ) -> bool:
        publications = [effect for effect in effects if isinstance(effect, _Publish)]
        remaining = tuple(effect for effect in effects if not isinstance(effect, _Publish))
        spaces: list[str] = []
        with self._lock:
            for publication in publications:
                queue = self._publish_queues.setdefault(
                    publication.space_id, deque()
                )
                for item in publication.items:
                    queue.append(item)
                    while len(queue) > PUBLISH_QUEUE_LIMIT:
                        queue.popleft()
                        self._publish_dropped[publication.space_id] = (
                            self._publish_dropped.get(publication.space_id, 0) + 1
                        )
                if publication.space_id not in spaces:
                    spaces.append(publication.space_id)
        for space_id in spaces:
            self.request_broadcast_drain(space_id)
        return super()._submit_post_commit_effects(operation_id, generation, remaining)

    def _space_has_unbroadcast(self, space_id: str) -> bool:
        with self._lock:
            return bool(self._publish_queues.get(space_id))

    def _execute_network_effect(self, space_id: str) -> _FacadeEffectResult:
        failures: list[str] = []
        with self._lock:
            detached = space_id in self._detached_spaces
            queue = self._publish_queues.get(space_id)
            items = tuple(queue) if queue else ()
            if queue:
                queue.clear()
            sync = self._syncs.get(space_id)
        if self._network_effect_stop.is_set() or detached:
            return _FacadeEffectResult(())
        if sync is None and self.mesh is not None and hasattr(self.mesh, "_mesh"):
            try:
                sync = self.mesh._mesh(space_id)
            except Exception as exc:
                failures.append(type(exc).__name__)
        if sync is None:
            return _FacadeEffectResult(tuple(failures))
        for doc_id, update, vector in items:
            try:
                sync.publish(doc_id, update, vector)
            except Exception as exc:
                failures.append(type(exc).__name__)
        return _FacadeEffectResult(tuple(failures))

    def publish_dropped(self, space_id: str) -> int:
        """Publications dropped from the bounded queue of ``space_id``."""

        with self._lock:
            return self._publish_dropped.get(space_id, 0)

    def detach_space(self, space_id: str, timeout: float = 5.0) -> bool:
        if not super().detach_space(space_id, timeout):
            return False
        with self._lock:
            registry = self._registries.pop(space_id, None)
            self._syncs.pop(space_id, None)
            self._publish_queues.pop(space_id, None)
        if registry is not None:
            registry.store.close()
        return True

    def close_effects(self, timeout: float = 5.0) -> bool:
        closed = super().close_effects(timeout)
        if closed:
            with self._lock:
                registries = tuple(self._registries.values())
            for registry in registries:
                try:
                    registry.store.close()
                except Exception:
                    pass
        return closed


__all__ = ["StateCommit", "_Publish", "row_value"]
