# Decisions:
# - Phase D T1: the R1 property (an rw member cannot start another tree that
#   replaces the space's tree) is driven through the state host's
#   ``SyncHost.apply_remote`` with C2's ``forge_update``; it asserts the
#   listing and the space directory's bytes, before and after a reopen.
# - Package S (2026-11-25): the journal-only tests (physical tree doc ids,
#   retirements, snapshot barriers) were deleted with the journal store.
from __future__ import annotations

import json
import uuid

from pycrdt import Doc, Map

from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.membership import Accepted

from tests.daemon.orgfs.membership.support import forge_update
from tests.daemon.orgfs.runtime_state.support import snapshot


def _space(runtime: OrgFsRuntime):
    info = runtime.facade.create_space("team")
    return info, runtime.stores.get(info.space_id)


def test_r1_rw_member_shadow_tree_update_cannot_touch_the_tree_or_reopen(tmp_path):
    """R1: an rw member's update for another tree-like document changes nothing."""

    state_dir = tmp_path / "r1-state"
    runtime = OrgFsRuntime(state_dir, node_id="node-owner", author="user:owner")
    try:
        info = runtime.facade.create_space("team")
        runtime.facade.invite(info.space_id, "user:rw")
        runtime.facade.mkdir(info.space_id, "kept")
        listing = _listing(runtime.facade, info.space_id)
        space_dir = state_dir / "orgfs" / "spaces" / info.space_id
        files_before = snapshot(space_dir)

        shadow_id = f"tree-{uuid.uuid4()}"

        def shadow(doc: Doc, author: str) -> None:
            doc.get("nodes", type=Map)["root"] = json.dumps(
                {"node_id": "root", "parent": None, "name": "", "kind": "dir",
                 "doc_id": None, "blob_hash": None, "deleted": False, "author": author}
            )
            doc.get("shadow", type=Map)["payload"] = "must-not-persist"

        frame = forge_update(shadow_id, shadow, author="user:rw", client_id=4242)
        outcome = runtime.facade.sync_host(info.space_id).apply_remote(
            frame.doc_id, frame.update, frame.vector, "node-rw"
        )

        assert not isinstance(outcome, Accepted)
        assert _listing(runtime.facade, info.space_id) == listing
        assert snapshot(space_dir) == files_before
    finally:
        runtime.close()

    reopened = OrgFsRuntime(state_dir, node_id="node-owner", author="user:owner")
    try:
        assert _listing(reopened.facade, info.space_id) == listing
        assert reopened.facade.stat(info.space_id, "kept").path == "kept"
        assert not any(shadow_id in str(path) for path in space_dir.rglob("*"))
    finally:
        reopened.close()


def _listing(fs, space_id: str) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (node.node_id, node.path, node.kind)
        for node in fs.listdir(space_id, "", recursive=True)
    )
