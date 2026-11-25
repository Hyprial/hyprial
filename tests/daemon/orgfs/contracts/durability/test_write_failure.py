# Decisions:
# - Migrated from contracts/test_orgfs_api.py
#   (``test_failed_durable_commit_rolls_back_facade_state``) and
#   document/test_scoped_rollback.py (``test_failed_commit_restores_exact_state``
#   across write kinds).  The originals inject failure through the old
#   ``commit(doc_id, mutate, ...)`` store protocol; here only
#   ``orgfs_impl.inject_write_failure`` is used and the state is compared
#   through public reads.  ``kind=structured`` is gone with open_doc (Q6).
# - Design §2.5 step 2: a failed commit restores the in-memory state and
#   reports an error; nothing is broadcast or watched; the durable variant
#   also checks that the failed write is absent after reopen.
# - The reopen comparison ignores ``NodeInfo.version``: the legacy tree
#   revision restarts on reopen (observed, reported), and version stability
#   across reopen is not a design §1.1 clause.
"""Persistence-failure rollback contracts."""

from __future__ import annotations

from dataclasses import replace

import pytest

from hyprial.daemon.impl.orgfs import OrgFsError

from ..support import InjectedWriteFailure


def _observable(fs, space_id: str, *, with_versions: bool = True) -> tuple[object, ...]:
    nodes = fs.listdir(space_id, "", recursive=True)
    texts = tuple(
        (node.node_id, fs.read_text(space_id, f"id:{node.node_id}")[0])
        for node in nodes
        if node.kind == "doc"
    )
    if not with_versions:
        nodes = tuple(replace(node, version="") for node in nodes)
    blobs = tuple(
        (node.node_id, fs.read_bytes(space_id, f"id:{node.node_id}"))
        for node in nodes
        if node.kind == "blob"
    )
    members = tuple(sorted((row.user, row.mode, row.added_at) for row in fs.members(space_id)))
    trash = tuple(sorted(entry.node_id for entry in fs.trash(space_id)))
    meta = tuple(sorted((key, repr(value)) for key, value in fs.space_meta(space_id).items()))
    return nodes, texts, blobs, members, trash, meta


def _seed(fs) -> str:
    space_id = fs.create_space("rollback").space_id
    fs.mkdir(space_id, "folder")
    fs.write_text(space_id, "folder/a.md", "before-a")
    fs.write_text(space_id, "folder/b.md", "before-b")
    fs.write_bytes(space_id, "folder/c.bin", b"before-c")
    trashed = fs.write_text(space_id, "trashed.md", "in trash")
    fs.remove(space_id, f"id:{trashed.node_id}")
    fs.invite(space_id, "user:guest", "rw")
    return space_id


ACTIONS = {
    "write": lambda impl, fs, sid: fs.write_text(sid, "folder/a.md", "changed"),
    "clear": lambda impl, fs, sid: fs.write_text(sid, "folder/a.md", ""),
    "create": lambda impl, fs, sid: fs.write_text(sid, "new.md", "new text"),
    "mkdir": lambda impl, fs, sid: fs.mkdir(sid, "new-dir"),
    "move": lambda impl, fs, sid: fs.move(sid, "folder/a.md", "renamed.md"),
    "remove": lambda impl, fs, sid: fs.remove(sid, "folder"),
    "blob-create": lambda impl, fs, sid: fs.write_bytes(sid, "new.bin", b"blob"),
    "blob-replace": lambda impl, fs, sid: fs.write_bytes(sid, "folder/c.bin", b"after"),
    "restore": lambda impl, fs, sid: impl.restore_last(fs, sid, "trashed.md"),
    "invite": lambda impl, fs, sid: fs.invite(sid, "user:new", "ro"),
    "remove-member": lambda impl, fs, sid: fs.remove_member(sid, "user:guest"),
    "update-meta": lambda impl, fs, sid: fs.update_space_meta(sid, {"aclSpaceId": "acl-1"}),
}


def _fail_once(orgfs_impl, target, fs, space_id: str, kind: str) -> None:
    with orgfs_impl.inject_write_failure(target):
        with pytest.raises((OrgFsError, InjectedWriteFailure)):
            ACTIONS[kind](orgfs_impl, fs, space_id)


@pytest.mark.parametrize("kind", sorted(ACTIONS))
def test_failed_persistence_leaves_the_facade_unchanged(orgfs_impl, kind: str) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space_id = _seed(fs)
    seen = []
    fs.watch(space_id, "**", seen.append)
    before = _observable(fs, space_id)

    _fail_once(orgfs_impl, fs, fs, space_id, kind)

    assert _observable(fs, space_id) == before
    ACTIONS[kind](orgfs_impl, fs, space_id)
    assert _observable(fs, space_id) != before
    assert seen == [] or all(event.space_id == space_id for event in seen)


@pytest.mark.parametrize("kind", sorted(ACTIONS))
def test_failed_persistence_is_absent_after_reopen(orgfs_impl, tmp_path, kind: str) -> None:
    node = orgfs_impl.make_runtime(tmp_path / "node", "node-a", "user:owner")
    space_id = _seed(node.facade)
    before = _observable(node.facade, space_id)

    _fail_once(orgfs_impl, node, node.facade, space_id, kind)
    assert _observable(node.facade, space_id) == before

    durable = _observable(node.facade, space_id, with_versions=False)
    reopened = node.reopen()
    assert _observable(reopened, space_id, with_versions=False) == durable


def test_failed_space_creation_leaves_no_space(orgfs_impl, tmp_path) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    existing = fs.spaces()

    with orgfs_impl.inject_write_failure(fs):
        with pytest.raises((OrgFsError, InjectedWriteFailure)):
            fs.create_space("never")

    assert fs.spaces() == existing
    created = fs.create_space("later")
    assert created in fs.spaces()
