# Decisions:
# - Design §6: ``remove`` puts the node in trash, ``restore`` brings back the
#   node's last state; ``restore(version)`` disappears in phase D, so tests
#   call ``orgfs_impl.restore_last`` only.  Migrated from
#   storage/tree/test_orgfs_tree.py (``test_public_tree_api_restore_and_change_events``,
#   ``test_restore_recovers_deleted_parent_chain``,
#   ``test_directory_restore_respects_recursive_flag``); the cases that
#   restore an *earlier* version (``test_restore_reverts_rename_and_move_to_earlier_version``,
#   snapshot-cycle corruption) are history semantics the design removes.
"""remove, trash and restore contracts."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.orgfs import OrgFsError


def test_remove_moves_a_document_to_trash_and_hides_it(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    created = fs.write_text(space.space_id, "note.md", "text")

    fs.remove(space.space_id, "note.md")

    trash = fs.trash(space.space_id)
    assert [(entry.node_id, entry.deleted) for entry in trash] == [(created.node_id, True)]
    assert fs.listdir(space.space_id, "") == ()
    for action in (
        lambda: fs.read_text(space.space_id, f"id:{created.node_id}"),
        lambda: fs.stat(space.space_id, "note.md"),
    ):
        with pytest.raises(OrgFsError) as error:
            action()
        assert error.value.code == "unknown-doc"


def test_restore_brings_back_the_last_state(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    fs.mkdir(space.space_id, "folder")
    created = fs.write_text(space.space_id, "draft.md", "v1")
    fs.write_text(space.space_id, "draft.md", "v2")
    fs.move(space.space_id, "draft.md", "folder/final.md")
    fs.remove(space.space_id, "folder/final.md")

    restored = orgfs_impl.restore_last(fs, space.space_id, f"id:{created.node_id}")

    assert (restored.node_id, restored.path, restored.deleted) == (
        created.node_id,
        "folder/final.md",
        False,
    )
    assert fs.read_text(space.space_id, "folder/final.md")[0] == "v2"
    assert fs.trash(space.space_id) == ()


def test_restoring_a_binary_node_restores_its_bytes(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    created = fs.write_bytes(space.space_id, "data.bin", b"payload")
    fs.remove(space.space_id, "data.bin")

    orgfs_impl.restore_last(fs, space.space_id, f"id:{created.node_id}")

    assert fs.read_bytes(space.space_id, "data.bin") == b"payload"


def test_removing_a_directory_trashes_its_subtree(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    directory = fs.mkdir(space.space_id, "directory")
    first = fs.write_text(space.space_id, "directory/first.txt", "one")
    second = fs.write_text(space.space_id, "directory/second.txt", "two")

    fs.remove(space.space_id, f"id:{directory.node_id}")

    assert {entry.node_id for entry in fs.trash(space.space_id)} == {
        directory.node_id,
        first.node_id,
        second.node_id,
    }
    assert fs.listdir(space.space_id, "") == ()


def test_directory_restore_respects_recursive_flag(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    directory = fs.mkdir(space.space_id, "directory")
    first = fs.write_text(space.space_id, "directory/first.txt", "one")
    second = fs.write_text(space.space_id, "directory/second.txt", "two")
    fs.remove(space.space_id, f"id:{directory.node_id}")

    orgfs_impl.restore_last(fs, space.space_id, f"id:{directory.node_id}", recursive=False)
    assert fs.listdir(space.space_id, f"id:{directory.node_id}") == ()
    assert {entry.node_id for entry in fs.trash(space.space_id)} == {first.node_id, second.node_id}

    orgfs_impl.restore_last(fs, space.space_id, f"id:{first.node_id}")
    orgfs_impl.restore_last(fs, space.space_id, f"id:{second.node_id}")
    assert {item.node_id for item in fs.listdir(space.space_id, "directory")} == {
        first.node_id,
        second.node_id,
    }
    assert fs.read_text(space.space_id, f"id:{first.node_id}")[0] == "one"
    assert fs.read_text(space.space_id, f"id:{second.node_id}")[0] == "two"


@pytest.mark.legacy_xfail(
    reason="design §6: restore(recursive=True) restores the descendants removed with the "
    "node; legacy restore(version) with the trash-listed version only restores the node"
)
def test_recursive_directory_restore_brings_back_the_subtree(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    directory = fs.mkdir(space.space_id, "directory")
    first = fs.write_text(space.space_id, "directory/first.txt", "one")
    fs.remove(space.space_id, f"id:{directory.node_id}")

    orgfs_impl.restore_last(fs, space.space_id, f"id:{directory.node_id}", recursive=True)

    assert [item.node_id for item in fs.listdir(space.space_id, "directory")] == [first.node_id]
    assert fs.trash(space.space_id) == ()


def test_restoring_a_child_restores_its_deleted_parent_chain(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    parent = fs.mkdir(space.space_id, "parent")
    child = fs.write_text(space.space_id, "parent/child.txt", "content")
    fs.remove(space.space_id, f"id:{parent.node_id}")

    restored = orgfs_impl.restore_last(fs, space.space_id, f"id:{child.node_id}")

    assert restored.path == "parent/child.txt"
    assert fs.stat(space.space_id, f"id:{parent.node_id}").deleted is False
    assert fs.read_text(space.space_id, "parent/child.txt")[0] == "content"


@pytest.mark.parametrize(("ref", "code"), [("", "invalid-argument"), ("missing.md", "unknown-doc")])
def test_remove_of_root_or_missing_node_is_refused(orgfs_impl, ref: str, code: str) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    fs.write_text(space.space_id, "kept.md", "kept")

    with pytest.raises(OrgFsError) as error:
        fs.remove(space.space_id, ref)

    assert error.value.code == code
    assert fs.read_text(space.space_id, "kept.md")[0] == "kept"
    assert fs.trash(space.space_id) == ()


def test_trash_and_restore_survive_reopen(orgfs_impl, tmp_path) -> None:
    node = orgfs_impl.make_runtime(tmp_path / "node", "node-a", "user:owner")
    space = node.facade.create_space("team")
    created = node.facade.write_text(space.space_id, "note.md", "kept text")
    node.facade.remove(space.space_id, "note.md")

    reopened = node.reopen()
    assert [entry.node_id for entry in reopened.trash(space.space_id)] == [created.node_id]
    orgfs_impl.restore_last(reopened, space.space_id, f"id:{created.node_id}")

    reopened = node.reopen()
    assert reopened.read_text(space.space_id, "note.md")[0] == "kept text"
    assert reopened.trash(space.space_id) == ()
