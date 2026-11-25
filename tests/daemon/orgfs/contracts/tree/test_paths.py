# Decisions:
# - Migrated from storage/tree/test_orgfs_tree.py
#   (``test_ambiguous_paths_require_stable_id``, path constraints of
#   ``test_path_constraints_and_stale_write``, cross-space half of
#   ``test_path_negatives_reject_empty_segments_unknown_ids_and_cross_space_move``).
#   Originals stay until phase E.
"""mkdir, path resolution and name-conflict contracts."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.orgfs import OrgFsError


def test_mkdir_creates_nested_directories(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")

    outer = fs.mkdir(space.space_id, "outer")
    inner = fs.mkdir(space.space_id, "outer/inner")

    assert (outer.kind, outer.path) == ("dir", "outer")
    assert (inner.kind, inner.path) == ("dir", "outer/inner")
    assert [node.node_id for node in fs.listdir(space.space_id, "outer")] == [inner.node_id]
    assert fs.resolve(space.space_id, "outer/inner") == (inner,)


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a//b", "outer"])
def test_mkdir_rejects_bad_or_existing_paths(orgfs_impl, path: str) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    fs.mkdir(space.space_id, "outer")
    before = fs.listdir(space.space_id, "", recursive=True)

    with pytest.raises(OrgFsError) as error:
        fs.mkdir(space.space_id, path)

    assert error.value.code == "invalid-argument"
    assert fs.listdir(space.space_id, "", recursive=True) == before


def test_mkdir_under_a_missing_parent_is_unknown_doc(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")

    with pytest.raises(OrgFsError) as error:
        fs.mkdir(space.space_id, "missing/child")

    assert error.value.code == "unknown-doc"


def test_name_conflict_is_flagged_and_paths_become_ambiguous(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    fs.mkdir(space.space_id, "a")
    first = fs.write_text(space.space_id, "a/same.txt", "one")
    second = fs.write_text(space.space_id, "a/other.txt", "two")

    moved = fs.move(space.space_id, f"id:{second.node_id}", "a/same.txt")

    assert moved.name_conflict is True
    assert fs.stat(space.space_id, f"id:{first.node_id}").name_conflict is True
    candidates = fs.resolve(space.space_id, "a/same.txt")
    assert {item.node_id for item in candidates} == {first.node_id, second.node_id}
    for action in (
        lambda: fs.read_text(space.space_id, "a/same.txt"),
        lambda: fs.write_text(space.space_id, "a/same.txt", "x"),
    ):
        with pytest.raises(OrgFsError) as error:
            action()
        assert error.value.code == "ambiguous-path"
        assert set(error.value.details["candidates"]) == {first.node_id, second.node_id}
    assert fs.read_text(space.space_id, f"id:{first.node_id}")[0] == "one"
    assert fs.read_text(space.space_id, f"id:{second.node_id}")[0] == "two"


def test_renaming_away_clears_the_name_conflict(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    first = fs.write_text(space.space_id, "same.txt", "one")
    second = fs.write_text(space.space_id, "other.txt", "two")
    fs.move(space.space_id, f"id:{second.node_id}", "same.txt")

    fs.move(space.space_id, f"id:{second.node_id}", "renamed.txt")

    assert fs.stat(space.space_id, f"id:{first.node_id}").name_conflict is False
    assert fs.stat(space.space_id, "same.txt").node_id == first.node_id
    assert fs.stat(space.space_id, "renamed.txt").node_id == second.node_id


def test_cross_space_move_is_unknown_doc_and_leaves_the_source(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    left = fs.create_space("left")
    right = fs.create_space("right")
    source = fs.write_text(left.space_id, "source.txt", "left")
    foreign = fs.mkdir(right.space_id, "foreign")

    with pytest.raises(OrgFsError) as error:
        fs.move(left.space_id, f"id:{source.node_id}", f"id:{foreign.node_id}")

    assert error.value.code == "unknown-doc"
    assert fs.stat(left.space_id, f"id:{source.node_id}").path == "source.txt"
    assert fs.listdir(right.space_id, "foreign") == ()
