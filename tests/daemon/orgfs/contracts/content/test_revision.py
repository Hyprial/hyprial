# Decisions:
# - Design §1.1: ``read_text_snapshot().content_revision`` is stable across a
#   normal close/reopen and changes on every write, including a write that
#   restores an earlier value (ABA).  The migration fence depends on it
#   (triage §5.3 item 12).  The only previous test needed FileSpaceStore
#   (tests/daemon/org_membership/migration/test_revision.py).
"""``content_revision`` contracts."""

from __future__ import annotations


def test_content_revision_changes_on_every_write_including_aba(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    fs.write_text(space.space_id, "fence.md", "a")
    first = fs.read_text_snapshot(space.space_id, "fence.md").content_revision
    fs.write_text(space.space_id, "fence.md", "b")
    second = fs.read_text_snapshot(space.space_id, "fence.md").content_revision
    fs.write_text(space.space_id, "fence.md", "a")
    third = fs.read_text_snapshot(space.space_id, "fence.md").content_revision

    assert first is not None
    assert len({first, second, third}) == 3


def test_content_revision_is_stable_without_writes(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    fs.write_text(space.space_id, "fence.md", "a")
    fs.write_text(space.space_id, "other.md", "unrelated")

    first = fs.read_text_snapshot(space.space_id, "fence.md").content_revision
    fs.write_text(space.space_id, "other.md", "still unrelated")

    assert fs.read_text_snapshot(space.space_id, "fence.md").content_revision == first


def test_content_revision_survives_reopen(orgfs_impl, tmp_path) -> None:
    node = orgfs_impl.make_runtime(tmp_path / "node", "node-a", "user:owner")
    space = node.facade.create_space("team")
    node.facade.write_text(space.space_id, "fence.md", "a")
    node.facade.write_text(space.space_id, "fence.md", "b")
    before = node.facade.read_text_snapshot(space.space_id, "fence.md")

    reopened = node.reopen()
    after = reopened.read_text_snapshot(space.space_id, "fence.md")

    assert after.content == "b"
    assert after.content_revision == before.content_revision
    reopened.write_text(space.space_id, "fence.md", "a")
    reopened.write_text(space.space_id, "fence.md", "b")
    assert reopened.read_text_snapshot(space.space_id, "fence.md").content_revision != (
        before.content_revision
    )
