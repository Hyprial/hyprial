# Decisions:
# - Migrated from tests/integration/orgfs/commands/test_orgfs_cli_daemon.py
#   (``test_runtime_restart_rehydrates_current_space_state``) and widened to
#   every facade-visible record: tree, text, bytes, members, meta and trash.
#   Only a normal close is covered here; crash points are C1's (design §7).
# - Design §3.3: ``NodeInfo.modified_by`` reads the tree row's ``author`` and
#   stays the last writer after reopen.  Legacy rebuilds it as the owner on
#   load (operations/spaces.py), so that contract is a legacy_xfail.
"""Normal close / reopen contracts."""

from __future__ import annotations

import pytest

from ..support import eventually


def test_reopen_rehydrates_spaces_tree_and_content(orgfs_impl, tmp_path) -> None:
    node = orgfs_impl.make_runtime(tmp_path / "node", "node-a", "user:owner")
    fs = node.facade
    space = fs.create_space("team")
    fs.mkdir(space.space_id, "docs")
    text = fs.write_text(space.space_id, "docs/readme.md", "durable")
    fs.write_text(space.space_id, f"id:{text.node_id}", "durable v2")
    blob = fs.write_bytes(space.space_id, "docs/data.bin", b"\x00\x01")
    fs.move(space.space_id, "docs/data.bin", "data.bin")
    fs.invite(space.space_id, "user:reader", "ro")
    fs.update_space_meta(space.space_id, {"aclSpaceId": "acl-1"})
    members = {(row.user, row.mode, row.added_at) for row in fs.members(space.space_id)}

    reopened = node.reopen()

    assert [item.space_id for item in reopened.spaces()] == [space.space_id]
    assert reopened.spaces()[0] == space
    assert reopened.read_text(space.space_id, f"id:{text.node_id}")[0] == "durable v2"
    assert reopened.stat(space.space_id, f"id:{text.node_id}").path == "docs/readme.md"
    assert reopened.read_bytes(space.space_id, f"id:{blob.node_id}") == b"\x00\x01"
    assert reopened.stat(space.space_id, f"id:{blob.node_id}").path == "data.bin"
    assert [node.path for node in reopened.listdir(space.space_id, "", recursive=True)] == [
        "data.bin",
        "docs",
        "docs/readme.md",
    ]
    assert {
        (row.user, row.mode, row.added_at) for row in reopened.members(space.space_id)
    } == members
    assert reopened.space_meta(space.space_id)["aclSpaceId"] == "acl-1"


def test_reopen_accepts_further_writes(orgfs_impl, tmp_path) -> None:
    node = orgfs_impl.make_runtime(tmp_path / "node", "node-a", "user:owner")
    space = node.facade.create_space("team")
    node.facade.write_text(space.space_id, "a.md", "one")

    reopened = node.reopen()
    _content, version = reopened.read_text(space.space_id, "a.md")
    reopened.write_text(space.space_id, "a.md", "two", expect_version=version)
    reopened.write_text(space.space_id, "b.md", "new")

    again = node.reopen()
    assert again.read_text(space.space_id, "a.md")[0] == "two"
    assert again.read_text(space.space_id, "b.md")[0] == "new"


def test_reopen_of_an_empty_state_has_no_spaces(orgfs_impl, tmp_path) -> None:
    node = orgfs_impl.make_runtime(tmp_path / "node", "node-a", "user:owner")

    assert node.reopen().spaces() == ()


def test_document_authors_lists_the_local_writer(orgfs_impl, tmp_path) -> None:
    node = orgfs_impl.make_runtime(tmp_path / "node", "node-a", "user:owner")
    space = node.facade.create_space("team")
    node.facade.write_text(space.space_id, "a.md", "one")

    assert node.facade.document_authors(space.space_id, "a.md") == ("user:owner",)
    assert node.reopen().document_authors(space.space_id, "a.md") == ("user:owner",)


@pytest.mark.legacy_xfail(
    reason="design §3.3: modified_by reads the tree row author and survives reopen; "
    "legacy resets it to the space owner when the space is loaded"
)
def test_modified_by_survives_reopen(orgfs_impl, tmp_path) -> None:
    owner = orgfs_impl.make_runtime(tmp_path / "owner", "node-owner", "user:owner")
    member = orgfs_impl.make_runtime(tmp_path / "member", "node-member", "user:member")
    orgfs_impl.connect(owner, member)
    space = owner.facade.create_space("team")
    owner.facade.invite(space.space_id, "user:member", "rw")
    member.facade.join(space.space_id)
    created = member.facade.write_text(space.space_id, "by-member.md", "hello")
    eventually(
        lambda: _assert_modified_by(owner.facade, space.space_id, created.node_id, "user:member")
    )

    reopened = owner.reopen()

    assert reopened.stat(space.space_id, f"id:{created.node_id}").modified_by == "user:member"


def _assert_modified_by(fs, space_id: str, node_id: str, expected: str) -> None:
    assert fs.stat(space_id, f"id:{node_id}").modified_by == expected
