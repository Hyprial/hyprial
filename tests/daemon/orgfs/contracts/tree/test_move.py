# Decisions:
# - Move semantics are kept unchanged by the rebuild (plan §2, design §7
#   last paragraph: TreeDocument move, cycle protection and name conflict).
#   The concurrent-cycle unit (``test_move_log_replay_skips_cycle``) drives
#   TreeDocument directly and stays a unit test; the facade-visible cycle
#   refusal is asserted here.
# - Phase F4 (real-node finding R2-3, design §9.2 "准入规则的补充"): in an org
#   directory space a move whose node, old parent or new parent is a
#   protected directory node is refused with ``protected-path`` and the shape
#   every other protected-path refusal has (``details == {"path": ...}``,
#   message ``protected path: <path>``), not a bare ``not-a-member``.  ``path`` is the protected path the first such id
#   encodes, in the order node, old parent, new parent.  Renaming inside a
#   protected directory stays refused: protected ids are derived from the
#   path, so a moved row would no longer match its id.
"""move, rename and cycle-protection contracts."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs import OrgFsError
from hyprial.identity import DirectoryDevice


def test_rename_keeps_identity_and_content(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    created = fs.write_text(space.space_id, "old.md", "content")

    moved = fs.move(space.space_id, "old.md", "new.md")

    assert (moved.node_id, moved.path, moved.name) == (created.node_id, "new.md", "new.md")
    assert fs.read_text(space.space_id, "new.md")[0] == "content"
    with pytest.raises(OrgFsError) as error:
        fs.stat(space.space_id, "old.md")
    assert error.value.code == "unknown-doc"


def test_move_into_a_directory_and_by_id(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    folder = fs.mkdir(space.space_id, "folder")
    created = fs.write_text(space.space_id, "a.md", "a")

    into = fs.move(space.space_id, f"id:{created.node_id}", f"id:{folder.node_id}")
    assert into.path == "folder/a.md"
    renamed = fs.move(space.space_id, "folder/a.md", "folder/b.md")
    assert renamed.path == "folder/b.md"
    assert [node.path for node in fs.listdir(space.space_id, "folder")] == ["folder/b.md"]


def test_moving_a_directory_carries_its_subtree(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    fs.mkdir(space.space_id, "src")
    fs.mkdir(space.space_id, "src/inner")
    leaf = fs.write_text(space.space_id, "src/inner/leaf.md", "leaf")
    fs.mkdir(space.space_id, "dst")

    fs.move(space.space_id, "src", "dst/src")

    assert fs.stat(space.space_id, f"id:{leaf.node_id}").path == "dst/src/inner/leaf.md"
    assert fs.read_text(space.space_id, "dst/src/inner/leaf.md")[0] == "leaf"


@pytest.mark.parametrize("destination", ["q/r/q", "q/r", "q"])
def test_move_into_its_own_subtree_is_refused(orgfs_impl, destination: str) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    outer = fs.mkdir(space.space_id, "q")
    inner = fs.mkdir(space.space_id, "q/r")

    with pytest.raises(OrgFsError) as error:
        fs.move(space.space_id, "q", destination)

    assert error.value.code == "invalid-argument"
    assert fs.stat(space.space_id, f"id:{outer.node_id}").path == "q"
    assert fs.stat(space.space_id, f"id:{inner.node_id}").path == "q/r"


@pytest.mark.parametrize(
    ("source", "destination", "code"),
    [
        ("missing.md", "x.md", "unknown-doc"),
        ("a.md", "missing/x.md", "unknown-doc"),
        ("a.md", "bad//x.md", "invalid-argument"),
    ],
)
def test_bad_moves_fail_without_mutation(orgfs_impl, source: str, destination: str, code: str) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    fs.write_text(space.space_id, "a.md", "a")
    before = fs.listdir(space.space_id, "", recursive=True)

    with pytest.raises(OrgFsError) as error:
        fs.move(space.space_id, source, destination)

    assert error.value.code == code
    assert fs.listdir(space.space_id, "", recursive=True) == before


def _org_directory_with_a_device(orgfs_impl):
    fs = orgfs_impl.make_facade("user:owner@example")
    space = fs.create_space("group-acme")
    store = OrgFsDirectoryStore(fs)
    store.ensure_org_directory_roots("group-acme")
    store.put_device(
        "group-acme",
        DirectoryDevice(
            device_id="node-a",
            owner="owner@example",
            server_public="nodekey:server-node-a",
            client_public="nodekey:client-node-a",
            address="tcp://127.0.0.1:7447#node-a",
            key_generation=1,
            updated_at="2026-11-25T00:00:00+00:00",
        ),
    )
    return fs, space.space_id


@pytest.mark.parametrize(
    "destination",
    [
        "directory/devices/owner@example/renamed.json",
        "directory/devices/node-a.json",
        "moved-out.json",
    ],
)
def test_a_move_touching_a_protected_directory_node_is_protected_path(orgfs_impl, destination: str) -> None:
    fs, space_id = _org_directory_with_a_device(orgfs_impl)
    source = "directory/devices/owner@example/node-a.json"
    before = fs.stat(space_id, source)

    with pytest.raises(OrgFsError) as error:
        fs.move(space_id, source, destination)

    assert error.value.code == "protected-path"
    assert error.value.details == {"path": source}
    assert f"protected path: {source}" in str(error.value)
    assert fs.stat(space_id, source).node_id == before.node_id


def test_a_plain_node_moved_into_a_protected_directory_is_protected_path(orgfs_impl) -> None:
    fs, space_id = _org_directory_with_a_device(orgfs_impl)
    fs.write_text(space_id, "loose.json", "{}")

    with pytest.raises(OrgFsError) as error:
        fs.move(space_id, "loose.json", "directory/devices/owner@example/loose.json")

    assert error.value.code == "protected-path"
    assert error.value.details == {"path": "directory/devices/owner@example"}
    assert "protected path: directory/devices/owner@example" in str(error.value)
    assert fs.read_text(space_id, "loose.json")[0] == "{}"
