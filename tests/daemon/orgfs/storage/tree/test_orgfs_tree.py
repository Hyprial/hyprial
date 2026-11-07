from __future__ import annotations

from dataclasses import replace
import threading

import pytest

from hyprial.daemon.impl.orgfs import LocalOrgFs, OrgFsError, TreeDocument


def test_public_tree_api_restore_and_history() -> None:
    fs = LocalOrgFs(author="user:alice", actor="agent:alice:mac:pytest")
    space = fs.create_space("shared")
    fs.mkdir(space.space_id, "notes")
    first = fs.write_text(space.space_id, "notes/readme.md", "before")
    original_version = first.version

    second = fs.write_text(space.space_id, "notes/readme.md", "after", base_version=original_version)
    assert fs.read_text(space.space_id, "notes/readme.md") == ("after", second.version)
    fs.remove(space.space_id, "notes/readme.md")
    assert fs.trash(space.space_id)[0].deleted is True
    restored = fs.restore(space.space_id, f"id:{first.node_id}", original_version)

    assert restored.path == "notes/readme.md"
    assert fs.read_text(space.space_id, f"id:{first.node_id}")[0] == "before"
    assert {entry.changed for entry in fs.history(space.space_id, f"id:{first.node_id}")} >= {
        "created",
        "content",
        "removed",
        "restored",
    }


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
    fs.restore(space.space_id, f"id:{child.node_id}", child.version)
    assert fs.stat(space.space_id, f"id:{parent.node_id}").deleted is False
    assert fs.read_text(space.space_id, "parent/child.txt")[0] == "content"


def test_restore_reverts_rename_and_move_to_earlier_version() -> None:
    """Review item 15l: restore returns both the earlier name and parent."""

    fs = LocalOrgFs(author="user:alice")
    space = fs.create_space("shared")
    fs.mkdir(space.space_id, "before")
    fs.mkdir(space.space_id, "after")
    original = fs.write_text(space.space_id, "before/name.txt", "content")
    fs.move(space.space_id, f"id:{original.node_id}", "after/renamed.txt")

    restored = fs.restore(space.space_id, f"id:{original.node_id}", original.version)
    assert restored.path == "before/name.txt"
    assert restored.name == "name.txt"
    assert fs.read_text(space.space_id, "before/name.txt")[0] == "content"


def test_directory_restore_respects_recursive_flag() -> None:
    """Review item 15l: directory restore covers recursive=False and True."""

    fs = LocalOrgFs(author="user:alice")
    space = fs.create_space("shared")
    directory = fs.mkdir(space.space_id, "directory")
    first = fs.write_text(space.space_id, "directory/first.txt", "one")
    second = fs.write_text(space.space_id, "directory/second.txt", "two")
    subtree_version = second.version
    fs.remove(space.space_id, f"id:{directory.node_id}")

    fs.restore(space.space_id, f"id:{directory.node_id}", subtree_version, recursive=False)
    assert fs.listdir(space.space_id, f"id:{directory.node_id}") == ()
    assert {item.node_id for item in fs.trash(space.space_id)} >= {
        first.node_id,
        second.node_id,
    }

    fs.restore(space.space_id, f"id:{directory.node_id}", subtree_version, recursive=True)
    assert {item.node_id for item in fs.listdir(space.space_id, "directory")} == {
        first.node_id,
        second.node_id,
    }
    assert fs.read_text(space.space_id, f"id:{first.node_id}")[0] == "one"
    assert fs.read_text(space.space_id, f"id:{second.node_id}")[0] == "two"


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


def test_restore_to_empty_is_atomic_and_repeated_restore_is_noop(tmp_path, request):
    """review-p1-r2 D.1/R8: restore-to-empty is one atomic batch and empty-to-empty is a no-op."""

    from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime

    runtime = OrgFsRuntime(
        tmp_path / "r8-state", node_id="node-owner", author="user:owner"
    )
    request.addfinalizer(runtime.close)
    fs = runtime.facade
    space = fs.create_space("team")
    fs.mkdir(space.space_id, "before")
    fs.mkdir(space.space_id, "after")
    original = fs.write_text(space.space_id, "before/note.md", "")
    fs.move(space.space_id, f"id:{original.node_id}", "after/renamed.md")
    fs.write_text(space.space_id, f"id:{original.node_id}", "filled")
    store = runtime.stores.get(space.space_id)
    before_rows = store._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
    store._db.execute(
        "CREATE TRIGGER fail_restore_tree BEFORE INSERT ON commits "
            f"WHEN NEW.doc_id = '{fs._space(space.space_id).tree_doc_id}' "
            "BEGIN SELECT RAISE(FAIL, 'restore-tree'); END"
    )

    with pytest.raises(OrgFsError, match="restore-tree"):
        fs.restore(space.space_id, f"id:{original.node_id}", original.version)
    assert fs.read_text(space.space_id, f"id:{original.node_id}")[0] == "filled"
    assert fs.stat(space.space_id, f"id:{original.node_id}").path == "after/renamed.md"
    assert store._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == before_rows

    store._db.execute("DROP TRIGGER fail_restore_tree")
    restored = fs.restore(space.space_id, f"id:{original.node_id}", original.version)
    assert restored.path == "before/note.md"
    assert fs.read_text(space.space_id, f"id:{original.node_id}")[0] == ""
    after_restore_rows = store._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
    fs.restore(space.space_id, f"id:{original.node_id}", original.version)
    assert store._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == after_restore_rows


def test_restore_snapshot_parent_cycle_fails_instead_of_looping() -> None:
    """review-p1-r2 m6: restore guards corrupt snapshot parent chains."""

    fs = LocalOrgFs(author="user:alice")
    space_info = fs.create_space("shared")
    node = fs.write_text(space_info.space_id, "note.md", "text")
    space = fs._spaces[space_info.space_id]
    # Versions are read-only copy-on-write layers; corrupt a plain copy.
    snapshots = dict(space.snapshots[node.version])
    target = snapshots[node.node_id]
    snapshots[node.node_id] = replace(target, parent="loop")
    snapshots["loop"] = replace(target, node_id="loop", parent="loop")
    space.snapshots[node.version] = snapshots
    errors: list[OrgFsError] = []

    def run_restore() -> None:
        try:
            fs.restore(space_info.space_id, f"id:{node.node_id}", node.version)
        except OrgFsError as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_restore, daemon=True)
    thread.start()
    thread.join(1)
    assert not thread.is_alive()
    assert errors and errors[0].code == "unknown-doc"
