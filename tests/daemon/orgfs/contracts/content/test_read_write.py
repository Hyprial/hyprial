# Decisions:
# - Migrated from tests/daemon/orgfs/contracts/test_orgfs_api.py (recursive
#   listdir, 4 MiB text limit) and storage/tree/test_orgfs_tree.py (id-anchored
#   child refs, path negatives); the originals stay until phase E.
# - Only facade-visible results are asserted: returned NodeInfo, reads,
#   listdir and error codes.
"""Text read/write contracts of the OrgFS facade."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.orgfs import OrgFsError
from hyprial.daemon.impl.orgfs.document.model import ORGFS_TEXT_MAX


def test_written_text_reads_back_with_the_returned_version(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    fs.mkdir(space.space_id, "notes")

    created = fs.write_text(space.space_id, "notes/readme.md", "hello")

    assert (created.kind, created.path, created.name) == ("doc", "notes/readme.md", "readme.md")
    assert created.modified_by == "user:owner"
    assert fs.read_text(space.space_id, "notes/readme.md") == ("hello", created.version)
    assert fs.read_text(space.space_id, f"id:{created.node_id}") == ("hello", created.version)
    snapshot = fs.read_text_snapshot(space.space_id, f"id:{created.node_id}")
    assert (snapshot.content, snapshot.version) == ("hello", created.version)
    assert snapshot.node.node_id == created.node_id
    assert fs.stat(space.space_id, "notes").kind == "dir"


def test_overwrite_changes_content_and_version(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    first = fs.write_text(space.space_id, "a.md", "one")

    second = fs.write_text(space.space_id, f"id:{first.node_id}", "two")

    assert second.node_id == first.node_id
    assert second.version != first.version
    assert fs.read_text(space.space_id, "a.md") == ("two", second.version)


def test_recursive_listdir_returns_visible_descendants_in_path_order(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    fs.write_text(space.space_id, "readme.md", "root")
    fs.mkdir(space.space_id, "docs")
    fs.mkdir(space.space_id, "docs/nested")
    fs.write_text(space.space_id, "docs/a.md", "a")
    fs.write_text(space.space_id, "docs/nested/b.md", "b")

    direct = fs.listdir(space.space_id, "")
    assert [node.path for node in direct] == ["docs", "readme.md"]
    recursive = fs.listdir(space.space_id, "", recursive=True)
    assert [(node.path, node.kind) for node in recursive] == [
        ("docs", "dir"),
        ("docs/a.md", "doc"),
        ("docs/nested", "dir"),
        ("docs/nested/b.md", "doc"),
        ("readme.md", "doc"),
    ]


def test_text_over_four_mib_rejects_with_blob_guidance(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")

    with pytest.raises(OrgFsError) as error:
        fs.write_text(space.space_id, "large.txt", "x" * (ORGFS_TEXT_MAX + 1))

    assert error.value.code == "too-large"
    assert error.value.details == {"maxBytes": ORGFS_TEXT_MAX, "use": "blob"}
    with pytest.raises(OrgFsError) as missing:
        fs.stat(space.space_id, "large.txt")
    assert missing.value.code == "unknown-doc"


def test_id_anchored_child_ref_creates_and_reads_under_the_anchor(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    folder = fs.mkdir(space.space_id, "records")

    created = fs.write_text(space.space_id, f"id:{folder.node_id}/entry.json", "one")

    assert created.path == "records/entry.json"
    assert fs.read_text(space.space_id, f"id:{created.node_id}")[0] == "one"
    assert fs.read_text(space.space_id, f"id:{folder.node_id}/entry.json")[0] == "one"


@pytest.mark.parametrize(
    ("ref", "code"),
    [
        ("bad//name.txt", "invalid-argument"),
        ("../escape.txt", "invalid-argument"),
        ("id:does-not-exist", "unknown-doc"),
        ("missing/child.txt", "unknown-doc"),
        ("file.txt/child.txt", "invalid-argument"),
        ("folder", "invalid-argument"),
    ],
)
def test_invalid_write_targets_fail_without_mutation(orgfs_impl, ref: str, code: str) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    fs.write_text(space.space_id, "file.txt", "kept")
    fs.mkdir(space.space_id, "folder")
    before = fs.listdir(space.space_id, "", recursive=True)

    with pytest.raises(OrgFsError) as error:
        fs.write_text(space.space_id, ref, "bad")

    assert error.value.code == code
    assert fs.listdir(space.space_id, "", recursive=True) == before
    assert fs.read_text(space.space_id, "file.txt")[0] == "kept"


def test_reads_of_unknown_space_and_wrong_kind_are_typed(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    fs.write_bytes(space.space_id, "data.bin", b"\x00\x01")
    fs.mkdir(space.space_id, "folder")

    with pytest.raises(OrgFsError) as unknown_space:
        fs.read_text("00000000-0000-0000-0000-000000000000", "a.md")
    assert unknown_space.value.code == "unknown-space"
    with pytest.raises(OrgFsError) as missing:
        fs.read_text(space.space_id, "missing.md")
    assert missing.value.code == "unknown-doc"
    with pytest.raises(OrgFsError) as binary:
        fs.read_text(space.space_id, "data.bin")
    assert binary.value.code == "invalid-argument"
    with pytest.raises(OrgFsError) as directory:
        fs.read_text(space.space_id, "folder")
    assert directory.value.code == "invalid-argument"
