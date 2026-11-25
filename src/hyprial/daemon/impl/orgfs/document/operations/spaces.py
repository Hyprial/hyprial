from __future__ import annotations


from collections.abc import Callable, Mapping


from pycrdt import Map




from hyprial.daemon.impl.orgfs.document.model import _MetaWatch, _Space, _facade_locked

class FacadeSpaces:
    """Responsibility methods on the sole LocalOrgFs state host.

    This class never constructs, copies, or persists an independent host.
    """


    @_facade_locked
    def space_meta(self, space_id: str) -> Mapping[str, object]:
        """Return the replicated owner-written ``space`` metadata map."""

        space = self._space(space_id)
        meta = space.meta.get("space", type=Map)
        return dict(meta.to_py() or {})


    @_facade_locked
    def watch_meta(self, space_id: str, callback: Callable[[], None]) -> _MetaWatch:
        watcher = _MetaWatch(callback)
        self._space(space_id).meta_watches.append(watcher)
        with self._lock:
            self._meta_watchers[watcher.watch_id] = watcher
        return watcher


    def _tree_change_events(
        self,
        space: _Space,
        previous: Mapping[str, tuple],
        old_paths: Mapping[str, str | None],
        attribution: tuple[str, str | None],
    ) -> None:
        """Finish every node whose tree row changed against ``previous``.

        A cross-node birth must materialize as one ``created`` version that
        already carries its content.  The tree update can be projected before
        the sibling content update (sync pages flatten documents in
        meta/tree/content order), so the birth is held in ``birth_pending``
        until the local content document covers the frontier the origin
        recorded at creation; while it waits, intermediate tree changes of
        that node produce no entries so the chain never interleaves.
        """

        origin_author, origin_actor = attribution
        for node_id, node in space.nodes.items():
            current = (
                node.parent,
                node.name,
                node.kind,
                node.blob_hash,
                node.deleted,
            )
            if previous.get(node_id) == current:
                continue
            if node_id in space.birth_pending:
                if node.deleted and not previous[node_id][4]:
                    space.birth_pending.pop(node_id, None)
                    changed = "removed"
                else:
                    continue
            elif node_id not in previous:
                changed = "created"
                if self._remote_birth_content_pending(space, node):
                    space.birth_pending[node_id] = (
                        origin_author,
                        origin_actor,
                    )
                    continue
            elif node.deleted and not previous[node_id][4]:
                changed = "removed"
            elif (node.parent, node.name) != previous[node_id][:2]:
                changed = "moved"
            else:
                changed = "content"
            self._finish(
                space,
                [node_id],
                changed,
                old_paths={node_id: old_paths.get(node_id)},
                author=origin_author,
                actor=origin_actor,
            )


