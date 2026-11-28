# Decisions:
# - Nodes are wired only through ``orgfs_impl.connect`` (in-process fake
#   session, support/network.py).  No test reads journal rows, builds
#   envelopes or calls mesh internals; catch-up after a partition relies on
#   the presence online edge the network emits on ``reconnect``.
# - Migrated from mesh/test_orgfs_mesh.py
#   (``test_online_replication_refreshes_open_facade_and_watch``, without the
#   client-id registration probe) and the user-visible results of
#   mesh/replication/content/test_content.py concurrent edits; the rest are
#   C4's new multi-node contracts (brief item 4, triage §5.3 items 14, 15).
# - A peer's blob is fetched by the runtime's content hydration, not by a
#   facade read; the facade contract is the typed ``content-pending`` with
#   the expected digest, size and holders (from mesh/test_orgfs_p1_integration.py
#   ``test_cold_cache_blob_unavailable_then_fetches_when_holder_returns``),
#   or the bytes themselves if the implementation already has them.
"""Two-node convergence contracts."""

from __future__ import annotations

from hyprial.daemon.impl.orgfs import OrgFsError

from ..support import eventually


def _pair(orgfs_impl, tmp_path, mode: str = "rw"):
    owner = orgfs_impl.make_runtime(tmp_path / "owner", "node-owner", "user:owner")
    member = orgfs_impl.make_runtime(tmp_path / "member", "node-member", "user:member")
    connection = orgfs_impl.connect(owner, member)
    space = owner.facade.create_space("team")
    owner.facade.invite(space.space_id, "user:member", mode)
    member.facade.join(space.space_id)
    return owner, member, connection, space.space_id


def _text(fs, space_id: str, ref: str) -> str:
    return fs.read_text(space_id, ref)[0]


def _assert_text(fs, space_id: str, ref: str, expected: str) -> None:
    assert _text(fs, space_id, ref) == expected


def _tree(fs, space_id: str) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (node.node_id, node.path, node.kind)
        for node in fs.listdir(space_id, "", recursive=True)
    )


def _assert_same_tree(left, right, space_id: str) -> None:
    assert _tree(left, space_id) == _tree(right, space_id)


def test_online_replication_refreshes_reads_and_watch(orgfs_impl, tmp_path) -> None:
    owner, member, _connection, space_id = _pair(orgfs_impl, tmp_path)
    seen = []
    member.facade.watch(space_id, "*.md", seen.append)

    created = owner.facade.write_text(space_id, "shared.md", "hello")
    eventually(lambda: _assert_text(member.facade, space_id, f"id:{created.node_id}", "hello"))
    owner.facade.write_text(space_id, f"id:{created.node_id}", "hello again")
    eventually(lambda: _assert_text(member.facade, space_id, f"id:{created.node_id}", "hello again"))

    eventually(
        lambda: _assert_events(seen, [("created", "shared.md"), ("modified", "shared.md")])
    )


def test_two_writers_converge_on_tree_and_content(orgfs_impl, tmp_path) -> None:
    owner, member, _connection, space_id = _pair(orgfs_impl, tmp_path)

    owner.facade.mkdir(space_id, "shared")
    eventually(lambda: member.facade.stat(space_id, "shared"))
    from_owner = owner.facade.write_text(space_id, "shared/owner.md", "by owner")
    from_member = member.facade.write_text(space_id, "member.md", "by member")
    blob = member.facade.write_bytes(space_id, "member.bin", b"\x00member")
    eventually(lambda: _assert_text(owner.facade, space_id, "member.md", "by member"))
    eventually(lambda: _assert_text(member.facade, space_id, "shared/owner.md", "by owner"))
    member.facade.move(space_id, f"id:{from_owner.node_id}", "moved.md")
    owner.facade.remove(space_id, f"id:{from_member.node_id}")

    def converged() -> None:
        _assert_same_tree(owner.facade, member.facade, space_id)
        assert owner.facade.stat(space_id, f"id:{from_owner.node_id}").path == "moved.md"
        assert [entry.node_id for entry in member.facade.trash(space_id)] == [from_member.node_id]

    eventually(converged)
    remote_blob = owner.facade.stat(space_id, "member.bin")
    assert (remote_blob.blob_hash, remote_blob.size) == (blob.blob_hash, blob.size)
    assert remote_blob.modified_by == "user:member"


def test_a_remote_blob_not_yet_fetched_reports_typed_content_pending(
    orgfs_impl, tmp_path
) -> None:
    owner, member, _connection, space_id = _pair(orgfs_impl, tmp_path)
    payload = b"\x00member blob"
    blob = member.facade.write_bytes(space_id, "member.bin", payload)
    eventually(lambda: owner.facade.stat(space_id, f"id:{blob.node_id}"))

    try:
        assert owner.facade.read_bytes(space_id, f"id:{blob.node_id}") == payload
    except OrgFsError as error:
        assert error.code == "content-pending"
        assert error.details["expectedSha256"] == blob.blob_hash
        assert error.details["expectedSize"] == len(payload)
        assert "node-member" in error.details["suggestedHolders"]


def test_concurrent_edits_from_one_base_keep_both_changes(orgfs_impl, tmp_path) -> None:
    owner, member, connection, space_id = _pair(orgfs_impl, tmp_path)
    created = owner.facade.write_text(space_id, "doc.md", "base\n")
    eventually(lambda: _assert_text(member.facade, space_id, "doc.md", "base\n"))

    connection.disconnect(member)
    owner.facade.write_text(space_id, "doc.md", "owner\nbase\n")
    member.facade.write_text(space_id, "doc.md", "base\nmember\n")
    connection.reconnect(member)

    def merged() -> None:
        left = _text(owner.facade, space_id, f"id:{created.node_id}")
        right = _text(member.facade, space_id, f"id:{created.node_id}")
        assert left == right
        assert left.count("base") == 1
        assert "owner" in left and "member" in left

    eventually(merged)


def test_writes_made_while_disconnected_arrive_after_reconnect(orgfs_impl, tmp_path) -> None:
    owner, member, connection, space_id = _pair(orgfs_impl, tmp_path)
    owner.facade.write_text(space_id, "before.md", "before")
    eventually(lambda: _assert_text(member.facade, space_id, "before.md", "before"))

    connection.disconnect(member)
    owner.facade.write_text(space_id, "owner-offline.md", "from owner")
    owner.facade.write_text(space_id, "before.md", "edited while apart")
    member.facade.write_text(space_id, "member-offline.md", "from member")
    connection.reconnect(member)

    eventually(lambda: _assert_text(member.facade, space_id, "owner-offline.md", "from owner"))
    eventually(lambda: _assert_text(member.facade, space_id, "before.md", "edited while apart"))
    eventually(lambda: _assert_text(owner.facade, space_id, "member-offline.md", "from member"))
    eventually(lambda: _assert_same_tree(owner.facade, member.facade, space_id))


def test_a_pure_deletion_made_while_disconnected_catches_up(orgfs_impl, tmp_path) -> None:
    owner, member, connection, space_id = _pair(orgfs_impl, tmp_path)
    created = owner.facade.write_text(space_id, "doc.md", "to be cleared")
    eventually(lambda: _assert_text(member.facade, space_id, "doc.md", "to be cleared"))

    connection.disconnect(member)
    owner.facade.write_text(space_id, f"id:{created.node_id}", "")
    connection.reconnect(member)

    eventually(lambda: _assert_text(member.facade, space_id, f"id:{created.node_id}", ""))


def test_a_write_from_before_a_restart_reaches_the_peer(orgfs_impl, tmp_path) -> None:
    owner, member, connection, space_id = _pair(orgfs_impl, tmp_path)

    connection.disconnect(member)
    created = member.facade.write_text(space_id, "unsent.md", "written before restart")
    reopened = member.reopen()
    assert _text(reopened, space_id, f"id:{created.node_id}") == "written before restart"
    connection.reconnect(member)

    eventually(
        lambda: _assert_text(owner.facade, space_id, f"id:{created.node_id}", "written before restart")
    )


def _assert_events(events, expected: list[tuple[str, str]]) -> None:
    assert [(event.kind, event.node.path) for event in events] == expected
