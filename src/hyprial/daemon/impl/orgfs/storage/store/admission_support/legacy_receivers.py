# Decision record
# - 2026-11-24 (S3a land): the guard's tree probe copied and parsed the
#   whole tree on every local tree commit (3.6 ms at 500 nodes, 14.1 ms at
#   2000, about 7-10% of a create).  Outside org-directory spaces it now runs
#   only when the tree holds the space's exact legacy receivers node id, a
#   single keyed lookup; this build never writes under that id, so a tree
#   without it cannot be touched by a local commit.  Org-directory spaces keep
#   the probe (they already pay the device-row probe).
# - 2026-11-24 (S3a fix, coordinator ruling P1-2): the legacy receivers
#   protected ids are decode-only and read-only.  An update that writes,
#   relinks, deletes or moves the legacy receivers document or tree row is
#   refused when this node authors it, in every space (``local=True`` from the
#   journal commit paths), and when a peer sends it into an org-directory
#   space.  Peer history in other spaces (leftover personal-* spaces written
#   by older builds) is still admitted: refusing it on import would leave a
#   fresh replica without the legacy commits and stall later commits of the
#   same writer that depend on them.
"""The legacy receivers write guard of one OrgFS space.

Extracted from :mod:`hyprial.daemon.impl.orgfs.storage.store.admission` to
keep that module inside its budget.  These are responsibility methods on the
single ``LocalSpaceStore`` state host; the tree probe reuses the host's
``_new_doc``, ``_doc``, ``_tree_rows`` and ``_tree_move_wires``.
"""

from __future__ import annotations

import json
from collections import Counter

from pycrdt import Doc, Map

from hyprial.identity import (
    legacy_receivers_node_id,
    parse_protected_directory_doc_id,
    parse_protected_directory_node_id,
)

from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _doc_roots

_LEGACY_RECEIVERS_SECTION = "receivers"
_LEGACY_RECEIVERS_PATH = "receivers.json"


def _legacy_receivers_node(node_id: object) -> bool:
    if not isinstance(node_id, str):
        return False
    try:
        identity = parse_protected_directory_node_id(node_id)
    except ValueError:
        return False
    return identity is not None and identity[2] == _LEGACY_RECEIVERS_PATH


def _legacy_receivers_doc(doc_id: object) -> bool:
    if not isinstance(doc_id, str):
        return False
    try:
        identity = parse_protected_directory_doc_id(doc_id)
    except ValueError:
        return False
    return identity is not None and identity[1] == _LEGACY_RECEIVERS_SECTION


class StoreLegacyReceivers:
    """Legacy receivers guard methods on the sole LocalSpaceStore state host."""

    def _tree_holds_legacy_receivers(
        self, doc_id: str, meta: Doc, space_owner: str | None
    ) -> bool:
        """Whether this tree holds the space's legacy receivers row (one keyed lookup)."""

        space = _doc_roots(meta).get("space")
        space_id = space.get("spaceId") if isinstance(space, dict) else None
        if not isinstance(space_id, str) or not space_id or not space_owner:
            return False
        try:
            node_id = legacy_receivers_node_id(space_id, space_owner)
        except ValueError:
            return False
        return node_id in self._doc(doc_id).get("nodes", type=Map)

    def _tree_touches_legacy_receivers(self, baseline: Doc, update: bytes) -> bool:
        """Whether a tree update changes, links or moves a legacy receivers row."""

        probe = self._new_doc("tree-legacy-probe")
        try:
            probe.apply_update(baseline.get_update())
            before = self._tree_rows(probe)
            before_moves = self._tree_move_wires(probe)
            probe.apply_update(update)
            after = self._tree_rows(probe)
            after_moves = self._tree_move_wires(probe)
        except (TypeError, ValueError):
            return False
        changed = {
            node_id
            for node_id in before.keys() | after.keys()
            if before.get(node_id) != after.get(node_id)
        }
        touched = set(changed)
        for raw in (Counter(after_moves) - Counter(before_moves)).elements():
            try:
                move = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(move, dict):
                touched.update(move.get(key) for key in ("node", "oldParent", "newParent"))
        if any(_legacy_receivers_node(node_id) for node_id in touched):
            return True
        return any(
            _legacy_receivers_doc((after.get(node_id) or {}).get("doc_id"))
            for node_id in changed
        )
