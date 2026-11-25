# Decisions:
# - Phase D2 brief: two and three ``StateOrgFsRuntime`` nodes on the in-process
#   network write to each other, catch up after a partition (pure deletion
#   included), converge on concurrent edits of one document with the
#   frontier repair terminating (design §4.2), recover a write that was never
#   broadcast before its node closed (design §4.4), delete content files on
#   peers after a permanent removal (design §6), report ``status.synced`` only
#   after verification and an observation covering the local vectors
#   (design §4.6, §9 R4), and close within the deadline with no callback
#   afterwards (D1 report §11).
# - Catch-up is driven by the presence edges the network emits on heal, or,
#   where a test needs a bounded sequence, by explicit pull rounds
#   (``Peers.pull_all``), which is what anti-entropy does.
"""Multi-node behaviour of ``StateOrgFsRuntime`` over the in-process network."""

from __future__ import annotations

import time

from .support import Peer, Peers, eventually, holds, text, tree


def _space(peers: Peers, *names: str) -> tuple[list[Peer], str]:
    nodes = [peers.add(f"node-{name}", f"user:{name}") for name in names]
    owner = nodes[0]
    space = owner.fs.create_space("team")
    for node in nodes[1:]:
        owner.fs.invite(space.space_id, node.author, "rw")
    for node in nodes[1:]:
        assert node.fs.join(space.space_id) == space
    return nodes, space.space_id


def _same(nodes: list[Peer], space_id: str) -> None:
    first = tree(nodes[0].fs, space_id)
    for node in nodes[1:]:
        assert tree(node.fs, space_id) == first


def _assert_text(node: Peer, space_id: str, ref: str, expected: str) -> None:
    assert text(node.fs, space_id, ref) == expected


def test_three_nodes_write_to_each_other_and_converge(peers: Peers) -> None:
    (a, b, c), space_id = _space(peers, "a", "b", "c")
    from_a = a.fs.write_text(space_id, "a.md", "from a")
    from_b = b.fs.write_text(space_id, "b.md", "from b")
    blob = c.fs.write_bytes(space_id, "c.bin", b"\x00from c")

    for node in (a, b, c):
        eventually(lambda node=node: _assert_text(node, space_id, f"id:{from_a.node_id}", "from a"))
        eventually(lambda node=node: _assert_text(node, space_id, f"id:{from_b.node_id}", "from b"))
    eventually(lambda: _same([a, b, c], space_id))
    assert a.fs.stat(space_id, "c.bin").blob_hash == blob.blob_hash
    assert a.fs.stat(space_id, "b.md").modified_by == "user:b"


def test_a_partitioned_node_catches_up_including_a_pure_deletion(peers: Peers) -> None:
    (a, b), space_id = _space(peers, "a", "b")
    kept = a.fs.write_text(space_id, "kept.md", "to be cleared")
    a.fs.mkdir(space_id, "folder")
    moved = a.fs.write_text(space_id, "moved.md", "moving")
    trashed = a.fs.write_text(space_id, "trashed.md", "trash me")
    eventually(lambda: _same([a, b], space_id))
    eventually(lambda: _assert_text(b, space_id, "kept.md", "to be cleared"))

    peers.disconnect(b)
    a.fs.write_text(space_id, f"id:{kept.node_id}", "")
    a.fs.move(space_id, f"id:{moved.node_id}", "folder/moved.md")
    a.fs.remove(space_id, f"id:{trashed.node_id}")
    a.fs.write_text(space_id, "new.md", "written apart")
    holds(lambda: _assert_text(b, space_id, f"id:{kept.node_id}", "to be cleared"), duration=0.3)
    peers.reconnect(b)

    eventually(lambda: _assert_text(b, space_id, f"id:{kept.node_id}", ""))
    eventually(lambda: _assert_text(b, space_id, "new.md", "written apart"))
    eventually(lambda: _same([a, b], space_id))
    assert b.fs.stat(space_id, f"id:{moved.node_id}").path == "folder/moved.md"
    assert [entry.node_id for entry in b.fs.trash(space_id)] == [trashed.node_id]


def test_concurrent_edits_from_one_base_converge_and_frontier_repair_stops(peers: Peers) -> None:
    (a, b, c), space_id = _space(peers, "a", "b", "c")
    created = a.fs.write_text(space_id, "doc.md", "base\n")
    for node in (b, c):
        eventually(lambda node=node: _assert_text(node, space_id, "doc.md", "base\n"))

    for node in (a, b, c):
        peers.disconnect(node)
    a.fs.write_text(space_id, "doc.md", "a\nbase\n")
    b.fs.write_text(space_id, "doc.md", "base\nb\n")
    c.fs.write_text(space_id, "doc.md", "base\nc\n")
    for node in (a, b, c):
        peers.reconnect(node)

    def merged() -> None:
        values = {text(node.fs, space_id, f"id:{created.node_id}") for node in (a, b, c)}
        assert len(values) == 1
        value = values.pop()
        assert value.count("base") == 1
        assert all(mark in value for mark in ("a\n", "b\n", "c\n"))

    def settled() -> None:
        peers.pull_all(space_id)
        merged()
        _same([a, b, c], space_id)

    eventually(settled, timeout=15.0)
    for node in (a, b, c):
        assert node.fs.drain_broadcasts((space_id,), timeout=5.0)
    peers.pull_all(space_id)
    vectors = [_tree_vector(node, space_id) for node in (a, b, c)]
    repairs = [_repairs(node, space_id) for node in (a, b, c)]
    for _ in range(5):
        assert peers.pull_all(space_id) == 0
    assert [_tree_vector(node, space_id) for node in (a, b, c)] == vectors
    assert [_repairs(node, space_id) for node in (a, b, c)] == repairs


def test_a_write_never_broadcast_before_close_reaches_the_peer_by_pull(peers: Peers) -> None:
    (a, b), space_id = _space(peers, "a", "b")
    peers.disconnect(a)
    created = a.fs.write_text(space_id, "unsent.md", "written, never broadcast")
    a.reopen()
    assert text(a.fs, space_id, f"id:{created.node_id}") == "written, never broadcast"
    peers.reconnect(a)

    eventually(lambda: _assert_text(b, space_id, f"id:{created.node_id}", "written, never broadcast"))


def test_a_permanent_removal_deletes_the_content_files_on_the_peer(peers: Peers) -> None:
    (a, b), space_id = _space(peers, "a", "b")
    a.fs.mkdir(space_id, "folder")
    first = a.fs.write_text(space_id, "folder/one.md", "one")
    second = a.fs.write_text(space_id, "folder/two.md", "two")
    for node in (a, b):
        eventually(lambda node=node: _assert_text(node, space_id, "folder/two.md", "two"))
        for created in (first, second):
            assert node.content_files(space_id, created.doc_id)

    a.fs.remove(space_id, "folder", permanent=True)

    def gone() -> None:
        for node in (a, b):
            for created in (first, second):
                assert node.content_files(space_id, created.doc_id) == []
            assert [entry.path for entry in node.fs.listdir(space_id, "")] == []

    eventually(gone)


def test_synced_is_not_true_before_verification_and_true_after_quiet_pulls(peers: Peers) -> None:
    (a, b), space_id = _space(peers, "a", "b")
    for index in range(5):
        a.fs.write_text(space_id, f"n{index}.md", f"body {index}")
    eventually(lambda: _assert_text(b, space_id, "n4.md", "body 4"))

    b.reopen()
    assert b.runtime is not None
    unverified = b.runtime.status(space_id)
    assert unverified.synced is not True
    assert unverified.content_pending == 5

    b.runtime._maintain_spaces(time.monotonic())  # noqa: SLF001 - the anti-entropy hook
    verified = b.runtime.status(space_id)
    assert verified.content_pending == 0
    assert verified.synced is not True

    def quiet() -> None:
        assert b.runtime is not None
        peers.pull_all(space_id)
        status = b.runtime.status(space_id)
        assert status.content_pending == 0
        assert status.synced is True
        assert "node-a" in status.holders_online

    eventually(quiet)


def test_close_finishes_within_its_deadline_and_no_callback_follows(peers: Peers) -> None:
    (a, b), space_id = _space(peers, "a", "b")
    seen: list[object] = []
    b.fs.watch(space_id, "**", seen.append)
    a.fs.write_text(space_id, "before.md", "seen")
    eventually(lambda: _assert_text(b, space_id, "before.md", "seen"))
    eventually(lambda: _assert_at_least(seen, 1))

    host_calls: list[str] = []
    original = b.fs.apply_remote

    def counted(*args: object, **kwargs: object) -> object:
        host_calls.append(str(args[1]))
        return original(*args, **kwargs)

    b.fs.apply_remote = counted
    runtime = b.runtime
    assert runtime is not None
    started = time.monotonic()
    runtime.close()
    assert time.monotonic() - started < 5.0
    count = len(seen)
    a.fs.write_text(space_id, "after.md", "not seen")
    a.fs.write_text(space_id, "before.md", "changed after close")
    assert a.fs.drain_broadcasts((space_id,), timeout=5.0)
    holds(lambda: _assert_count(seen, count), duration=0.5)
    assert host_calls == []


def _assert_at_least(seen: list[object], count: int) -> None:
    assert len(seen) >= count


def _assert_count(seen: list[object], count: int) -> None:
    assert len(seen) == count


def _tree_vector(node: Peer, space_id: str) -> bytes:
    from hyprial.daemon.impl.orgfs.membership import decode_vector

    return repr(sorted(decode_vector(node.fs.sync_host(space_id).read_vectors(("tree",))["tree"]).items())).encode()


def _repairs(node: Peer, space_id: str) -> int:
    return node.fs._registry(space_id).frontier_repairs  # noqa: SLF001 - repair counter


def test_a_closing_node_withdraws_its_holdings_with_a_protocol_3_announce(peers: Peers) -> None:
    (a, b), space_id = _space(peers, "a", "b")
    assert a.runtime is not None
    eventually(lambda: _assert_holder(a, space_id, "node-b", present=True))

    b.close()

    eventually(lambda: _assert_holder(a, space_id, "node-b", present=False))


def _assert_holder(node: Peer, space_id: str, holder: str, *, present: bool) -> None:
    assert node.runtime is not None
    assert (holder in node.runtime.status(space_id).holders_online) is present
