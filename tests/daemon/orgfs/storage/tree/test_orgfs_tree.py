# Decisions:
# - Phase D T1: ``test_public_tree_api_restore_and_change_events`` and
#   ``test_directory_restore_respects_recursive_flag`` restored an earlier body
#   by ``version`` (design §6 removed that); they were deleted, not rewritten.
#   Their user-visible parts are the contracts
#   ``content/test_cas.py::test_base_version_current_is_a_plain_overwrite``,
#   ``tree/test_trash.py::test_restore_brings_back_the_last_state``,
#   ``tree/test_trash.py::test_directory_restore_respects_recursive_flag``,
#   ``tree/test_trash.py::test_recursive_directory_restore_brings_back_the_subtree``
#   and ``watch/test_watch.py::test_watch_reports_every_change_kind_in_order``.
from __future__ import annotations


import pytest

from hyprial.daemon.impl.orgfs import LocalOrgFs, OrgFsError, TreeDocument


def test_ambiguous_paths_require_stable_id() -> None:
    fs = LocalOrgFs(author="user:alice")
    space = fs.create_space("shared")
    fs.mkdir(space.space_id, "a")
    first = fs.write_text(space.space_id, "a/same.txt", "one")
    second = fs.write_text(space.space_id, "a/other.txt", "two")
    fs.move(space.space_id, f"id:{second.node_id}", "a/same.txt")

    candidates = fs.resolve(space.space_id, "a/same.txt")
    assert {item.node_id for item in candidates} == {first.node_id, second.node_id}
    with pytest.raises(OrgFsError) as error:
        fs.read_text(space.space_id, "a/same.txt")
    assert error.value.code == "ambiguous-path"
    assert set(error.value.details["candidates"]) == {first.node_id, second.node_id}
    assert fs.read_text(space.space_id, f"id:{first.node_id}")[0] == "one"


def test_id_anchored_child_ref_bypasses_ambiguous_parent_paths() -> None:
    fs = LocalOrgFs(author="user:alice")
    space = fs.create_space("shared")
    folder = fs.mkdir(space.space_id, "records")

    created = fs.write_text(
        space.space_id, f"id:{folder.node_id}/entry.json", "one"
    )

    assert created.path == "records/entry.json"
    assert fs.read_text(space.space_id, f"id:{created.node_id}")[0] == "one"
    assert fs.read_text(
        space.space_id, f"id:{folder.node_id}/entry.json"
    )[0] == "one"


def test_path_constraints_and_stale_write() -> None:
    fs = LocalOrgFs(author="user:alice")
    space = fs.create_space("shared")
    with pytest.raises(OrgFsError, match="invalid-argument"):
        fs.mkdir(space.space_id, "../escape")
    with pytest.raises(OrgFsError):
        fs.mkdir(space.space_id, "/absolute")
    fs.write_text(space.space_id, "file.txt", "one")
    with pytest.raises(OrgFsError) as error:
        fs.write_text(space.space_id, "file.txt", "two", expect_version="stale")
    assert error.value.code == "stale-write"
    assert fs.read_text(space.space_id, "file.txt")[0] == "one"


def test_move_log_replay_skips_cycle() -> None:
    first = TreeDocument(client_id=1)
    first.record_node(
        type("Node", (), {"node_id": "a", "parent": "root", "name": "a", "kind": "dir", "doc_id": None, "blob_hash": None, "deleted": False})()
    )
    first.record_node(
        type("Node", (), {"node_id": "b", "parent": "root", "name": "b", "kind": "dir", "doc_id": None, "blob_hash": None, "deleted": False})()
    )
    first.record_move(type("Node", (), {"node_id": "a", "parent": "b", "name": "a"})(), timestamp=1, peer="a", old_parent="root", new_parent="b", name="a")
    first.record_move(type("Node", (), {"node_id": "b", "parent": "a", "name": "b"})(), timestamp=2, peer="b", old_parent="root", new_parent="a", name="b")
    assert first.has_cycle() is False
    projected = first.materialize()
    assert projected["a"]["parent"] == "b"
    assert projected["b"]["parent"] == "root"


def test_restore_recovers_deleted_parent_chain() -> None:
    fs = LocalOrgFs(author="user:alice")
    space = fs.create_space("shared")
    parent = fs.mkdir(space.space_id, "parent")
    child = fs.write_text(space.space_id, "parent/child.txt", "content")
    fs.remove(space.space_id, f"id:{parent.node_id}")
    fs.restore(space.space_id, f"id:{child.node_id}")
    assert fs.stat(space.space_id, f"id:{parent.node_id}").deleted is False
    assert fs.read_text(space.space_id, "parent/child.txt")[0] == "content"


def test_path_negatives_reject_empty_segments_unknown_ids_and_cross_space_move() -> None:
    """Review item 15l: path and same-space fences fail before mutation."""

    fs = LocalOrgFs(author="user:alice")
    left = fs.create_space("left")
    right = fs.create_space("right")
    source = fs.write_text(left.space_id, "source.txt", "left")
    foreign = fs.mkdir(right.space_id, "foreign")

    with pytest.raises(OrgFsError) as empty_segment:
        fs.write_text(left.space_id, "bad//name.txt", "bad")
    assert empty_segment.value.code == "invalid-argument"
    with pytest.raises(OrgFsError) as missing_id:
        fs.write_text(left.space_id, "id:does-not-exist", "bad")
    assert missing_id.value.code == "unknown-doc"
    with pytest.raises(OrgFsError) as cross_space:
        fs.move(left.space_id, f"id:{source.node_id}", f"id:{foreign.node_id}")
    assert cross_space.value.code == "unknown-doc"
    assert fs.stat(left.space_id, f"id:{source.node_id}").path == "source.txt"
